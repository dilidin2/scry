"""scry: let an LLM "see" TikTok and Instagram.

CPU-only pipeline: metadata + media download + STT (faster-whisper) +
top comments with a likes-based consensus score. Media files are
saved locally and listed in the output. An optional local VLM
(Qwen3.5-0.8B, [vision] extra, -v flag) adds visual descriptions for
models without their own vision.
"""
__version__ = "0.1.0"
