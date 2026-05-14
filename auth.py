"""Supabase auth + per-user quota tracking.

When CLOUD_MODE=1, every /generate and /tts request must carry a
valid Supabase JWT in the Authorization header. We verify the JWT,
attach the user to flask.g, check the user's quota, and after the
TTS finishes we log a usage_events row.

When CLOUD_MODE is unset (papa's local box), auth is a no-op — the
require_user decorator passes through.
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


def cloud_mode() -> bool:
    """True when the server is running in production cloud mode.
    Local mode bypasses auth and quota entirely."""
    return os.getenv("CLOUD_MODE") == "1"


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
    """Protect a Flask route. In cloud mode, validates the JWT and
    attaches the user info to flask.g.user. In local mode, no-op.
    """
    @functools.wraps(handler)
    def wrapped(*args, **kwargs):
        if not cloud_mode():
            g.user = None
            return handler(*args, **kwargs)

        token = _extract_token()
        if not token:
            return jsonify({"error": "Authentication required"}), 401
        try:
            claims = verify_jwt(token)
        except AuthError as e:
            return jsonify({"error": str(e)}), 401

        g.user = {
            "id": claims["sub"],
            "email": claims.get("email"),
            "claims": claims,
        }
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
