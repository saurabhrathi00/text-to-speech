import os
import json
import time
import uuid
import threading
import traceback
from pathlib import Path


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

from flask import Flask, g, jsonify, redirect, render_template, request, send_from_directory

import auth
import audio_storage
import security

from config import (MAX_AUDIO_FILES, PROVIDERS, PARLER_SPEAKERS, HARD_MAX_CHARS,
                    CURRENCY_CODE, CURRENCY_SYMBOLS, SUPPORTED_CURRENCIES)


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
    return f"{llm_display(provider)} couldn't refine the text{tail}. Please try again in a moment."
from normalizer import normalize_text, generate_scene_prompts, OllamaError
import llm
import payments  # Razorpay checkout — requests + stdlib only, no heavy deps
import coupons   # discount codes + pricing-psychology helpers
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
import observability   # Datadog log shipping — no-op unless DD_API_KEY is set
observability.install(app)


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
            "started_at": _progress.get(job_id, {}).get("started_at", time.time()),
        }


def _finish_progress(job_id: str | None, result: dict | None = None,
                     error: str | None = None):
    if not job_id:
        return
    with _progress_lock:
        entry = _progress.get(job_id, {})
        _progress[job_id] = {
            "stage": "error" if error else "done",
            "eta_seconds": 0,
            "started_at": entry.get("started_at", time.time()),
            "result": result,
            "error": error,
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
        # "oauth" → frontend uses our Google OAuth + session cookie (no
        # Supabase JS). "supabase" → existing Supabase auth.
        "auth_provider": auth.AUTH_PROVIDER,
    }


CANONICAL_HOST = os.getenv("CANONICAL_HOST", "https://sastaspeech.in").rstrip("/")


CHARS_PER_AUDIO_MINUTE = 500   # rough Hindi narration pace; same as UI


def _public_plans() -> list[dict]:
    """Plan ladder for the public landing — same query as /api/plans.
    Returns [] on DB error so the page still renders (just without
    the pricing section). The landing template tolerates an empty
    list by hiding the pricing grid."""
    try:
        res = (auth.admin_client().table("plan_limits")
               .select("plan,display_name,price_inr_monthly,validity_hours,kind,"
                       "daily_uses,max_chars_per_request,monthly_chars,notes")
               .neq("plan", "admin")
               .execute())
        rows = getattr(res, "data", None) or []
        rows.sort(key=lambda r: (r.get("price_inr_monthly") or 0))
        auto = coupons.get_auto_apply_coupon()
        for r in rows:
            chars = r.get("monthly_chars") or 0
            r["audio_minutes"] = round(chars / CHARS_PER_AUDIO_MINUTE) if chars else 0
            r["is_topup"] = (r.get("kind") or "subscription").lower() == "topup"
            # Coupon-aware pricing so the landing shows the SAME discounted
            # price as the app (struck-through anchor + "you pay" + OFF badge),
            # not the raw high anchor. USD for the public/SEO page.
            base = int(r.get("price_inr_monthly") or 0) * 100
            r["pricing"] = coupons.effective_price(r["plan"], base, auto=auto) if base > 0 else None
        return rows
    except Exception as e:
        print(f"[app] _public_plans failed: {e}")
        return []


@app.route("/")
def landing_page():
    """Public marketing page — what crawlers and first-time visitors
    see. Pricing is server-rendered from plan_limits so the landing
    never drifts from the DB. Logged-in users get auto-redirected
    to /app by JS."""
    return render_template(
        "landing.html",
        supabase=_public_supabase_config(),
        canonical_host=CANONICAL_HOST,
        plans=_public_plans(),
    )


@app.route("/app")
def app_page():
    """The actual TTS UI — gated on auth via JS. Marked noindex so
    Google doesn't try to rank a spinner."""
    return render_template("index.html", supabase=_public_supabase_config())


@app.route("/admin")
def admin_page():
    """Dedicated admin dashboard (coupons, support, users, upgrades). Shell
    only — the page's JS gates on /api/me role=admin; all data comes from the
    require_admin APIs."""
    return render_template("admin.html")


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

# Data-driven SEO landing pages (config/seo_pages.json). Each slug is served
# by the /<page> route via templates/seo_page.html. Add a page there + a
# <url> in static/sitemap.xml — no code change needed.
_SEO_PATH = Path(__file__).parent / "config" / "seo_pages.json"
try:
    SEO_PAGES = json.loads(_SEO_PATH.read_text(encoding="utf-8"))
    SEO_PAGES.pop("_comment", None)
