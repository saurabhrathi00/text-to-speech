import os
import re
import requests

from config import (
    QWEN_SYSTEM_PROMPT as SYSTEM_PROMPT,
    QWEN_TIMEOUT_SECONDS,
    QWEN_TEMPERATURE,
    ELEVEN_EMOTION_TRIGGERS,
)

_COMPILED_TRIGGERS = [(re.compile(p, re.IGNORECASE), tag) for p, tag in ELEVEN_EMOTION_TRIGGERS]


def _inject_emotion_tags(text: str) -> str:
    """Insert ElevenLabs emotion tags before any trigger phrase from
    ELEVEN_EMOTION_TRIGGERS. Tags are only inserted at positions that
    don't already have a tag immediately before them (so re-running is
    idempotent).
    """
    inserted = 0
    for pattern, tag in _COMPILED_TRIGGERS:
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
        print(f"[normalizer] regex injected {inserted} emotion tag(s)")
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
      Pass 2 — Deterministic regex emotion-tag injection (ElevenLabs only)

    The Qwen second-pass approach was unreliable on a 14B local model;
    swapped out for a regex pass driven by ELEVEN_EMOTION_TRIGGERS in
    config.py — predictable, fast, no LLM dependency.
    """
    # Pass 1: Qwen normalize
    content = _qwen_call(SYSTEM_PROMPT, text, timeout)
    if not _verify_devanagari_preserved(text, content):
        print("[normalizer] pass-1 substitution detected — falling back to original input")
        content = text

    if target_provider.lower() != "elevenlabs":
        return content

    # Pass 2: regex-based emotion tag injection
    tagged = _inject_emotion_tags(content)
    return tagged
