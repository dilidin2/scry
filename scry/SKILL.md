---
name: scry
description: Scrape TikTok videos and Instagram posts (videos, reels, photos, carousels) and turn them into LLM-readable intel (STT transcripts, visual understanding via a small VLM, image description plus on-screen text, captions, stats) plus top comments with a community consensus/reliability score. Use this skill whenever the user shares a TikTok or Instagram link and wants to know what is in it, what it says, or what people think about it, or asks to watch a TikTok or IG post, transcribe a reel, read the comments, or check if a claim is supported by the community. Everything runs CPU-only (no GPU; a lightweight browser is only launched for Instagram when the fast path fails).
license: MIT
---

# scry

Let an LLM "see" TikTok and Instagram: download the content, transcribe the
audio (STT), describe the images and read the on-screen text (small VLM),
collect metadata and top comments with a consensus/reliability score.
Everything CPU-only (no GPU).

**Scraping tiers** (lightest to heaviest):
- **TikTok**: curl_cffi (TLS impersonation) → page with embedded JSON →
  direct CDN download → comments via API.
- **Instagram**: curl_cffi → if the page gives no data, **Camoufox**
  (anti-detect Firefox, ~200MB) opens the reel in a window, reads the data
  from the page (XDT format), opens the comments popup and extracts text,
  usernames, likes and attached images from the comments in the DOM.

The browser opens a visible window on the user's desktop (headed is more
reliable against detection); `--headless` disables the window (more
fragile).

## Setup (one-time)

```bash
uv tool install "scry-social[vision]"    # or: pip install "scry-social[vision]"
scry setup --all                          # Camoufox browser + vision model
```

External dependencies: system `ffmpeg`/`ffprobe`; a C compiler is needed at
install time (llama-cpp-python compiles ggml from source).
Models (whisper ~600MB, LFM2.5-VL-1.6B Q8_0 ~2.1GB) are cached in
`~/.cache/scry/models/`.

## Commands

```bash
# TikTok: download + transcription + comments + consensus
scry tiktok "https://www.tiktok.com/@user/video/1234567890"

# Instagram (reel/video -> STT + VLM frames; photo/carousel -> VLM)
scry instagram "https://www.instagram.com/reel/XXXX/"

# Platform autodetect (also handles short links)
scry auto "https://vm.tiktok.com/XXXXX/"

# Metadata + comments only (no download/stt)
scry tiktok "<url>" --no-download --no-stt

# Main options
#   --max-comments N    comments to analyze (default 30)
#   --no-comments       skip comments+consensus
#   --stt-model NAME    tiny|base|small|medium|large-v3 (default small)
#   --language it       force the STT language (default auto-detect)
#   --no-vision         skip VLM visual analysis (instagram)
#   --no-download       skip download+STT
#   --cookies FILE      Netscape cookies for content that requires login
#   --json              stdout only JSON (for further processing)
#   Instagram extra:
#   --no-browser        force curl tier only (faster, less robust)
#   --headless          browser without a window (more detectable)
```

Outputs are saved in `/tmp/scry/output/<timestamp>-<platform>-<id>.{md,json}`
(per-run data is **ephemeral** by default: lost on reboot; set
`SCRY_DATA_DIR=/elsewhere` to keep it). Progress logs use an `[HH:MM:SS]`
prefix; the final markdown goes to stdout.

**TikTok URL note**: prefer the full `@user/video/<id>` URL (as copied from
the share button). The bare `tiktok.com/video/<id>` URL may return 404.

**Instagram comments note**: comments open in a popup (no dedicated URL).
The browser clicks the comments icon, waits for the popup, and extracts from
the DOM: text, usernames, likes, timestamps, and any images/stickers
attached to the comment (reported in the output). Collapsed nested replies
("N replies") are not in the DOM: top-level comments are extracted, which
are the most relevant for consensus.

## Cookies (when needed and how)

