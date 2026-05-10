import os
import re
import requests

from config import (
    QWEN_SYSTEM_PROMPT as SYSTEM_PROMPT,
    QWEN_TIMEOUT_SECONDS,
    QWEN_TEMPERATURE,
    ELEVEN_EMOTION_TRIGGERS,
    BARK_EMOTION_TRIGGERS,
)


def _compile(triggers: list) -> list:
    return [(re.compile(p, re.IGNORECASE), tag) for p, tag in triggers]


_COMPILED_BY_PROVIDER = {
    "elevenlabs": _compile(ELEVEN_EMOTION_TRIGGERS),
    "bark": _compile(BARK_EMOTION_TRIGGERS),
}


def _inject_emotion_tags(text: str, provider: str) -> str:
    """Insert provider-appropriate emotion tags before any trigger
    phrase from the matching trigger list. Idempotent — won't double-tag
    if a tag is already present immediately before a match.
    """
    triggers = _COMPILED_BY_PROVIDER.get(provider, [])
    if not triggers:
        return text
    inserted = 0
    for pattern, tag in triggers:
        def _repl(m: re.Match) -> str:
            nonlocal inserted
            start = m.start()
            preceding = text[max(0, start - len(tag) - 2):start]
            if tag in preceding:
                return m.group(0)
            inserted += 1
            return f"{tag} {m.group(0)}"

        text = pattern.sub(_repl, text)
    if inserted:
        print(f"[normalizer] regex injected {inserted} {provider} tag(s)")
    return text

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
               temperature: float | None = None) -> str:
    payload = {
        "model": MODEL_NAME,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ],
        "stream": False,
        "options": {"temperature": QWEN_TEMPERATURE if temperature is None else temperature},
        # Unload the model from GPU after responding so Parler+Whisper
        # have room. Otherwise qwen3:14b ~9GB + Parler ~3GB exceeds
        # the RTX 3060's 12GB and Parler hangs.
        "keep_alive": 0,
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
                    target_provider: str = "parler") -> str:
    """Normalize text for TTS:
      Pass 1 — Qwen script + punctuation cleanup (all providers)
      Pass 2 — Regex emotion-tag injection
                 ElevenLabs: always (tags are direction, no extra noise)
                 Bark:       only when BARK_USE_TAGS=1 (tags produce
                             literal non-speech sounds)
                 Parler:     never (no inline tag support)
    """
    # Pass 1: Qwen normalize
    content = _qwen_call(SYSTEM_PROMPT, text, timeout)
    if not _verify_devanagari_preserved(text, content):
        print("[normalizer] pass-1 substitution detected — falling back to original input")
        content = text

    provider = target_provider.lower()
    if provider == "elevenlabs":
        return _inject_emotion_tags(content, "elevenlabs")
    if provider == "bark" and os.getenv("BARK_USE_TAGS") == "1":
        return _inject_emotion_tags(content, "bark")
    return content
