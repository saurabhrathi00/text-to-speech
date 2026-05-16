import os
import json
import time
import uuid
import threading
import traceback
from pathlib import Path
from flask import Response


def _load_env_file():
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


_load_env_file()

from flask import Flask, g, jsonify, render_template, request, send_from_directory

import auth
import audio_storage
import security

from config import MAX_AUDIO_FILES, PROVIDERS, PARLER_SPEAKERS


# ──────────────────────────────────────────────────────────────────────
# Provider registry — single source of truth for every provider's
# id / display name / icon / kind (local|cloud). Edit config/providers.json
# to add or rename a provider; nothing else in the code references these
# names directly. Frontend fetches the same data via /api/providers/registry.
# ──────────────────────────────────────────────────────────────────────
_REGISTRY_PATH = Path(__file__).parent / "config" / "providers.json"


def _load_provider_registry() -> dict:
    raw = json.loads(_REGISTRY_PATH.read_text(encoding="utf-8"))
    return {
        "tts": [p for p in raw.get("tts", []) if isinstance(p, dict) and p.get("id")],
        "llm": [p for p in raw.get("llm", []) if isinstance(p, dict) and p.get("id")],
    }


PROVIDER_REGISTRY = _load_provider_registry()


def _provider_entry(kind: str, provider_id: str | None) -> dict | None:
    pid = (provider_id or "").lower()
    for p in PROVIDER_REGISTRY.get(kind, []):
        if p["id"].lower() == pid:
            return p
    return None


def llm_display(provider: str | None) -> str:
    entry = _provider_entry("llm", provider)
    return entry["display"] if entry else (provider or "LLM")


def tts_display(provider: str | None) -> str:
    entry = _provider_entry("tts", provider)
    return entry["display"] if entry else (provider or "TTS")


def _llm_error_message(provider: str | None, detail: str = "") -> str:
    """User-facing message that names the actual model that failed —
    don't blame Qwen when Gemini timed out."""
    tail = f" ({detail})" if detail else ""
    return f"{llm_display(provider)} se text refine nahi ho paya{tail}. Thodi der baad try kar."
from normalizer import normalize_text, generate_scene_prompts, OllamaError
import llm
from tts import eleven as eleven_tts  # cloud HTTP, no heavy deps

# Heavy local-only modules (torch / transformers / parler_tts /
# faster-whisper) are lazy-imported on the cloud build so the import
# doesn't crash on a server that intentionally skipped requirements-local.txt.
# On admin's GPU box all four resolve normally.
try:
    from tts.parler import synthesize as parler_synthesize, build_description, load_model
except ImportError as _e:
    print(f"[app] tts.parler unavailable ({_e}); Parler/Bark routes will 503")
    parler_synthesize = build_description = load_model = None

try:
    from tts.aligner import align as align_words, load_aligner
except ImportError as _e:
    print(f"[app] tts.aligner unavailable ({_e}); Whisper trim disabled")
    align_words = lambda *a, **kw: []
    load_aligner = lambda: None

try:
    from tts import bark as bark_tts
except ImportError as _e:
    print(f"[app] tts.bark unavailable ({_e}); Bark routes will 503")
    bark_tts = None

try:
    import image_gen
except ImportError as _e:
    print(f"[app] image_gen unavailable ({_e}); image routes will 503")
    image_gen = None


def _default_provider() -> str:
    """Provider from .env — used as initial UI state."""
    return (os.getenv("TTS_PROVIDER") or "parler").strip().lower()


def _resolve_provider(requested: str | None) -> str:
    """Provider for THIS request. If client passed one, use it; else env."""
    p = (requested or "").strip().lower()
    if p in PROVIDERS:
        return p
    return _default_provider()


def _resolve_llm_provider_for_user(requested: str | None = None) -> tuple[str | None, str | None]:
    """Pick the LLM provider for this request, gated by the user's
    plan whitelist. Returns (provider, error_msg).

    Selection order:
      1. Client-requested provider, if in the user's allowed list.
      2. Env LLM_PROVIDER if it's in the allowed list.
      3. First allowed provider.
    Rejects (403) when the client explicitly asks for one the plan
    doesn't allow. AUTH_DISABLED mode bypasses the gate entirely.
    """
    from llm import config as llm_config
    env_default = llm_config.LLM_PROVIDER
    requested_clean = (requested or "").strip().lower() or None

    user = getattr(g, "user", None)
    if not user:
        return requested_clean or env_default, None

    profile = auth.get_profile(user["id"])
    if profile is not None:
        profile["role"] = user.get("role") or profile.get("role")
    raw_allowed = auth.get_allowed_providers(profile).get("llm") or []
    # Same allowed × available intersection as the TTS resolver — a
    # cloud deploy without Ollama drops 'ollama' even for admins whose
    # plan technically grants it.
    allowed = [p for p in raw_allowed if _provider_available("llm", p)]
    if not allowed:
        return None, ("No LLM providers configured for your plan on this "
                      "server. Contact support.")

    if requested_clean and requested_clean in allowed:
        return requested_clean, None
    if requested_clean and requested_clean not in allowed:
        return None, (f"Text model '{requested_clean}' not available on "
                      f"your plan. Allowed here: {', '.join(allowed)}.")

    if env_default in allowed:
        return env_default, None
    return allowed[0], None


