"""Precompiled llama-cpp-python wheels: GPU detection and installation.

PyPI ships only the llama-cpp-python sdist, which compiles ggml from source
(a CPU build; needs a C compiler). Precompiled wheels for the GPU backends
live on the upstream project's custom index:

    https://abetlen.github.io/llama-cpp-python/whl/<tag>/

pip extras cannot select an index (a `[extra]` only chooses *which*
dependencies to install, never *where* from), so `scry setup --vision` does
it: detect the accelerator (or take `--backend`), pick the matching wheel
tag, and pip-install the wheel. No C compiler needed on supported hardware.

Tags (re-verified against the upstream README when this module was written;
the index layout changes from time to time — if installs start failing,
check https://github.com/abetlen/llama-cpp-python/blob/master/README.md):

    cu118 cu121 cu122 cu123 cu124 cu125 cu130 cu132   NVIDIA CUDA
    rocm72                                             AMD ROCm (Linux)
    hip-radeon                                         AMD HIP (Windows)
    metal                                              Apple (macOS)
    vulkan                                             Linux/Windows
    cpu                                                basic CPU wheel

The CUDA/ROCm/Metal wheels are x86_64 Linux (ROCm/CUDA) or macOS (Metal)
only; on any other platform/Python pip transparently falls back to the
source build (this module warns about that).
"""
from __future__ import annotations

import platform
import shutil
import subprocess
import sys

from .common import log

WHEEL_INDEX_URL = "https://abetlen.github.io/llama-cpp-python/whl/{tag}"

BACKENDS = ("auto", "cuda", "rocm", "metal", "cpu")

# CUDA version reported by nvidia-smi -> wheel tag.
CUDA_TAGS = {
    "11.8": "cu118",
    "12.1": "cu121",
    "12.2": "cu122",
    "12.3": "cu123",
    "12.4": "cu124",
    "12.5": "cu125",
    "13.0": "cu130",
    "13.2": "cu132",
}
# Fallback when nvidia-smi reports no (or an unrecognized) CUDA version:
# a cu124 wheel loads on any CUDA >= 12.4 driver.
CUDA_FALLBACK_TAG = "cu124"

# Upstream publishes one ROCm wheel at a time (ROCm 7.2 as of
# llama-cpp-python 0.3.34). Update when a new one appears.
ROCM_TAG = "rocm72"

METAL_TAG = "metal"

# Prebuilt basic-CPU wheel: last resort when the source build fails
# (e.g. no C compiler). Slower than a tuned source build, but works.
CPU_WHEEL_TAG = "cpu"


def detect_backend() -> str:
    """Auto-detect the accelerator: metal (macOS), cuda (nvidia-smi),
    rocm (rocminfo/rocm-smi), or cpu (nothing found)."""
    if platform.system() == "Darwin":
        return "metal"
    if shutil.which("nvidia-smi"):
        return "cuda"
    if shutil.which("rocminfo") or shutil.which("rocm-smi"):
        return "rocm"
    return "cpu"


