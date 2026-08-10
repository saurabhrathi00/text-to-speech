# SastaSpeech — cloud profile (HTTP-only providers: ElevenLabs + Gemini).
# No torch/transformers here; heavy ML lives in requirements-local.txt and
# is only needed for the local/admin box (Parler/Bark/Whisper).
FROM python:3.12-slim

# libsndfile1 is a runtime dependency of the `soundfile` package (WAV I/O).
RUN apt-get update \
    && apt-get install -y --no-install-recommends libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install deps first so the layer caches across code changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code.
COPY . .

# gunicorn.conf.py binds 0.0.0.0:$PORT with a single worker (in-memory
# progress/rate-limit state) + threads for concurrency. Caddy reaches this
# container by name over the shared docker network — no host port published.
ENV PORT=8001 \
    HOST=0.0.0.0 \
    PYTHONUNBUFFERED=1

EXPOSE 8001

CMD ["gunicorn", "app:app", "--config", "gunicorn.conf.py"]