def _resolve_tts_provider_for_user(requested: str | None) -> tuple[str | None, str | None]:
    """Pick the TTS provider for the current request, gated by the
    user's allowed list. Returns (provider, error_msg) — exactly one
    is non-None.

    Selection order:
      1. If the client requested a specific provider AND the user is
         allowed to use it → use it.
      2. Else fall back to the user's first allowed provider that the
         server actually supports.
      3. If the user has no usable providers → error.

    Auth-disabled mode (papa's local dev): skip the gate entirely,
    just resolve via env default.
    """
    requested_clean = (requested or "").strip().lower() or None

    if not g.user:
        # local dev with AUTH_DISABLED=1 — no role/plan to consult
        return _resolve_provider(requested_clean), None

    profile = auth.get_profile(g.user["id"])
    if profile is not None:
        profile["role"] = g.user.get("role") or profile.get("role")
    allowed = auth.get_allowed_providers(profile).get("tts") or []
    # 1. plan-allowed AND in this build's registry, then
    # 2. actually runnable on THIS deployment (Parler needs torch,
    #    Bark needs torch, ElevenLabs needs the API key). Cloud admin
    #    has parler/bark in their plan but only elevenlabs survives.
    allowed_supported = [p for p in allowed
                         if p in PROVIDERS and _provider_available("tts", p)]
    if not allowed_supported:
        return None, ("No TTS providers configured for your plan on this "
                      "server. Contact support.")

    if requested_clean and requested_clean in allowed_supported:
        return requested_clean, None
    if requested_clean and requested_clean not in allowed_supported:
        return None, (f"Provider '{requested_clean}' not available on "
                      f"your plan. Allowed here: {', '.join(allowed_supported)}.")

    # No specific request — prefer env default if user has it, else first allowed
    env_default = _default_provider()
    if env_default in allowed_supported:
        return env_default, None
    return allowed_supported[0], None


def _tts_synthesize(text: str, out_path: str, description: str,
                     voice: dict, provider: str) -> str:
    t0 = time.time()
    print(f"[app] dispatch → provider={provider}, text={len(text)} chars")
    try:
        if provider == "elevenlabs":
            if not eleven_tts.is_configured():
                raise RuntimeError("ELEVENLABS_API_KEY not set in .env")
            result = eleven_tts.synthesize(text, out_path, voice_config=voice)
        elif provider == "bark":
            if bark_tts is None:
                raise RuntimeError("Bark is not installed on this server. "
                                    "Run on a box with requirements-local.txt installed.")
            result = bark_tts.synthesize(text, out_path, voice_config=voice)
        else:
            if parler_synthesize is None:
                raise RuntimeError("Parler is not installed on this server. "
                                    "Run on a box with requirements-local.txt installed.")
            result = parler_synthesize(text, out_path, description=description)
        print(f"[app] {provider} done in {time.time() - t0:.1f}s → {result}")
        return result
    except Exception:
        print(f"[app] {provider} FAILED after {time.time() - t0:.1f}s")
        traceback.print_exc()
        raise

BASE_DIR = Path(__file__).parent.resolve()
AUDIO_DIR = BASE_DIR / "audio"
AUDIO_DIR.mkdir(exist_ok=True)
IMAGE_DIR = BASE_DIR / "images"
IMAGE_DIR.mkdir(exist_ok=True)

app = Flask(__name__)
security.install(app)  # MAX_CONTENT_LENGTH + CORS allowlist + 413 handler


def _prune_old_audio(keep: int = MAX_AUDIO_FILES):
    files = sorted(
        list(AUDIO_DIR.glob("*.wav")) + list(AUDIO_DIR.glob("*.mp3")),
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )
    for f in files[keep:]:
        try:
            f.unlink()
        except OSError:
            pass


# ──────────────────────────────────────────────────────────────────────
# Per-request progress tracking (for UI loader during long Qwen + TTS)
# ──────────────────────────────────────────────────────────────────────
_progress: dict[str, dict] = {}
_progress_lock = threading.Lock()


def _set_progress(job_id: str | None, stage: str, eta_seconds: int):
    if not job_id:
        return
    with _progress_lock:
        _progress[job_id] = {
            "stage": stage,
            "eta_seconds": eta_seconds,
            "started_at": time.time(),
        }


def _clear_progress(job_id: str | None):
    if not job_id:
        return
    with _progress_lock:
        _progress.pop(job_id, None)


def _prune_old_progress(max_age: int = 600):
    """Drop progress entries older than max_age seconds — prevents leak
    when a client never polls the final 'done' state."""
    now = time.time()
    with _progress_lock:
        stale = [k for k, v in _progress.items() if now - v.get("started_at", now) > max_age]
        for k in stale:
            _progress.pop(k, None)


def _public_supabase_config() -> dict:
    """Values safe to inject into the frontend HTML (anon key is public
    by Supabase design — it only gives access subject to RLS)."""
    return {
        "url": os.getenv("SUPABASE_URL", ""),
        "anon_key": os.getenv("SUPABASE_ANON_KEY", ""),
        "auth_disabled": os.getenv("AUTH_DISABLED") == "1",
    }


