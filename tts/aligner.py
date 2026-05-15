import os
import torch
import soundfile as sf
from faster_whisper import WhisperModel

from config import (
    WHISPER_REPETITION_RATIO as REPETITION_RATIO,
    WHISPER_MIN_RATIO as MIN_WHISPER_RATIO,
    WHISPER_TAIL_PAD_SEC as TAIL_PAD_SEC,
)

MODEL_SIZE = os.getenv("WHISPER_MODEL", "small")

_model = None
_device = None


class AlignError(Exception):
    pass


def load_aligner():
    """Load whisper. Defaults to CPU even when CUDA is available so it
    runs in true parallel with Parler/Bark on GPU instead of serializing
    on the same device. Override with WHISPER_DEVICE=cuda if VRAM is
    plentiful and you want max whisper speed.
    """
    global _model, _device
    if _model is not None:
        return
    requested = (os.getenv("WHISPER_DEVICE") or "cpu").strip().lower()
    if requested == "cuda" and torch.cuda.is_available():
        _device = "cuda"
        compute = "float16"
    else:
        _device = "cpu"
        # int8 quantization on CPU is ~2x faster than float32 with
        # negligible accuracy loss for whisper-tiny.
        compute = "int8"
    _model = WhisperModel(
        MODEL_SIZE,
        device=_device,
        compute_type=compute,
        cpu_threads=int(os.getenv("WHISPER_CPU_THREADS", "4")),
    )
    print(f"[aligner] loaded whisper-{MODEL_SIZE} on {_device} ({compute})")


def align(audio_path: str) -> list[dict]:
    """Return word-level timestamps for a given audio file.

    Returns list of {"word": str, "start": float, "end": float}.
    Empty list on failure.
    """
    try:
        load_aligner()
        segments, _info = _model.transcribe(
            audio_path,
            language="hi",
            word_timestamps=True,
            vad_filter=False,
            beam_size=1,
        )
        words = []
        for s in segments:
            for w in (s.words or []):
                token = (w.word or "").strip()
                if token:
                    words.append({
                        "word": token,
                        "start": float(w.start),
                        "end": float(w.end),
                    })
        return words
    except Exception as e:
        print(f"[aligner] alignment failed: {e}")
        return []


def trim_audio_to_words(audio_path: str, words: list[dict],
                         expected_word_count: int = 0) -> list[dict]:
    """Rewrite the audio file in-place, trimming everything after the last
    "real" word as detected by whisper.

    If whisper found significantly more words than the input text suggests
    (a sign of trailing repetition), we cut at the Nth word where
    N = expected_word_count. Otherwise cut after the last whisper word.

    Returns the (possibly truncated) word list reflecting the new audio.
    """
    if not words:
        return words

    # Safety: if whisper detected significantly fewer words than the input
    # has, it likely missed some at the end (whisper-tiny is fast but less
    # accurate). Trimming at the last detected word would cut off real
    # spoken content. Better to keep the raw audio (possibly with some
    # trailing junk) than risk losing actual words.
    if expected_word_count > 0 and len(words) < expected_word_count * MIN_WHISPER_RATIO:
        print(f"[aligner] whisper found {len(words)}/{expected_word_count} words "
              f"(<{int(MIN_WHISPER_RATIO * 100)}%) — skipping trim")
        return words

    cut_idx = len(words) - 1
    if expected_word_count > 0 and len(words) > expected_word_count * REPETITION_RATIO:
        cut_idx = expected_word_count - 1
        print(f"[aligner] detected repetition: whisper={len(words)} words, "
              f"expected={expected_word_count}; trimming at word #{cut_idx + 1}")

    cut_idx = max(0, min(cut_idx, len(words) - 1))
    end_time = words[cut_idx]["end"] + TAIL_PAD_SEC

    try:
        audio, sr = sf.read(audio_path)
        end_sample = min(len(audio), int(end_time * sr))
        if end_sample < len(audio):
            sf.write(audio_path, audio[:end_sample], sr)
            print(f"[aligner] trimmed {len(audio) - end_sample} samples "
                  f"(~{(len(audio) - end_sample) / sr:.2f}s) from end")
    except Exception as e:
        print(f"[aligner] trim write failed: {e}")
        return words

    return words[: cut_idx + 1]
