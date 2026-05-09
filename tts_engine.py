import os
import re
import tempfile
import numpy as np
import torch
import soundfile as sf
from parler_tts import ParlerTTSForConditionalGeneration
from transformers import AutoTokenizer

MODEL_ID = "ai4bharat/indic-parler-tts"
# Soft cap on combined chunk size. Multiple short sentences may be merged
# up to this limit. A single sentence longer than this becomes its own
# chunk and is NEVER split mid-sentence.
MAX_CHARS_PER_CHUNK = 250
# Per-chunk token budget for max_new_tokens. Sized generously so a single
# long sentence has room to complete without hitting the model's default
# ~2580 token cap (which causes garbled output).
TOKENS_PER_CHAR = 8
MIN_NEW_TOKENS = 256
MAX_NEW_TOKENS_CAP = 5000

SPEAKERS = {
    "rohit": "deep, mature male",
    "aman": "energetic young male",
    "divya": "warm, clear female",
    "rani": "formal news-anchor female",
}

SPEED_PHRASES = {
    "slow": "at a slow, deliberate pace",
    "moderate": "at a moderate, natural pace",
    "fast": "at a fast, brisk pace",
}

PITCH_PHRASES = {
    "low": "with a low, deep pitch",
    "normal": "with a natural pitch",
    "high": "with a slightly higher pitch",
}

EXPRESSIVITY_PHRASES = {
    "expressive": "in a very expressive, engaging tone like a professional documentary narrator",
    "neutral": "in a neutral, balanced tone",
    "calm": "in a calm, steady, soothing tone",
}


def build_description(speaker: str = "rohit", speed: str = "moderate",
                       pitch: str = "low", expressivity: str = "expressive") -> str:
    name = speaker.capitalize() if speaker.lower() in SPEAKERS else "Rohit"
    desc_voice = SPEAKERS.get(speaker.lower(), SPEAKERS["rohit"])
    speed_p = SPEED_PHRASES.get(speed, SPEED_PHRASES["moderate"])
    pitch_p = PITCH_PHRASES.get(pitch, PITCH_PHRASES["low"])
    expr_p = EXPRESSIVITY_PHRASES.get(expressivity, EXPRESSIVITY_PHRASES["expressive"])
    return (
        f"{name} speaks in a {desc_voice} voice with a clear natural Indian Hindi accent. "
        f"He delivers the lines {speed_p}, {pitch_p}, {expr_p}. "
        f"The pronunciation is very clear. The recording is very high quality, "
        f"studio-grade, close-sounding, with no background noise at all."
    )


VOICE_DESCRIPTION = build_description()

_model = None
_tokenizer = None
_desc_tokenizer = None
_device = None


def _pick_device() -> str:
    if torch.cuda.is_available():
        return "cuda:0"
    return "cpu"


def load_model():
    global _model, _tokenizer, _desc_tokenizer, _device
    if _model is not None:
        return
    _device = _pick_device()
    dtype = torch.bfloat16 if _device.startswith("cuda") else torch.float32
    _model = ParlerTTSForConditionalGeneration.from_pretrained(
        MODEL_ID, torch_dtype=dtype
    ).to(_device)
    _model.eval()
    _tokenizer = AutoTokenizer.from_pretrained(MODEL_ID)
    desc_name = getattr(_model.config, "text_encoder", None)
    desc_id = getattr(desc_name, "_name_or_path", MODEL_ID) if desc_name else MODEL_ID
    _desc_tokenizer = AutoTokenizer.from_pretrained(desc_id)
    print(f"[tts] loaded on {_device} dtype={dtype}")


def _split_text(text: str) -> list[str]:
    """Break text into chunks along sentence boundaries.

    Rules:
    - Each chunk is one or more complete sentences.
    - A sentence is NEVER split across chunks, even if it exceeds
      MAX_CHARS_PER_CHUNK on its own (it becomes its own oversized chunk).
    - Multiple short sentences are merged into the same chunk only while
      the running total stays within MAX_CHARS_PER_CHUNK.
    """
    text = text.strip()
    if not text:
        return []

    sentences = [s for s in re.split(r"(?<=[।.!?])\s+", text) if s]
    if not sentences:
        return [text]

    chunks: list[str] = []
    cur = ""
    for s in sentences:
        if not cur:
            cur = s
            continue
        if len(cur) + 1 + len(s) <= MAX_CHARS_PER_CHUNK:
            cur = cur + " " + s
        else:
            chunks.append(cur)
            cur = s
    if cur:
        chunks.append(cur)
    return chunks


def _generate_chunk(prompt: str, description: str | None = None) -> np.ndarray:
    desc = description or VOICE_DESCRIPTION
    desc_inputs = _desc_tokenizer(desc, return_tensors="pt").to(_device)
    prompt_inputs = _tokenizer(prompt, return_tensors="pt").to(_device)

    budget = max(MIN_NEW_TOKENS, min(MAX_NEW_TOKENS_CAP, len(prompt) * TOKENS_PER_CHAR))

    with torch.inference_mode():
        audio = _model.generate(
            input_ids=desc_inputs.input_ids,
            attention_mask=desc_inputs.attention_mask,
            prompt_input_ids=prompt_inputs.input_ids,
            prompt_attention_mask=prompt_inputs.attention_mask,
            do_sample=True,
            temperature=1.0,
            max_new_tokens=budget,
        )
    return audio.cpu().to(torch.float32).numpy().squeeze()


def _trim_chunk_audio(audio: np.ndarray, sr: int, chunk_text: str) -> np.ndarray:
    """Run whisper on a single chunk's audio and trim trailing silence /
    repetition. Returns trimmed audio. Falls back to raw audio on failure.
    """
    from aligner import align as _align, trim_audio_to_words

    fd, tmp_path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    try:
        sf.write(tmp_path, audio, sr)
        words = _align(tmp_path)
        trim_audio_to_words(tmp_path, words, expected_word_count=len(chunk_text.split()))
        trimmed, _ = sf.read(tmp_path)
        return trimmed.astype(np.float32)
    except Exception as e:
        print(f"[tts] chunk trim failed: {e} — using raw audio")
        return audio
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def synthesize(text: str, out_path: str, description: str | None = None) -> str:
    global _device
    load_model()

    chunks = _split_text(text)
    audio_parts = []
    sr = _model.config.sampling_rate
    try:
        for c in chunks:
            raw = _generate_chunk(c, description)
            audio_parts.append(_trim_chunk_audio(raw, sr, c))
    except torch.cuda.OutOfMemoryError:
        torch.cuda.empty_cache()
        _move_to_cpu()
        audio_parts = []
        for c in chunks:
            raw = _generate_chunk(c, description)
            audio_parts.append(_trim_chunk_audio(raw, sr, c))

    if len(audio_parts) == 1:
        final = audio_parts[0]
    else:
        silence = np.zeros(int(sr * 0.25), dtype=np.float32)
        joined = []
        for i, a in enumerate(audio_parts):
            if i:
                joined.append(silence)
            joined.append(a)
        final = np.concatenate(joined)

    sf.write(out_path, final, sr)
    return out_path


def _move_to_cpu():
    global _model, _device
    _device = "cpu"
    _model.to(_device)
