"""Visual understanding with Qwen3.5-0.8B (unsloth GGUF) via llama-cpp-python.

Qwen3.5-0.8B is a small VLM (Gated DeltaNet + Gated Attention hybrid, vision
encoder included). It describes the image AND transcribes any on-screen text
(multilingual) in one CPU-only pass. No torch: inference goes through GGUF
weights (Q8_0) via the llama-cpp-python mtmd / libmtmd backend. Thinking is
OFF by default for this 0.8B model, so no extra toggle is needed.

Optional extra:
    pip install "scry-social[vision]"
One-time setup:
    scry setup --vision    (downloads ~1.1 GB to ~/.cache/scry/models)

Model: unsloth/Qwen3.5-0.8B-GGUF (Qwen3.5-0.8B-Q8_0 + mmproj-F16).
"""
from __future__ import annotations

import os
import time
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

_llm = None
_load_error: str | None = None


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
    """True if the [vision] extra is installed and the model is downloaded."""
    if _load_error is not None:
        return False
    d = model_dir()
    if not (d / MODEL_FILE).exists() or not (d / MMPROJ_FILE).exists():
        return False
    try:
        import llama_cpp  # noqa: F401
        from llama_cpp.llama_chat_format import MTMDChatHandler  # noqa: F401
        return True
    except ImportError:
        return False


def _get_llm():
    """Lazy singleton: load LFM2.5-VL once per process."""
    global _llm, _load_error
    if _llm is None and _load_error is None:
        try:
            from llama_cpp import Llama
            from llama_cpp.llama_chat_format import MTMDChatHandler

            d = model_dir()
            t0 = time.time()
            handler = MTMDChatHandler(
                clip_model_path=str(d / MMPROJ_FILE),
                verbose=False,
                use_gpu=False,
            )
            _llm = Llama(
                model_path=str(d / MODEL_FILE),
                chat_handler=handler,
                n_ctx=4096,
                verbose=False,
            )
            log(f"Vision: {MODEL_LABEL} loaded in {time.time() - t0:.1f}s")
        except Exception as e:
            _load_error = str(e)
            log(f"Vision: failed to load model: {e}")
    return _llm


def describe_image(image_path: str) -> dict:
    """Run the VLM on one image -> {"text": ..., "time_s": ...}."""
    llm = _get_llm()
    if llm is None:
        return {"text": "", "time_s": 0.0, "error": _load_error}
    t0 = time.time()
    url = "file://" + os.path.abspath(image_path)
    from llama_cpp._utils import suppress_stdout_stderr
    with suppress_stdout_stderr(disable=False):
        resp = llm.create_chat_completion(
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": url}},
                    {"type": "text", "text": "Describe this image."},
                ]},
            ],
            temperature=0.7,
            top_p=0.80,
            top_k=20,
            min_p=0.0,
            presence_penalty=1.5,
            max_tokens=MAX_TOKENS,
        )
    return {
        "text": (resp["choices"][0]["message"]["content"] or "").strip(),
        "time_s": round(time.time() - t0, 1),
    }


def describe_video(video_path: str, n_frames: int = 3) -> dict:
    """Sample frames from a video and describe each (dedup identical text)."""
    outdir = os.path.join(os.path.dirname(video_path) or ".", "frames")
    frames = extract_frames(video_path, outdir, n=n_frames)
    if not frames:
        return {"text": "", "frames": []}
    texts: list[str] = []
    items: list[dict] = []
    for i, f in enumerate(frames, 1):
        log(f"Vision: frame {i}/{len(frames)} ...")
        r = describe_image(f)
        items.append({"file": os.path.basename(f), "text": r["text"],
                      "time_s": r["time_s"]})
        if r["text"] and r["text"] not in texts:
            texts.append(r["text"])
    return {"text": "\n\n".join(texts), "frames": items}
