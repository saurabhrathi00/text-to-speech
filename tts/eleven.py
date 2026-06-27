import os
import re
import requests

from config import (
    ELEVEN_API_BASE as API_BASE,
    ELEVEN_CURATED_VOICES as CURATED_VOICES,
    ELEVEN_DEFAULT_VOICE_SETTINGS,
    ELEVEN_DEFAULT_SIMILARITY_BOOST,
)

# v3 alpha caps a single request lower than v2. Chunk to the model's
# ceiling so long scripts still go through (concatenated afterwards).
_MAX_CHARS_V2 = 4800
_MAX_CHARS_V3 = 2800

DEFAULT_MODEL = os.getenv("ELEVENLABS_MODEL", "eleven_v3")
DEFAULT_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "pNInz6obpgDQGBDnXBQb")

# v3 only accepts three discrete stability values (0.0 Creative,
# 0.5 Natural, 1.0 Robust) and has no `style` knob — the inline
# performance tags ([excited], [whispers], [sobbing], …) the emotion
# pipeline injects carry the expression instead. v2 takes continuous
# stability + style. We build the right payload shape per model.
_V3_STABILITY_STEPS = (0.0, 0.5, 1.0)


def _is_v3(model_id: str) -> bool:
    return "v3" in (model_id or "").lower()


def _max_chars_for(model_id: str) -> int:
    return _MAX_CHARS_V3 if _is_v3(model_id) else _MAX_CHARS_V2


def _snap_v3_stability(value: float) -> float:
    """Round an arbitrary 0–1 stability to the nearest v3-legal step."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        value = 0.5
    return min(_V3_STABILITY_STEPS, key=lambda s: abs(s - value))


def _build_voice_settings(model_id: str, voice_config: dict) -> dict:
    defaults = dict(ELEVEN_DEFAULT_VOICE_SETTINGS)
    stability = voice_config.get("stability", defaults["stability"])
    similarity = voice_config.get("similarity_boost", ELEVEN_DEFAULT_SIMILARITY_BOOST)
    if _is_v3(model_id):
        return {
            "stability": _snap_v3_stability(stability),
            "similarity_boost": similarity,
            "use_speaker_boost": True,
        }
    return {
        "stability": stability,
        "similarity_boost": similarity,
        "style": voice_config.get("style", defaults["style"]),
        "use_speaker_boost": True,
    }


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


def _chunk_text(text: str, limit: int = _MAX_CHARS_V2) -> list[str]:
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

    url = f"{API_BASE}/text-to-speech/{voice_id}"
    headers = {
        "xi-api-key": api_key,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    }
    voice_settings = _build_voice_settings(model_id, voice_config)

    mp3_path = out_path
    if out_path.endswith(".wav"):
        mp3_path = out_path[:-4] + ".mp3"

    chunks = _chunk_text(text, _max_chars_for(model_id))

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
            if i > 0:
                audio = _strip_mp3_headers(audio)
            f.write(audio)

    return mp3_path


def _strip_mp3_headers(data: bytes) -> bytes:
    """Strip ID3v2 header (front) and ID3v1 tag (last 128 bytes) so
    concatenated chunks form a single valid MP3 stream."""
    offset = 0
    # ID3v2: starts with "ID3", size at bytes 6-9 (syncsafe int)
    if data[:3] == b"ID3" and len(data) > 10:
        size_bytes = data[6:10]
        size = (size_bytes[0] << 21 | size_bytes[1] << 14 |
                size_bytes[2] << 7 | size_bytes[3])
        offset = 10 + size
    # Skip forward to first MPEG sync word (0xFF 0xE* or 0xFF 0xF*)
    while offset < len(data) - 1:
        if data[offset] == 0xFF and (data[offset + 1] & 0xE0) == 0xE0:
            break
        offset += 1
    # Strip trailing ID3v1 tag (128 bytes starting with "TAG")
    end = len(data)
    if end >= 128 and data[end - 128:end - 125] == b"TAG":
        end -= 128
    return data[offset:end]


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
