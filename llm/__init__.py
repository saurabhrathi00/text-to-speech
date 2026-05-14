"""Public API for the isolated LLM module.

Only this file is meant to be imported from the rest of the app.
Everything inside `llm/` plays by the isolation contract in README.md.

Three tasks the app uses:

  refine_for_tts(text)            → str
      Pass-1 cleanup so an Indian TTS narrator can read the text
      smoothly. Hindi/Hinglish in, formatted Hindi/Hinglish out.

  classify_emotions(sentences)    → list[str | None]
      Per-sentence vocal-emotion tag (or None for pure narration).
      Output length always equals input length.

  generate_scene_prompts(text)    → dict
      Splits story text into SDXL image prompts plus a shared
      character list. Returns {"characters": [...], "scenes": [...]}.
"""
import json
from pathlib import Path

from .client import chat, LLMError


_PROMPT_DIR = Path(__file__).parent / "prompts"


def _load_prompt(name: str) -> str:
    path = _PROMPT_DIR / f"{name}.md"
    return path.read_text(encoding="utf-8")


# Loaded once at import-time. Edit the .md files to change behavior.
_NORMALIZE_PROMPT = _load_prompt("normalize")
_EMOTIONS_PROMPT = _load_prompt("emotions")
_SCENES_PROMPT = _load_prompt("scenes")


# ──────────────────────────────────────────────────────────────────────
# Pass 1 — normalize for TTS
# ──────────────────────────────────────────────────────────────────────

def refine_for_tts(text: str, keep_loaded: bool = False) -> str:
    """Format Hindi/Hinglish text for a TTS narrator. Returns the model's
    output text verbatim — caller is responsible for post-validation
    (e.g. Devanagari skeleton check).

    `keep_loaded=True` hints the transport to keep the model resident
    in VRAM for ~30s, so a follow-up call (e.g. emotion classify) skips
    the cold-reload cost. No-op when using a remote API like Gemini.
    """
    return chat(_NORMALIZE_PROMPT, text,
                keep_alive="30s" if keep_loaded else None)


# ──────────────────────────────────────────────────────────────────────
# Pass 2 — sentence emotion classification
# ──────────────────────────────────────────────────────────────────────

def classify_emotions(sentences: list[str]) -> list[str | None]:
    """Return a list of emotion tags (or None) — same length as input.

    Falls back to all-None on parse failure rather than raising, since
    a missing tag is non-fatal: the TTS just speaks the sentence
    without inflection.
    """
    if not sentences:
        return []

    payload = json.dumps(sentences, ensure_ascii=False)
    raw = chat(_EMOTIONS_PROMPT, payload, temperature=0.3)
    parsed = _safe_extract_json(raw)
    if not isinstance(parsed, dict):
        print(f"[llm] emotion classify: non-dict response, falling back to none")
        return [None] * len(sentences)

    tags = parsed.get("emotions")
    if not isinstance(tags, list):
        print(f"[llm] emotion classify: missing 'emotions' array")
        return [None] * len(sentences)

    # Pad / truncate to match input length.
    out: list[str | None] = []
    for i in range(len(sentences)):
        t = tags[i] if i < len(tags) else None
        out.append(t if isinstance(t, str) and t.startswith("[") else None)
    return out


# ──────────────────────────────────────────────────────────────────────
# Scene-prompt generator (for image generation)
# ──────────────────────────────────────────────────────────────────────

def generate_scene_prompts(text: str) -> dict:
    """Return {'characters': [...], 'scenes': [...]} or empty shape on
    parse failure."""
    raw = chat(_SCENES_PROMPT, text, temperature=0.4)
    parsed = _safe_extract_json(raw)
    if not isinstance(parsed, dict):
        return {"characters": [], "scenes": []}
    return {
        "characters": parsed.get("characters") or [],
        "scenes": parsed.get("scenes") or [],
    }


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────

def _safe_extract_json(text: str):
    """Best-effort JSON extraction. Tolerates stray markdown fences."""
    s = text.strip()
    if s.startswith("```"):
        # strip ``` or ```json fences
        s = s.split("```", 2)
        s = s[1] if len(s) > 1 else ""
        if s.startswith("json"):
            s = s[4:]
        s = s.strip("` \n")
    try:
        return json.loads(s)
    except (json.JSONDecodeError, TypeError):
        # try to find first {...} block
        start = s.find("{")
        end = s.rfind("}")
        if 0 <= start < end:
            try:
                return json.loads(s[start:end + 1])
            except json.JSONDecodeError:
                pass
        return None


__all__ = [
    "refine_for_tts",
    "classify_emotions",
    "generate_scene_prompts",
    "LLMError",
]