except Exception as _e:
    print(f"[app] seo_pages.json load failed: {_e} — SEO landing pages disabled")
    SEO_PAGES = {}

# Data-driven blog (config/blog.json). /blog lists posts; /blog/<slug>
# renders one via templates/blog_post.html. Add an article there + a <url>
# in static/sitemap.xml — no code change needed.
_BLOG_PATH = Path(__file__).parent / "config" / "blog.json"
try:
    BLOG_POSTS = json.loads(_BLOG_PATH.read_text(encoding="utf-8"))
    BLOG_POSTS.pop("_comment", None)
except Exception as _e:
    print(f"[app] blog.json load failed: {_e} — blog disabled")
    BLOG_POSTS = {}


@app.context_processor
def _inject_business():
    """Expose `biz` + canonical_host to every template (legal pages use
    canonical_host for their <link rel=canonical>)."""
    return {"biz": BUSINESS_CONFIG, "canonical_host": CANONICAL_HOST}


def _placeholder_filter(value):
    """Wrap unfilled 'TBD:'-prefixed strings in the amber legal-todo
    pill so they stand out on the page; render filled values plainly."""
    from markupsafe import Markup, escape
    s = str(value) if value is not None else ""
    if s.startswith("TBD"):
        return Markup(f'<span class="legal-todo">{escape(s)}</span>')
    return s


app.jinja_env.filters["pl"] = _placeholder_filter


@app.route("/blog")
def blog_index():
    posts = [{**v, "slug": k} for k, v in
             sorted(BLOG_POSTS.items(),
                    key=lambda kv: kv[1].get("date", ""), reverse=True)]
    return render_template("blog_index.html", posts=posts)


@app.route("/blog/<slug>")
def blog_post(slug: str):
    post = BLOG_POSTS.get(slug)
    if not post:
        from flask import abort
        abort(404)
    related = {s: p for s, p in BLOG_POSTS.items() if s != slug}
    return render_template("blog_post.html", page=post, slug=slug, related=related)


@app.route("/<page>")
def legal_page(page: str):
    # SEO landing pages first (config-driven, indexable marketing pages).
    if page in SEO_PAGES:
        related = {s: p for s, p in SEO_PAGES.items() if s != page}
        return render_template("seo_page.html", page=SEO_PAGES[page],
                                slug=page, related=related)
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
@auth.require_user
@security.require_json
@security.rate_limit("user", *security.RATE_GENERATE_USER)
@security.rate_limit("ip", *security.RATE_NORMALIZE_IP)
def normalize():
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "Please type something first"}), 400
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
        return jsonify({"error": "Please type something first"}), 400
    if len(text) > HARD_MAX_CHARS:
        return jsonify({"error": f"That's too long ({len(text):,} characters). "
                        f"The maximum is {HARD_MAX_CHARS:,} characters per "
                        f"generation — please split it into smaller parts."}), 413

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
        return jsonify({"error": "Couldn't generate the audio. Please try again."}), 500

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


# ── Self-hosted Google OAuth (AUTH_PROVIDER=oauth) ─────────────────────
# Consent screen reads "continue to sastaspeech.in" (our redirect domain),
# not a supabase.co subdomain. Issues our own HttpOnly session cookie.

