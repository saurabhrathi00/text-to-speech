import os
import time
import uuid
import threading
import traceback
from pathlib import Path


def _load_env_file():
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


_load_env_file()

from flask import Flask, jsonify, render_template, request, send_from_directory

from config import MAX_AUDIO_FILES, PROVIDERS as _CONFIG_PROVIDERS, PARLER_SPEAKERS as _CONFIG_PARLER_SPEAKERS
from normalizer import normalize_text, generate_scene_prompts, OllamaError
from tts_engine import synthesize as parler_synthesize, build_description, load_model
from aligner import align as align_words, load_aligner
import eleven_tts
import bark_tts
import image_gen


PARLER_SPEAKERS = _CONFIG_PARLER_SPEAKERS


def _default_provider() -> str:
    """Provider from .env — used as initial UI state."""
    return (os.getenv("TTS_PROVIDER") or "parler").strip().lower()


PROVIDERS = _CONFIG_PROVIDERS


def _resolve_provider(requested: str | None) -> str:
    """Provider for THIS request. If client passed one, use it; else env."""
    p = (requested or "").strip().lower()
    if p in PROVIDERS:
        return p
    return _default_provider()


def _tts_synthesize(text: str, out_path: str, description: str,
                     voice: dict, provider: str) -> str:
    t0 = time.time()
    print(f"[app] dispatch → provider={provider}, text={len(text)} chars")
    try:
        if provider == "elevenlabs":
            if not eleven_tts.is_configured():
                raise RuntimeError("ELEVENLABS_API_KEY not set in .env")
            result = eleven_tts.synthesize(text, out_path, voice_config=voice)
        elif provider == "bark":
            result = bark_tts.synthesize(text, out_path, voice_config=voice)
        else:
            result = parler_synthesize(text, out_path, description=description)
        print(f"[app] {provider} done in {time.time() - t0:.1f}s → {result}")
        return result
    except Exception:
        print(f"[app] {provider} FAILED after {time.time() - t0:.1f}s")
        traceback.print_exc()
        raise

BASE_DIR = Path(__file__).parent.resolve()
AUDIO_DIR = BASE_DIR / "audio"
AUDIO_DIR.mkdir(exist_ok=True)
IMAGE_DIR = BASE_DIR / "images"
IMAGE_DIR.mkdir(exist_ok=True)

app = Flask(__name__)


def _prune_old_audio(keep: int = MAX_AUDIO_FILES):
    files = sorted(
        list(AUDIO_DIR.glob("*.wav")) + list(AUDIO_DIR.glob("*.mp3")),
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )
    for f in files[keep:]:
        try:
            f.unlink()
        except OSError:
            pass


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/sw.js")
def service_worker():
    return send_from_directory(BASE_DIR / "static", "sw.js", mimetype="application/javascript")


@app.route("/manifest.json")
def manifest():
    return send_from_directory(BASE_DIR / "static", "manifest.json", mimetype="application/manifest+json")


def _build_voice_description(voice: dict) -> str:
    custom_desc = (voice.get("custom") or "").strip()
    if custom_desc:
        return custom_desc
    return build_description(
        speaker=voice.get("speaker", "rohit"),
        speed=voice.get("speed", "moderate"),
        pitch=voice.get("pitch", "low"),
        expressivity=voice.get("expressivity", "expressive"),
        emotion=voice.get("emotion", "none"),
    )


@app.route("/normalize", methods=["POST"])
def normalize():
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "Pehle kuch type karein"}), 400
    provider = _resolve_provider(data.get("provider"))
    try:
        normalized = normalize_text(text, target_provider=provider)
    except OllamaError:
        return jsonify({"error": "Qwen server se connect nahi ho paya"}), 502
    return jsonify({"normalized_text": normalized})


