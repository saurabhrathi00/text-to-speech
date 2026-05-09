import os
import requests

API_BASE = "https://api.elevenlabs.io/v1"
DEFAULT_MODEL = os.getenv("ELEVENLABS_MODEL", "eleven_multilingual_v2")
DEFAULT_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "pNInz6obpgDQGBDnXBQb")  # Adam — English-leaning

# Curated multilingual voices that handle Hindi reasonably well using
# eleven_multilingual_v2. User can override via voice library.
CURATED_VOICES = [
    {"id": "pNInz6obpgDQGBDnXBQb", "label": "Adam (male, deep)"},
    {"id": "ErXwobaYiN019PkySvjV", "label": "Antoni (male, warm)"},
    {"id": "VR6AewLTigWG4xSOukaG", "label": "Arnold (male, narrator)"},
    {"id": "21m00Tcm4TlvDq8ikWAM", "label": "Rachel (female, calm)"},
    {"id": "EXAVITQu4vr4xnSDxMaL", "label": "Sarah (female, soft)"},
    {"id": "AZnzlk1XvdvUeBnXmlld", "label": "Domi (female, strong)"},
    {"id": "MF3mGyEYCl7XYWbV9V6O", "label": "Elli (female, young)"},
    {"id": "TxGEqnHWrfWFTfGW9XjX", "label": "Josh (male, deep)"},
]


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

    # Map our generic emotion → ElevenLabs style slider (0..1).
    # Higher = more expressive/emotional. Lower = neutral.
    emotion = (voice_config.get("emotion") or "none").lower()
    style = {
        "none": 0.0,
        "happy": 0.6,
        "sad": 0.5,
        "excited": 0.8,
        "angry": 0.7,
        "fearful": 0.5,
        "whisper": 0.3,
        "serious": 0.2,
    }.get(emotion, 0.0)

    url = f"{API_BASE}/text-to-speech/{voice_id}"
    headers = {
        "xi-api-key": api_key,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    }
    payload = {
        "text": text,
        "model_id": model_id,
        "voice_settings": {
            "stability": voice_config.get("stability", 0.5),
            "similarity_boost": voice_config.get("similarity_boost", 0.75),
            "style": style,
            "use_speaker_boost": True,
        },
    }

    try:
        r = requests.post(url, headers=headers, json=payload, timeout=180)
    except requests.RequestException as e:
        raise ElevenLabsError(f"network error: {e}") from e

    if r.status_code != 200:
        raise ElevenLabsError(f"API error {r.status_code}: {r.text[:300]}")

    # Save as MP3 — change extension if caller passed .wav
    mp3_path = out_path
    if out_path.endswith(".wav"):
        mp3_path = out_path[:-4] + ".mp3"

    with open(mp3_path, "wb") as f:
        f.write(r.content)

    return mp3_path
