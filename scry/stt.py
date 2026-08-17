"""STT (speech-to-text) on CPU with faster-whisper (CTranslate2, int8).

Design choices (benchmarked on Ryzen 7 7800X3D, 16 cores, CPU-only):
- faster-whisper int8: 'base' ~35x realtime, 'small' ~15x realtime
- 'small' is the default: good accuracy/speed tradeoff, multilingual
  (EN WER ~3.4%, acceptable handling of Italian/Spanish)
- Integrated Silero VAD: skips silence, speeds up music-heavy videos
Models are downloaded on first use into MODELS_DIR (portable cache).
"""
from __future__ import annotations

import os
import time

from .common import MODELS_DIR, log

_models: dict = {}


def get_model(name: str = "small", compute_type: str = "int8", threads: int | None = None):
    """Load (once) a Whisper model. name: tiny|base|small|medium|large-v3."""
    key = (name, compute_type)
    if key not in _models:
        from faster_whisper import WhisperModel
        t0 = time.time()
        _models[key] = WhisperModel(
            name,
            device="cpu",
            compute_type=compute_type,
            download_root=str(MODELS_DIR / "whisper"),
            cpu_threads=threads or os.cpu_count(),
        )
        log(f"STT: model '{name}' loaded in {time.time()-t0:.1f}s")
    return _models[key]


def transcribe(wav_path: str, model: str = "small", language: str | None = None,
               vad: bool = True) -> dict:
    """Transcribe a 16kHz WAV. Returns dict with text, language, timing, segments."""
    import sys
    from .common import video_duration  # for the realtime factor (works on wav too)

    t0 = time.time()
    m = get_model(model)
    t_load = time.time() - t0  # model load (first call per process); kept out of the realtime factor
    segments, info = m.transcribe(
        wav_path,
        language=language,          # None = auto-detect
        vad_filter=vad,
        vad_parameters={"min_silence_duration_ms": 400},
    )
    segs = []
    for s in segments:  # consumes the lazy iterator
        segs.append({"start": round(s.start, 1), "end": round(s.end, 1), "text": s.text.strip()})
        print(f"  [{s.start:6.1f}-{s.end:6.1f}] {s.text.strip()}", file=sys.stderr, flush=True)
    text = " ".join(s["text"] for s in segs).strip()
    dur = video_duration(wav_path)
    took = time.time() - t0 - t_load  # transcription only, excludes model load
    return {
        "text": text,
        "language": info.language,
        "language_probability": round(info.language_probability, 2),
        "audio_duration_s": round(dur, 1),
        "processing_time_s": round(took, 1),
        "realtime_factor": round(dur / took, 1) if took > 0 else None,
        "model_load_s": round(t_load, 1),
        "model": model,
        "segments": segs,
    }
