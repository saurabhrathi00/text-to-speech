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

    # torch.compile speeds up subsequent generations ~1.2x on CUDA after a
    # one-time compilation cost on the first call. Disabled on CPU since
    # the gain is small and the overhead is large.
    if _device.startswith("cuda") and os.getenv("TTS_NO_COMPILE") != "1":
        try:
            _model.forward = torch.compile(_model.forward, mode="reduce-overhead", fullgraph=False)
            print("[tts] torch.compile applied (first request will be slower)")
        except Exception as e:
            print(f"[tts] torch.compile skipped: {e}")


def _split_text(text: str) -> list[str]:
    """One sentence per chunk. Sentences are detected by terminal
    punctuation (। . ! ?). Sentences are never merged together and never
    split internally.
    """
    text = text.strip()
    if not text:
        return []
    sentences = [s.strip() for s in re.split(r"(?<=[।.!?])\s+", text) if s.strip()]
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
