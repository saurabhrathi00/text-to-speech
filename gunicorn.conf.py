import multiprocessing
import os

bind = f"0.0.0.0:{os.getenv('PORT', '5000')}"
# Single worker on purpose: job progress, the rate-limiter buckets and
# model-warmup state all live in per-process memory. With workers > 1 a
# /generate job created on one worker becomes unpollable from another
# (~50% of polls hit the wrong process → the user never receives audio
# they were billed for). Concurrency comes from threads instead. Move
# this state to Redis/DB before going multi-worker.
workers = 1
threads = 8
timeout = 300
worker_class = "gthread"
