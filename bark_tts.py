import os
import re
import numpy as np
import torch
import soundfile as sf

# Bark is available through transformers. We import lazily so the module
# loads even when Bark deps are missing — only synthesize() requires them.
_BarkModel = None
_AutoProcessor = None

MODEL_ID = os.getenv("BARK_MODEL", "suno/bark")
DEFAULT_VOICE = os.getenv("BARK_VOICE", "v2/hi_speaker_0")

# Bark generates roughly ~13s of audio per call. Keep chunks small enough
# that one sentence fits comfortably within the budget.
MAX_CHARS_PER_CHUNK = 200

VOICES = [
    {"id": "v2/hi_speaker_0", "label": "Hindi 0 (male)"},
    {"id": "v2/hi_speaker_1", "label": "Hindi 1 (female)"},
    {"id": "v2/hi_speaker_2", "label": "Hindi 2 (male)"},
    {"id": "v2/hi_speaker_3", "label": "Hindi 3 (female)"},
    {"id": "v2/hi_speaker_4", "label": "Hindi 4 (male)"},
    {"id": "v2/hi_speaker_5", "label": "Hindi 5 (female)"},
    {"id": "v2/en_speaker_6", "label": "English narrator (male)"},
    {"id": "v2/en_speaker_9", "label": "English narrator (female)"},
]

# Bark inline tags ([laughs], [sighs], [gasps], [music] etc.) literally
# trigger those non-speech sounds in the audio, which produces noisy
# output for narration. We don't inject emotion tags by default —
# emotional tone comes from the voice preset itself. Set BARK_USE_TAGS=1
# to opt in if you want the experimental tag-driven emotion.
EMOTION_TAGS_RAW = {
    "none": "",
    "happy": "[laughs]",
    "sad": "[sighs]",
    "excited": "[gasps]",
    "angry": "",
    "fearful": "[breathes shakily]",
    "whisper": "[whispers]",
    "serious": "",
}


def _emotion_tag(emotion: str) -> str:
    if os.getenv("BARK_USE_TAGS") != "1":
        return ""
    return EMOTION_TAGS_RAW.get(emotion.lower(), "")


_model = None
_processor = None
_device = None


class BarkError(Exception):
    pass


def is_configured() -> bool:
    return True


def list_voices() -> list[dict]:
    return VOICES


def load_model():
    global _model, _processor, _device, _BarkModel, _AutoProcessor
    if _model is not None:
        return
    if _BarkModel is None:
        from transformers import BarkModel as BM, AutoProcessor as AP
        _BarkModel = BM
        _AutoProcessor = AP

    _device = "cuda:0" if torch.cuda.is_available() else "cpu"
    dtype = torch.bfloat16 if _device.startswith("cuda") else torch.float32
    _model = _BarkModel.from_pretrained(MODEL_ID, torch_dtype=dtype).to(_device)
    _model.eval()
    _processor = _AutoProcessor.from_pretrained(MODEL_ID)
    print(f"[bark] loaded {MODEL_ID} on {_device} dtype={dtype}")


def _split_text(text: str) -> list[str]:
    text = text.strip()
    if not text:
        return []
    sentences = [s.strip() for s in re.split(r"(?<=[।.!?])\s*", text) if s.strip()]
    if not sentences:
        return [text]
    chunks: list[str] = []
    cur = ""
    for s in sentences:
        if len(s) > MAX_CHARS_PER_CHUNK:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(s)
            continue
        if not cur:
            cur = s
        elif len(cur) + 1 + len(s) <= MAX_CHARS_PER_CHUNK:
            cur = cur + " " + s
        else:
            chunks.append(cur)
            cur = s
    if cur:
        chunks.append(cur)
    return chunks


def _generate_chunk(text: str, voice_preset: str) -> tuple[np.ndarray, int]:
    inputs = _processor(text=text, voice_preset=voice_preset, return_tensors="pt").to(_device)
    with torch.inference_mode():
        audio = _model.generate(**inputs, do_sample=True)
    sr = _model.generation_config.sample_rate
    arr = audio.cpu().to(torch.float32).numpy().squeeze()
    return arr, sr


def synthesize(text: str, out_path: str, voice_config: dict | None = None) -> str:
    """Generate audio with Bark. Saves WAV to out_path. Returns path written."""
    try:
        load_model()
    except Exception as e:
        raise BarkError(f"Bark model load failed: {e}") from e

    voice_config = voice_config or {}
    voice_preset = voice_config.get("voice_id") or DEFAULT_VOICE
    emotion = (voice_config.get("emotion") or "none").lower()
    tag = _emotion_tag(emotion)

    chunks = _split_text(text)
    if not chunks:
        chunks = [text]

    audio_parts: list[np.ndarray] = []
    sr = None
    for i, c in enumerate(chunks):
        prompt = f"{tag} {c}".strip() if i == 0 and tag else c
        arr, sr = _generate_chunk(prompt, voice_preset)
        audio_parts.append(arr)

    if sr is None:
        raise BarkError("no audio produced")

    if len(audio_parts) == 1:
        final = audio_parts[0]
    else:
        silence = np.zeros(int(sr * 0.18), dtype=np.float32)
        joined = []
        for i, a in enumerate(audio_parts):
            if i:
                joined.append(silence)
            joined.append(a)
        final = np.concatenate(joined)

    sf.write(out_path, final, sr)
    return out_path