@app.route("/tts", methods=["POST"])
def tts():
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "Pehle kuch type karein"}), 400

    voice = data.get("voice") or {}
    description = _build_voice_description(voice)
    provider = _resolve_provider(data.get("provider"))
    filename = f"output_{int(time.time())}_{uuid.uuid4().hex[:6]}.wav"
    out_path = AUDIO_DIR / filename

    t_req = time.time()
    print(f"[app] /tts request → {len(text)} chars, provider={provider}, voice={voice}")
    try:
        actual_path = _tts_synthesize(text, str(out_path), description, voice, provider)
    except Exception:
        traceback.print_exc()
        return jsonify({"error": "Awaaz generate nahi ho payi, dobara try karein"}), 500

    actual_filename = Path(actual_path).name
    words = align_words(actual_path) if provider == "parler" else []
    _prune_old_audio()

    print(f"[app] /tts response in {time.time() - t_req:.1f}s → {actual_filename}")
    return jsonify({
        "audio_url": f"/audio/{actual_filename}",
        "description_used": description if provider == "parler" else "",
        "words": words,
        "provider": provider,
    })


@app.route("/generate", methods=["POST"])
def generate():
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "Pehle kuch type karein"}), 400

    skip_normalize = bool(data.get("skip_normalize"))
    add_emotion_tags = bool(data.get("emotion_tags"))
    voice = data.get("voice") or {}
    description = _build_voice_description(voice)
    provider = _resolve_provider(data.get("provider"))

    t_req = time.time()
    print(f"[app] /generate request → {len(text)} chars, provider={provider}, "
          f"skip_normalize={skip_normalize}, emotion_tags={add_emotion_tags}, voice={voice}")

    if skip_normalize:
        normalized = text
    else:
        t_qwen = time.time()
        try:
            normalized = normalize_text(text, target_provider=provider,
                                         add_emotion_tags=add_emotion_tags)
            print(f"[app] qwen done in {time.time() - t_qwen:.1f}s → {len(normalized)} chars")
        except OllamaError as e:
            print(f"[app] qwen FAILED in {time.time() - t_qwen:.1f}s: {e}")
            return jsonify({"error": "Qwen server se connect nahi ho paya"}), 502

    filename = f"output_{int(time.time())}_{uuid.uuid4().hex[:6]}.wav"
    out_path = AUDIO_DIR / filename

    try:
        actual_path = _tts_synthesize(normalized, str(out_path), description, voice, provider)
    except Exception:
        traceback.print_exc()
        return jsonify({"error": "Awaaz generate nahi ho payi, dobara try karein"}), 500

    actual_filename = Path(actual_path).name
    words = align_words(actual_path) if provider == "parler" else []
    _prune_old_audio()

    print(f"[app] /generate response in {time.time() - t_req:.1f}s → {actual_filename}")
    return jsonify({
        "normalized_text": normalized,
        "audio_url": f"/audio/{actual_filename}",
        "description_used": description if provider == "parler" else "",
        "words": words,
        "provider": provider,
    })


@app.route("/audio/<path:filename>")
def serve_audio(filename):
    mimetype = "audio/mpeg" if filename.lower().endswith(".mp3") else "audio/wav"
    return send_from_directory(AUDIO_DIR, filename, mimetype=mimetype)


@app.route("/images/<path:filename>")
def serve_image(filename):
    return send_from_directory(IMAGE_DIR, filename, mimetype="image/png")


@app.route("/api/image", methods=["POST"])
def api_image():
    data = request.get_json(silent=True) or {}
    prompt = (data.get("prompt") or "").strip()
    if not prompt:
        return jsonify({"error": "prompt required"}), 400

    if not image_gen.is_configured():
        return jsonify({
            "error": "ComfyUI reachable nahi hai. Make sure run_nvidia_gpu.bat chal raha hai at localhost:8188"
        }), 503

    width = int(data.get("width", 1024))
    height = int(data.get("height", 1024))
    steps = int(data.get("steps", 20))
    negative = (data.get("negative") or "blurry, low quality, distorted, ugly").strip()
    seed = data.get("seed")
    seed = int(seed) if seed is not None else None

    t0 = time.time()
    print(f"[app] /api/image → {len(prompt)} chars, {width}x{height}, {steps} steps")
    try:
        img_bytes = image_gen.generate(
            prompt=prompt, negative=negative,
            width=width, height=height, steps=steps, seed=seed,
        )
    except image_gen.ComfyError as e:
        print(f"[app] image gen FAILED: {e}")
        return jsonify({"error": f"Image generate nahi hui: {e}"}), 502
    except Exception:
        traceback.print_exc()
        return jsonify({"error": "Image generate nahi hui"}), 500

    filename = f"img_{int(time.time())}_{uuid.uuid4().hex[:6]}.png"
    out_path = IMAGE_DIR / filename
    out_path.write_bytes(img_bytes)
    print(f"[app] /api/image done in {time.time() - t0:.1f}s → {filename}")
    return jsonify({"image_url": f"/images/{filename}"})


