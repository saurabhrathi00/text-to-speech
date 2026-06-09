import os
import re
import requests

from config import (
    ELEVEN_API_BASE as API_BASE,
    ELEVEN_CURATED_VOICES as CURATED_VOICES,
    ELEVEN_DEFAULT_VOICE_SETTINGS,
    ELEVEN_DEFAULT_SIMILARITY_BOOST,
)

_MAX_CHARS = 4800

DEFAULT_MODEL = os.getenv("ELEVENLABS_MODEL", "eleven_multilingual_v2")
DEFAULT_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "pNInz6obpgDQGBDnXBQb")


def list_voices() -> list[dict]:
    """Return available voices. Tries to fetch from the ElevenLabs API
    (so user's cloned/saved Hindi voices show up); falls back to the
    curated list if the API isn't reachable or no key is set.
    """
    api_key = os.getenv("ELEVENLABS_API_KEY", "")
    if not api_key:
        return CURATED_VOICES

    try:
        r = requests.get(
            f"{API_BASE}/voices",
            headers={"xi-api-key": api_key},
            timeout=10,
        )
        if r.status_code != 200:
            return CURATED_VOICES
        data = r.json()
        voices = []
        for v in data.get("voices", []):
            label = v.get("name", "Unknown")
            labels = v.get("labels") or {}
            extras = []
            if labels.get("gender"):
                extras.append(labels["gender"])
            if labels.get("accent"):
                extras.append(labels["accent"])
            if labels.get("description"):
                extras.append(labels["description"])
            if extras:
                label = f"{label} ({', '.join(extras)})"
            voices.append({"id": v["voice_id"], "label": label})
        return voices or CURATED_VOICES
    except Exception:
        return CURATED_VOICES


class ElevenLabsError(Exception):
    pass


def is_configured() -> bool:
    return bool(os.getenv("ELEVENLABS_API_KEY"))


def _chunk_text(text: str, limit: int = _MAX_CHARS) -> list[str]:
    if len(text) <= limit:
        return [text]
    sentences = re.split(r"(?<=[।.!?])\s*", text)
    chunks: list[str] = []
    buf = ""
    for s in sentences:
        s = s.strip()
        if not s:
            continue
        if buf and len(buf) + 1 + len(s) > limit:
            chunks.append(buf)
            buf = s
        else:
            buf = f"{buf} {s}" if buf else s
    if buf:
        chunks.append(buf)
    return chunks or [text]


def synthesize(text: str, out_path: str, voice_config: dict | None = None) -> str:
    """Generate audio via ElevenLabs and save as MP3.
    Returns the actual file path written (extension may be .mp3).
    """
    api_key = os.getenv("ELEVENLABS_API_KEY", "")
    if not api_key:
        raise ElevenLabsError("ELEVENLABS_API_KEY not set in .env")

    voice_config = voice_config or {}
    voice_id = voice_config.get("voice_id") or DEFAULT_VOICE_ID
    model_id = voice_config.get("model_id") or DEFAULT_MODEL

    settings = dict(ELEVEN_DEFAULT_VOICE_SETTINGS)

    url = f"{API_BASE}/text-to-speech/{voice_id}"
    headers = {
        "xi-api-key": api_key,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    }
    voice_settings = {
        "stability": voice_config.get("stability", settings["stability"]),
        "similarity_boost": voice_config.get("similarity_boost", ELEVEN_DEFAULT_SIMILARITY_BOOST),
        "style": voice_config.get("style", settings["style"]),
        "use_speaker_boost": True,
    }

    mp3_path = out_path
    if out_path.endswith(".wav"):
        mp3_path = out_path[:-4] + ".mp3"

    chunks = _chunk_text(text)

    if len(chunks) == 1:
        audio = _eleven_request(url, headers, chunks[0], model_id, voice_settings)
        with open(mp3_path, "wb") as f:
            f.write(audio)
        return mp3_path

    print(f"[elevenlabs] text too long ({len(text)} chars), splitting into {len(chunks)} chunks")
    with open(mp3_path, "wb") as f:
        for i, chunk in enumerate(chunks):
            print(f"[elevenlabs] chunk {i + 1}/{len(chunks)} ({len(chunk)} chars)")
            audio = _eleven_request(url, headers, chunk, model_id, voice_settings)
            f.write(audio)

    return mp3_path


def _eleven_request(url: str, headers: dict, text: str,
                    model_id: str, voice_settings: dict) -> bytes:
    payload = {
        "text": text,
        "model_id": model_id,
        "voice_settings": voice_settings,
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=180)
    except requests.RequestException as e:
        raise ElevenLabsError(f"network error: {e}") from e
    if r.status_code != 200:
        raise ElevenLabsError(f"API error {r.status_code}: {r.text[:300]}")
    return r.content
