"""Anti-abuse middleware: rate limiting, content-type enforcement,
CORS allowlist, suspicious-activity flagging.

All state is in-memory + per-process. Adequate for a single-host or
small gunicorn deployment (2–4 workers); each worker gets its own
counters, so effective limits are N_workers × the configured rate.
Move to Redis when you need cross-worker accuracy or multi-host.
"""
import os
import time
import threading
import functools
from collections import deque, defaultdict
from flask import request, jsonify, g


# ──────────────────────────────────────────────────────────────────────
# Config (env-driven so the cloud box can dial these without code changes)
# ──────────────────────────────────────────────────────────────────────
MAX_REQUEST_BYTES = int(os.getenv("MAX_REQUEST_BYTES", str(1 * 1024 * 1024)))   # 1 MB
CORS_ALLOWED_ORIGINS = [
    o.strip().rstrip("/")
    for o in os.getenv("CORS_ALLOWED_ORIGINS", "").split(",")
    if o.strip()
]


# ──────────────────────────────────────────────────────────────────────
# Sliding-window rate limiter
# ──────────────────────────────────────────────────────────────────────
_buckets: "defaultdict[str, deque[float]]" = defaultdict(deque)
_buckets_lock = threading.Lock()


def _client_ip() -> str:
    """First IP in X-Forwarded-For if present (Render/Cloudflare set
    this), else the direct remote_addr. Trust XFF only because we
    expect to deploy behind a CDN/PaaS that strips client-supplied
    headers."""
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.remote_addr or "unknown"


def _rate_check(key: str, max_calls: int, window_sec: float) -> tuple[bool, int]:
    """True if the call should be allowed; second return is seconds
    until the oldest counted hit falls out of the window (= cooldown
    hint for the 429 response)."""
    now = time.time()
    with _buckets_lock:
        q = _buckets[key]
        # Drop expired entries from the left
        cutoff = now - window_sec
        while q and q[0] < cutoff:
            q.popleft()
        if len(q) >= max_calls:
            retry_in = int(q[0] + window_sec - now) + 1
            return False, max(1, retry_in)
        q.append(now)
        return True, 0


def rate_limit(scope: str, max_calls: int, window_sec: float):
    """Decorator. `scope` is 'ip' (per remote IP) or 'user' (per
    authenticated user; falls back to IP for anonymous routes)."""
    if scope not in ("ip", "user"):
        raise ValueError(f"rate_limit scope must be 'ip' or 'user', got {scope!r}")

    def decorator(handler):
        @functools.wraps(handler)
        def wrapped(*args, **kwargs):
            user = getattr(g, "user", None)
            ident = (user.get("id") if scope == "user" and user else None) or _client_ip()
            key = f"{scope}:{handler.__name__}:{ident}"
            ok, retry_in = _rate_check(key, max_calls, window_sec)
            if not ok:
                flag_suspicious("rate_limit",
                                f"{handler.__name__} scope={scope} ident={ident} "
                                f"retry_in={retry_in}s")
                resp = jsonify({
                    "error": f"Too many requests. Try again in {retry_in}s.",
                    "retry_after": retry_in,
                })
                resp.headers["Retry-After"] = str(retry_in)
                return resp, 429
            return handler(*args, **kwargs)
        return wrapped
    return decorator


# ──────────────────────────────────────────────────────────────────────
# Strict Content-Type enforcement on JSON POST routes — kills
# accidental CORS-bypass form posts and obvious bot probing.
# ──────────────────────────────────────────────────────────────────────

def require_json(handler):
    @functools.wraps(handler)
    def wrapped(*args, **kwargs):
        if request.method in ("POST", "PATCH", "PUT"):
            ctype = (request.content_type or "").split(";")[0].strip().lower()
            if ctype and ctype != "application/json":
                return jsonify({"error": "Content-Type must be application/json"}), 415
        return handler(*args, **kwargs)
    return wrapped


# ──────────────────────────────────────────────────────────────────────
# CORS allowlist
# ──────────────────────────────────────────────────────────────────────

def install_cors(app):
    """Reply to preflight + stamp Access-Control-* on allowed origins.
    If CORS_ALLOWED_ORIGINS is empty the app is same-origin-only
    (no CORS headers emitted at all)."""
    if not CORS_ALLOWED_ORIGINS:
        return

    allowed = set(CORS_ALLOWED_ORIGINS)

    @app.after_request
    def _cors(resp):
        origin = request.headers.get("Origin", "").rstrip("/")
        if origin and origin in allowed:
            resp.headers["Access-Control-Allow-Origin"] = origin
            resp.headers["Vary"] = "Origin"
            resp.headers["Access-Control-Allow-Credentials"] = "true"
            resp.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
            resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PATCH, DELETE, OPTIONS"
        return resp

    @app.route("/<path:_>", methods=["OPTIONS"])
    def _cors_preflight(_):
        return ("", 204)


# ──────────────────────────────────────────────────────────────────────
# Suspicious activity log — single hook so the same line gets emitted
# everywhere. Later this can fan out to a real alerting target
# (Slack webhook, Sentry, etc.); for now print + a small in-memory
# ring buffer for /api/admin/security/recent.
# ──────────────────────────────────────────────────────────────────────
_recent_flags: deque = deque(maxlen=200)
_flags_lock = threading.Lock()


def flag_suspicious(kind: str, detail: str = ""):
    user = getattr(g, "user", None)
    entry = {
        "ts": time.time(),
        "kind": kind,
        "detail": detail,
        "user_id": (user or {}).get("id"),
        "email": (user or {}).get("email"),
        "ip": _client_ip(),
        "path": getattr(request, "path", None),
    }
    with _flags_lock:
        _recent_flags.append(entry)
    print(f"[security] {kind} user={entry['email'] or '-'} ip={entry['ip']} {detail}")


def recent_flags(limit: int = 50) -> list[dict]:
    with _flags_lock:
        return list(_recent_flags)[-limit:]


# ──────────────────────────────────────────────────────────────────────
# Setup hook called once from app.py
# ──────────────────────────────────────────────────────────────────────

def install(app):
    """Wire body-size cap + CORS into the Flask app."""
    app.config["MAX_CONTENT_LENGTH"] = MAX_REQUEST_BYTES
    install_cors(app)

    @app.errorhandler(413)
    def _too_big(e):
        flag_suspicious("oversized_body",
                        f"limit={MAX_REQUEST_BYTES}")
        return jsonify({"error": "Request body too large."}), 413
