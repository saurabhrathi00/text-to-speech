"""Datadog log shipping — off unless DD_API_KEY is set.

Ships structured logs to Datadog's HTTP log intake from a background thread
(requests + stdlib only, no agent, no heavy deps). Wires per-request access
logs + unhandled-exception logs into Flask, and exposes log_event() for
business events (payments, signups, generations).

Env:
  DD_API_KEY   - enables shipping (from Datadog → Organization Settings → API Keys)
  DD_SITE      - datadoghq.com (US1, default) | datadoghq.eu | us5.datadoghq.com | ...
  DD_SERVICE   - service tag (default 'sastaspeech')
  APP_ENV      - env tag (falls back to RENDER_SERVICE_NAME / 'render')
"""
import os
import queue
import threading
import time

import requests

DD_API_KEY = os.getenv("DD_API_KEY", "")
DD_SITE = os.getenv("DD_SITE", "datadoghq.com")
DD_SERVICE = os.getenv("DD_SERVICE", "sastaspeech")
DD_ENV = os.getenv("APP_ENV") or os.getenv("RENDER_SERVICE_NAME") or "render"

_INTAKE_URL = f"https://http-intake.logs.{DD_SITE}/api/v2/logs"
_q: "queue.Queue" = queue.Queue(maxsize=2000)
_started = False


def is_configured() -> bool:
    return bool(DD_API_KEY)


def _worker():
    """Drain the queue and POST batches to Datadog. Best-effort — a failed
    flush drops that batch rather than blocking the app."""
    while True:
        batch = []
        try:
            batch.append(_q.get(timeout=3))
            while len(batch) < 50:
                batch.append(_q.get_nowait())
        except queue.Empty:
            pass
        if not batch:
            continue
        try:
            requests.post(
                _INTAKE_URL,
                headers={"DD-API-KEY": DD_API_KEY, "Content-Type": "application/json"},
                json=batch,
                timeout=10,
            )
        except Exception:
            pass  # never let logging break the request path


def log_event(message: str, level: str = "info", **fields):
    """Queue a structured log line for Datadog. No-op if not configured."""
    if not is_configured():
        return
    entry = {
        "ddsource": "python",
        "service": DD_SERVICE,
        "ddtags": f"env:{DD_ENV}",
        "hostname": DD_ENV,
        "status": level,
        "message": message,
    }
    if fields:
        entry.update(fields)
    try:
        _q.put_nowait(entry)
    except queue.Full:
        pass  # shed load rather than block


def install(app):
    """Attach per-request + error logging to the Flask app and start the
    background shipper. Safe to call when unconfigured (logs a notice)."""
    global _started
    if not is_configured():
        print("[observability] DD_API_KEY not set — Datadog logs disabled")
        return
    if not _started:
        threading.Thread(target=_worker, daemon=True).start()
        _started = True

    from flask import g, request

    @app.before_request
    def _dd_start_timer():
        g._dd_t0 = time.time()

    @app.after_request
    def _dd_access_log(resp):
        try:
            dur_ms = round((time.time() - getattr(g, "_dd_t0", time.time())) * 1000)
            path = request.path
            # Don't log health/keep-warm pings — they'd drown real traffic.
            if path not in ("/health", "/api/status"):
                log_event(
                    f"{request.method} {path} {resp.status_code} {dur_ms}ms",
                    level=("error" if resp.status_code >= 500 else
                           "warning" if resp.status_code >= 400 else "info"),
                    http={"method": request.method, "path": path,
                          "status_code": resp.status_code, "duration_ms": dur_ms},
                )
        except Exception:
            pass
        return resp

    from werkzeug.exceptions import HTTPException

    @app.errorhandler(Exception)
    def _dd_exception(e):
        # Normal HTTP errors (404, 405, 400, redirects, aborts) are NOT bugs —
        # return them untouched so Flask renders the right status. Re-raising
        # them here previously turned every 404 into a 500. Their status codes
        # are already captured by the per-request access log above.
        if isinstance(e, HTTPException):
            return e
        # Genuine unhandled exception — log with stack, return a clean 500
        # (returning rather than re-raising avoids an error-handling loop).
        import traceback
        log_event(f"Unhandled exception: {e}", level="error",
                  error={"stack": traceback.format_exc()[:4000]})
        return ("Internal Server Error", 500)

    print(f"[observability] Datadog logs enabled → {DD_SITE} service={DD_SERVICE} env={DD_ENV}")
