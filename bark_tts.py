import os
import re
import time
import traceback
import numpy as np
import torch
import soundfile as sf

# Bark is available through transformers. We import lazily so the module
# loads even when Bark deps are missing — only synthesize() requires them.
_BarkModel = None
_AutoProcessor = None

from config import (
    BARK_MAX_CHARS_PER_CHUNK as MAX_CHARS_PER_CHUNK,
    BARK_VOICES as VOICES,
    BARK_EMOTION_TAGS as EMOTION_TAGS_RAW,
    INTER_CHUNK_SILENCE_SEC,
)

MODEL_ID = os.getenv("BARK_MODEL", "suno/bark")
DEFAULT_VOICE = os.getenv("BARK_VOICE", "v2/hi_speaker_0")


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
    t0 = time.time()
    inputs = _processor(text=text, voice_preset=voice_preset, return_tensors="pt").to(_device)

    if "attention_mask" not in inputs:
        inputs["attention_mask"] = torch.ones_like(inputs["input_ids"])

    pad_id = (
        getattr(_model.generation_config, "pad_token_id", None)
        or getattr(_model.generation_config, "eos_token_id", None)
        or 0
    )

    with torch.inference_mode():
        audio = _model.generate(**inputs, do_sample=True, pad_token_id=pad_id)
    sr = _model.generation_config.sample_rate
    arr = audio.cpu().to(torch.float32).numpy().squeeze()
    dur = arr.size / sr if arr.size else 0
    print(f"[bark] chunk {len(text):>4} chars → {dur:.1f}s audio in {time.time() - t0:.1f}s")
    return arr, sr


def synthesize(text: str, out_path: str, voice_config: dict | None = None) -> str:
    """Generate audio with Bark. Saves WAV to out_path. Returns path written."""
    t_start = time.time()
    print(f"[bark] synthesize entered → {len(text)} chars")
    print("[bark] load_model...")
    t_load = time.time()
    try:
        load_model()
    except Exception as e:
        raise BarkError(f"Bark model load failed: {e}") from e
    print(f"[bark] load_model done in {time.time() - t_load:.1f}s on {_device}")

    voice_config = voice_config or {}
    voice_preset = voice_config.get("voice_id") or DEFAULT_VOICE
    emotion = (voice_config.get("emotion") or "none").lower()
    tag = _emotion_tag(emotion)

    chunks = _split_text(text)
    if not chunks:
        chunks = [text]

    print(f"[bark] split into {len(chunks)} chunk(s):")
    for i, c in enumerate(chunks, 1):
        print(f"  [{i}/{len(chunks)}] ({len(c)} chars) {c}")

    audio_parts: list[np.ndarray] = []
    sr = None
    for i, c in enumerate(chunks):
        prompt = f"{tag} {c}".strip() if i == 0 and tag else c
        try:
            arr, sr = _generate_chunk(prompt, voice_preset)
        except Exception as e:
            print(f"[bark] chunk {i + 1}/{len(chunks)} FAILED: {e}")
            traceback.print_exc()
            raise BarkError(f"chunk {i + 1} generation failed: {e}") from e
        audio_parts.append(arr)

    if sr is None:
        raise BarkError("no audio produced")

    if len(audio_parts) == 1:
        final = audio_parts[0]
    else:
        silence = np.zeros(int(sr * INTER_CHUNK_SILENCE_SEC), dtype=np.float32)
        joined = []
        for i, a in enumerate(audio_parts):
            if i:
                joined.append(silence)
            joined.append(a)
        final = np.concatenate(joined)

    sf.write(out_path, final, sr)
    print(f"[bark] done synthesize in {time.time() - t_start:.1f}s → {out_path}")
    return out_path