@app.route("/auth/google/login")
def auth_google_login():
    if not auth.oauth_enabled():
        return jsonify({"error": "OAuth is not enabled"}), 404
    if not auth.GOOGLE_CLIENT_ID:
        return "Google OAuth not configured (set GOOGLE_CLIENT_ID)", 500
    import secrets
    from urllib.parse import urlencode
    state = secrets.token_urlsafe(24)
    params = {
        "client_id": auth.GOOGLE_CLIENT_ID,
        "redirect_uri": auth.OAUTH_REDIRECT_URI,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "access_type": "online",
        "prompt": "select_account",
    }
    resp = redirect("https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params))
    resp.set_cookie("oauth_state", state, max_age=600, httponly=True,
                    secure=True, samesite="Lax")
    return resp


@app.route("/auth/google/callback")
def auth_google_callback():
    if not auth.oauth_enabled():
        return jsonify({"error": "OAuth is not enabled"}), 404
    if request.args.get("error"):
        return redirect("/login?error=" + request.args.get("error"))
    code = request.args.get("code")
    state = request.args.get("state")
    if not code or not state or state != request.cookies.get("oauth_state"):
        return redirect("/login?error=bad_state")
    import requests as _rq
    try:
        tok = _rq.post("https://oauth2.googleapis.com/token", data={
            "code": code,
            "client_id": auth.GOOGLE_CLIENT_ID,
            "client_secret": auth.GOOGLE_CLIENT_SECRET,
            "redirect_uri": auth.OAUTH_REDIRECT_URI,
            "grant_type": "authorization_code",
        }, timeout=15)
        if tok.status_code != 200:
            print(f"[oauth] token exchange failed {tok.status_code}: {tok.text[:200]}")
            return redirect("/login?error=token_exchange")
        access_token = tok.json().get("access_token")
        ui = _rq.get("https://www.googleapis.com/oauth2/v3/userinfo",
                     headers={"Authorization": "Bearer " + access_token}, timeout=15)
        if ui.status_code != 200:
            return redirect("/login?error=userinfo")
        info = ui.json()
    except _rq.RequestException as e:
        print(f"[oauth] callback network error: {e}")
        return redirect("/login?error=network")

    sub = info.get("sub")
    email = info.get("email")
    if not sub or info.get("email_verified") is False:
        return redirect("/login?error=email")
    profile = auth.upsert_google_profile(sub, email, info.get("name"))
    observability.log_event(
        "signup" if profile.get("_new") else "login",
        evt="signup" if profile.get("_new") else "login",
        user_id=sub, email=email, role=profile.get("role", "user"),
    )
    session_jwt = auth.make_session_token(sub, email, profile.get("role", "user"))
    resp = redirect("/app")
    resp.set_cookie(auth.SESSION_COOKIE, session_jwt,
                    max_age=auth.SESSION_TTL_HOURS * 3600, httponly=True,
                    secure=True, samesite="Lax")
    resp.delete_cookie("oauth_state")
    return resp


@app.route("/auth/logout", methods=["GET", "POST"])
def auth_logout():
    resp = redirect("/")
    resp.delete_cookie(auth.SESSION_COOKIE)
    return resp


@app.route("/api/plans")
def api_plans():
    """Public ladder of plans, sourced from plan_limits. Includes display
    name + monthly price for the upgrade picker. Admin row excluded —
    it's role-driven, not purchasable."""
    # Currency the buyer is checking out in (INR for India, USD otherwise).
    # Prices are normalized to this currency's anchor so the UI just swaps
    # the symbol; the percent auto-coupon is currency-agnostic.
    currency = (request.args.get("currency") or CURRENCY_CODE).upper()
    if currency not in SUPPORTED_CURRENCIES:
        currency = CURRENCY_CODE
    try:
        res = (auth.admin_client().table("plan_limits")
               .select("plan,display_name,price_inr_monthly,price_inr,validity_hours,"
                       "kind,daily_uses,max_chars_per_request,monthly_chars,notes")
               .neq("plan", "admin")
               .execute())
        rows = getattr(res, "data", None) or []
        auto = coupons.get_auto_apply_coupon()
        for r in rows:
            # Pick the per-currency anchor; INR falls back to the USD anchor
            # if an INR price isn't set yet. Normalize price_inr_monthly to the
            # chosen currency's number so the frontend renders it directly.
            if currency == "INR":
                anchor = int(r.get("price_inr") or r.get("price_inr_monthly") or 0)
            else:
                anchor = int(r.get("price_inr_monthly") or 0)
            r["price_inr_monthly"] = anchor
            r.pop("price_inr", None)
            base = anchor * 100
            if base > 0:
                r["pricing"] = coupons.effective_price(r["plan"], base, auto=auto)
        # Sort by (normalized) price ascending; free first.
        rows.sort(key=lambda r: (r.get("price_inr_monthly") or 0))
        observability.log_event("pricing_viewed", evt="pricing_viewed",
                                currency=currency)
        return jsonify({"plans": rows, "currency": currency,
                        "symbol": CURRENCY_SYMBOLS.get(currency, "$")})
    except Exception as e:
        print(f"[app] /api/plans failed: {e}")
        return jsonify({"plans": [], "currency": currency,
                        "symbol": CURRENCY_SYMBOLS.get(currency, "$")})


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


# ── Self-serve payments (Razorpay) ─────────────────────────────────────

@app.route("/api/checkout/create-order", methods=["POST"])
@auth.require_user
@security.require_json
@security.rate_limit("user", *security.RATE_UPGRADE_USER)
def api_checkout_create_order():
    """Start a Razorpay checkout for a plan. When payments aren't
    configured (e.g. the local admin box) returns 503 + fallback:true so
    the frontend uses the manual upgrade-request flow instead."""
    if not payments.is_configured():
        return jsonify({"error": "Payments not configured", "fallback": True}), 503
    data = request.get_json(silent=True) or {}
    plan = (data.get("plan") or "").lower().strip()
    currency = (data.get("currency") or "").upper()
    order, err = payments.create_order(g.user["id"], plan,
                                       coupon_code=data.get("coupon"),
                                       currency=currency)
    if err:
        observability.log_event("checkout_failed", level="warning",
                                evt="checkout_failed", user_id=g.user["id"],
                                plan=plan, reason=err)
        return jsonify({"error": err}), 400
    observability.log_event("checkout_started", evt="checkout_started",
                            user_id=g.user["id"], plan=order.get("plan"),
                            currency=order.get("currency"),
                            amount=(order.get("amount") or 0) / 100,
                            coupon=order.get("coupon"))
    return jsonify(order)


@app.route("/api/checkout/verify", methods=["POST"])
@auth.require_user
@security.require_json
@security.rate_limit("user", *security.RATE_UPGRADE_USER)
def api_checkout_verify():
    """Verify the Razorpay checkout callback signature and apply the plan
    (UX fast-path; the webhook is the authoritative backstop)."""
    data = request.get_json(silent=True) or {}
    result, err = payments.verify_payment(
        g.user["id"],
        (data.get("razorpay_order_id") or "").strip(),
        (data.get("razorpay_payment_id") or "").strip(),
        (data.get("razorpay_signature") or "").strip(),
    )
    if err:
        observability.log_event("payment_failed", level="warning",
                                evt="payment_failed", user_id=g.user["id"],
                                order_id=(data.get("razorpay_order_id") or "").strip(),
                                reason=err)
        return jsonify({"error": err}), 400
    return jsonify(result)


@app.route("/api/webhooks/razorpay", methods=["POST"])
def api_razorpay_webhook():
    """Razorpay server-to-server webhook. No auth decorator — the HMAC
    signature on the raw body IS the authentication. Always 200 on a
    verified event (even if ignored) so Razorpay stops retrying; 400
    only when the signature itself fails."""
    raw = request.get_data()  # raw bytes — required for signature check
    signature = request.headers.get("X-Razorpay-Signature", "")
    ok, err = payments.handle_webhook(raw, signature)
    if not ok:
        return jsonify({"error": err or "rejected"}), 400
    return jsonify({"status": "ok"})


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


# ── Admin: coupon management ────────────────────────────────────────────

@app.route("/api/admin/coupons", methods=["GET", "POST"])
@auth.require_admin
def api_admin_coupons():
    if request.method == "POST":
        row, err = coupons.upsert_coupon(request.get_json(silent=True) or {})
        if err:
            return jsonify({"error": err}), 400
        return jsonify({"coupon": row})
    return jsonify({"coupons": coupons.list_coupons()})


@app.route("/api/admin/coupons/<code>", methods=["DELETE"])
@auth.require_admin
def api_admin_coupon_delete(code: str):
    ok, err = coupons.delete_coupon(code)
    if err:
        return jsonify({"error": err}), 400
    return jsonify({"ok": ok})


# ── Support / grievance system ─────────────────────────────────────────

@app.route("/api/support", methods=["POST"])
@security.require_json
@security.rate_limit("ip", 5, 300)   # 5 submissions / 5 min / IP (anti-spam)
def api_support():
    """Public support/grievance submission. Works logged-out; if a valid
    session token is present it is best-effort linked to the account so the
    user can see their own tickets."""
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip()
    message = (data.get("message") or "").strip()
    if not email or "@" not in email:
        return jsonify({"error": "A valid email is required"}), 400
    if len(message) < 5:
        return jsonify({"error": "Please describe your issue"}), 400
    uid = None
    try:
        tok = auth._extract_token()
        if tok:
            uid = auth.verify_jwt(tok).get("sub")
    except Exception:
        uid = None
    row = {
        "user_id": uid,
        "name": ((data.get("name") or "").strip()[:120] or None),
        "email": email[:200],
        "phone": ((data.get("phone") or "").strip()[:40] or None),
        "category": (data.get("category") or "query").strip().lower()[:20],
        "message": message[:4000],
    }
    try:
        auth.admin_client().table("support_tickets").insert(row).execute()
    except Exception as e:
        print(f"[support] insert failed: {e}")
        return jsonify({"error": "Could not submit — please email us directly"}), 500
    return jsonify({"ok": True})


@app.route("/api/admin/support")
@auth.require_admin
def api_admin_support():
    status = request.args.get("status", "open")
    q = (auth.admin_client().table("support_tickets")
         .select("*").order("created_at", desc=True))
    if status != "all":
        q = q.eq("status", status)
    res = q.execute()
    return jsonify({"tickets": getattr(res, "data", None) or []})


@app.route("/api/admin/support/<int:tid>/resolve", methods=["POST"])
@auth.require_admin
def api_admin_support_resolve(tid: int):
    data = request.get_json(silent=True) or {}
    upd = {"status": "resolved", "resolved_at": "now()"}
    if (data.get("note") or "").strip():
        upd["admin_note"] = data["note"].strip()[:2000]
    auth.admin_client().table("support_tickets").update(upd).eq("id", tid).execute()
    return jsonify({"ok": True})


# ── Admin: user look-up ("view-as" / read-only impersonation) ──────────

@app.route("/api/admin/user-lookup")
@auth.require_admin
def api_admin_user_lookup():
    """Read-only snapshot of any user's account so an admin can see exactly
    what that user sees (plan, quota, orders, tickets) to help them — without
    minting a session as them."""
    email = (request.args.get("email") or "").strip().lower()
    if not email:
        return jsonify({"error": "email required"}), 400
    ac = auth.admin_client()
    prof = (ac.table("profiles").select("*").ilike("email", email)
            .limit(1).execute())
    rows = getattr(prof, "data", None) or []
    if not rows:
        return jsonify({"error": "No user found with that email"}), 404
    profile = rows[0]
    uid = profile["user_id"]
    summary = auth.get_usage_summary(uid)
    orders = (ac.table("payment_orders")
              .select("plan,amount_paise,discount_paise,coupon_code,status,granted,created_at")
              .eq("user_id", uid).order("created_at", desc=True).limit(5).execute())
    tickets = (ac.table("support_tickets")
               .select("category,status,created_at")
               .eq("user_id", uid).order("created_at", desc=True).limit(5).execute())
    return jsonify({
        "profile": profile,
        "effective_plan": auth.get_effective_plan(profile),
        "effective_limits": auth.get_effective_limits({**profile, "user_id": uid}, summary),
        "usage": summary,
        "orders": getattr(orders, "data", None) or [],
        "tickets": getattr(tickets, "data", None) or [],
    })


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
        # Do NOT pop terminal entries on read — a dropped/duplicated poll
        # (network blip, backgrounded tab) would otherwise lose the result
        # permanently. _prune_old_progress reclaims them by age (600s).
    elapsed = max(0, time.time() - entry["started_at"])
    resp = {
        "stage": entry["stage"],
        "elapsed": round(elapsed, 1),
        "eta_seconds": entry["eta_seconds"],
    }
    if entry["stage"] == "done":
        resp["result"] = entry.get("result")
    elif entry["stage"] == "error":
        resp["error"] = entry.get("error")
    return jsonify(resp)


@app.route("/generate", methods=["POST"])
@auth.require_user
@security.require_json
@security.rate_limit("user", *security.RATE_GENERATE_USER)
@security.rate_limit("ip",   *security.RATE_GENERATE_IP)
def generate():
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "Please type something first"}), 400
    if len(text) > HARD_MAX_CHARS:
        return jsonify({"error": f"That's too long ({len(text):,} characters). "
                        f"The maximum is {HARD_MAX_CHARS:,} characters per "
                        f"generation — please split it into smaller parts."}), 413

    job_id = (data.get("job_id") or "").strip() or str(uuid.uuid4())
    _prune_old_progress()

    skip_normalize = bool(data.get("skip_normalize"))
    add_emotion_tags = bool(data.get("emotion_tags"))
    voice = data.get("voice") or {}
    description = _build_voice_description(voice)
    provider, err = _resolve_tts_provider_for_user(data.get("provider"))
    if err:
        return jsonify({"error": err}), 403

    print(f"[app] /generate request → {len(text)} chars, provider={provider}, "
          f"skip_normalize={skip_normalize}, emotion_tags={add_emotion_tags}, voice={voice}")

    user = g.user
    if user:
        ok, msg = auth.check_limits(user["id"], len(text))
        if not ok:
            return jsonify({"error": msg}), 402

    llm_provider, err = _resolve_llm_provider_for_user(data.get("llm_provider"))
    if err:
        return jsonify({"error": err}), 403

    _set_progress(job_id, "queued", 60)

    def _run_generate():
        t_req = time.time()
        try:
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
                    observability.log_event("generation_failed", level="error",
                        evt="generation_failed", stage="llm",
                        user_id=(user or {}).get("id"), provider=llm_provider,
                        reason=str(e)[:200])
                    _finish_progress(job_id, error=_llm_error_message(llm_provider, str(e)))
                    return

            filename = f"output_{int(time.time())}_{uuid.uuid4().hex[:6]}.wav"
            out_path = AUDIO_DIR / filename

            _set_progress(job_id, "tts", 30)
            try:
                actual_path = _tts_synthesize(normalized, str(out_path), description, voice, provider)
            except Exception as e:
                traceback.print_exc()
                observability.log_event("generation_failed", level="error",
                    evt="generation_failed", stage="tts",
                    user_id=(user or {}).get("id"), provider=provider,
                    reason=str(e)[:200])
                _finish_progress(job_id, error="Couldn't generate the audio. Please try again.")
                return

            actual_filename = Path(actual_path).name
            words = align_words(actual_path) if provider == "parler" else []
            _prune_old_audio()

            audio_url = f"/audio/{actual_filename}"
            if user:
                signed = audio_storage.upload(user["id"], actual_path, actual_filename)
                if signed:
                    audio_url = signed
                    try:
                        Path(actual_path).unlink(missing_ok=True)
                    except Exception:
                        pass
                audio_storage.prune_user_audio(user["id"])
                auth.log_usage(
                    user_id=user["id"],
                    kind="tts.generate",
                    provider=provider,
                    chars=len(normalized),
                    meta={"input_chars": len(text), "emotion_tags": add_emotion_tags},
                )
                auth.consume_bonus_if_used(user["id"])

            print(f"[app] /generate done in {time.time() - t_req:.1f}s → {actual_filename}")
            observability.log_event(
                "generation", evt="generation",
                user_id=(user or {}).get("id"), provider=provider,
                chars=len(normalized), input_chars=len(text),
                emotion_tags=add_emotion_tags,
                duration_s=round(time.time() - t_req, 1))
            _finish_progress(job_id, result={
                "normalized_text": normalized,
                "audio_url": audio_url,
                "description_used": description if provider == "parler" else "",
                "words": words,
                "provider": provider,
            })
        except Exception:
            traceback.print_exc()
            _finish_progress(job_id, error="Something went wrong. Please try again.")

    threading.Thread(target=_run_generate, daemon=True).start()
    return jsonify({"job_id": job_id}), 202