CANONICAL_HOST = os.getenv("CANONICAL_HOST", "https://sastaspeech.in").rstrip("/")


@app.route("/")
def landing_page():
    """Public marketing page — what crawlers and first-time visitors
    see. Logged-in users get auto-redirected to /app by JS."""
    return render_template(
        "landing.html",
        supabase=_public_supabase_config(),
        canonical_host=CANONICAL_HOST,
    )


@app.route("/app")
def app_page():
    """The actual TTS UI — gated on auth via JS. Marked noindex so
    Google doesn't try to rank a spinner."""
    return render_template("index.html", supabase=_public_supabase_config())


@app.route("/login")
def login_page():
    return render_template("login.html", supabase=_public_supabase_config())


@app.route("/sw.js")
def service_worker():
    return send_from_directory(BASE_DIR / "static", "sw.js", mimetype="application/javascript")


@app.route("/manifest.json")
def manifest():
    return send_from_directory(BASE_DIR / "static", "manifest.json", mimetype="application/manifest+json")


@app.route("/robots.txt")
def robots_txt():
    return send_from_directory(BASE_DIR / "static", "robots.txt", mimetype="text/plain")


@app.route("/sitemap.xml")
def sitemap_xml():
    return send_from_directory(BASE_DIR / "static", "sitemap.xml", mimetype="application/xml")


# ──────────────────────────────────────────────────────────────────────
# Legal / business pages (public — no auth required so payment-gateway
# KYC can verify them and so anyone can read T&C before signing up).
# ──────────────────────────────────────────────────────────────────────
_LEGAL_PAGES = {"about", "contact", "privacy", "terms", "refund", "faq"}

# Business / legal placeholder values — single source of truth.
# Edit config/business.json to update; templates read via {{ biz.X }}.
_BUSINESS_PATH = Path(__file__).parent / "config" / "business.json"
try:
    BUSINESS_CONFIG = json.loads(_BUSINESS_PATH.read_text(encoding="utf-8"))
    BUSINESS_CONFIG.pop("_comment", None)
except Exception as _e:
    print(f"[app] business.json load failed: {_e} — legal pages will show empty fields")
    BUSINESS_CONFIG = {}


@app.context_processor
def _inject_business():
    """Expose `biz` to every template."""
    return {"biz": BUSINESS_CONFIG}


def _placeholder_filter(value):
    """Wrap unfilled 'TBD:'-prefixed strings in the amber legal-todo
    pill so they stand out on the page; render filled values plainly."""
    from markupsafe import Markup, escape
    s = str(value) if value is not None else ""
    if s.startswith("TBD"):
        return Markup(f'<span class="legal-todo">{escape(s)}</span>')
    return s


app.jinja_env.filters["pl"] = _placeholder_filter


@app.route("/<page>")
def legal_page(page: str):
    if page not in _LEGAL_PAGES:
        from flask import abort
        abort(404)
    from datetime import date
    return render_template(f"legal/{page}.html",
                            updated_at=date.today().isoformat())


def _build_voice_description(voice: dict) -> str:
    custom_desc = (voice.get("custom") or "").strip()
    if custom_desc:
        return custom_desc
    if build_description is None:
        # Cloud server (no Parler installed) — voice description is
        # Parler-only anyway; ElevenLabs ignores it. Return empty.
        return ""
    return build_description(
        speaker=voice.get("speaker", "rohit"),
        speed=voice.get("speed", "moderate"),
        pitch=voice.get("pitch", "low"),
        expressivity=voice.get("expressivity", "expressive"),
        emotion=voice.get("emotion", "none"),
    )


@app.route("/normalize", methods=["POST"])
@security.require_json
@security.rate_limit("ip", *security.RATE_NORMALIZE_IP)
def normalize():
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "Pehle kuch type karein"}), 400
    provider = _resolve_provider(data.get("provider"))
    llm_provider, err = _resolve_llm_provider_for_user(data.get("llm_provider"))
    if err:
        return jsonify({"error": err}), 403
    try:
        normalized = normalize_text(text, target_provider=provider,
                                     llm_provider=llm_provider)
    except OllamaError as e:
        print(f"[app] /normalize llm={llm_provider} FAILED: {e}")
        return jsonify({"error": _llm_error_message(llm_provider, str(e))}), 502
    return jsonify({"normalized_text": normalized})


