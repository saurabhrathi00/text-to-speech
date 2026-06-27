"""Supabase auth + per-user quota tracking.

Every request must carry a valid Supabase JWT. There is no local
bypass — papa logs in like any user but gets role='admin' in his
profile, which skips the quota check.

Admin emails listed in ADMIN_EMAILS env var (comma-separated) are
auto-promoted to role='admin' on first sign-in.
"""
import os
import time
import functools
from typing import Callable, Any

import jwt as pyjwt
from flask import g, jsonify, request


SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
SUPABASE_JWT_SECRET = os.getenv("SUPABASE_JWT_SECRET", "")

# Emails listed here are auto-promoted to role='admin' on sign-in.
# Comma-separated, case-insensitive. Example: ADMIN_EMAILS=papa@x.com,me@x.com
_ADMIN_EMAILS = {
    e.strip().lower()
    for e in os.getenv("ADMIN_EMAILS", "").split(",")
    if e.strip()
}

# Roles that bypass quota entirely. Comma-separated, default = "admin".
_UNLIMITED_ROLES = {
    r.strip().lower()
    for r in os.getenv("UNLIMITED_ROLES", "admin").split(",")
    if r.strip()
}


def auth_disabled() -> bool:
    """Escape hatch — disables auth entirely. Useful only for first-run
    local dev / smoke tests before Supabase keys exist. Default off so
    production stays safe by default."""
    return os.getenv("AUTH_DISABLED") == "1"


# ──────────────────────────────────────────────────────────────────────
# Supabase admin client (service role — bypasses RLS for backend ops)
# ──────────────────────────────────────────────────────────────────────

_admin_client = None


def admin_client():
    global _admin_client
    if _admin_client is not None:
        return _admin_client
    if not (SUPABASE_URL and SUPABASE_SERVICE_KEY):
        raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_KEY must be set in cloud mode")
    from supabase import create_client
    _admin_client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    return _admin_client


# ──────────────────────────────────────────────────────────────────────
# JWT verification
# ──────────────────────────────────────────────────────────────────────

class AuthError(Exception):
    """Raised when JWT is missing, malformed, or expired."""