@app.route("/api/image/status")
def api_image_status():
    return jsonify({"comfy_reachable": image_gen.is_configured()})


@app.route("/api/scenes", methods=["POST"])
def api_scenes():
    """Convert Hindi/Hinglish/English text into English image prompts
    using Qwen. Returns scenes + characters arrays.
    """
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "Pehle kuch text dein"}), 400

    t0 = time.time()
    print(f"[app] /api/scenes → {len(text)} chars")
    result = generate_scene_prompts(text)
    print(f"[app] /api/scenes done in {time.time() - t0:.1f}s → "
          f"{len(result.get('scenes', []))} scene(s)")
    if result.get("error"):
        return jsonify(result), 502
    return jsonify(result)


def _warmup_in_background():
    provider = _default_provider()
    print(f"[startup] TTS provider: {provider}")
    if provider == "parler":
        print("[startup] Parler + aligner warmup in background...")
        t0 = time.time()
        try:
            load_model()
            print(f"[startup] Parler ready in {time.time() - t0:.1f}s")
        except Exception as e:
            print(f"[startup] Parler warmup failed: {e} (will retry on first request)")
        t1 = time.time()
        try:
            load_aligner()
            print(f"[startup] aligner ready in {time.time() - t1:.1f}s")
        except Exception as e:
            print(f"[startup] aligner warmup failed: {e}")
    elif provider == "bark":
        print("[startup] Bark warmup in background...")
        t0 = time.time()
        try:
            bark_tts.load_model()
            print(f"[startup] Bark ready in {time.time() - t0:.1f}s")
        except Exception as e:
            print(f"[startup] Bark warmup failed: {e} (will retry on first request)")
    elif provider == "elevenlabs":
        if not eleven_tts.is_configured():
            print("[startup] WARNING: TTS_PROVIDER=elevenlabs but ELEVENLABS_API_KEY not set")
        else:
            print("[startup] using ElevenLabs API — no local model load needed")


@app.route("/health")
def health():
    from tts_engine import _model
    return jsonify({"server": "up", "tts_ready": _model is not None})


@app.route("/api/providers")
def api_providers():
    return jsonify({
        "current": _default_provider(),
        "available": list(PROVIDERS),
        "elevenlabs_configured": eleven_tts.is_configured(),
    })


@app.route("/api/providers/<name>/voices")
def api_voices(name: str):
    name = name.lower()
    if name == "parler":
        return jsonify({
            "voices": PARLER_SPEAKERS,
            "emotions_supported": False,
            "speed_supported": True,
            "pitch_supported": True,
            "expressivity_supported": True,
        })
    if name == "elevenlabs":
        return jsonify({
            "voices": eleven_tts.list_voices(),
            "emotions_supported": True,
            "speed_supported": False,
            "pitch_supported": False,
            "expressivity_supported": False,
        })
    if name == "bark":
        return jsonify({
            "voices": bark_tts.list_voices(),
            "emotions_supported": True,
            "speed_supported": False,
            "pitch_supported": False,
            "expressivity_supported": False,
        })
    return jsonify({"error": "unknown provider"}), 404


if __name__ == "__main__":
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "5000"))
    threading.Thread(target=_warmup_in_background, daemon=True).start()
    app.run(host=host, port=port, debug=False, threaded=True)