@app.route("/tts", methods=["POST"])
@auth.require_user
@security.require_json
@security.rate_limit("user", *security.RATE_TTS_USER)
@security.rate_limit("ip",   *security.RATE_TTS_IP)
def tts():
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "Pehle kuch type karein"}), 400

    voice = data.get("voice") or {}
    description = _build_voice_description(voice)
    provider, err = _resolve_tts_provider_for_user(data.get("provider"))
    if err:
        return jsonify({"error": err}), 403
    filename = f"output_{int(time.time())}_{uuid.uuid4().hex[:6]}.wav"
    out_path = AUDIO_DIR / filename

    t_req = time.time()
    print(f"[app] /tts request → {len(text)} chars, provider={provider}, voice={voice}")

    if g.user:
        ok, msg = auth.check_limits(g.user["id"], len(text))
        if not ok:
            return jsonify({"error": msg}), 402

    try:
        actual_path = _tts_synthesize(text, str(out_path), description, voice, provider)
    except Exception:
        traceback.print_exc()
        return jsonify({"error": "Awaaz generate nahi ho payi, dobara try karein"}), 500

    actual_filename = Path(actual_path).name
    words = align_words(actual_path) if provider == "parler" else []
    _prune_old_audio()

    # Cloud-store per-user. Locally-served path is the fallback for
    # AUTH_DISABLED dev (no user_id) or when Supabase Storage is down.
    audio_url = f"/audio/{actual_filename}"
    if g.user:
        signed = audio_storage.upload(g.user["id"], actual_path, actual_filename)
        if signed:
            audio_url = signed
            try:
                Path(actual_path).unlink(missing_ok=True)  # local copy not needed
            except Exception:
                pass
        audio_storage.prune_user_audio(g.user["id"])
        auth.log_usage(
            user_id=g.user["id"],
            kind="tts.regenerate",
            provider=provider,
            chars=len(text),
            meta={"emotion_tags": False},
        )
        auth.consume_bonus_if_used(g.user["id"])

    print(f"[app] /tts response in {time.time() - t_req:.1f}s → {actual_filename}")
    return jsonify({
        "audio_url": audio_url,
        "description_used": description if provider == "parler" else "",
        "words": words,
        "provider": provider,
    })


@app.route("/api/my-audios")
@auth.require_user
def api_my_audios():
    """Latest <=AUDIO_MAX_PER_USER audios for the signed-in user with
    fresh signed URLs (1h TTL). Frontend polls this after each
    successful generation."""
    return jsonify({"audios": audio_storage.list_user_audio(g.user["id"])})


@app.route("/api/me")
@auth.require_user
def api_me():
    """Return the authenticated user + profile + plan limits + current
    usage so the frontend can render quota indicators."""
    user = g.user
    if not user:
        return jsonify({"user": None, "profile": None, "limits": None,
                         "usage": None, "auth_disabled": True})
    profile = auth.get_profile(user["id"])
    # Stamp role onto the profile dict so get_allowed_providers can
    # distinguish admin (allowed=admin row) from normal users.
    if profile is not None:
        profile["role"] = user.get("role") or profile.get("role")
    # Effective plan accounts for expiry — what limits actually apply
    # right now. Raw profile.plan stays in the payload for debugging.
    effective_plan = auth.get_effective_plan(profile)
    usage = auth.get_usage_summary(user["id"])
    if profile is not None:
        profile["user_id"] = user["id"]  # for get_effective_limits helper
    effective_limits = (auth.get_effective_limits(profile, usage)
                         if profile is not None else None)
    return jsonify({
        "user": {"id": user["id"], "email": user["email"], "role": user["role"]},
        "profile": profile,
        "effective_plan": effective_plan,
        "plan_expires_at": (profile or {}).get("plan_expires_at"),
        "limits": auth.get_plan_limits(effective_plan),
        "effective_limits": effective_limits,
        "allowed_providers": auth.get_allowed_providers(profile),
        "usage": usage,
        "pending_upgrade": auth.get_pending_upgrade(user["id"]),
    })


@app.route("/api/plans")
def api_plans():
    """Public ladder of plans, sourced from plan_limits. Includes display
    name + monthly price for the upgrade picker. Admin row excluded —
    it's role-driven, not purchasable."""
    try:
        res = (auth.admin_client().table("plan_limits")
               .select("plan,display_name,price_inr_monthly,validity_hours,kind,"
                       "daily_uses,max_chars_per_request,monthly_chars,notes")
               .neq("plan", "admin")
               .execute())
        rows = getattr(res, "data", None) or []
        # Sort by price ascending; nulls (free) first.
        rows.sort(key=lambda r: (r.get("price_inr_monthly") or 0))
        return jsonify({"plans": rows})
    except Exception as e:
        print(f"[app] /api/plans failed: {e}")
        return jsonify({"plans": []})


@app.route("/api/upgrade-request", methods=["POST"])
@auth.require_user
@security.require_json
@security.rate_limit("user", *security.RATE_UPGRADE_USER)
def api_upgrade_request():
    """User asks to be moved to a higher plan. Admin reviews + approves
    out-of-band (payment handled outside the app for now)."""
    data = request.get_json(silent=True) or {}
    plan = (data.get("plan") or "pro").lower()
    note = (data.get("note") or "").strip()
    row, err = auth.create_upgrade_request(g.user["id"], plan, note=note)
    if err:
        return jsonify({"error": err}), 400
    return jsonify({"request": row})


@app.route("/api/admin/security/recent")
@auth.require_admin
def api_admin_security_recent():
    """Last ~50 security flags (rate-limit hits, oversized bodies, etc.).
    Eyeball this before/after a launch to see if anyone is probing."""
    return jsonify({"flags": security.recent_flags(50)})


@app.route("/api/admin/upgrade-requests")
@auth.require_admin
def api_admin_list_upgrade_requests():
    status = request.args.get("status", "pending")
    if status == "all":
        status = None
    return jsonify({"requests": auth.list_upgrade_requests(status)})