def _extract_token() -> str | None:
    header = request.headers.get("Authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return None


def verify_jwt(token: str) -> dict:
    """Verify a Supabase JWT and return its claims.

    Two-step:
      1. Try local HS256 verification using SUPABASE_JWT_SECRET (fast,
         no network).
      2. If that fails (algorithm mismatch, secret rotated, asymmetric
         signing key, etc.), fall back to Supabase's auth.get_user()
         which validates the token server-side.

    The fallback covers new Supabase projects that ship with asymmetric
    JWT signing keys where the dashboard "JWT Secret" doesn't HMAC-verify
    the tokens locally.
    """
    # ── Step 1: local HMAC verification (fast, no network) ──────────
    if SUPABASE_JWT_SECRET:
        try:
            header = pyjwt.get_unverified_header(token)
        except pyjwt.DecodeError:
            header = {}
        alg = header.get("alg", "")
        if alg.startswith("HS"):
            try:
                return pyjwt.decode(
                    token, SUPABASE_JWT_SECRET,
                    algorithms=["HS256"],
                    audience="authenticated",
                )
            except pyjwt.ExpiredSignatureError as e:
                raise AuthError("token expired") from e
            except pyjwt.InvalidTokenError as e:
                print(f"[auth] local HS256 verify failed ({e}); falling back to Supabase SDK")

    # ── Step 2: ask Supabase to validate ──────────────────────────────
    try:
        resp = admin_client().auth.get_user(token)
    except Exception as e:
        raise AuthError(f"invalid token: {e}") from e
    user = getattr(resp, "user", None)
    if not user:
        raise AuthError("invalid token (no user returned)")
    return {
        "sub": user.id,
        "email": getattr(user, "email", None),
        "_via": "supabase_sdk",
    }


# ──────────────────────────────────────────────────────────────────────
# Flask decorator
# ──────────────────────────────────────────────────────────────────────

def require_user(handler: Callable) -> Callable:
    """Protect a Flask route — Supabase JWT required.
    On success, flask.g.user is set with id, email, and role.
    Admin emails (ADMIN_EMAILS env var) are auto-promoted on first hit.
    """
    @functools.wraps(handler)
    def wrapped(*args, **kwargs):
        if auth_disabled():
            g.user = None
            return handler(*args, **kwargs)

        token = _extract_token()
        if not token:
            return jsonify({"error": "Authentication required"}), 401
        try:
            claims = verify_jwt(token)
        except AuthError as e:
            return jsonify({"error": str(e)}), 401

        user_id = claims["sub"]
        email = (claims.get("email") or "").lower()

        # Auto-promote admin emails on first contact.
        if email and email in _ADMIN_EMAILS:
            _ensure_admin(user_id, email)

        # Profile sanity check. The auth.users row is the source of
        # truth for "can this user authenticate?" — the profiles row
        # is just our app-side mirror. If it got deleted manually,
        # re-create it on next request instead of leaving the user in
        # a half-broken state. If it exists but banned=true, lock out.
        profile = get_profile(user_id)
        if profile is None:
            _ensure_profile(user_id, email)
        elif profile.get("banned"):
            return jsonify({"error": "Account suspended. Contact support."}), 403

        g.user = {
            "id": user_id,
            "email": email,
            "claims": claims,
            "role": _get_role(user_id),
        }
        return handler(*args, **kwargs)
    return wrapped


def _ensure_profile(user_id: str, email: str):
    """Recreate the public.profiles row if it's missing. Happens when
    someone deletes the row directly in the DB — the user can still
    sign in (auth.users still has them), so we patch the mirror back
    rather than break their session."""
    try:
        admin_client().table("profiles").insert({
            "user_id": user_id,
            "email": email,
        }).execute()
        print(f"[auth] recreated missing profile for {email}")
    except Exception as e:
        # Most likely cause: row exists but read failed transiently.
        # Either way, swallow — we never want a profile sync issue to
        # 500 a legit request.
        print(f"[auth] _ensure_profile({email}) skipped: {e}")


def _get_role(user_id: str) -> str:
    """Read the user's current role from profiles. Default 'user'."""
    try:
        res = admin_client().table("profiles").select("role").eq("user_id", user_id).execute()
        rows = getattr(res, "data", None) or []
        if rows:
            return (rows[0].get("role") or "user").lower()
    except Exception as e:
        print(f"[auth] _get_role failed: {e}")
    return "user"


def _ensure_admin(user_id: str, email: str):
    """Idempotent — set role='admin' AND plan='admin' for the given
    user_id. The plan bump keeps the profile row internally consistent
    (otherwise admins show plan='free' from the signup trigger), and
    means provider-list lookups by plan resolve to the admin row even
    if a caller forgets to short-circuit on role."""
    try:
        admin_client().table("profiles").update(
            {"role": "admin", "plan": "admin"}
        ).eq("user_id", user_id).execute()
    except Exception as e:
        print(f"[auth] _ensure_admin({email}) failed: {e}")


def has_unlimited_quota(role: str) -> bool:
    return (role or "").lower() in _UNLIMITED_ROLES


def require_admin(handler: Callable) -> Callable:
    """Protect a Flask route — JWT required AND user's email must be
    in ADMIN_EMAILS env. The DB role column is informational; the
    authoritative source of admin status is the env file, so a
    tampered profiles row can never grant admin powers."""
    @functools.wraps(handler)
    def wrapped(*args, **kwargs):
        if auth_disabled():
            g.user = None
            return handler(*args, **kwargs)

        token = _extract_token()
        if not token:
            return jsonify({"error": "Authentication required"}), 401
        try:
            claims = verify_jwt(token)
        except AuthError as e:
            return jsonify({"error": str(e)}), 401

        user_id = claims["sub"]
        email = (claims.get("email") or "").lower()
        if not email or email not in _ADMIN_EMAILS:
            return jsonify({"error": "Admin access required"}), 403
        _ensure_admin(user_id, email)
        role = _get_role(user_id)

        g.user = {"id": user_id, "email": email, "claims": claims, "role": role}
        return handler(*args, **kwargs)
    return wrapped


# ──────────────────────────────────────────────────────────────────────
# Quota tracking
# ──────────────────────────────────────────────────────────────────────

def get_profile(user_id: str) -> dict | None:
    """Fetch the user's profile row (plan + quota). Returns None if
    missing (should never happen because of the auth trigger)."""
    res = admin_client().table("profiles").select("*").eq("user_id", user_id).execute()
    rows = getattr(res, "data", None) or []
    return rows[0] if rows else None


def get_usage_summary(user_id: str) -> dict:
    """Read the rolling usage view for one user. Returns a dict with
    chars_24h, chars_30d, chars_total, uses_24h, uses_30d, uses_total.
    Missing user (no usage yet) returns all zeros."""
    try:
        res = admin_client().table("usage_summary").select(
            "chars_24h,chars_30d,chars_total,gen_chars_30d,topup_credit_30d,"
            "uses_24h,uses_30d,uses_total"
        ).eq("user_id", user_id).execute()
        rows = getattr(res, "data", None) or []
        if rows:
            return rows[0]
    except Exception as e:
        print(f"[auth] get_usage_summary failed: {e}")
    return {"chars_24h": 0, "chars_30d": 0, "chars_total": 0,
             "gen_chars_30d": 0, "topup_credit_30d": 0,
             "uses_24h": 0, "uses_30d": 0, "uses_total": 0}


def consume_bonus_if_used(user_id: str):
    """Call AFTER a successful generation. If the just-logged gen
    pushed the user past their base daily allowance, decrement
    bonus_uses by 1. When bonus_uses hits zero, clear bonus_max — the
    per-request override only applies while gens remain in the pool."""
    profile = get_profile(user_id) or {}
    bonus_uses = int(profile.get("bonus_uses") or 0)
    if bonus_uses <= 0:
        return

    plan = get_effective_plan(profile)
    base = get_plan_limits(plan) or {}
    base_daily = base.get("daily_uses")
    if base_daily is None:
        return  # plan has no daily cap → bonus_uses not relevant

    usage = get_usage_summary(user_id)
    if int(usage.get("uses_24h") or 0) <= base_daily:
        return  # base allowance covered this gen

    update = {
        "bonus_uses": max(0, bonus_uses - 1),
        "updated_at": "now()",
    }
    if update["bonus_uses"] == 0:
        update["bonus_max_chars_per_request"] = None
    try:
        admin_client().table("profiles").update(update).eq(
            "user_id", user_id).execute()
    except Exception as e:
        print(f"[auth] consume_bonus_if_used({user_id}) failed: {e}")


def get_plan_limits(plan: str) -> dict | None:
    """Read the plan_limits row for the given plan. Falls back to
    the 'free' row if the user's plan isn't in the table yet."""
    plan = (plan or "free").lower()
    try:
        res = admin_client().table("plan_limits").select("*").eq("plan", plan).execute()
        rows = getattr(res, "data", None) or []
        if rows:
            return rows[0]
        # fall back to free
        res = admin_client().table("plan_limits").select("*").eq("plan", "free").execute()
        rows = getattr(res, "data", None) or []
        if rows:
            return rows[0]
    except Exception as e:
        print(f"[auth] get_plan_limits({plan}) failed: {e}")
    return None


# ──────────────────────────────────────────────────────────────────────
# Plan upgrade requests
# ──────────────────────────────────────────────────────────────────────

# Lower number = lower tier. Sourced from DB at runtime via _plan_rank
# so adding a tier in plan_limits doesn't need a code change. The
# hardcoded fallback only kicks in if the DB read fails entirely.
_PLAN_RANK_FALLBACK = {"free": 0, "starter": 1, "pro": 2, "pro_plus": 3, "admin": 99}


def _plan_rank(plan: str) -> int:
    plan = (plan or "free").lower()
    try:
        res = admin_client().table("plan_limits").select("plan,price_inr_monthly").execute()
        rows = getattr(res, "data", None) or []
        if rows:
            ranked = sorted(rows, key=lambda r: (r.get("price_inr_monthly") or 0))
            order = {r["plan"]: i for i, r in enumerate(ranked)}
            if "admin" in order:
                order["admin"] = 99
            return order.get(plan, 0)
    except Exception:
        pass
    return _PLAN_RANK_FALLBACK.get(plan, 0)


def _now_utc():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


def _parse_ts(value) -> "datetime | None":
    """Supabase returns timestamps as ISO strings; normalize to aware datetime."""
    if not value:
        return None
    from datetime import datetime
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def get_effective_plan(profile: dict | None) -> str:
    """The plan that actually applies right now.

    Rules:
      - role='admin' → always 'admin' (no expiry)
      - profiles.plan_expires_at in the past → 'free' (reverted)
      - otherwise → profiles.plan
    """
    if not profile:
        return "free"
    role = (profile.get("role") or "").lower()
    if role == "admin":
        return "admin"
    plan = (profile.get("plan") or "free").lower()
    if plan in ("free", "admin"):
        return plan
    expires = _parse_ts(profile.get("plan_expires_at"))
    if expires and expires < _now_utc():
        return "free"
    return plan


def get_pending_upgrade(user_id: str) -> dict | None:
    """Return the user's most recent pending upgrade request, if any."""
    try:
        res = (admin_client().table("upgrade_requests")
               .select("id,requested_plan,status,created_at")
               .eq("user_id", user_id)
               .eq("status", "pending")
               .order("created_at", desc=True)
               .limit(1)
               .execute())
        rows = getattr(res, "data", None) or []
        return rows[0] if rows else None
    except Exception as e:
        print(f"[auth] get_pending_upgrade failed: {e}")
        return None


def create_upgrade_request(user_id: str, requested_plan: str,
                            note: str = "") -> tuple[dict | None, str | None]:
    """Insert an upgrade request. Returns (row, error).

    Rejects when:
      - target plan isn't recognised
      - user is already at or above the requested tier
      - user already has a pending request
    """
    plan = (requested_plan or "").lower().strip()
    if plan == "admin":
        return None, "Admin tier is not user-requestable"
    limits = get_plan_limits(plan)
    if not limits or plan == "free":
        return None, f"Unknown or unsupported plan '{plan}'"

    kind = (limits.get("kind") or "subscription").lower()
    profile = get_profile(user_id) or {}

    # Subscriptions require a rank-up. Top-ups are additive credits —
    # any user on any plan can buy them as often as they want.
    if kind == "subscription":
        current = (profile.get("plan") or "free").lower()
        if _plan_rank(current) >= _plan_rank(plan):
            return None, f"Already on {current} plan — no upgrade needed"
        # Block another pending SUBSCRIPTION request; pending top-ups
        # are fine to coexist (different lifecycle, different approval).
        pending = get_pending_upgrade(user_id)
        if pending:
            p_limits = get_plan_limits(pending["requested_plan"]) or {}
            p_kind = (p_limits.get("kind") or "subscription").lower()
            if p_kind == "subscription":
                return None, "You already have a pending upgrade request"

    try:
        res = admin_client().table("upgrade_requests").insert({
            "user_id": user_id,
            "requested_plan": plan,
            "note": note or None,
        }).execute()
        rows = getattr(res, "data", None) or []
        return (rows[0] if rows else None), None
    except Exception as e:
        return None, f"Failed to create upgrade request: {e}"


def list_upgrade_requests(status: str | None = "pending") -> list[dict]:
    """Admin-only — list requests joined with user email for display."""
    try:
        q = (admin_client().table("upgrade_requests")
             .select("id,user_id,requested_plan,status,note,"
                     "created_at,processed_at")
             .order("created_at", desc=True))
        if status:
            q = q.eq("status", status)
        res = q.execute()
        rows = getattr(res, "data", None) or []
        if not rows:
            return []
        # Best-effort email enrichment from profiles
        ids = list({r["user_id"] for r in rows})
        prof_res = (admin_client().table("profiles")
                    .select("user_id,email,plan").in_("user_id", ids).execute())
        prof_rows = getattr(prof_res, "data", None) or []
        emap = {p["user_id"]: p for p in prof_rows}
        for r in rows:
            p = emap.get(r["user_id"]) or {}
            r["email"] = p.get("email")
            r["current_plan"] = p.get("plan")
        return rows
    except Exception as e:
        print(f"[auth] list_upgrade_requests failed: {e}")
        return []


def apply_plan_grant(user_id: str, target_plan: str,
                      source_ref: str | None = None) -> tuple[bool, str | None]:
    """Apply a purchased/approved plan to a user's profile.

    Shared by admin approval (resolve_upgrade_request) and self-serve
    payment capture (payments.handle_webhook / verify). `source_ref` is
    a label stored on the top-up credit ledger row for traceability
    (e.g. 'request:12', 'order:order_abc'). Returns (ok, error).

    Mechanics mirror the original resolve_upgrade_request logic:
      - subscription → stamp profiles.plan + plan_expires_at
      - top-up       → additive overlay (chars ledger credit, bump
        bonus_uses + bonus_max_chars_per_request, extend expiry)
    """
    limits = get_plan_limits(target_plan) or {}
    if not limits:
        return False, f"Unknown plan '{target_plan}'"
    kind = (limits.get("kind") or "subscription").lower()

    if kind == "topup":
        bonus_chars = int(limits.get("monthly_chars") or 0)
        bonus_reqs  = int(limits.get("daily_uses") or 0)
        bonus_max   = int(limits.get("max_chars_per_request") or 0)
        bonus_hours = int(limits.get("validity_hours") or 0)
        if (bonus_chars <= 0 and bonus_reqs <= 0
                and bonus_max <= 0 and bonus_hours <= 0):
            return False, "Top-up plan has nothing to grant — contact support"
        try:
            # (1) chars credit on the ledger
            if bonus_chars > 0:
                admin_client().table("usage_events").insert({
                    "user_id": user_id,
                    "kind": "credit.topup",
                    "provider": None,
                    "chars": -bonus_chars,
                    "cost_usd": 0,
                    "meta": {"topup_plan": target_plan, "source": source_ref},
                }).execute()

            # (2)+(3) bump bonus_uses + bonus_max on profile
            cur_profile = get_profile(user_id) or {}
            profile_update = {"updated_at": "now()"}
            if bonus_reqs > 0:
                profile_update["bonus_uses"] = (
                    int(cur_profile.get("bonus_uses") or 0) + bonus_reqs)
            if bonus_max > 0:
                profile_update["bonus_max_chars_per_request"] = (
                    int(cur_profile.get("bonus_max_chars_per_request") or 0) + bonus_max)

            # (4) extend plan_expires_at by topup's validity_hours
            if bonus_hours > 0:
                from datetime import timedelta
                current_expiry = _parse_ts(cur_profile.get("plan_expires_at"))
                base_ts = current_expiry if (current_expiry and current_expiry > _now_utc()) else _now_utc()
                profile_update["plan_expires_at"] = (
                    base_ts + timedelta(hours=bonus_hours)).isoformat()

            if len(profile_update) > 1:  # more than just updated_at
                admin_client().table("profiles").update(profile_update).eq(
                    "user_id", user_id).execute()
        except Exception as e:
            return False, f"Failed to credit top-up: {e}"
    else:
        # Subscription: stamp plan + expiry timestamp atomically.
        validity = limits.get("validity_hours")
        update = {"plan": target_plan, "updated_at": "now()"}
        if validity:
            from datetime import timedelta
            update["plan_expires_at"] = (
                _now_utc() + timedelta(hours=int(validity))).isoformat()
        else:
            update["plan_expires_at"] = None
        try:
            admin_client().table("profiles").update(update).eq(
                "user_id", user_id).execute()
        except Exception as e:
            return False, f"Failed to apply subscription: {e}"
    return True, None


def resolve_upgrade_request(request_id: int, action: str,
                             admin_user_id: str) -> tuple[dict | None, str | None]:
    """Admin marks a request approved or rejected. On approve, also
    bumps the target user's plan. Idempotent: already-resolved requests
    can't be flipped."""
    if action not in ("approve", "reject"):
        return None, "Action must be 'approve' or 'reject'"
    try:
        # Fetch the row first so we know the target user + plan.
        res = (admin_client().table("upgrade_requests")
               .select("*").eq("id", request_id).execute())
        rows = getattr(res, "data", None) or []
        if not rows:
            return None, "Request not found"
        req = rows[0]
        if req["status"] != "pending":
            return None, f"Request already {req['status']}"

        new_status = "approved" if action == "approve" else "rejected"

        if action == "approve":
            ok, grant_err = apply_plan_grant(
                req["user_id"], req["requested_plan"],
                source_ref=f"request:{req['id']}")
            if not ok:
                return None, grant_err

        upd = admin_client().table("upgrade_requests").update({
            "status": new_status,
            "processed_at": "now()",
            "processed_by": admin_user_id,
        }).eq("id", request_id).execute()
        rows = getattr(upd, "data", None) or []
        return (rows[0] if rows else None), None
    except Exception as e:
        return None, f"Failed to resolve request: {e}"


def get_allowed_providers(profile: dict | None) -> dict:
    """Return the LLM + TTS providers this user is allowed to call.

    Lookup rule:
      - role='admin' → admin row (regardless of plan)
      - everyone else → row matching their EFFECTIVE plan (expired
        paid plans revert to 'free' for provider whitelist too)
    Falls back to a conservative cloud-only default if the table read
    fails — never returns an empty list (would 403 every request).
    """
    role = (profile or {}).get("role", "").lower()
    lookup_plan = "admin" if role == "admin" else get_effective_plan(profile)
    limits = get_plan_limits(lookup_plan) or {}
    return {
        "llm": list(limits.get("llm_providers") or ["gemini"]),
        "tts": list(limits.get("tts_providers") or ["elevenlabs"]),
    }


def get_effective_limits(profile: dict, summary: dict | None = None) -> dict:
    """Resolve the limits actually in force for this user RIGHT NOW.

    Combines:
      - base plan limits (plan_limits[effective_plan])
      - active top-up bonus pool (profile.bonus_uses,
        profile.bonus_max_chars_per_request)
      - cumulative chars credit from negative usage_events
        (summary.topup_credit_30d)

    Result: {max_chars_per_request, daily_cap, monthly_cap, daily_used,
    monthly_used, chars_remaining, daily_remaining, has_topup}
    Numbers are what the UI should display 'X / Y'-style.
    """
    plan = get_effective_plan(profile)
    base = get_plan_limits(plan) or {}
    if summary is None:
        summary = get_usage_summary(profile.get("user_id"))

    base_max     = base.get("max_chars_per_request")
    base_daily   = base.get("daily_uses")
    base_monthly = base.get("monthly_chars")

    bonus_uses     = int(profile.get("bonus_uses") or 0)
    bonus_max      = profile.get("bonus_max_chars_per_request")
    topup_credit   = int(summary.get("topup_credit_30d") or 0)
    gen_chars_30d  = int(summary.get("gen_chars_30d") or 0)
    uses_24h       = int(summary.get("uses_24h") or 0)

    # All three caps are additive: base + bonus. User's stated intent —
    # buying a top-up grows every limit by the top-up's value, never
    # replaces. None means unlimited (kept as None to preserve admin).
    eff_max     = ((int(base_max or 0)     + int(bonus_max or 0)))  if (base_max or bonus_max) else None
    eff_daily   = (int(base_daily or 0)    + bonus_uses)            if base_daily   is not None else None
    eff_monthly = (int(base_monthly or 0)  + topup_credit)          if base_monthly is not None else None

    return {
        "max_chars_per_request": eff_max,
        "daily_cap":             eff_daily,
        "monthly_cap":           eff_monthly,
        "daily_used":            uses_24h,
        "monthly_used":          gen_chars_30d,
        "daily_remaining":       None if eff_daily   is None else max(0, eff_daily   - uses_24h),
        "chars_remaining":       None if eff_monthly is None else max(0, eff_monthly - gen_chars_30d),
        "has_topup":             bonus_uses > 0 or topup_credit > 0,
        "bonus_uses":            bonus_uses,
        "topup_credit_30d":      topup_credit,
    }


def check_limits(user_id: str, requested_chars: int) -> tuple[bool, str]:
    """Enforce all plan limits in order, cheapest check first.
    Returns (allowed, denial_reason). Top-up bonuses extend each cap
    individually (see get_effective_limits)."""
    profile = get_profile(user_id)
    if not profile:
        return False, "Profile not found — please re-login"

    # Unlimited roles (admin) skip all checks
    if has_unlimited_quota(profile.get("role")):
        return True, ""

    profile["user_id"] = user_id  # ensure helpers can find it
    summary = get_usage_summary(user_id)
    eff = get_effective_limits(profile, summary)
    plan = get_effective_plan(profile)
    base = get_plan_limits(plan) or {}

    # 1. Per-request size (effective cap = base or higher of base/bonus)
    max_per = eff.get("max_chars_per_request")
    if max_per is not None and requested_chars > max_per:
        return False, (
            f"Script too long: {requested_chars} chars exceeds your "
            f"{max_per}-char per-request limit. Upgrade to send longer scripts."
        )

    # 2. Lifetime check (rarely used now; still on free trial rows)
    lifetime_cap = base.get("lifetime_uses")
    if lifetime_cap is not None and summary["uses_total"] >= lifetime_cap:
        return False, (
            f"You've used your free trial ({lifetime_cap} generation). Upgrade to continue."
        )

    # 3. Daily — effective cap includes bonus_uses
    daily_cap = eff.get("daily_uses")
    if daily_cap is not None and summary["uses_24h"] >= daily_cap:
        return False, (
            f"Daily limit reached: {summary['uses_24h']}/{daily_cap} generations used today. "
            f"Buy a top-up or wait 24 hours."
        )

    # 4. Monthly — effective cap includes topup_credit_30d. Compare
    # against gen_chars_30d (positive-only) so refunds don't double-
    # count and the '<used>/<cap>' arithmetic matches what the UI shows.
    monthly_cap = eff.get("monthly_chars")
    gen_chars = int(summary.get("gen_chars_30d") or 0)
    if monthly_cap is not None and (gen_chars + requested_chars) > monthly_cap:
        return False, (
            f"Monthly character budget exceeded: {gen_chars}/{monthly_cap} chars used, "
            f"request needs {requested_chars} more. Buy a top-up or wait 30 days."
        )

    return True, ""


def log_usage(user_id: str, kind: str, provider: str, chars: int,
               cost_usd: float = 0.0, meta: dict | None = None):
    """Append a usage_events row + decrement top-up bonus pool if active.
    Called after a successful TTS generation."""
    try:
        admin_client().table("usage_events").insert({
            "user_id": user_id,
            "kind": kind,
            "provider": provider,
            "chars": chars,
            "cost_usd": cost_usd,
            "meta": meta or {},
        }).execute()
    except Exception as e:
        # Never fail the user request because of logging issues — just record.
        print(f"[auth] log_usage failed: {e}")

    # Decrement bonus_uses if this generation was past the base plan's
    # daily cap (and a top-up was funding it). When bonus_uses reaches
    # zero, clear the per-request cap override too — base plan takes over.
    try:
        profile = get_profile(user_id) or {}
        bonus_uses = int(profile.get("bonus_uses") or 0)
        if bonus_uses <= 0:
            return
        plan = get_effective_plan(profile)
        base = get_plan_limits(plan) or {}
        base_daily = base.get("daily_uses")
        if base_daily is None:
            return  # no daily cap → no need to dip into bonus pool
        summary = get_usage_summary(user_id)
        # uses_24h already includes the row we just inserted.
        if summary.get("uses_24h", 0) > base_daily:
            new_bonus = max(0, bonus_uses - 1)
            update = {"bonus_uses": new_bonus, "updated_at": "now()"}
            if new_bonus == 0:
                update["bonus_max_chars_per_request"] = None
            admin_client().table("profiles").update(update).eq(
                "user_id", user_id).execute()
    except Exception as e:
        print(f"[auth] bonus_uses decrement failed: {e}")
