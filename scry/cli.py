"""scry: let an LLM "see" TikTok and Instagram (CPU-only pipeline).

Usage:
  scry tiktok <url> [options]
  scry instagram <url> [options]
  scry auto <url> [options]       (detects the platform automatically)
  scry setup                      (one-time: downloads the Camoufox browser)
  scry setup -v                   (one-time: vision stack — installs the
                                   matching precompiled llama-cpp-python wheel
                                   (CUDA/ROCm/Metal, or CPU) + Qwen3.5-0.8B
                                   model, ~1.1GB)
  scry setup -v --backend NAME    (auto|cuda|rocm|metal|cpu, default auto)
  scry setup --all                (both)
  scry skill                      (show the bundled agent skill)
  scry skill --path DIR           (install it into an agent skills directory)

Common options:
  --max-comments N     comments to analyze (default 30)
  --no-comments        skip comments+consensus
  --stt-model NAME     whisper: base|small|medium|large-v3 (default small)
  --language CODE      force STT language (default: auto-detect)
  --no-stt             skip transcription
  -v, --vision         run the local VLM visual analysis (instagram; needs
                       the [vision] extra; default: off)
  --no-download        skip media download (metadata+comments only)
  --cookies FILE       Netscape cookies file (for content that requires login)
  --json               print only the JSON to stdout
"""
from __future__ import annotations

import argparse
import json
import sys
import time


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="scry", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("tiktok", "instagram", "auto"):
        sp = sub.add_parser(name, help=f"{name} pipeline")
        sp.add_argument("url")
        _add_opts(sp)
    for name in ("instagram", "auto"):
        sub.choices[name].add_argument("--no-browser",
            action="store_true",
            help="do not use the browser fallback (Camoufox)")
        sub.choices[name].add_argument("--headless",
            action="store_true",
            help="browser without a window (more detectable, only if needed)")
    sp = sub.add_parser("setup",
                        help="one-time setup: download the Camoufox browser "
                             "and/or the vision stack")
    sp.add_argument("-v", "--vision", action="store_true",
                    help="install the vision stack: llama-cpp-python wheel for "
                         "your GPU (precompiled; no C compiler needed) + the "
                         "Qwen3.5-0.8B Q8_0 model (~1.1GB)")
    sp.add_argument("--all", action="store_true",
                    help="download both browser and vision model")
    sp.add_argument("--backend", default="auto",
                    choices=["auto", "cuda", "rocm", "metal", "cpu"],
                    help="which llama-cpp-python build for --vision: auto "
                         "(detect), cuda, rocm, metal, or cpu (build from "
                         "source; needs a C compiler) (default: auto)")
    sp = sub.add_parser("skill",
                        help="show or install the agent skill (SKILL.md)")
    sp.add_argument("--path", default=None,
                    help="install into DIR: copies to DIR/scry/SKILL.md")
    return p


def _add_opts(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("--max-comments", type=int, default=30)
    sp.add_argument("--no-comments", action="store_true")
    sp.add_argument("--stt-model", default="small",
                    choices=["tiny", "base", "small", "medium", "large-v3"])
    sp.add_argument("--language", default=None, help="e.g. it, en (default: auto)")
    sp.add_argument("--no-stt", action="store_true")
    sp.add_argument("-v", "--vision", action="store_true",
                    help="local VLM visual analysis (needs the [vision] extra; "
                         "default: off)")
    sp.add_argument("--cpu", action="store_true",
                    help="with -v: run the VLM on CPU (skip the GPU attempt; "
                         "equivalent to SCRY_VLM_GPU=0)")
    sp.add_argument("--no-download", action="store_true")
    sp.add_argument("--cookies", default=None, help="Netscape cookies file")
    sp.add_argument("--json", action="store_true", help="JSON only on stdout")


def main() -> int:
    args = build_parser().parse_args()
    cmd = args.cmd

    if cmd == "skill":
        from pathlib import Path
        src = Path(__file__).parent / "SKILL.md"
        if not src.exists():
            print("scry: bundled SKILL.md not found in the package", file=sys.stderr)
            return 1
        if getattr(args, "path", None):
            import shutil
            dest = Path(args.path).expanduser() / "scry"
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest / "SKILL.md")
            print(f"Skill installed: {dest / 'SKILL.md'}")
            print("Make sure your agent harness scans that directory for skills")
            print("(cross-harness standard: ~/.agents/skills).")
        else:
            print(f"Bundled skill file: {src}")
            print()
            print("Install it into your agent's skills directory, e.g.:")
            print("  scry skill --path ~/.agents/skills     (cross-harness standard)")
            print("  scry skill --path ~/.claude/skills     (Claude Code)")
            print("  scry skill --path ~/.codex/skills      (OpenAI Codex)")
            print("  scry skill --path ~/.pi/agent/skills   (pi)")
        return 0

    if cmd == "setup":
        import subprocess
        from .common import log
        want_browser = not (args.vision and not args.all)
        want_vision = args.vision or args.all
        if not want_browser and not want_vision:
            want_browser = True  # bare `scry setup` = browser (legacy behavior)
        rc = 0
        if want_browser:
            log("Setup: downloading the Camoufox browser (one-time, ~150MB)...")
            t0 = time.time()
            try:
                subprocess.run([sys.executable, "-m", "camoufox", "fetch"],
                               check=True)
            except subprocess.CalledProcessError as e:
                log(f"Setup: browser fetch failed ({e})")
                rc = 1
            log(f"Setup: browser done in {time.time()-t0:.0f}s")
        if want_vision:
            t0 = time.time()
            try:
                from . import gpu
                gpu.setup_runtime(backend=args.backend)
                from .vision import setup_vision
                setup_vision()
            except Exception as e:
                log(f"Setup: vision setup failed ({e})")
                rc = 1
            else:
                log(f"Setup: vision done in {time.time()-t0:.0f}s")
        return rc

    platform = cmd

    if platform == "auto":
        from .common import classify_url
        info = classify_url(args.url)
        if not info:
            print(json.dumps({"error": f"Unrecognized URL: {args.url}"}), file=sys.stderr)
            return 2
        platform = info["platform"]

    kwargs = dict(
        do_stt=not args.no_stt,
        do_comments=not args.no_comments,
        max_comments=args.max_comments,
        stt_model=args.stt_model,
        language=args.language,
        cookies=args.cookies,
    )

    kwargs["vlm_cpu"] = args.cpu

    if platform == "tiktok":
        if args.vision:
            from .common import log
            log("TikTok: vision is planned but not implemented yet - "
                "running without the VLM (media files are still listed "
                "for your own vision)")
        from .tiktok import process
        kwargs["do_download"] = not args.no_download
        result, md = process(args.url, **kwargs)
    else:
        from .instagram import process
        kwargs["do_vision"] = args.vision
        kwargs["do_download"] = not args.no_download
        kwargs["use_browser"] = not getattr(args, "no_browser", False)
        kwargs["headless"] = getattr(args, "headless", False)
        result, md = process(args.url, **kwargs)

    from .common import save_outputs, data_note
    paths = save_outputs(f"{platform}-{result.get('id') or result.get('shortcode')}",
                         md, result)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(md)
        print(f"\n[saved: {paths['markdown']}]")
        print(f"[{data_note()}]")

    return 0 if not result.get("error") else 1


if __name__ == "__main__":
    sys.exit(main())
