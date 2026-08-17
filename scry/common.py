"""scry: shared utilities (HTTP sessions, link parsing, output, media).

Everything runs on CPU. Sessions use curl_cffi with TLS impersonation
("chrome136") for direct fetches; the browser tier (Camoufox, Instagram
only) lives in browser.py.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import sys
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
# MODELS_DIR: model weights (whisper, LFM2.5-VL). PERSISTENT: lives in the
#             user cache (~/.cache/scry by default) so it survives reinstalls
#             and reboots. Override: SCRY_CACHE_DIR.
# DATA_DIR: per-run data (downloads/ + output/). EPHEMERAL by default in
#           /tmp/scry — on Linux with systemd /tmp is cleared on reboot
#           (not guaranteed everywhere: with SCRY_CLEAN=1 the tool wipes
#           stale run data explicitly at startup).
#           Override: SCRY_DATA_DIR to keep data deliberately elsewhere.
CACHE_DIR = Path(os.environ.get("SCRY_CACHE_DIR", Path.home() / ".cache" / "scry"))
MODELS_DIR = CACHE_DIR / "models"
DEFAULT_DATA_DIR = Path(tempfile.gettempdir()) / "scry"
DATA_DIR = Path(os.environ.get("SCRY_DATA_DIR", DEFAULT_DATA_DIR))
DOWNLOADS_DIR = DATA_DIR / "downloads"
OUTPUT_DIR = DATA_DIR / "output"

if os.environ.get("SCRY_CLEAN") == "1":
    for d in (DOWNLOADS_DIR, OUTPUT_DIR):
        if d.exists():
            shutil.rmtree(d)
for d in (DOWNLOADS_DIR, OUTPUT_DIR, MODELS_DIR):
    d.mkdir(parents=True, exist_ok=True)


def data_note() -> str:
    """Runtime note about where (and how long) this run's data lives."""
    if DATA_DIR == DEFAULT_DATA_DIR:
        return (f"run data: {DATA_DIR} — ephemeral: lost on reboot on most "
                f"systems; copy it elsewhere or set SCRY_DATA_DIR to keep it")
    return f"run data: {DATA_DIR} (persistent, SCRY_DATA_DIR)"

# Optional cookies (Netscape): export from a browser if content requires login
COOKIES_FILE = os.environ.get("SCRY_COOKIES")  # path or None

COOKIE_CONFIG_DIR = Path.home() / ".config" / "scry"


def default_cookies(platform: str) -> str | None:
    """Resolve the cookie file for a platform.

    Priority: SCRY_COOKIES_<PLAT> > SCRY_COOKIES
              > ./<platform>_cookies.txt > ./cookies.txt
              > ~/.config/scry/<platform>_cookies.txt
              > ~/.config/scry/cookies.txt
    Returns None if no file exists.
    """
    env = os.environ.get(f"SCRY_COOKIES_{platform.upper()}") or COOKIES_FILE
    if env:
        return env
    for base in (Path.cwd(), COOKIE_CONFIG_DIR):
        for name in (f"{platform}_cookies.txt", "cookies.txt"):
            p = base / name
            if p.exists():
                return str(p)
    return None

CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# TLS impersonation used for direct fetches.
# NOTE: in curl_cffi 0.15.x the generic "chrome" alias maps to an older
# version whose fingerprint TikTok blocks ("Site Maintenance").
# "chrome136" (and safari/firefox/edge) pass fine.
IMPERSONATE = os.environ.get("SCRY_IMPERSONATE", "chrome136")


# ---------------------------------------------------------------------------
# Link recognition
# ---------------------------------------------------------------------------
def classify_url(url: str) -> dict:
    """Recognize a URL and extract the content id.

    Returns dict {platform, kind, id, canonical} or {} if not recognized.
    """
    url = url.strip()
    p = urlparse(url)
    host = p.netloc.lower().removeprefix("www.").removeprefix("m.")

    if host in ("tiktok.com", "vm.tiktok.com", "vt.tiktok.com", "www.tiktok.com"):
        m = re.search(r"/(?:video|v)/(\d+)", p.path)
        if m:
            vid = m.group(1)
            # Preserve the full path with @username when present: a bare
            # /video/<id> URL returns 404 on TikTok (verified 2026-08).
            # Share links look like /@user/video/<id>?is_from_webapp=1.
            has_user = bool(re.search(r"/@[^/]+/", p.path))
            path = p.path if has_user else f"/video/{vid}"
            return {"platform": "tiktok", "kind": "video", "id": vid,
                    "canonical": f"https://www.tiktok.com{path}",
                    "has_user": has_user}
        if host in ("vm.tiktok.com", "vt.tiktok.com") or p.path.startswith("/t/"):
            return {"platform": "tiktok", "kind": "share", "id": None, "canonical": url}
        # embed / other tiktok paths
        m = re.search(r"embed/v?(\d+)", p.path)
        if m:
            return {"platform": "tiktok", "kind": "video", "id": m.group(1),
                    "canonical": f"https://www.tiktok.com/video/{m.group(1)}"}
        return {}

    if host in ("instagram.com", "www.instagram.com"):
        m = re.match(r"/(p|reels?|tv)/([A-Za-z0-9_-]+)/?", p.path)
        if m:
            kind = "reel" if m.group(1).startswith("reel") else m.group(1)
            return {"platform": "instagram", "kind": kind,
                    "id": m.group(2),
                    "canonical": f"https://www.instagram.com/{kind}/{m.group(2)}/"}
        return {}

    return {}


