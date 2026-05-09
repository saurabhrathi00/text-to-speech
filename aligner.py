import os
import torch
from faster_whisper import WhisperModel

MODEL_SIZE = os.getenv("WHISPER_MODEL", "small")

_model = None
_device = None


class AlignError(Exception):
    pass


def load_aligner():
    global _model, _device
    if _model is not None:
        return
    if torch.cuda.is_available():
        _device = "cuda"
        compute = "float16"
    else:
        _device = "cpu"
        compute = "int8"
    _model = WhisperModel(MODEL_SIZE, device=_device, compute_type=compute)
    print(f"[aligner] loaded whisper-{MODEL_SIZE} on {_device}")


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
