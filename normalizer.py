"""App-side wrapper around the isolated `llm/` module.

This file handles concerns that DON'T belong inside `llm/`:
  - Devanagari skeleton verification (rejecting hallucinated substitutions)
  - Sentence splitting + emotion-tag placement
  - Provider-specific tag filtering (Bark vs ElevenLabs)
  - Public surface used by app.py

All actual model calls live in `llm/` and obey the isolation contract.
"""
import re

from llm import (
    refine_for_tts,
    classify_emotions as _llm_classify_emotions,
    generate_scene_prompts as _llm_generate_scene_prompts,
    LLMError,
)


# Re-exported under the old name so existing callers in app.py keep
# working. New code should catch LLMError directly.
OllamaError = LLMError


# Tags Bark can render cleanly. ElevenLabs accepts the full tag set.
BARK_SUPPORTED_TAGS = {
    "[crying]", "[laughs]", "[chuckles]", "[sighs]", "[whispers]",
    "[gasps]", "[breathless]",
}


# ──────────────────────────────────────────────────────────────────────
# Devanagari safety check — rejects LLM outputs that silently swapped
# consonants (a content change, not a pronunciation fix).
# ──────────────────────────────────────────────────────────────────────

def _devanagari_words(text: str) -> list[str]:
    return re.findall(r"[ऀ-ॿ]+", text)


def _letter_skeleton(word: str) -> str:
    """Independent vowels + consonants only — drops matras, nukta,
    anusvara. Adding a nukta is fine; swapping a consonant is not."""
    return "".join(
        c for c in word
        if "ऄ" <= c <= "ह" or "क़" <= c <= "ॡ"
    )


def _verify_devanagari_preserved(input_text: str, output_text: str) -> bool:
    in_words = _devanagari_words(input_text)
    if not in_words:
        return True
    out_skels: dict[str, list[str]] = {}
    for w in _devanagari_words(output_text):
        out_skels.setdefault(_letter_skeleton(w), []).append(w)

    substituted = [w for w in in_words if _letter_skeleton(w) not in out_skels]
    if substituted:
        print(f"[normalizer] LLM changed letter skeleton of "
              f"{len(substituted)} word(s): {substituted[:5]}")
        return False
    return True


# ──────────────────────────────────────────────────────────────────────
# Sentence splitting + emotion tagging
# ──────────────────────────────────────────────────────────────────────

def _split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[।.!?])\s*", text)
    return [p.strip() for p in parts if p and p.strip()]


def _apply_tags_per_sentence(text: str, tags: list[str | None],
                              allowed: set[str] | None = None) -> tuple[str, int]:
    sentences = _split_sentences(text)
    if not sentences:
        return text, 0
    out_parts: list[str] = []
    inserted = 0
    for s, t in zip(sentences, tags):
        if t and (allowed is None or t in allowed):
            out_parts.append(f"{t} {s}")
            inserted += 1
        else:
            out_parts.append(s)
    return " ".join(out_parts), inserted


def _add_emotion_tags(text: str, provider: str) -> str:
    sentences = _split_sentences(text)
    if not sentences:
        return text
    print(f"[normalizer] classifying emotions for {len(sentences)} sentence(s)...")
    tags = _llm_classify_emotions(sentences)
    allowed = BARK_SUPPORTED_TAGS if provider == "bark" else None
    new_text, count = _apply_tags_per_sentence(text, tags, allowed=allowed)
    print(f"[normalizer] applied {count} {provider} tag(s)")
    return new_text


# ──────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────

def normalize_text(text: str, target_provider: str = "parler",
                    add_emotion_tags: bool = False,
                    progress_cb=None) -> str:
    """Pass 1 — script + punctuation cleanup for TTS.
    Pass 2 — emotion classification (only for providers that render
    inline tags: ElevenLabs, Bark). Parler skips Pass 2 because it
    would speak the tag literally.
    """
    will_classify = add_emotion_tags and target_provider.lower() in ("elevenlabs", "bark")

    if progress_cb:
        progress_cb("qwen_normalize", 45)

    content = refine_for_tts(text, keep_loaded=will_classify)
    if not _verify_devanagari_preserved(text, content):
        print("[normalizer] pass-1 substitution detected — falling back to input")
        content = text

    if not add_emotion_tags:
        return content

    provider = target_provider.lower()
    if provider in ("elevenlabs", "bark"):
        if progress_cb:
            progress_cb("qwen_emotions", 30)
        return _add_emotion_tags(content, provider)

    print(f"[normalizer] emotion-tags requested but provider={provider} "
          f"doesn't support inline tags — skipping")
    return content


def generate_scene_prompts(text: str) -> dict:
    """Story → list of SDXL image prompts (with shared character list).
    Returns {'characters': [...], 'scenes': [...], 'error'?: str}.
    """
    try:
        data = _llm_generate_scene_prompts(text)
    except LLMError as e:
        print(f"[scenes] LLM failed: {e}")
        return {"characters": [], "scenes": [], "error": str(e)}

    scenes = []
    for s in data.get("scenes") or []:
        if isinstance(s, dict) and isinstance(s.get("prompt"), str):
            scenes.append({
                "hindi": s.get("hindi", ""),
                "prompt": s["prompt"].strip(),
            })
    characters = []
    for c in data.get("characters") or []:
        if isinstance(c, dict) and isinstance(c.get("description"), str):
            characters.append({
                "name": c.get("name", ""),
                "description": c["description"].strip(),
            })
    print(f"[scenes] produced {len(scenes)} scene(s), {len(characters)} character(s)")
    return {"characters": characters, "scenes": scenes}
