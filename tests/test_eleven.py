"""ElevenLabs v3 settings, chunking, and MP3 header stripping."""
import pytest

from tts import eleven


# ── model detection + limits ─────────────────────────────────────────
def test_is_v3():
    assert eleven._is_v3("eleven_v3") is True
    assert eleven._is_v3("eleven_multilingual_v2") is False
    assert eleven._is_v3(None) is False


def test_max_chars_per_model():
    assert eleven._max_chars_for("eleven_v3") == eleven._MAX_CHARS_V3
    assert eleven._max_chars_for("eleven_multilingual_v2") == eleven._MAX_CHARS_V2


# ── v3 stability snapping ────────────────────────────────────────────
@pytest.mark.parametrize("value,expected", [
    (0.0, 0.0), (0.2, 0.0), (0.3, 0.5), (0.5, 0.5),
    (0.7, 0.5), (0.8, 1.0), (1.0, 1.0),
])
def test_snap_v3_stability(value, expected):
    assert eleven._snap_v3_stability(value) == expected


def test_snap_v3_stability_bad_input_defaults_natural():
    assert eleven._snap_v3_stability("nonsense") == 0.5


# ── voice settings shape per model ───────────────────────────────────
def test_v3_settings_have_no_style_and_snapped_stability():
    s = eleven._build_voice_settings("eleven_v3", {"stability": 0.3})
    assert "style" not in s
    assert s["stability"] == 0.5
    assert s["use_speaker_boost"] is True


def test_v2_settings_keep_style():
    s = eleven._build_voice_settings("eleven_multilingual_v2", {})
    assert "style" in s


# ── chunking ─────────────────────────────────────────────────────────
def test_short_text_single_chunk():
    assert eleven._chunk_text("Hello world.", 4800) == ["Hello world."]


def test_long_text_splits_on_sentences():
    text = ". ".join(f"Sentence number {i} here" for i in range(400)) + "."
    chunks = eleven._chunk_text(text, 500)
    assert len(chunks) > 1
    assert all(len(c) <= 500 or " " not in c for c in chunks)


# ── header stripping ─────────────────────────────────────────────────
def test_strip_headers_raises_on_no_audio():
    # A body with no MPEG sync word must fail loudly, not return garbage.
    with pytest.raises(eleven.ElevenLabsError):
        eleven._strip_mp3_headers(b"not an mp3 at all, just plain text bytes")


def test_strip_headers_finds_sync_word():
    data = b"\x00\x00\xff\xfb\x90\x00somedata"
    out = eleven._strip_mp3_headers(data)
    assert out.startswith(b"\xff\xfb")