Public content mostly works without login. Cookie lookup order: env
`SCRY_COOKIES[_TIKTOK/_INSTAGRAM]` → `<platform>_cookies.txt` in the current
directory → `~/.config/scry/<platform>_cookies.txt`.

If the output contains a "login wall" / "cookies" error, ask the user to
export cookies from their browser (extension **Cookie-Editor** → Export
Netscape) while logged in on `tiktok.com` / `instagram.com`, and save the
file accordingly (e.g. `~/.config/scry/instagram_cookies.txt`). From
datacenter IPs TikTok/Instagram degrade service; from a residential IP
everything works better. If a run fails due to an IP block, tell the user
clearly.

## How to read the output (and how to use it)

The markdown has stable sections:

1. **Header**: URL, ID, date, stats (plays/likes/comments/shares).
   The stats are context for reliability: a "viral" claim with 50M plays
   carries different weight than one with 2k.
2. **Caption**: the author's original text.
3. **Transcript (STT)**: what is said in the video. Includes model,
   detected language, realtime factor. If empty: the video has no speech
   (or only music).
4. **Visual (VLM)** [Instagram]: concise description of each image/frame
   plus verbatim on-screen text (hooks, claims, CTAs). This is often the
   reel's real "message". The 1.6B model is small: treat descriptions and
   transcribed text as a signal, and double-check important numbers/words
   against the caption/transcript or the original.
5. **Top N comments (by likes)**: the most "validated" comments by the
   community. Each comment has likes and replies.
6. **Comment consensus (heuristic)**: counts agree/disagree/neutral and
   gives an `agreement_ratio` among those with a clear opinion. Lists the
   most "validated" comments (many likes = the community agrees with that
   opinion).

### Calculating the reliability of a claim (procedure)

When the user asks "is it true?" / "is it reliable?":
1. **The claim**: extract the central claim from caption + transcript +
   visual text. If the claim is in the comments (e.g. "people say X"),
   note that the source is the community, not the author.
2. **Who says it**: check the author (verified? followers? does the video
   cite sources?). Engagement stats (views/likes ratio) give a hint of
   reach but NOT of truth.
3. **The community**: look at `agreement_ratio` + high-like comments.
   - High ratio + agreeing top comments → the community is aligned
     (caveat: alignment ≠ truth; it can be an echo chamber or hype).
   - Low ratio / contrary top comments → the community is divided or
     opposed: flag this explicitly.
   - High-like, high-reply comments are the strongest signals.
4. **Independent verification**: the comment heuristic is NOT a factual
   source. If the claim is factual (numbers, events, health, money),
   verify with a web search before confirming or refuting. Always
   distinguish: "the community thinks that..." vs "the facts say that...".

### Answer template for the user

Typical structure (adapt as needed):
- **What it is**: 1-2 lines (author, type, key stats).
- **What it says**: summary of the claim (caption + voice + image text).
- **What people think**: consensus (ratio, top comments with likes,
  divisions if present).
- **Reliability**: combined judgment (author? community? sources? web
  verification if factual). Use the heuristic's disclaimer: comment likes
  = community validation, not truth.

## Known limits (declare them when relevant)

- STT can get proper nouns/slang wrong: if the transcript is ambiguous and
  it matters, say so.
- The VLM (1.6B) is small: short descriptions, occasional imperfections,
  on-screen text can drop or alter words.
- Comments are top-N by likes, not the full corpus: bias toward
  "mainstream" comments (radical minorities don't surface).
- The consensus heuristic is lexical EN/IT: comments in other languages
  count as "neutral".
- TikTok/Instagram change their formats: if a run fails with "no method
  produced data", the page format probably changed or a login is needed
  → ask for cookies.
- Instagram comments are those visible in the popup (top-level, ~15-30
  with one scroll): not the full corpus.
- The Camoufox browser opens a window on the user's display for ~30-60s
  during Instagram runs that use the fallback.