@app.route("/audio/<path:filename>")
def serve_audio(filename):
    mimetype = "audio/mpeg" if filename.lower().endswith(".mp3") else "audio/wav"
    return send_from_directory(AUDIO_DIR, filename, mimetype=mimetype)



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


_APP_STARTED_AT = time.time()  # module load time — powers uptime on /status


def _deployment_status() -> dict:
    """Which build is live here, plus config sanity. Reads platform env vars
    (Render sets RENDER_GIT_COMMIT; a future Vercel/AWS deploy can set
    GIT_COMMIT / APP_VERSION + APP_ENV). Extensible to a multi-env board."""
    commit = (os.getenv("RENDER_GIT_COMMIT") or os.getenv("GIT_COMMIT")
              or os.getenv("VERCEL_GIT_COMMIT_SHA") or os.getenv("APP_VERSION") or "")
    env = (os.getenv("APP_ENV") or ("vercel" if os.getenv("VERCEL") else None)
           or os.getenv("RENDER_SERVICE_NAME") or "render")
    return {
        "environment": env,
        "version": (commit[:12] if commit else "unknown"),
        "commit": commit or None,
        "uptime_seconds": round(time.time() - _APP_STARTED_AT),
        "python_backend": True,
        "tts_provider": _default_provider(),
        "payments_configured": payments.is_configured(),
        "elevenlabs_configured": eleven_tts.is_configured(),
    }


@app.route("/api/status")
def api_status():
    return jsonify(_deployment_status())


@app.route("/status")
def status_page():
    return render_template("status.html", status=_deployment_status())


@app.route("/support")
def support_page():
    return render_template("support.html", biz=BUSINESS_CONFIG)


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