import os
import time
import uuid
import requests

COMFY_HOST = os.getenv("COMFY_HOST", "http://127.0.0.1:8188")
COMFY_MODEL = os.getenv("COMFY_MODEL", "sd_xl_base_1.0.safetensors")


class ComfyError(Exception):
    pass


def is_configured() -> bool:
    """Quick health check — is ComfyUI reachable?"""
    try:
        r = requests.get(f"{COMFY_HOST}/system_stats", timeout=3)
        return r.status_code == 200
    except requests.RequestException:
        return False


def _build_workflow(prompt: str, negative: str, width: int, height: int,
                     steps: int, seed: int, cfg: float) -> dict:
    """Minimal SDXL text-to-image workflow in ComfyUI's API format."""
    return {
        "3": {
            "class_type": "KSampler",
            "inputs": {
                "seed": seed,
                "steps": steps,
                "cfg": cfg,
                "sampler_name": "euler",
                "scheduler": "normal",
                "denoise": 1.0,
                "model": ["4", 0],
                "positive": ["6", 0],
                "negative": ["7", 0],
                "latent_image": ["5", 0],
            },
        },
        "4": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": COMFY_MODEL},
        },
        "5": {
            "class_type": "EmptyLatentImage",
            "inputs": {"width": width, "height": height, "batch_size": 1},
        },
        "6": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": prompt, "clip": ["4", 1]},
        },
        "7": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": negative, "clip": ["4", 1]},
        },
        "8": {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["3", 0], "vae": ["4", 2]},
        },
        "9": {
            "class_type": "SaveImage",
            "inputs": {"filename_prefix": "tts_app", "images": ["8", 0]},
        },
    }


def generate(prompt: str, negative: str = "",
              width: int = 1024, height: int = 1024,
              steps: int = 20, seed: int | None = None,
              cfg: float = 7.5, poll_timeout: int = 300) -> bytes:
    """Generate an image via ComfyUI's HTTP API. Returns PNG bytes.

    Submits a workflow, polls history endpoint until the prompt finishes,
    then fetches the generated image from /view.
    """
    if seed is None:
        seed = int(time.time())

    workflow = _build_workflow(prompt, negative, width, height, steps, seed, cfg)
    client_id = uuid.uuid4().hex

    t0 = time.time()
    print(f"[comfy] submit prompt → {prompt[:80]!r} ({width}x{height}, {steps} steps)")
    try:
        r = requests.post(
            f"{COMFY_HOST}/prompt",
            json={"prompt": workflow, "client_id": client_id},
            timeout=10,
        )
        r.raise_for_status()
    except requests.RequestException as e:
        raise ComfyError(f"submit failed: {e}") from e

    body = r.json()
    if "prompt_id" not in body:
        raise ComfyError(f"unexpected submit response: {body}")
    prompt_id = body["prompt_id"]

    deadline = time.time() + poll_timeout
    while time.time() < deadline:
        time.sleep(1)
        try:
            h = requests.get(f"{COMFY_HOST}/history/{prompt_id}", timeout=5)
        except requests.RequestException:
            continue
        if h.status_code != 200:
            continue
        history = h.json()
        if prompt_id not in history:
            continue
        entry = history[prompt_id]
        if entry.get("status", {}).get("status_str") == "error":
            err = entry.get("status", {}).get("messages", [])
            raise ComfyError(f"workflow failed: {err}")
        outputs = entry.get("outputs", {})
        for node_id, output in outputs.items():
            images = output.get("images") or []
            if not images:
                continue
            meta = images[0]
            params = {
                "filename": meta["filename"],
                "subfolder": meta.get("subfolder", ""),
                "type": meta.get("type", "output"),
            }
            try:
                img_resp = requests.get(f"{COMFY_HOST}/view", params=params, timeout=10)
                img_resp.raise_for_status()
            except requests.RequestException as e:
                raise ComfyError(f"fetch image failed: {e}") from e
            print(f"[comfy] generated in {time.time() - t0:.1f}s → {meta['filename']}")
            return img_resp.content
    raise ComfyError(f"timed out after {poll_timeout}s waiting for prompt {prompt_id}")