@app.route("/api/admin/upgrade-requests/<int:req_id>/<string:action>",
            methods=["POST"])
@auth.require_admin
def api_admin_resolve_upgrade(req_id: int, action: str):
    row, err = auth.resolve_upgrade_request(req_id, action, g.user["id"])
    if err:
        return jsonify({"error": err}), 400
    return jsonify({"request": row})


# ── Admin endpoints ────────────────────────────────────────────────────

@app.route("/api/admin/limits")
@auth.require_admin
def api_admin_limits_list():
    """Return all plan_limits rows."""
    res = auth.admin_client().table("plan_limits").select("*").order("plan").execute()
    return jsonify({"limits": getattr(res, "data", None) or []})


@app.route("/api/admin/limits/<plan>", methods=["PATCH"])
@auth.require_admin
def api_admin_limits_update(plan: str):
    """Update one plan's limits. Body: any subset of
    daily_uses, lifetime_uses, max_chars_per_request, monthly_chars, notes.
    Null values explicitly remove a limit (unlimited)."""
    data = request.get_json(silent=True) or {}
    allowed = {"daily_uses", "lifetime_uses", "max_chars_per_request",
                "monthly_chars", "notes"}
    payload = {k: v for k, v in data.items() if k in allowed}
    if not payload:
        return jsonify({"error": "no updatable fields in body"}), 400
    payload["updated_at"] = "now()"
    res = auth.admin_client().table("plan_limits").update(payload).eq("plan", plan).execute()
    rows = getattr(res, "data", None) or []
    if not rows:
        return jsonify({"error": f"plan '{plan}' not found"}), 404
    return jsonify({"plan": rows[0]})


@app.route("/api/admin/users")
@auth.require_admin
def api_admin_users():
    """List users with their plan + usage summary. Paginated."""
    limit = int(request.args.get("limit", 50))
    offset = int(request.args.get("offset", 0))
    profiles_res = auth.admin_client().table("profiles").select("*").order(
        "created_at", desc=True
    ).range(offset, offset + limit - 1).execute()
    profiles = getattr(profiles_res, "data", None) or []
    # Attach usage summary per user
    out = []
    for p in profiles:
        out.append({**p, "usage": auth.get_usage_summary(p["user_id"])})
    return jsonify({"users": out, "limit": limit, "offset": offset})


@app.route("/api/admin/users/<user_id>", methods=["PATCH"])
@auth.require_admin
def api_admin_user_update(user_id: str):
    """Update a user's profile fields. Body: any subset of
    plan, display_name, banned.

    role is INTENTIONALLY excluded — admin promotion is env-only via
    ADMIN_EMAILS. No API path can grant admin to another user; adding
    a new admin requires editing .env and restarting the server.

    banned=true locks the user out of every protected route while
    preserving their history (usage, upgrade requests, etc.). To
    permanently delete a user, do it from the Supabase Auth dashboard
    — that cascades via auth.users."""
    data = request.get_json(silent=True) or {}
    allowed = {"plan", "display_name", "banned"}
    payload = {k: v for k, v in data.items() if k in allowed}
    if not payload:
        return jsonify({"error": "no updatable fields in body"}), 400
    payload["updated_at"] = "now()"
    res = auth.admin_client().table("profiles").update(payload).eq("user_id", user_id).execute()
    rows = getattr(res, "data", None) or []
    if not rows:
        return jsonify({"error": "user not found"}), 404
    return jsonify({"user": rows[0]})


@app.route("/api/progress/<job_id>")
def api_progress(job_id: str):
    with _progress_lock:
        entry = _progress.get(job_id)
    if not entry:
        return jsonify({"stage": "unknown", "elapsed": 0, "eta_seconds": 0}), 200
    elapsed = max(0, time.time() - entry["started_at"])
    return jsonify({
        "stage": entry["stage"],
        "elapsed": round(elapsed, 1),
        "eta_seconds": entry["eta_seconds"],
    })


