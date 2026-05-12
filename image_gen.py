import os
import time
import uuid
import requests

COMFY_HOST = os.getenv("COMFY_HOST", "http://127.0.0.1:8188")
COMFY_MODEL = os.getenv("COMFY_MODEL", "sd_xl_base_1.0.safetensors")
IPADAPTER_PRESET = os.getenv("IPADAPTER_PRESET", "PLUS (high strength)")
IPADAPTER_WEIGHT = float(os.getenv("IPADAPTER_WEIGHT", "0.8"))


class ComfyError(Exception):
    pass


def is_configured() -> bool:
    """Quick health check — is ComfyUI reachable?"""
    try:
        r = requests.get(f"{COMFY_HOST}/system_stats", timeout=3)
        return r.status_code == 200
    except requests.RequestException:
        return False


def upload_reference(image_bytes: bytes, suggested_name: str = "anchor.png") -> str:
    """Upload an image to ComfyUI's input folder. Returns the actual
    filename ComfyUI assigned (may differ to avoid collisions)."""
    files = {"image": (suggested_name, image_bytes, "image/png")}
    data = {"type": "input", "overwrite": "true"}
    try:
        r = requests.post(f"{COMFY_HOST}/upload/image", files=files, data=data, timeout=15)
        r.raise_for_status()
    except requests.RequestException as e:
        raise ComfyError(f"upload failed: {e}") from e
    body = r.json()
    name = body.get("name") or suggested_name
    print(f"[comfy] uploaded reference → {name}")
    return name


def _build_workflow_ipadapter(prompt: str, negative: str, width: int, height: int,
                                steps: int, seed: int, cfg: float,
                                reference_filename: str,
                                weight: float = IPADAPTER_WEIGHT) -> dict:
    """SDXL workflow with IP-Adapter for character consistency.
    Loads a reference image, runs it through the IP-Adapter Unified
    Loader + IPAdapter node, then KSampler uses the modified model.
    """
    return {
        "4": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": COMFY_MODEL},
        },
        "10": {
            "class_type": "LoadImage",
            "inputs": {"image": reference_filename},
        },
        "11": {
            "class_type": "IPAdapterUnifiedLoader",
            "inputs": {"model": ["4", 0], "preset": IPADAPTER_PRESET},
        },
        "12": {
            "class_type": "IPAdapter",
            "inputs": {
                "model": ["11", 0],
                "ipadapter": ["11", 1],
                "image": ["10", 0],
                "weight": weight,
                "start_at": 0.0,
                "end_at": 1.0,
            },
        },
        "6": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": prompt, "clip": ["4", 1]},
        },
        "7": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": negative, "clip": ["4", 1]},
        },
        "5": {
            "class_type": "EmptyLatentImage",
            "inputs": {"width": width, "height": height, "batch_size": 1},
        },
        "3": {
            "class_type": "KSampler",
            "inputs": {
                "seed": seed,
                "steps": steps,
                "cfg": cfg,
                "sampler_name": "euler",
                "scheduler": "normal",
                "denoise": 1.0,
                "model": ["12", 0],
                "positive": ["6", 0],
                "negative": ["7", 0],
                "latent_image": ["5", 0],
            },
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
              cfg: float = 7.5, poll_timeout: int = 300,
              reference_filename: str | None = None,
              ipadapter_weight: float = IPADAPTER_WEIGHT) -> bytes:
    """Generate an image via ComfyUI's HTTP API. Returns PNG bytes.

    If reference_filename is provided, an IP-Adapter workflow is used
    so the output character matches that reference. The file must
    already exist in ComfyUI's input folder (use upload_reference()
    first).
    """
    if seed is None:
        seed = int(time.time())

    if reference_filename:
        workflow = _build_workflow_ipadapter(
            prompt, negative, width, height, steps, seed, cfg,
            reference_filename, weight=ipadapter_weight,
        )
        mode = f"ip-adapter (ref={reference_filename}, w={ipadapter_weight})"
    else:
        workflow = _build_workflow(prompt, negative, width, height, steps, seed, cfg)
        mode = "plain"
    client_id = uuid.uuid4().hex

    t0 = time.time()
    print(f"[comfy] submit prompt [{mode}] → {prompt[:80]!r} ({width}x{height}, {steps} steps)")
    try:
        r = requests.post(
            f"{COMFY_HOST}/prompt",
            json={"prompt": workflow, "client_id": client_id},
            timeout=10,
        )
    except requests.RequestException as e:
        raise ComfyError(f"submit failed: {e}") from e
    if r.status_code != 200:
        # ComfyUI returns the validation error in the body — surface it
        try:
            err_body = r.json()
            err_summary = err_body.get("error") or err_body
        except ValueError:
            err_summary = r.text[:500]
        print(f"[comfy] submit rejected with {r.status_code}: {err_summary}")
        raise ComfyError(f"submit rejected ({r.status_code}): {err_summary}")

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