def resolve_share_url(session, url: str) -> str | None:
    """Resolve a tiktok short link (vm.tiktok.com/...) to the canonical URL.

    Returns None if unresolvable (e.g. blocked IP).
    """
    try:
        r = session.get(url, impersonate=IMPERSONATE, allow_redirects=True, timeout=20)
        m = re.search(r"/(?:video|v)/(\d+)", str(r.url))
        if m:
            return f"https://www.tiktok.com/video/{m.group(1)}"
        return str(r.url) if "tiktok.com" in str(r.url) else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# HTTP sessions
# ---------------------------------------------------------------------------
def make_session() -> "requests.Session":
    """curl_cffi session with chrome impersonation.

    curl_cffi reproduces the TLS/HTTP2 fingerprint of a real browser:
    the modern, lightweight way to pass walls that block 'suspicious'
    clients (requests/httpx) without using a browser.
    """
    from curl_cffi import requests
    return requests.Session(impersonate=IMPERSONATE)


def add_netscape_cookies(session, path: str) -> int:
    """Load a Netscape cookies file into the curl_cffi session.

    Returns how many cookies were loaded. Format: as exported by
    extensions like 'Get cookies.txt LOCALLY' (one line per cookie,
    tab-separated, 7 fields).
    """
    n = 0
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 7:
                continue
            domain, _, path_c, secure, expires, name, value = parts[:7]
            try:
                expires_i = int(expires)
            except ValueError:
                expires_i = 0
            # curl_cffi: cookie(name, value, domain=..., path=...)
            try:
                session.cookies.set(name, value, domain=domain, path=path_c or "/")
                n += 1
            except Exception:
                continue
    return n


# ---------------------------------------------------------------------------
# External processes
# ---------------------------------------------------------------------------
def run_cmd(cmd: list[str], timeout: int = 600, cwd: str | None = None) -> tuple[int, str, str]:
    """Run a command, return (rc, stdout, stderr)."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
        return p.returncode, p.stdout or "", p.stderr or ""
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except FileNotFoundError as e:
        return 127, "", str(e)


def video_duration(path: str) -> float:
    rc, out, _ = run_cmd(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "default=nw=1:nk=1", path], timeout=30)
    try:
        return float(out.strip())
    except ValueError:
        return 0.0


def extract_audio(video_path: str, wav_path: str) -> bool:
    """Video -> 16kHz mono WAV (ideal format for Whisper)."""
    rc, _, err = run_cmd(
        ["ffmpeg", "-y", "-v", "error", "-i", video_path,
         "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", wav_path],
        timeout=300)
    return rc == 0 and Path(wav_path).exists()


def extract_frames(video_path: str, outdir: str, n: int = 3) -> list[str]:
    """Extract n frames spread across the video (avoiding 0% and 100%)."""
    dur = video_duration(video_path)
    if dur <= 0:
        return []
    frames = []
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        t = dur * (i + 1) / (n + 1)
        out = outdir / f"frame_{i+1}.jpg"
        rc, _, _ = run_cmd(
            ["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.2f}", "-i", video_path,
             "-frames:v", "1", "-q:v", "3", str(out)], timeout=120)
        if rc == 0 and out.exists():
            frames.append(str(out))
    return frames


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
def slug(*parts) -> str:
    s = "-".join(str(p) for p in parts if p)
    return re.sub(r"[^A-Za-z0-9_-]+", "_", s)[:80]


def save_outputs(name: str, markdown: str, data: dict) -> dict[str, str]:
    """Save markdown + json outputs to OUTPUT_DIR, return the paths."""
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = OUTPUT_DIR / f"{ts}-{slug(name)}"
    md_path = Path(str(base) + ".md")
    json_path = Path(str(base) + ".json")
    md_path.write_text(markdown, encoding="utf-8")
    json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"markdown": str(md_path), "json": str(json_path)}


def fmt(n) -> str:
    """1234 -> '1.2k', 1234567 -> '1.2M'."""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return str(n)
    for div, suf in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "k")):
        if abs(n) >= div:
            v = n / div
            return f"{v:.1f}{suf}" if v < 10 else f"{v:.0f}{suf}"
    return str(int(n))


def fmt_ts(epoch) -> str:
    try:
        return datetime.fromtimestamp(int(epoch), tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except (TypeError, ValueError, OSError):
        return "?"


def ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def log(msg: str) -> None:
    """Progress line -> stderr, so stdout stays clean (--json = JSON only)."""
    print(f"[{ts()}] {msg}", file=sys.stderr, flush=True)