@app.route("/generate", methods=["POST"])
@auth.require_user
@security.require_json
@security.rate_limit("user", *security.RATE_GENERATE_USER)
@security.rate_limit("ip",   *security.RATE_GENERATE_IP)
def generate():
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "Pehle kuch type karein"}), 400

    job_id = (data.get("job_id") or "").strip() or None
    _prune_old_progress()

    skip_normalize = bool(data.get("skip_normalize"))
    add_emotion_tags = bool(data.get("emotion_tags"))
    voice = data.get("voice") or {}
    description = _build_voice_description(voice)
    provider, err = _resolve_tts_provider_for_user(data.get("provider"))
    if err:
        return jsonify({"error": err}), 403

    t_req = time.time()
    print(f"[app] /generate request → {len(text)} chars, provider={provider}, "
          f"skip_normalize={skip_normalize}, emotion_tags={add_emotion_tags}, voice={voice}")

    # Cloud-mode quota check (no-op in local mode)
    if g.user:
        ok, msg = auth.check_limits(g.user["id"], len(text))
        if not ok:
            return jsonify({"error": msg}), 402  # 402 Payment Required

    llm_provider, err = _resolve_llm_provider_for_user(data.get("llm_provider"))
    if err:
        return jsonify({"error": err}), 403

    if skip_normalize:
        normalized = text
    else:
        t_llm = time.time()
        try:
            normalized = normalize_text(
                text, target_provider=provider,
                add_emotion_tags=add_emotion_tags,
                progress_cb=lambda stage, eta: _set_progress(job_id, stage, eta),
                llm_provider=llm_provider,
            )
            print(f"[app] llm({llm_provider}) done in {time.time() - t_llm:.1f}s → {len(normalized)} chars")
        except OllamaError as e:
            print(f"[app] llm({llm_provider}) FAILED in {time.time() - t_llm:.1f}s: {e}")
            _clear_progress(job_id)
            return jsonify({"error": _llm_error_message(llm_provider, str(e))}), 502

    filename = f"output_{int(time.time())}_{uuid.uuid4().hex[:6]}.wav"
    out_path = AUDIO_DIR / filename

    _set_progress(job_id, "tts", 30)
    try:
        actual_path = _tts_synthesize(normalized, str(out_path), description, voice, provider)
    except Exception:
        traceback.print_exc()
        _clear_progress(job_id)
        return jsonify({"error": "Awaaz generate nahi ho payi, dobara try karein"}), 500

    actual_filename = Path(actual_path).name
    words = align_words(actual_path) if provider == "parler" else []
    _prune_old_audio()

    audio_url = f"/audio/{actual_filename}"
    if g.user:
        signed = audio_storage.upload(g.user["id"], actual_path, actual_filename)
        if signed:
            audio_url = signed
            try:
                Path(actual_path).unlink(missing_ok=True)
            except Exception:
                pass
        audio_storage.prune_user_audio(g.user["id"])
        auth.log_usage(
            user_id=g.user["id"],
            kind="tts.generate",
            provider=provider,
            chars=len(normalized),
            meta={"input_chars": len(text), "emotion_tags": add_emotion_tags},
        )
        auth.consume_bonus_if_used(g.user["id"])

    _clear_progress(job_id)
    print(f"[app] /generate response in {time.time() - t_req:.1f}s → {actual_filename}")
    return jsonify({
        "normalized_text": normalized,
        "audio_url": audio_url,
        "description_used": description if provider == "parler" else "",
        "words": words,
        "provider": provider,
    })


@app.route("/audio/<path:filename>")
def serve_audio(filename):
    mimetype = "audio/mpeg" if filename.lower().endswith(".mp3") else "audio/wav"
    return send_from_directory(AUDIO_DIR, filename, mimetype=mimetype)


@app.route("/images/<path:filename>")
def serve_image(filename):
    return send_from_directory(IMAGE_DIR, filename, mimetype="image/png")


def _image_unavailable():
    return jsonify({
        "error": "Image generation is not available on this server. "
                  "Run from a box with requirements-local.txt + ComfyUI."
    }), 503


@app.route("/api/image", methods=["POST"])
@auth.require_admin
def api_image():
    if image_gen is None:
        return _image_unavailable()
    data = request.get_json(silent=True) or {}
    prompt = (data.get("prompt") or "").strip()
    if not prompt:
        return jsonify({"error": "prompt required"}), 400

    if not image_gen.is_configured():
        return jsonify({
            "error": "ComfyUI reachable nahi hai. Make sure run_nvidia_gpu.bat chal raha hai at localhost:8188"
        }), 503

    width = int(data.get("width", 1024))
    height = int(data.get("height", 1024))
    steps = int(data.get("steps", 20))
    negative = (data.get("negative") or "blurry, low quality, distorted, ugly").strip()
    seed = data.get("seed")
    seed = int(seed) if seed is not None else None
    use_anchor = bool(data.get("use_anchor"))

    reference_filename = None
    if use_anchor:
        anchor = _anchor_state()
        if not anchor.get("set"):
            return jsonify({"error": "Pehle ek anchor image set karein"}), 400
        reference_filename = anchor.get("comfy_filename")

    t0 = time.time()
    print(f"[app] /api/image → {len(prompt)} chars, {width}x{height}, {steps} steps, "
          f"anchor={'yes' if reference_filename else 'no'}")
    try:
        img_bytes = image_gen.generate(
            prompt=prompt, negative=negative,
            width=width, height=height, steps=steps, seed=seed,
            reference_filename=reference_filename,
        )
    except image_gen.ComfyError as e:
        print(f"[app] image gen FAILED: {e}")
        return jsonify({"error": f"Image generate nahi hui: {e}"}), 502
    except Exception:
        traceback.print_exc()
        return jsonify({"error": "Image generate nahi hui"}), 500

    filename = f"img_{int(time.time())}_{uuid.uuid4().hex[:6]}.png"
    out_path = IMAGE_DIR / filename
    out_path.write_bytes(img_bytes)
    print(f"[app] /api/image done in {time.time() - t0:.1f}s → {filename}")
    return jsonify({"image_url": f"/images/{filename}"})


