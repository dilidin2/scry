"""VLM worker: one Qwen3.5-0.8B inference in an isolated subprocess.

Why a subprocess: a SIGSEGV (e.g. the ggml OOM crash in
clip_image_batch_encode) kills the whole Python process — no in-process
try/except can catch it. Running inference in a child process turns such
crashes into an exit code the caller can react to: scry/vision.py then
retries the same image on CPU.

Usage (internal, not a public API):

    python -m scry._vlm_worker <image_path> --gpu
    python -m scry._vlm_worker <image_path> --cpu

Contract:
    stdout: exactly one JSON line, nothing else.
        {"text": "...", "time_s": 1.2}   on success
        {"error": "..."}                 on handled error
    exit code:
        0   success
        1   handled error (JSON above explains)
        anything else: the worker crashed (SIGSEGV & co.). Negative on
            Unix, arbitrary positive on Windows (e.g. 3221225477).

--gpu runs the LLM with n_gpu_layers=-1 (all layers) and the vision
encoder (mmproj) on the GPU as well. If the installed llama-cpp-python
build does not support GPU offload (CPU-only install, or no visible GPU
device), --gpu silently degrades to CPU: a "try the GPU" request must
still produce a result.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time


def _fail(msg: str) -> int:
    print(json.dumps({"error": msg}))
    return 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="scry._vlm_worker")
    p.add_argument("image", help="path to the image file")
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--gpu", dest="gpu", action="store_true",
                      help="offload to the GPU if this build supports it")
    mode.add_argument("--cpu", dest="gpu", action="store_false",
                      help="force CPU inference")
    a = p.parse_args(argv)

    if not os.path.isfile(a.image):
        return _fail(f"image not found: {a.image}")

    from scry.vision import (
        MAX_TOKENS, MODEL_FILE, MMPROJ_FILE, SYSTEM_PROMPT, model_dir,
    )

    try:
        from llama_cpp import Llama, llama_supports_gpu_offload
        from llama_cpp.llama_chat_format import MTMDChatHandler
    except ImportError as e:
        return _fail(f"llama-cpp-python not importable: {e}")

    use_gpu = a.gpu and llama_supports_gpu_offload()
    d = model_dir()
    t0 = time.time()
    try:
        handler = MTMDChatHandler(
            clip_model_path=str(d / MMPROJ_FILE),
            verbose=False,
            use_gpu=use_gpu,
        )
        llm = Llama(
            model_path=str(d / MODEL_FILE),
            chat_handler=handler,
            n_ctx=4096,
            n_gpu_layers=-1 if use_gpu else 0,
            verbose=False,
        )
    except Exception as e:
        return _fail(f"failed to load the VLM: {e}")

    try:
        from llama_cpp._utils import suppress_stdout_stderr
        with suppress_stdout_stderr(disable=False):
            resp = llm.create_chat_completion(
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": [
                        {"type": "image_url",
                         "image_url": {"url": "file://" + os.path.abspath(a.image)}},
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
        text = (resp["choices"][0]["message"]["content"] or "").strip()
    except Exception as e:
        return _fail(f"inference failed: {e}")

    print(json.dumps({"text": text, "time_s": round(time.time() - t0, 1)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
