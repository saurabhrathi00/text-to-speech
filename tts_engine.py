import re
import numpy as np
import torch
import soundfile as sf
from parler_tts import ParlerTTSForConditionalGeneration
from transformers import AutoTokenizer

MODEL_ID = "ai4bharat/indic-parler-tts"
MAX_CHARS_PER_CHUNK = 400
# Parler-TTS audio frame rate ~ 86 tokens / sec.
TOKENS_PER_CHAR = 10
MIN_NEW_TOKENS = 256
MAX_NEW_TOKENS_CAP = 4096

# Trailing silence trim params
TRIM_FRAME_MS = 30
TRIM_SILENCE_THRESHOLD = 0.008  # RMS below this is "silence"
TRIM_TAIL_PAD_MS = 200          # keep this much padding after last speech

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
    text = text.strip()
    if len(text) <= MAX_CHARS_PER_CHUNK:
        return [text]
    sentences = re.split(r"(?<=[।.!?])\s+", text)
    chunks, cur = [], ""
    for s in sentences:
        if not s:
            continue
        if len(cur) + len(s) + 1 <= MAX_CHARS_PER_CHUNK:
            cur = (cur + " " + s).strip()
        else:
            if cur:
                chunks.append(cur)
            if len(s) > MAX_CHARS_PER_CHUNK:
                for i in range(0, len(s), MAX_CHARS_PER_CHUNK):
                    chunks.append(s[i : i + MAX_CHARS_PER_CHUNK])
                cur = ""
            else:
                cur = s
    if cur:
        chunks.append(cur)
    return chunks


def _trim_trailing(audio: np.ndarray, sr: int) -> np.ndarray:
    """Remove only trailing silence from the very end of generated audio.

    Walks frames from the end backwards, finds the last frame whose RMS
    exceeds the silence threshold, and cuts after that + a small pad.
    Does not touch any interior silence (sentence/comma pauses).
    """
    if audio.size == 0:
        return audio

    frame = max(1, int(sr * TRIM_FRAME_MS / 1000))
    pad_samples = int(sr * TRIM_TAIL_PAD_MS / 1000)

    n_frames = audio.size // frame
    if n_frames == 0:
        return audio
    trimmed_len = n_frames * frame
    rms = np.sqrt(np.mean(
        audio[:trimmed_len].reshape(n_frames, frame).astype(np.float32) ** 2,
        axis=1,
    ))
    speech_idx = np.where(rms > TRIM_SILENCE_THRESHOLD)[0]
    if speech_idx.size == 0:
        return audio
    last_speech = int(speech_idx[-1])
    end_sample = min(audio.size, (last_speech + 1) * frame + pad_samples)
    return audio[:end_sample]


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
            repetition_penalty=2.0,
        )
    arr = audio.cpu().to(torch.float32).numpy().squeeze()
    sr = _model.config.sampling_rate
    return _trim_trailing(arr, sr)


def synthesize(text: str, out_path: str, description: str | None = None) -> str:
    global _device
    load_model()

    chunks = _split_text(text)
    audio_parts = []
    try:
        for c in chunks:
            audio_parts.append(_generate_chunk(c, description))
    except torch.cuda.OutOfMemoryError:
        torch.cuda.empty_cache()
        _move_to_cpu()
        audio_parts = [_generate_chunk(c, description) for c in chunks]

    sr = _model.config.sampling_rate
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