@app.route("/api/image/status")
@auth.require_admin
def api_image_status():
    if image_gen is None:
        return jsonify({"comfy_reachable": False, "anchor": _anchor_state(),
                         "available": False})
    return jsonify({
        "comfy_reachable": image_gen.is_configured(),
        "anchor": _anchor_state(),
    })


# ── Character anchor (IP-Adapter reference) ────────────────────────────
# Stored as a tiny state file so it survives server restarts.
_ANCHOR_FILE = BASE_DIR / ".anchor.json"


def _anchor_state() -> dict:
    if not _ANCHOR_FILE.exists():
        return {"set": False}
    try:
        import json as _json
        return _json.loads(_ANCHOR_FILE.read_text())
    except Exception:
        return {"set": False}


def _save_anchor(comfy_filename: str, local_filename: str):
    import json as _json
    _ANCHOR_FILE.write_text(_json.dumps({
        "set": True,
        "comfy_filename": comfy_filename,
        "local_filename": local_filename,
    }))


@app.route("/api/image/anchor", methods=["POST"])
@auth.require_admin
def api_set_anchor():
    """Set the current image (from /images/<file>) as the character
    anchor — uploaded to ComfyUI and used as IP-Adapter reference for
    subsequent generations."""
    if image_gen is None:
        return _image_unavailable()
    data = request.get_json(silent=True) or {}
    filename = (data.get("filename") or "").strip()
    if not filename:
        return jsonify({"error": "filename required"}), 400
    local_path = IMAGE_DIR / filename
    if not local_path.exists():
        return jsonify({"error": "image not found"}), 404

    try:
        comfy_name = image_gen.upload_reference(
            local_path.read_bytes(),
            suggested_name=f"anchor_{filename}",
        )
    except image_gen.ComfyError as e:
        return jsonify({"error": str(e)}), 502

    _save_anchor(comfy_name, filename)
    return jsonify({"set": True, "comfy_filename": comfy_name, "local_filename": filename})


@app.route("/api/image/anchor", methods=["DELETE"])
@auth.require_admin
def api_clear_anchor():
    if _ANCHOR_FILE.exists():
        _ANCHOR_FILE.unlink()
    return jsonify({"set": False})


@app.route("/api/scenes", methods=["POST"])
@auth.require_admin
def api_scenes():
    """Convert Hindi/Hinglish/English text into English image prompts
    using Qwen. Returns scenes + characters arrays.
    """
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "Pehle kuch text dein"}), 400

    t0 = time.time()
    print(f"[app] /api/scenes → {len(text)} chars")
    result = generate_scene_prompts(text)
    print(f"[app] /api/scenes done in {time.time() - t0:.1f}s → "
          f"{len(result.get('scenes', []))} scene(s)")
    if result.get("error"):
        return jsonify(result), 502
    return jsonify(result)


_warmup_started = threading.Lock()
_warmup_done = False


def _warmup_in_background():
    global _warmup_done
    if _warmup_done:
        return
    provider = _default_provider()
    print(f"[startup] TTS provider: {provider}")
    if provider == "parler":
        if load_model is None:
            print("[startup] Parler env set but module not installed — skipping warmup")
        else:
            print("[startup] Parler + aligner warmup in background...")
            t0 = time.time()
            try:
                load_model()
                print(f"[startup] Parler ready in {time.time() - t0:.1f}s")
            except Exception as e:
                print(f"[startup] Parler warmup failed: {e} (will retry on first request)")
            t1 = time.time()
            try:
                load_aligner()
                print(f"[startup] aligner ready in {time.time() - t1:.1f}s")
            except Exception as e:
                print(f"[startup] aligner warmup failed: {e}")
    elif provider == "bark":
        if bark_tts is None:
            print("[startup] Bark env set but module not installed — skipping warmup")
        else:
            print("[startup] Bark warmup in background...")
            t0 = time.time()
            try:
                bark_tts.load_model()
                print(f"[startup] Bark ready in {time.time() - t0:.1f}s")
            except Exception as e:
                print(f"[startup] Bark warmup failed: {e} (will retry on first request)")
    elif provider == "elevenlabs":
        if not eleven_tts.is_configured():
            print("[startup] WARNING: TTS_PROVIDER=elevenlabs but ELEVENLABS_API_KEY not set")
        else:
            print("[startup] using ElevenLabs API — no local model load needed")

    # LLM warmup — for Ollama this loads Qwen into VRAM so the first
    # /generate doesn't pay a 30–60s cold-load. Gemini is a no-op.
    print("[startup] LLM warmup in background...")
    t_llm = time.time()
    llm.warmup()
    print(f"[startup] LLM warmup done in {time.time() - t_llm:.1f}s")
    _warmup_done = True


def _kick_off_warmup():
    """Spawn the warmup thread exactly once, regardless of how the app
    is launched (python app.py, flask run, gunicorn, mod_wsgi, ...).
    Werkzeug's reloader runs the parent twice; the lock prevents a
    double-load on the GPU."""
    if not _warmup_started.acquire(blocking=False):
        return
    threading.Thread(target=_warmup_in_background, daemon=True).start()


