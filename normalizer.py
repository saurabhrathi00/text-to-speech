import os
import re
import json
import requests

from config import (
    QWEN_SYSTEM_PROMPT as SYSTEM_PROMPT,
    QWEN_EMOTION_CLASSIFY_PROMPT,
    QWEN_SCENE_PROMPT_PROMPT,
    QWEN_TIMEOUT_SECONDS,
    QWEN_TEMPERATURE,
)

# Tags Bark can render cleanly. ElevenLabs accepts the full tag set.
BARK_SUPPORTED_TAGS = {
    "[crying]", "[laughs]", "[chuckles]", "[sighs]", "[whispers]",
    "[gasps]", "[breathless]",
}


def _classify_emotions_via_qwen(sentences: list[str], timeout: int) -> list[str | None]:
    """Ask Qwen to classify each sentence's vocal emotion. Returns a list
    of tags (with brackets) or None per sentence. On any failure returns
    [None, None, ...] of the right length (no tagging).
    """
    user_payload = json.dumps(sentences, ensure_ascii=False)
    try:
        raw = _qwen_call(QWEN_EMOTION_CLASSIFY_PROMPT, user_payload, timeout, temperature=0.3)
    except OllamaError as e:
        print(f"[normalizer] emotion classify FAILED: {e}")
        return [None] * len(sentences)

    # Strip any surrounding code fences or commentary; pull out the first
    # JSON object we can find.
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        print(f"[normalizer] emotion classify: no JSON in output — {raw[:200]}")
        return [None] * len(sentences)
    try:
        data = json.loads(m.group(0))
        emotions = data.get("emotions") or []
    except json.JSONDecodeError as e:
        print(f"[normalizer] emotion classify: JSON parse error — {e}")
        return [None] * len(sentences)

    if not isinstance(emotions, list):
        print(f"[normalizer] emotion classify: 'emotions' not a list")
        return [None] * len(sentences)

    # Pad / truncate to expected length
    if len(emotions) < len(sentences):
        emotions = emotions + [None] * (len(sentences) - len(emotions))
    elif len(emotions) > len(sentences):
        emotions = emotions[: len(sentences)]

    # Normalize each entry — must be a tag string or None
    cleaned: list[str | None] = []
    for e in emotions:
        if isinstance(e, str) and e.startswith("[") and e.endswith("]"):
            cleaned.append(e)
        else:
            cleaned.append(None)
    return cleaned


def _split_sentences(text: str) -> list[str]:
    """Split text into sentences keeping the terminating punctuation."""
    parts = re.split(r"(?<=[।.!?])\s*", text)
    return [p.strip() for p in parts if p and p.strip()]


def _apply_tags_per_sentence(text: str, tags: list[str | None],
                              allowed: set[str] | None = None) -> tuple[str, int]:
    """Rebuild text with each sentence prefixed by its emotion tag (if any
    and if the tag is in `allowed`). Returns (new_text, tag_count).
    """
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


