"""Visual understanding with Qwen3.5-0.8B (unsloth GGUF) via llama-cpp-python.

Qwen3.5-0.8B is a small VLM (Gated DeltaNet + Gated Attention hybrid, vision
encoder included). It describes the image AND transcribes any on-screen text
(multilingual) in one pass. No torch: inference goes through GGUF weights
(Q8_0) via the llama-cpp-python mtmd / libmtmd backend, on the GPU when the
installed build supports it (n_gpu_layers=-1, vision encoder included) and
on CPU otherwise. Thinking is OFF by default for this 0.8B model, so no
extra toggle is needed.

Inference runs in a worker subprocess (_vlm_worker.py): a GPU crash
(SIGSEGV) must not take down scry, and a crashed GPU attempt is retried
once on CPU. Use --cpu (or SCRY_VLM_GPU=0) to skip the GPU attempt.

One-time setup:
    scry setup --vision    (precompiled llama-cpp-python wheel for your
                            accelerator + ~1.1 GB model in ~/.cache/scry)

Model: unsloth/Qwen3.5-0.8B-GGUF (Qwen3.5-0.8B-Q8_0 + mmproj-F16).
"""
from __future__ import annotations

import os
from pathlib import Path

from .common import MODELS_DIR, extract_frames, log

MODEL_REPO = "unsloth/Qwen3.5-0.8B-GGUF"
MODEL_FILE = "Qwen3.5-0.8B-Q8_0.gguf"
MMPROJ_FILE = "mmproj-F16.gguf"
MODEL_LABEL = "Qwen3.5-0.8B (Q8_0)"

# Qwen3.5-0.8B ha il thinking OFF di default (a differenza dei modelli 27B+
# usati altrove nel progetto). Nessun chat_template_kwargs necessario: il
# default e' gia' quello che vogliamo, letto dal template Jinja embedded nel
# GGUF via MTMDChatHandler. Se in futuro si aggiorna il GGUF e il default
# cambiasse, il modo per forzarlo sarebbe iniettare l'istruzione nel
# SYSTEM_PROMPT, dato che Qwen3.5 non supporta il soft-switch /no_think.
SYSTEM_PROMPT = (
    "Describe this image, then transcribe all readable text in it verbatim. "
    "No markdown. No preamble. No translation of the text. No guesswork."
)

MAX_TOKENS = 512

def model_dir() -> Path:
    return MODELS_DIR / "qwen3.5-0.8b"


def setup_vision() -> None:
    """Download model + vision projector (one-time, ~2.1 GB)."""
    from huggingface_hub import hf_hub_download

    d = model_dir()
    d.mkdir(parents=True, exist_ok=True)
    for f in (MODEL_FILE, MMPROJ_FILE):
        dest = d / f
        if dest.exists() and dest.stat().st_size > 100_000:
            log(f"Vision: {f} already present")
            continue
        log(f"Vision: downloading {f} from {MODEL_REPO} ...")
        hf_hub_download(repo_id=MODEL_REPO, filename=f, local_dir=str(d))
    log(f"Vision: model ready in {d}")


def vision_available() -> bool:
    """True if the [vision] deps are installed and the model is downloaded."""
    d = model_dir()
    if not (d / MODEL_FILE).exists() or not (d / MMPROJ_FILE).exists():
        return False
    try:
        import llama_cpp  # noqa: F401
        from llama_cpp.llama_chat_format import MTMDChatHandler  # noqa: F401
        return True
    except ImportError:
        return False


def _run_vlm(image_path: str, use_gpu: bool) -> dict:
    """Run one VLM inference in a worker subprocess (see _vlm_worker.py).

    A GPU crash (SIGSEGV) kills only the worker: the exit code is anything
    other than 0 (success) or 1 (handled error), in which case the same
    image is retried once on CPU. Handled errors (exit 1) are reported
    as-is, without a retry.
    """
    import json
    import subprocess
    import sys

    def spawn(gpu: bool) -> subprocess.CompletedProcess:
        cmd = [sys.executable, "-m", "scry._vlm_worker", image_path,
               "--gpu" if gpu else "--cpu"]
        try:
            return subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=900)
        except subprocess.TimeoutExpired:
            log(f"Vision: worker timed out after 900s ({'GPU' if gpu else 'CPU'})")
            return subprocess.CompletedProcess(cmd, -15, "", "timeout")

    cp = spawn(use_gpu)
    if use_gpu and cp.returncode not in (0, 1):
        # crash (negative rc on Unix, arbitrary positive on Windows):
        # neutral message on purpose — the cause may be VRAM pressure, a
        # driver bug, or a wheel/backend bug; we just try CPU.
        log(f"Vision: GPU inference failed (exit {cp.returncode}); "
            "falling back to CPU for this image")
        cp = spawn(False)

    line = ""
    for l in reversed((cp.stdout or "").strip().splitlines()):
        if l.strip():
            line = l
            break
    try:
        data = json.loads(line) if line else {}
    except json.JSONDecodeError:
        data = {}
    if cp.returncode != 0:
        err = data.get("error")
        if not err:
            tail = [l for l in (cp.stderr or "").strip().splitlines() if l.strip()]
            err = tail[-1] if tail else f"worker exited with code {cp.returncode}"
        return {"text": "", "time_s": 0.0, "error": f"VLM worker failed: {err}"}
    return {"text": data.get("text", ""), "time_s": data.get("time_s", 0.0)}


def describe_image(image_path: str, gpu: bool | None = None) -> dict:
    """Run the VLM on one image -> {"text": ..., "time_s": ...}.

    gpu: whether to try the GPU. None = auto (GPU, unless SCRY_VLM_GPU=0);
    True/False to force — the --cpu CLI flag passes False. A crashed GPU
    attempt is retried once on CPU automatically.
    """
    if gpu is None:
        gpu = os.environ.get("SCRY_VLM_GPU") != "0"
    return _run_vlm(image_path, use_gpu=gpu)


def describe_video(video_path: str, n_frames: int = 3,
                   gpu: bool | None = None) -> dict:
    """Sample frames from a video and describe each (dedup identical text).

    gpu: passed through to describe_image (see its docstring).
    """
    outdir = os.path.join(os.path.dirname(video_path) or ".", "frames")
    frames = extract_frames(video_path, outdir, n=n_frames)
    if not frames:
        return {"text": "", "frames": []}
    texts: list[str] = []
    items: list[dict] = []
    for i, f in enumerate(frames, 1):
        log(f"Vision: frame {i}/{len(frames)} ...")
        r = describe_image(f, gpu=gpu)
        items.append({"file": os.path.basename(f), "text": r["text"],
                      "time_s": r["time_s"]})
        if r["text"] and r["text"] not in texts:
            texts.append(r["text"])
    return {"text": "\n\n".join(texts), "frames": items}
