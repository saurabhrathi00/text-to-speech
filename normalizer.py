import os
import re
import requests

from config import (
    QWEN_SYSTEM_PROMPT as SYSTEM_PROMPT,
    QWEN_ELEVENLABS_EMOTION_ADDENDUM,
    QWEN_TIMEOUT_SECONDS,
    QWEN_TEMPERATURE,
)

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


def normalize_text(text: str, timeout: int = QWEN_TIMEOUT_SECONDS,
                    target_provider: str = "parler") -> str:
    """Normalize text for TTS. If target_provider is 'elevenlabs', also
    ask Qwen to insert inline emotion tags ([cry], [whispers], etc.)
    at strong emotional moments — those tags are direction for v3 voice
    and won't be spoken literally."""
    system_prompt = SYSTEM_PROMPT
    if target_provider.lower() == "elevenlabs":
        system_prompt = SYSTEM_PROMPT + QWEN_ELEVENLABS_EMOTION_ADDENDUM

    payload = {
        "model": MODEL_NAME,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text},
        ],
        "stream": False,
        "options": {"temperature": QWEN_TEMPERATURE},
        # Tell Ollama to unload the model from GPU immediately after
        # responding. Otherwise a 14B Qwen + Parler + Whisper together
        # exceed the RTX 3060's 12GB VRAM and Parler hangs / OOMs
        # silently. 0 = unload now, "5m" = keep loaded 5 min, etc.
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

    # Safety net: if Qwen modified or dropped any Devanagari word that was
    # in the original input, fall back to the input text. We trust Qwen on
    # script conversion (Roman → Devanagari) and on punctuation, but not
    # on rewriting Devanagari words.
    if not _verify_devanagari_preserved(text, content):
        print("[normalizer] falling back to original input (no Qwen edits applied)")
        return text

    return content