# Kick off model loading at module import time so the first user
# request doesn't pay the 30–60s cold-load cost. Skipped under the
# Werkzeug debug reloader's parent process (WERKZEUG_RUN_MAIN unset).
if not os.getenv("FLASK_SKIP_WARMUP") and (
        os.getenv("WERKZEUG_RUN_MAIN") == "true"
        or not os.getenv("FLASK_DEBUG")):
    _kick_off_warmup()


@app.route("/health")
def health():
    """Detailed readiness — used by the frontend to decide whether to
    show a "loading models" splash before the TTS UI."""
    # Locally these imports succeed; on cloud the modules aren't present
    # and we report 'not loaded' (= cloud doesn't care about Parler etc.)
    try:
        from tts.parler import _model as parler_model
    except ImportError:
        parler_model = None
    try:
        from tts.aligner import _model as whisper_model
    except ImportError:
        whisper_model = None

    provider = _default_provider()
    from llm import config as llm_config
    llm_provider = llm_config.LLM_PROVIDER
    llm_warm = llm.is_warm()

    parler_loaded = parler_model is not None
    bark_loaded = bark_tts is not None and bark_tts._model is not None
    whisper_loaded = whisper_model is not None

    # A "local model" is anything that takes meaningful time to load on
    # this box — TTS weights AND/OR a local Ollama LLM. Cloud providers
    # (elevenlabs, gemini) need no warmup, so users on those skip the
    # loading screen entirely.
    tts_needs_local = provider in ("parler", "bark")
    llm_needs_local = llm_provider == "ollama"
    needs_local_models = tts_needs_local or llm_needs_local

    tts_ready = True
    if provider == "parler":
        tts_ready = parler_loaded and whisper_loaded
    elif provider == "bark":
        tts_ready = bark_loaded
    ready = tts_ready and (not llm_needs_local or llm_warm)

    return jsonify({
        "server": "up",
        "provider": provider,
        "llm_provider": llm_provider,
        "needs_local_models": needs_local_models,
        "ready": ready,
        "models": {
            "parler": parler_loaded,
            "whisper": whisper_loaded,
            "bark": bark_loaded,
            "llm": llm_warm,
        },
    })


@app.route("/api/providers")
def api_providers():
    return jsonify({
        "current": _default_provider(),
        "available": list(PROVIDERS),
        "elevenlabs_configured": eleven_tts.is_configured(),
    })


def _provider_available(kind: str, pid: str) -> bool:
    """Runtime check: is this provider actually runnable on THIS box?
    Cloud deploys skip torch/parler/bark/ollama; same code, different
    answer. Used by the frontend to gray out unreachable buttons even
    when the user's plan technically allows them."""
    pid = (pid or "").lower()
    if kind == "tts":
        if pid == "parler":     return parler_synthesize is not None
        if pid == "bark":       return bark_tts is not None
        if pid == "elevenlabs": return eleven_tts.is_configured()
        return False
    if kind == "llm":
        if pid == "ollama":
            # Treat env presence as intent + reachability hint. Real
            # reachability is whatever llm.warmup() resolved to.
            return os.getenv("LLM_PROVIDER", "").lower() == "ollama" or bool(
                os.getenv("OLLAMA_URL"))
        if pid == "gemini":
            return bool(os.getenv("GEMINI_API_KEY"))
    return False


@app.route("/api/providers/registry")
def api_providers_registry():
    """Static metadata + a per-entry `available` flag computed at
    request time so the frontend can render only what THIS deployment
    can actually serve. Cloud admins still get whatever the local box
    has (elevenlabs + gemini); their Parler / Bark / Ollama options
    quietly disappear instead of failing on click."""
    payload = {"tts": [], "llm": []}
    for kind in ("tts", "llm"):
        for entry in PROVIDER_REGISTRY.get(kind, []):
            payload[kind].append({
                **entry,
                "available": _provider_available(kind, entry["id"]),
            })
    return jsonify(payload)


@app.route("/api/providers/<name>/voices")
def api_voices(name: str):
    name = name.lower()
    if name == "parler":
        return jsonify({
            "voices": PARLER_SPEAKERS,
            "emotions_supported": False,
            "speed_supported": True,
            "pitch_supported": True,
            "expressivity_supported": True,
        })
    if name == "elevenlabs":
        return jsonify({
            "voices": eleven_tts.list_voices(),
            "emotions_supported": True,
            "speed_supported": False,
            "pitch_supported": False,
            "expressivity_supported": False,
        })
    if name == "bark":
        if bark_tts is None:
            return jsonify({"voices": [], "emotions_supported": True,
                             "available": False})
        return jsonify({
            "voices": bark_tts.list_voices(),
            "emotions_supported": True,
            "speed_supported": False,
            "pitch_supported": False,
            "expressivity_supported": False,
        })
    return jsonify({"error": "unknown provider"}), 404


if __name__ == "__main__":
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "5000"))
    _kick_off_warmup()  # safe to call again — the lock dedupes
    app.run(host=host, port=port, debug=False, threaded=True)


@app.route('/sitemap.xml')
def sitemap():
    sitemap_xml = '''<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">

  <url>
    <loc>https://sastaspeech.in/</loc>
  </url>

  <url>
    <loc>https://sastaspeech.in/pricing</loc>
  </url>

</urlset>
'''

    return Response(sitemap_xml, mimetype='application/xml')