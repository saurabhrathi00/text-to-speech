import os
import re
import queue
import tempfile
import threading
import numpy as np
import torch
import soundfile as sf
from parler_tts import ParlerTTSForConditionalGeneration
from transformers import AutoTokenizer

MODEL_ID = "ai4bharat/indic-parler-tts"
# Strategy: one sentence per chunk. No merging, no splitting mid-sentence.
# Each chunk goes through Parler-TTS independently and gets its own
# whisper-based trim, then chunks are joined with a small pause.
# Per-chunk token budget for max_new_tokens. Sized generously so a single
# long sentence has room to complete without hitting Parler's default
# ~2580 token cap (which causes garbled output).
TOKENS_PER_CHAR = 8
MIN_NEW_TOKENS = 256
MAX_NEW_TOKENS_CAP = 5000
# Pause inserted between consecutive sentence-chunks when joining audio.
INTER_CHUNK_SILENCE_SEC = 0.18

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

EMOTION_PHRASES = {
    "none": "",
    "happy": "with a cheerful, upbeat, happy mood",
    "sad": "with a sad, melancholic, sorrowful mood",
    "angry": "with an angry, forceful, intense mood",
    "excited": "with enthusiastic, energetic, excited emotion",
    "fearful": "with a fearful, tense, hesitant mood",
    "whisper": "in a quiet, intimate, whispering voice",
    "serious": "in a serious, formal, authoritative mood",
}


def build_description(speaker: str = "rohit", speed: str = "moderate",
                       pitch: str = "low", expressivity: str = "expressive",
                       emotion: str = "none") -> str:
    name = speaker.capitalize() if speaker.lower() in SPEAKERS else "Rohit"
    desc_voice = SPEAKERS.get(speaker.lower(), SPEAKERS["rohit"])
    speed_p = SPEED_PHRASES.get(speed, SPEED_PHRASES["moderate"])
    pitch_p = PITCH_PHRASES.get(pitch, PITCH_PHRASES["low"])
    expr_p = EXPRESSIVITY_PHRASES.get(expressivity, EXPRESSIVITY_PHRASES["expressive"])
    emo_p = EMOTION_PHRASES.get(emotion.lower(), "")
    parts = [
        f"{name} speaks in a {desc_voice} voice with a clear natural Indian Hindi accent.",
        f"He delivers the lines {speed_p}, {pitch_p}, {expr_p}.",
    ]
    if emo_p:
        parts.append(f"The lines are spoken {emo_p}.")
    parts.append(
        "The pronunciation is very clear. The recording is very high quality, "
        "studio-grade, close-sounding, with no background noise at all."
    )
    return " ".join(parts)


VOICE_DESCRIPTION = build_description()

_model = None
_tokenizer = None
_desc_tokenizer = None
_device = None


def _pick_device() -> str:
    if torch.cuda.is_available():
        return "cuda:0"
    return "cpu"


def _triton_available() -> bool:
    try:
        import triton  # noqa: F401
        return True
    except Exception:
        return False


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

    # torch.compile speeds up generation ~1.2x on CUDA but requires Triton,
    # which has no official Windows builds. Off by default; opt-in via
    # TTS_COMPILE=1 (only enable on Linux with triton installed).
    if (_device.startswith("cuda")
            and os.getenv("TTS_COMPILE") == "1"
            and _triton_available()):
        try:
            _model.forward = torch.compile(_model.forward, mode="reduce-overhead", fullgraph=False)
            print("[tts] torch.compile applied (first request will be slower)")
        except Exception as e:
            print(f"[tts] torch.compile skipped: {e}")


def _split_text(text: str) -> list[str]:
    """One sentence per chunk.

    Sentence boundaries:
    - Hindi danda (।) — always splits, even if no space follows.
      ("हुआ।सीख" → ["हुआ।", "सीख"])
    - . ! ? — splits when followed by whitespace, a quote, end of text,
      or a Devanagari character. Avoids splitting on dots in numbers
      like "5.5" or English abbreviations like "Mr.S" since those are
      followed by digits or Latin letters.

    No characters are dropped: re.split preserves all text. The only
    possible failure mode is a missed split (sentences staying merged),
    never lost words.
    """
    text = text.strip()
    if not text:
        return []
    pattern = r'(?<=।)|(?<=[.!?])(?=[\s"\'ऀ-ॿ]|$)'
    parts = re.split(pattern, text)
    sentences = [p.strip() for p in parts if p and p.strip()]
    return sentences or [text]


def _tokenize_description(description: str | None):
    desc = description or VOICE_DESCRIPTION
    return _desc_tokenizer(desc, return_tensors="pt").to(_device)


def _generate_chunk(prompt: str, desc_inputs=None, description: str | None = None) -> np.ndarray:
    if desc_inputs is None:
        desc_inputs = _tokenize_description(description)
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
    load_model()
    chunks = _split_text(text)
    if not chunks:
        return out_path
    sr = _model.config.sampling_rate

    print(f"[tts] split into {len(chunks)} chunk(s):")
    for i, c in enumerate(chunks, 1):
        print(f"  [{i}/{len(chunks)}] ({len(c)} chars) {c}")

    # Tokenize description once and reuse across all chunks of this request.
    desc_inputs = _tokenize_description(description)

    n = len(chunks)
    audio_parts: list[np.ndarray | None] = [None] * n
    work_q: queue.Queue = queue.Queue()
    error_box: list[BaseException] = []

    def worker():
        while True:
            item = work_q.get()
            if item is None:
                work_q.task_done()
                return
            idx, raw, ctext = item
            try:
                audio_parts[idx] = _trim_chunk_audio(raw, sr, ctext)
            except BaseException as e:
                error_box.append(e)
                audio_parts[idx] = raw
            finally:
                work_q.task_done()

    t = threading.Thread(target=worker, daemon=True)
    t.start()

    # Producer: generate Parler chunks sequentially, queue each for trim
    try:
        for i, c in enumerate(chunks):
            raw = _generate_chunk(c, desc_inputs=desc_inputs)
            work_q.put((i, raw, c))
    except torch.cuda.OutOfMemoryError:
        torch.cuda.empty_cache()
        _move_to_cpu()
        desc_inputs = _tokenize_description(description)  # re-tokenize on new device
        for i, c in enumerate(chunks):
            if audio_parts[i] is None and not any(idx == i for idx, *_ in list(work_q.queue)):
                raw = _generate_chunk(c, desc_inputs=desc_inputs)
                work_q.put((i, raw, c))

    work_q.put(None)
    work_q.join()
    t.join()

    if error_box:
        print(f"[tts] worker errors: {error_box[0]} (and {len(error_box) - 1} more)")

    parts = [a for a in audio_parts if a is not None and a.size > 0]
    if not parts:
        raise RuntimeError("no audio chunks produced")

    if len(parts) == 1:
        final = parts[0]
    else:
        silence = np.zeros(int(sr * INTER_CHUNK_SILENCE_SEC), dtype=np.float32)
        joined = []
        for i, a in enumerate(parts):
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
