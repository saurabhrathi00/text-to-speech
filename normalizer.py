import os
import re
import requests

from config import (
    QWEN_SYSTEM_PROMPT as SYSTEM_PROMPT,
    QWEN_EMOTION_TAG_PROMPT,
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


def _qwen_call(system_prompt: str, user_text: str, timeout: int) -> str:
    payload = {
        "model": MODEL_NAME,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ],
        "stream": False,
        "options": {"temperature": QWEN_TEMPERATURE},
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
    """Normalize text for TTS. Two-pass when target_provider is
    'elevenlabs':
      Pass 1 — script + punctuation cleanup (same as other providers)
      Pass 2 — insert ElevenLabs emotion tags at vocal-emotion moments

    Splitting into two focused calls works better than one large
    combined prompt for a 14B local model.
    """
    # Pass 1: normalize
    content = _qwen_call(SYSTEM_PROMPT, text, timeout)
    if not _verify_devanagari_preserved(text, content):
        print("[normalizer] pass-1 substitution detected — falling back to original input")
        content = text

    if target_provider.lower() != "elevenlabs":
        return content

    # Pass 2: emotion tags (ElevenLabs only)
    print("[normalizer] pass-2: emotion-tag injection for ElevenLabs")
    try:
        tagged = _qwen_call(QWEN_EMOTION_TAG_PROMPT, content, timeout)
    except OllamaError as e:
        print(f"[normalizer] emotion-tag pass failed: {e} — using untagged text")
        return content

    if not _verify_devanagari_preserved(content, tagged):
        print("[normalizer] pass-2 substitution detected — using untagged text")
        return content

    return tagged
