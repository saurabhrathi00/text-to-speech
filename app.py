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

from normalizer import normalize_text, OllamaError
from tts_engine import synthesize, build_description

BASE_DIR = Path(__file__).parent.resolve()
AUDIO_DIR = BASE_DIR / "audio"
AUDIO_DIR.mkdir(exist_ok=True)

CLEANUP_AGE_SECONDS = 60 * 60  # 1 hour
CLEANUP_INTERVAL_SECONDS = 10 * 60  # every 10 min

app = Flask(__name__)


def _cleanup_loop():
    while True:
        try:
            now = time.time()
            for f in AUDIO_DIR.glob("*.wav"):
                if now - f.stat().st_mtime > CLEANUP_AGE_SECONDS:
                    try:
                        f.unlink()
                    except OSError:
                        pass
        except Exception:
            pass
        time.sleep(CLEANUP_INTERVAL_SECONDS)


threading.Thread(target=_cleanup_loop, daemon=True).start()


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/generate", methods=["POST"])
def generate():
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "Pehle kuch type karein"}), 400

    voice = data.get("voice") or {}
    custom_desc = (voice.get("custom") or "").strip()
    if custom_desc:
        description = custom_desc
    else:
        description = build_description(
            speaker=voice.get("speaker", "rohit"),
            speed=voice.get("speed", "moderate"),
            pitch=voice.get("pitch", "low"),
            expressivity=voice.get("expressivity", "expressive"),
        )

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
