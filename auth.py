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
    """Decode + verify a Supabase JWT using the project's HS256 secret.
    Returns the decoded claims. Raises AuthError on failure.
    """
    if not SUPABASE_JWT_SECRET:
        raise AuthError("server misconfigured: SUPABASE_JWT_SECRET missing")
    try:
        claims = pyjwt.decode(
            token,
            SUPABASE_JWT_SECRET,
            algorithms=["HS256"],
            audience="authenticated",
        )
    except pyjwt.ExpiredSignatureError as e:
        raise AuthError("token expired") from e
    except pyjwt.InvalidTokenError as e:
        raise AuthError(f"invalid token: {e}") from e
    if not claims.get("sub"):
        raise AuthError("token missing sub claim")
    return claims


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
    """Idempotent — set role='admin' for the given user_id."""
    try:
        admin_client().table("profiles").update({"role": "admin"}).eq("user_id", user_id).execute()
    except Exception as e:
        print(f"[auth] _ensure_admin({email}) failed: {e}")


def has_unlimited_quota(role: str) -> bool:
    return (role or "").lower() in _UNLIMITED_ROLES


# ──────────────────────────────────────────────────────────────────────
# Quota tracking
# ──────────────────────────────────────────────────────────────────────

def get_profile(user_id: str) -> dict | None:
    """Fetch the user's profile row (plan + quota). Returns None if
    missing (should never happen because of the auth trigger)."""
    res = admin_client().table("profiles").select("*").eq("user_id", user_id).execute()
    rows = getattr(res, "data", None) or []
    return rows[0] if rows else None


def get_monthly_chars(user_id: str) -> int:
    """How many chars this user has consumed in the last 30 days."""
    res = admin_client().table("monthly_usage").select("chars_30d").eq("user_id", user_id).execute()
    rows = getattr(res, "data", None) or []
    if not rows:
        return 0
    return int(rows[0].get("chars_30d") or 0)


def check_quota(user_id: str, requested_chars: int) -> tuple[bool, str]:
    """Return (allowed, message_if_denied)."""
    profile = get_profile(user_id)
    if not profile:
        return False, "Profile not found — please re-login"
    if has_unlimited_quota(profile.get("role")):
        return True, ""
    quota = int(profile.get("quota_chars") or DEFAULT_QUOTA_CHARS)
    used = get_monthly_chars(user_id)
    if used + requested_chars > quota:
        return False, (
            f"Monthly quota exceeded: {used}/{quota} chars used, "
            f"request needs {requested_chars} more. Upgrade plan to continue."
        )
    return True, ""


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
