import os
import time
import uuid
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

from normalizer import normalize_text, OllamaError
from tts_engine import synthesize, build_description

BASE_DIR = Path(__file__).parent.resolve()
AUDIO_DIR = BASE_DIR / "audio"
AUDIO_DIR.mkdir(exist_ok=True)

MAX_AUDIO_FILES = 1

app = Flask(__name__)


def _prune_old_audio(keep: int = MAX_AUDIO_FILES):
    files = sorted(AUDIO_DIR.glob("*.wav"), key=lambda f: f.stat().st_mtime, reverse=True)
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
    )


@app.route("/normalize", methods=["POST"])
def normalize():
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "Pehle kuch type karein"}), 400
    try:
        normalized = normalize_text(text)
    except OllamaError:
        return jsonify({"error": "Qwen server se connect nahi ho paya"}), 502
    return jsonify({"normalized_text": normalized})


@app.route("/tts", methods=["POST"])
def tts():
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "Pehle kuch type karein"}), 400

    description = _build_voice_description(data.get("voice") or {})
    filename = f"output_{int(time.time())}_{uuid.uuid4().hex[:6]}.wav"
    out_path = AUDIO_DIR / filename

    try:
        synthesize(text, str(out_path), description=description)
    except Exception:
        traceback.print_exc()
        return jsonify({"error": "Awaaz generate nahi ho payi, dobara try karein"}), 500

    _prune_old_audio()

    return jsonify({
        "audio_url": f"/audio/{filename}",
        "description_used": description,
    })


@app.route("/generate", methods=["POST"])
def generate():
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "Pehle kuch type karein"}), 400

    skip_normalize = bool(data.get("skip_normalize"))
    description = _build_voice_description(data.get("voice") or {})

    if skip_normalize:
        normalized = text
    else:
        try:
            normalized = normalize_text(text)
        except OllamaError:
            return jsonify({"error": "Qwen server se connect nahi ho paya"}), 502

    filename = f"output_{int(time.time())}_{uuid.uuid4().hex[:6]}.wav"
    out_path = AUDIO_DIR / filename

    try:
        synthesize(normalized, str(out_path), description=description)
    except Exception:
        traceback.print_exc()
        return jsonify({"error": "Awaaz generate nahi ho payi, dobara try karein"}), 500

    _prune_old_audio()

    return jsonify({
        "normalized_text": normalized,
        "audio_url": f"/audio/{filename}",
        "description_used": description,
    })


@app.route("/audio/<path:filename>")
def serve_audio(filename):
    return send_from_directory(AUDIO_DIR, filename, mimetype="audio/wav")


if __name__ == "__main__":
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "5000"))
    app.run(host=host, port=port, debug=False, threaded=True)
