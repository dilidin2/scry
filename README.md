# scry

Turn TikTok videos and Instagram posts (reels, photos, carousels) into
**LLM-readable intel**: audio transcripts (STT), text from images (OCR),
post metadata, and top comments with a transparent community-consensus
score — so an AI agent can understand what a post is, what it claims,
and how the community reacts to it.

Everything runs **CPU-only** (no GPU). A lightweight anti-detect browser
(Camoufox) is launched *only* for Instagram when the fast HTTP path is
not enough.

> **Personal-use tool.** This project is for occasional, personal
> research on a few links at a time — not for mass scraping, not for
> building datasets, not for automation at scale. See
> [Responsible use](#responsible-use--personal-use).

## Install

Requirements: Linux with `ffmpeg`/`ffprobe` (e.g. `apt install ffmpeg`),
Python ≥ 3.10.

```bash
pip install scry-social        # or: uv tool install scry-social
scry setup                     # one-time: downloads the Camoufox browser (~200MB)
```

That's it. `scry` is now on your PATH.

From source:

```bash
git clone <this-repo> && cd scry
pip install -e .
scry setup
```

Models (Whisper ~600MB, RapidOCR ~15MB) download automatically on first
use and are cached persistently in `~/.cache/scry/models/`.

## Usage

```bash
# TikTok
scry tiktok "https://www.tiktok.com/@user/video/1234567890"

# Instagram (reel/video -> STT + OCR; photo/carousel -> OCR)
scry instagram "https://www.instagram.com/reel/XXXX/"

# Auto-detect platform
scry auto "https://www.instagram.com/reel/XXXX/"

# Options
#   --max-comments N    comments to analyze (default 30)
#   --no-comments       skip comments + consensus
#   --no-download       skip media download + STT (metadata + comments only)
#   --no-ocr            skip OCR (Instagram)
#   --stt-model NAME    tiny|base|small|medium|large-v3 (default small)
#   --language it       force STT language (default auto-detect)
#   --cookies FILE      Netscape cookies file for login-walled content
#   --json              print JSON only
#   Instagram extra:
#   --no-browser        force HTTP-only tier (faster, less robust)
#   --headless          run the fallback browser without a window
```

Use the **full share URL** for TikTok (`@user/video/<id>`), as copied
from TikTok's share button — the bare `/video/<id>` URL may 404 (the tool
retries automatically with the author name from oEmbed).

### What you get

For one URL you get a Markdown report (plus raw JSON) with:

1. **Header** — URL, ID, date, stats (plays/likes/comments/shares)
2. **Caption** — the author's original text
3. **Transcript (STT)** — what is said in the video, with timestamps
4. **Text in images (OCR)** — on-screen text (hooks, claims, CTAs)
5. **Top comments** — most-liked comments with like/reply counts
   (Instagram: including images/stickers attached to comments)
6. **Consensus (heuristic)** — agree/disagree/neutral counts and an
   agreement ratio, as a *pre-LLM* signal of community alignment

The consensus score is a deliberately simple, transparent lexical
heuristic (EN/IT) weighted by comment likes. It is an **indicator, not
truth** — treat it as context, and verify factual claims independently.

### Cookies (optional)

Public content mostly works without login. If a run hits a login wall,
export cookies from a logged-in browser (extension **Cookie-Editor** →
Export Netscape) and save as `tiktok_cookies.txt` /
`instagram_cookies.txt` — in the current directory or in
`~/.config/scry/` — they are picked up automatically. A residential IP
works much better than a datacenter one.

## How it works (tiers)

| Platform | Tier 1 (fast) | Tier 2 (fallback) |
|---|---|---|
| TikTok | `curl_cffi` with Chrome 136 TLS impersonation → page JSON → direct CDN download → comments API | oEmbed (minimal metadata) |
| Instagram | `curl_cffi` → embedded page data (XDT/`__additionalData`) + og meta | **Camoufox** (headed anti-detect Firefox): opens the post, reads page data, opens the comments popup, extracts comments from the DOM |

Media download: direct CDN URLs first, then the browser's request
context, then `gallery-dl`.

Instagram comments live in a popup (no dedicated URL): the browser clicks
the comment icon, waits for the popup to populate, scrolls it via JS, and
extracts text, usernames, like counts, timestamps, and attached images
using stable structural anchors (permalinks, "Mi piace: N" spans) — no
screenshot/OCR.

