"""Normalizer safety: sentence split, refusal detection, Devanagari guard."""
import normalizer as nz


# ── sentence splitting ───────────────────────────────────────────────
def test_split_basic():
    assert nz._split_sentences("Hello. How are you? Fine!") == \
        ["Hello.", "How are you?", "Fine!"]


def test_split_devanagari_danda():
    out = nz._split_sentences("यह एक वाक्य है। यह दूसरा है।")
    assert len(out) == 2


def test_split_empty():
    assert nz._split_sentences("   ") == []


# ── refusal detection (protects a paid TTS call from LLM meta-replies) ─
def test_refusal_detected():
    assert nz._looks_like_refusal("I'm sorry, I cannot help with that request.") is True
    assert nz._looks_like_refusal("As an AI language model, I ...") is True


def test_normal_text_not_refusal():
    assert nz._looks_like_refusal("Once upon a time there was a king.") is False


# ── Devanagari preservation (reject silent consonant swaps) ───────────
def test_devanagari_preserved_when_same():
    src = "राम वन को गए।"
    assert nz._verify_devanagari_preserved(src, "राम, वन को गए।") is True


def test_devanagari_substitution_rejected():
    # 'राम' -> 'रात' is a consonant swap; must be flagged as not preserved.
    assert nz._verify_devanagari_preserved("राम वन को गए", "रात वन को गए") is False


def test_no_devanagari_always_ok():
    assert nz._verify_devanagari_preserved("hello world", "Hello, world!") is True


# ── tag application ──────────────────────────────────────────────────
def test_apply_tags_per_sentence():
    text = "He whispered. She left."
    out, count = nz._apply_tags_per_sentence(text, ["[whispers]", None])
    assert out.startswith("[whispers] He whispered.")
    assert count == 1


def test_apply_tags_respects_allowed_set():
    text = "One. Two."
    out, count = nz._apply_tags_per_sentence(
        text, ["[laughs]", "[unknown]"], allowed={"[laughs]"})
    assert count == 1 and "[laughs]" in out and "[unknown]" not in out
