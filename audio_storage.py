"""Per-user audio storage on Supabase Storage.

Files live at  audio/<user_id>/<filename>.wav  inside a private bucket.
The backend uploads via service-role (bypasses RLS), then hands the
browser a short-lived signed URL — no public path leak.

Retention policy enforced after every successful generation:
  - keep at most 5 audios per user
  - drop anything older than 24h, even if it's in the latest 5

If Supabase is unreachable we fall back to local-disk serving so a
single transient outage doesn't break the user's request.
"""
import os
import time
from pathlib import Path
from datetime import datetime, timedelta, timezone

import auth


BUCKET = os.getenv("AUDIO_BUCKET", "audio")
SIGNED_URL_TTL_SEC = int(os.getenv("AUDIO_SIGNED_URL_TTL", "3600"))   # 1 hour
RETENTION_HOURS    = int(os.getenv("AUDIO_RETENTION_HOURS", "24"))
MAX_FILES_PER_USER = int(os.getenv("AUDIO_MAX_PER_USER", "10"))


def _bucket():
    return auth.admin_client().storage.from_(BUCKET)


def _user_prefix(user_id: str) -> str:
    return f"{user_id}/"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(value) -> "datetime | None":
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def upload(user_id: str, local_path: str, dest_filename: str) -> str | None:
    """Upload a local audio file to per-user storage. Returns a signed
    URL the browser can play / download from, or None on failure (the
    caller should fall back to local serving)."""
    if not user_id:
        return None
    key = f"{_user_prefix(user_id)}{dest_filename}"
    try:
        with open(local_path, "rb") as fh:
            _bucket().upload(
                path=key,
                file=fh,
                file_options={"content-type": _guess_mime(dest_filename),
                              "upsert": "true"},
            )
        signed = _bucket().create_signed_url(key, SIGNED_URL_TTL_SEC)
        # supabase-py returns {'signedURL': '/storage/...'} or similar
        url = (signed.get("signedURL")
               or signed.get("signed_url")
               or signed.get("signedUrl"))
        if not url:
            print(f"[audio] no signedURL in response: {signed}")
            return None
        # Older SDKs return a path; prepend SUPABASE_URL when needed.
        if url.startswith("/"):
            url = auth.SUPABASE_URL.rstrip("/") + url
        return url
    except Exception as e:
        print(f"[audio] upload({user_id}/{dest_filename}) failed: {e}")
        return None


def _guess_mime(filename: str) -> str:
    name = filename.lower()
    if name.endswith(".mp3"): return "audio/mpeg"
    if name.endswith(".wav"): return "audio/wav"
    if name.endswith(".ogg"): return "audio/ogg"
    return "application/octet-stream"


def prune_user_audio(user_id: str):
    """Enforce 'newest 5 + younger than RETENTION_HOURS' per user.
    Safe to call after every successful upload — costs one list + one
    bulk delete at most."""
    if not user_id:
        return
    try:
        files = _bucket().list(_user_prefix(user_id).rstrip("/")) or []
    except Exception as e:
        print(f"[audio] list({user_id}) failed: {e}")
        return

    cutoff = _now() - timedelta(hours=RETENTION_HOURS)
    # Newest first. Supabase returns created_at as ISO strings.
    def created(f):
        return (_parse_ts(f.get("created_at"))
                or _parse_ts(f.get("updated_at"))
                or datetime.min.replace(tzinfo=timezone.utc))
    files.sort(key=created, reverse=True)

    to_delete = []
    for i, f in enumerate(files):
        ts = created(f)
        if i >= MAX_FILES_PER_USER or ts < cutoff:
            to_delete.append(f"{_user_prefix(user_id)}{f['name']}")

    if not to_delete:
        return
    try:
        _bucket().remove(to_delete)
        print(f"[audio] pruned {len(to_delete)} file(s) for {user_id}")
    except Exception as e:
        print(f"[audio] remove({user_id}) failed: {e}")


def list_user_audio(user_id: str, limit: int = MAX_FILES_PER_USER) -> list[dict]:
    """Return the user's recent audio files (newest first) as a list of
    {filename, created_at, size, signed_url} dicts. signed_url is fresh
    each call so it's safe to display immediately."""
    if not user_id:
        return []
    try:
        files = _bucket().list(_user_prefix(user_id).rstrip("/")) or []
    except Exception as e:
        print(f"[audio] list_user_audio({user_id}) failed: {e}")
        return []

    def created(f):
        return (_parse_ts(f.get("created_at"))
                or _parse_ts(f.get("updated_at"))
                or datetime.min.replace(tzinfo=timezone.utc))
    files.sort(key=created, reverse=True)

    out = []
    for f in files[:limit]:
        name = f.get("name") or ""
        if not name:
            continue
        url = signed_url_for(user_id, name)
        out.append({
            "filename":  name,
            "created_at": f.get("created_at") or f.get("updated_at"),
            "size":      (f.get("metadata") or {}).get("size"),
            "signed_url": url,
        })
    return out


def signed_url_for(user_id: str, filename: str) -> str | None:
    """Re-mint a signed URL for an existing file. Used when /generate
    is replayed or the user reloads a stale page."""
    if not user_id or not filename:
        return None
    key = f"{_user_prefix(user_id)}{filename}"
    try:
        signed = _bucket().create_signed_url(key, SIGNED_URL_TTL_SEC)
        url = (signed.get("signedURL")
               or signed.get("signed_url")
               or signed.get("signedUrl"))
        if url and url.startswith("/"):
            url = auth.SUPABASE_URL.rstrip("/") + url
        return url
    except Exception as e:
        print(f"[audio] signed_url_for({user_id}/{filename}) failed: {e}")
        return None