## Data & privacy

- **All data stays local.** No telemetry, no third-party calls beyond the
  two target platforms (and the model downloads on first run).
- **Per-run data is ephemeral by default.** Downloaded media, audio,
  frames, comments, and reports go to `/tmp/scry/` — cleared on reboot on
  most Linux systems. Set `SCRY_CLEAN=1` to wipe stale run data on every
  start, or set `SCRY_DATA_DIR` to keep data deliberately somewhere else.
- **Comments are collected as local context only.** They are gathered to
  give the AI agent a better understanding of how a community reacts to a
  post — nothing more. They are not uploaded, not sold, not aggregated,
  and this tool is **not intended for building datasets** of user
  content.
- Cookie files are yours, are git-ignored, and can be revoked by
  regenerating your session.

## Responsible use / personal use

- This is a **personal, low-volume** research tool. Use it on a handful of
  links when you need to understand a post — not in loops, not at scale.
- Mass scraping of TikTok/Instagram violates their Terms of Service, can
  put your IP/account at risk, and raises obvious privacy concerns about
  the people who write comments. **Do not do that.**
- Collected comments belong to their authors. Use them as ephemeral
  context for your own research; don't republish, dataset-ize, or
  profile people with them.
- Respect the platforms: no bypassing paywalls, no harvesting private
  content, no hammering endpoints. If a request fails, the answer is to
  stop, not to add more requests.

## Configuration (env vars)

| Variable | Default | Purpose |
|---|---|---|
| `SCRY_CACHE_DIR` | `~/.cache/scry` | persistent model cache lives under `models/` here |
| `SCRY_DATA_DIR` | `/tmp/scry` | where per-run data (downloads, reports) is stored |
| `SCRY_CLEAN` | unset | `1` = wipe stale run data on every start |
| `SCRY_IMPERSONATE` | `chrome136` | curl_cffi TLS impersonation target |
| `SCRY_COOKIES[_TIKTOK/_INSTAGRAM]` | `<plat>_cookies.txt` in cwd or `~/.config/scry/` | Netscape cookies file(s) |

## Limitations

- STT can miss proper nouns/slang; ambiguous transcripts are flagged in
  the report.
- Comments are the **top-N by likes**, not the full corpus — a bias toward
  mainstream opinions (declared in every report).
- Instagram: top-level comments in the popup (~15–30 with one scroll);
  collapsed nested replies are not extracted.
- The consensus heuristic is lexical (EN/IT); other languages count as
  neutral.
- Both platforms change their page formats; when a tier fails, the report
  says which one and why. `notes/RESEARCH.md` documents the current
  formats.

## Project layout

```
scry/
  __init__.py
  cli.py                CLI (tiktok|instagram|auto <url> [options], setup)
  common.py             sessions, URL parsing, paths, ffmpeg helpers, output
  stt.py                faster-whisper wrapper
  ocr.py                RapidOCR wrapper (PP-OCRv6, onnxruntime)
  browser.py            Camoufox wrapper (page open, comments popup, download)
  tiktok.py             TikTok pipeline
  instagram.py          Instagram pipeline
  consensus.py          lexical consensus/reliability heuristic
pyproject.toml          packaging (pip install scry-social)
notes/                  RESEARCH.md (format docs), DECISIONS.md (rationale)
SKILL.md                agent skill description (how an LLM should use this)
```

Per-run data (default, ephemeral):

```
/tmp/scry/downloads/<platform>-<id>/   media, audio, frames
/tmp/scry/output/<ts>-<platform>-<id>.{md,json}
```

## License

MIT — see [LICENSE](LICENSE).

Note on scope: the MIT license covers the code. Platform content,
collected comments, and your cookies are **not** part of this project:
keep them private, use them as ephemeral personal context, and don't
build datasets or anything commercial from them. The tool is intentionally
designed for personal, low-volume use only.

## Publishing to PyPI

The package on PyPI is [`scry-social`](https://pypi.org/project/scry-social/)
(the bare name `scry` is taken by an old SPARQL project); the import
package and the CLI are both `scry`.

To publish a new version:

```bash
# 1. bump the version in pyproject.toml AND scry/__init__.py
# 2. build
uv build
# 3. upload (token from https://pypi.org/manage/account/token/)
pip install twine
twine upload dist/*
```