def generate_scene_prompts(text: str, timeout: int = QWEN_TIMEOUT_SECONDS) -> dict:
    """Use Qwen to split Hindi/Hinglish/English text into visual scenes
    and produce an English SDXL image prompt per scene. Also returns a
    `characters` list so the caller can keep character descriptions
    consistent across scenes.

    Returns a dict shaped like:
      {"characters": [{"name": str, "description": str}, ...],
       "scenes":     [{"hindi": str, "prompt": str}, ...]}

    On failure (Ollama down, malformed JSON), returns
      {"characters": [], "scenes": [], "error": "..."}.
    """
    try:
        raw = _qwen_call(QWEN_SCENE_PROMPT_PROMPT, text, timeout, temperature=0.4)
    except OllamaError as e:
        print(f"[scenes] qwen FAILED: {e}")
        return {"characters": [], "scenes": [], "error": str(e)}

    print(f"[scenes] qwen raw output ({len(raw)} chars):\n{raw[:1000]}")
    if len(raw) > 1000:
        print(f"... [truncated, total {len(raw)} chars]")

    # Strip common LLM artifacts before JSON extraction
    cleaned = raw.strip()
    # Remove markdown code fences
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```\s*$", "", cleaned)

    m = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not m:
        print(f"[scenes] no JSON braces found — model likely refused structured output")
        return {"characters": [], "scenes": [], "error": "no JSON in output"}
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        print(f"[scenes] JSON parse error — {e}")
        return {"characters": [], "scenes": [], "error": f"JSON parse: {e}"}

    characters = data.get("characters") or []
    scenes = data.get("scenes") or []
    # Validate shapes; drop malformed entries silently
    clean_scenes = []
    for s in scenes:
        if isinstance(s, dict) and isinstance(s.get("prompt"), str):
            clean_scenes.append({
                "hindi": s.get("hindi", ""),
                "prompt": s["prompt"].strip(),
            })
    clean_chars = []
    for c in characters:
        if isinstance(c, dict) and isinstance(c.get("description"), str):
            clean_chars.append({
                "name": c.get("name", ""),
                "description": c["description"].strip(),
            })
    print(f"[scenes] produced {len(clean_scenes)} scene(s), {len(clean_chars)} character(s)")
    return {"characters": clean_chars, "scenes": clean_scenes}


def _add_emotion_tags(text: str, provider: str, timeout: int) -> str:
    """Use Qwen to classify each sentence's emotion, then prepend the
    chosen tag to that sentence.
    """
    sentences = _split_sentences(text)
    if not sentences:
        return text
    print(f"[normalizer] classifying emotions for {len(sentences)} sentence(s) via Qwen...")
    tags = _classify_emotions_via_qwen(sentences, timeout)
    allowed = BARK_SUPPORTED_TAGS if provider == "bark" else None
    new_text, count = _apply_tags_per_sentence(text, tags, allowed=allowed)
    print(f"[normalizer] applied {count} {provider} tag(s)")
    return new_text

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://192.168.1.10:11434/api/chat")
MODEL_NAME = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")

class OllamaError(Exception):
    pass


def _devanagari_words(text: str) -> list[str]:
    return re.findall(r"[ऀ-ॿ]+", text)


def _letter_skeleton(word: str) -> str:
    """Extract the letter skeleton of a Devanagari word — independent
    vowels and consonants only, dropping matras, nukta, and anusvara/
    chandrabindu.

    The skeleton is the part of a word that cannot legitimately change
    during pronunciation-based formatting. Adding a nukta, swapping
    matras, or moving an anusvara are allowed; changing the consonant
    sequence is not.

    Letters: U+0904–U+0939 (अ–ह) + U+0958–U+0961 (extended consonants).
    """
    return "".join(
        c for c in word
        if "ऄ" <= c <= "ह" or "क़" <= c <= "ॡ"
    )


def _verify_devanagari_preserved(input_text: str, output_text: str) -> bool:
    in_words = _devanagari_words(input_text)
    if not in_words:
        return True
    out_skels: dict[str, list[str]] = {}
    for w in _devanagari_words(output_text):
        out_skels.setdefault(_letter_skeleton(w), []).append(w)

    substituted: list[str] = []
    for w in in_words:
        skel = _letter_skeleton(w)
        if skel in out_skels:
            continue
        substituted.append(w)

    if substituted:
        print(f"[normalizer] Qwen changed letter skeleton of {len(substituted)} word(s): {substituted[:5]}")
        return False
    return True


def _qwen_call(system_prompt: str, user_text: str, timeout: int,
               temperature: float | None = None,
               keep_alive: int | str = 0) -> str:
    """Single chat completion call against Ollama.
    keep_alive=0 unloads model from GPU after response (frees VRAM for
    Parler/Bark). Pass a duration like "30s" if a follow-up Qwen call
    is coming so we don't pay the reload cost twice.
    """
    payload = {
        "model": MODEL_NAME,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ],
        "stream": False,
        "options": {"temperature": QWEN_TEMPERATURE if temperature is None else temperature},
        "keep_alive": keep_alive,
    }
    try:
        r = requests.post(OLLAMA_URL, json=payload, timeout=timeout)
        r.raise_for_status()
    except requests.RequestException as e:
        raise OllamaError(str(e)) from e
    data = r.json()
    content = data.get("message", {}).get("content", "").strip()
    if not content:
        raise OllamaError("Empty response from Qwen")
    return content


def normalize_text(text: str, timeout: int = QWEN_TIMEOUT_SECONDS,
                    target_provider: str = "parler",
                    add_emotion_tags: bool = False) -> str:
    """Normalize text for TTS:
      Pass 1 — Qwen script + punctuation cleanup (all providers)
      Pass 2 — Qwen-driven emotion classification per sentence
               (only when add_emotion_tags is True AND the provider
                actually renders inline tags). For Bark, only Bark-
                supported tags are kept; Parler skips entirely since it
                speaks bracketed text literally.
    """
    will_classify = add_emotion_tags and target_provider.lower() in ("elevenlabs", "bark")

    # Pass 1: Qwen normalize. Keep model loaded if a classify call
    # follows so we don't pay the reload cost twice.
    content = _qwen_call(SYSTEM_PROMPT, text, timeout,
                          keep_alive="30s" if will_classify else 0)
    if not _verify_devanagari_preserved(text, content):
        print("[normalizer] pass-1 substitution detected — falling back to original input")
        content = text

    if not add_emotion_tags:
        return content

    provider = target_provider.lower()
    if provider == "elevenlabs":
        return _add_emotion_tags(content, "elevenlabs", timeout)
    if provider == "bark":
        return _add_emotion_tags(content, "bark", timeout)
    # parler: skip — would speak the tag literally
    print(f"[normalizer] emotion-tags requested but provider={provider} doesn't support inline tags — skipping")
    return content