def cuda_driver_version() -> str | None:
    """Max CUDA version supported by the installed NVIDIA driver (nvidia-smi)."""
    try:
        out = subprocess.run(["nvidia-smi"], capture_output=True,
                             text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        if "CUDA Version:" in line:
            ver = line.split("CUDA Version:")[1].strip().split()[0]
            if ver[:1].isdigit():
                return ver
    return None


def wheel_tag(backend: str) -> str | None:
    """Wheel index tag for a backend (None for 'cpu' = build from source)."""
    if backend == "cuda":
        ver = cuda_driver_version()
        if ver is None:
            log(f"Setup: nvidia-smi reports no CUDA version; using the "
                f"{CUDA_FALLBACK_TAG} wheel (a recent NVIDIA driver is "
                "needed to load it)")
            return CUDA_FALLBACK_TAG
        tag = CUDA_TAGS.get(ver)
        if tag is None:
            log(f"Setup: no exact wheel for CUDA {ver}; falling back to "
                f"{CUDA_FALLBACK_TAG}")
        return tag or CUDA_FALLBACK_TAG
    if backend == "rocm":
        return ROCM_TAG
    if backend == "metal":
        return METAL_TAG
    return None


_PROBE = (
    "try:\n"
    "    import llama_cpp\n"
    "except ImportError:\n"
    "    print('absent')\n"
    "else:\n"
    "    try:\n"
    "        from llama_cpp import llama_supports_gpu_offload\n"
    "        print('gpu' if llama_supports_gpu_offload() else 'cpu')\n"
    "    except Exception:\n"
    "        print('cpu')\n"
)


def _llama_cpp_state() -> str:
    """'absent' | 'cpu' | 'gpu' for the llama-cpp-python installed in this
    environment. Runs in a subprocess: importing a GPU build can hard-crash
    (SIGSEGV) on broken drivers, which would otherwise take scry down."""
    try:
        out = subprocess.run([sys.executable, "-c", _PROBE],
                             capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.SubprocessError):
        return "cpu"  # probe failed; assume something is installed
    lines = [l.strip() for l in (out.stdout or "").splitlines() if l.strip()]
    return lines[-1] if lines and lines[-1] in ("absent", "cpu", "gpu") else "cpu"


def _module_missing(module: str) -> bool:
    import importlib
    try:
        importlib.import_module(module)
        return False
    except ImportError:
        return True


def _run(cmd: list[str]) -> None:
    log("Setup: " + " ".join(cmd))
    subprocess.check_call(cmd)


def _ensure_pip() -> None:
    """Make `python -m pip` usable (uv-managed venvs ship without pip)."""
    try:
        subprocess.run([sys.executable, "-m", "pip", "--version"],
                       check=True, capture_output=True, timeout=60)
        return
    except (subprocess.CalledProcessError, OSError):
        pass
    log("Setup: pip is not available in this environment; trying ensurepip ...")
    try:
        subprocess.check_call([sys.executable, "-m", "ensurepip", "--upgrade"])
    except (subprocess.CalledProcessError, OSError) as e:
        raise RuntimeError(
            "no usable pip in this environment (ensurepip failed too). "
            "Install the vision dependencies with your package manager "
            "instead: pip install \"scry-social[vision]\" "
            "(or: uv tool install \"scry-social[vision]\")") from e


def setup_runtime(backend: str = "auto") -> str:
    """Install the [vision] runtime for this machine.

    Picks the matching precompiled llama-cpp-python wheel (CUDA/ROCm/Metal)
    or builds it from source (CPU), installs huggingface-hub if missing.
    Returns the backend that was used.
    """
    if backend not in BACKENDS:
        raise ValueError(
            f"unknown backend {backend!r} (expected one of: {', '.join(BACKENDS)})")
    resolved = detect_backend() if backend == "auto" else backend
    log(f"Setup: vision backend: {resolved}"
        + (" (auto-detected)" if backend == "auto" else " (forced via --backend)"))

    _ensure_pip()
    tag = wheel_tag(resolved)
    state = _llama_cpp_state()

    if tag is not None and state != "gpu":
        log(f"Setup: installing llama-cpp-python from the precompiled {tag} "
            f"wheel (a few hundred MB to ~2GB, from {WHEEL_INDEX_URL.format(tag=tag)}) ...")
        if state == "cpu":
            log("Setup: (replacing the installed CPU build)")
        _run([sys.executable, "-m", "pip", "install",
              "--force-reinstall", "--no-cache-dir", "llama-cpp-python",
              "--extra-index-url", WHEEL_INDEX_URL.format(tag=tag)])
    elif tag is None and state == "absent":
        log("Setup: installing llama-cpp-python (CPU: build from source — "
            "needs a C compiler, a few minutes) ...")
        try:
            _run([sys.executable, "-m", "pip", "install", "llama-cpp-python"])
        except subprocess.CalledProcessError:
            log("Setup: source build failed (missing compiler?); retrying "
                f"with the prebuilt {CPU_WHEEL_TAG} wheel ...")
            _run([sys.executable, "-m", "pip", "install",
                  "--force-reinstall", "--no-cache-dir", "llama-cpp-python",
                  "--extra-index-url", WHEEL_INDEX_URL.format(tag=CPU_WHEEL_TAG)])
    else:
        what = "GPU build" if state == "gpu" else "CPU build"
        log(f"Setup: llama-cpp-python already installed ({what}); skipping")
        if tag is not None:
            log(f"Setup: to force the {tag} wheel anyway: pip install "
                "--force-reinstall --no-cache-dir llama-cpp-python "
                f"--extra-index-url {WHEEL_INDEX_URL.format(tag=tag)}")

    if _module_missing("huggingface_hub"):
        _run([sys.executable, "-m", "pip", "install", "huggingface-hub"])

    if tag is not None and _llama_cpp_state() != "gpu":
        log("Setup: WARNING: the installed llama-cpp-python does not report "
            "GPU support: either no precompiled wheel matched this "
            "platform/Python (pip fell back to a source build) or no GPU "
            "device is visible. For a CPU-only setup re-run with --backend cpu.")
    return resolved
