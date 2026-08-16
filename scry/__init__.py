"""scry: let an LLM "see" TikTok and Instagram.

CPU-only pipeline: metadata + media download + STT (faster-whisper) +
VLM visual understanding (LFM2.5-VL, optional) + top comments with a
transparent consensus heuristic.
"""
__version__ = "0.1.0"
