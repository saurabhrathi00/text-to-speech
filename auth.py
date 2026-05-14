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

DEFAULT_QUOTA_CHARS = int(os.getenv("DEFAULT_QUOTA_CHARS", "1000"))

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
    # ── Step 1: local HS256 ────────────────────────────────────────────
    if SUPABASE_JWT_SECRET:
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

        g.user = {
            "id": user_id,
            "email": email,
            "claims": claims,
            "role": _get_role(user_id),
        }
        return handler(*args, **kwargs)
    return wrapped


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
            "chars_24h,chars_30d,chars_total,uses_24h,uses_30d,uses_total"
        ).eq("user_id", user_id).execute()
        rows = getattr(res, "data", None) or []
        if rows:
            return rows[0]
    except Exception as e:
        print(f"[auth] get_usage_summary failed: {e}")
    return {"chars_24h": 0, "chars_30d": 0, "chars_total": 0,
             "uses_24h": 0, "uses_30d": 0, "uses_total": 0}


# Backward-compat alias used by /api/me etc.
def get_monthly_chars(user_id: str) -> int:
    return int(get_usage_summary(user_id).get("chars_30d") or 0)


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
    # Validate plan exists in plan_limits
    if not get_plan_limits(plan) or plan == "free":
        return None, f"Unknown or unsupported plan '{plan}'"

    profile = get_profile(user_id) or {}
    current = (profile.get("plan") or "free").lower()
    if _plan_rank(current) >= _plan_rank(plan):
        return None, f"Already on {current} plan — no upgrade needed"

    if get_pending_upgrade(user_id):
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
            # Bump the user's plan first; only mark the request resolved
            # if the plan update succeeded.
            admin_client().table("profiles").update(
                {"plan": req["requested_plan"], "updated_at": "now()"}
            ).eq("user_id", req["user_id"]).execute()

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
      - everyone else → row matching their plan
    Falls back to a conservative cloud-only default if the table read
    fails — never returns an empty list (would 403 every request).
    """
    role = (profile or {}).get("role", "").lower()
    plan = (profile or {}).get("plan") or "free"
    lookup_plan = "admin" if role == "admin" else plan
    limits = get_plan_limits(lookup_plan) or {}
    return {
        "llm": list(limits.get("llm_providers") or ["gemini"]),
        "tts": list(limits.get("tts_providers") or ["elevenlabs"]),
    }


def check_limits(user_id: str, requested_chars: int) -> tuple[bool, str]:
    """Enforce all plan limits in order, cheapest check first.
    Returns (allowed, denial_reason)."""
    profile = get_profile(user_id)
    if not profile:
        return False, "Profile not found — please re-login"

    # Unlimited roles (admin) skip all checks
    if has_unlimited_quota(profile.get("role")):
        return True, ""

    plan = profile.get("plan") or "free"
    limits = get_plan_limits(plan)
    if not limits:
        return False, "Plan configuration not found — contact support"

    # 1. Per-request size (instant, no DB read)
    max_per = limits.get("max_chars_per_request")
    if max_per is not None and requested_chars > max_per:
        return False, (
            f"Script too long: {requested_chars} chars exceeds your "
            f"{plan} plan limit of {max_per} chars per request. Upgrade to send longer scripts."
        )

    # 2-4. Usage-based checks need one query
    summary = get_usage_summary(user_id)

    lifetime_cap = limits.get("lifetime_uses")
    if lifetime_cap is not None and summary["uses_total"] >= lifetime_cap:
        return False, (
            f"You've used your free trial ({lifetime_cap} generation). Upgrade to continue."
        )

    daily_cap = limits.get("daily_uses")
    if daily_cap is not None and summary["uses_24h"] >= daily_cap:
        return False, (
            f"Daily limit reached: {summary['uses_24h']}/{daily_cap} generations used today. "
            f"Wait 24 hours or upgrade your plan."
        )

    monthly_cap = limits.get("monthly_chars")
    if monthly_cap is not None and summary["chars_30d"] + requested_chars > monthly_cap:
        return False, (
            f"Monthly character budget exceeded: {summary['chars_30d']}/{monthly_cap} chars used, "
            f"request needs {requested_chars} more. Upgrade or wait 30 days."
        )

    return True, ""


# Backward-compat alias for existing call sites.
def check_quota(user_id: str, requested_chars: int) -> tuple[bool, str]:
    return check_limits(user_id, requested_chars)


def log_usage(user_id: str, kind: str, provider: str, chars: int,
               cost_usd: float = 0.0, meta: dict | None = None):
    """Append a usage_events row. Called after a successful TTS generation."""
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
