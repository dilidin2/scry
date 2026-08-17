"""TikTok pipeline: link -> metadata + download -> STT -> (+ comments + consensus).

Strategy (no browser):
  Metadata: GET the video page with curl_cffi (chrome impersonation) + a
          logged-in session (cookie.txt) -> embedded JSON
          __UNIVERSAL_DATA_FOR_REHYDRATION__
          (itemInfo.itemStruct: desc, stats, author, music) + token for the API.
          This is the only metadata path: without valid cookies TikTok serves
          an anti-bot shell page and no data is available.
  Comments: internal comments API /api/comment/list/ with msToken+aid+region
          (extractable from the page; works from residential IPs)
  Download: direct from the CDN (playAddr/bitrateInfo from the page JSON,
          needs header Referer: https://www.tiktok.com/) -> yt-dlp fallback
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

from .common import (
    DOWNLOADS_DIR, IMPERSONATE, add_netscape_cookies, classify_url,
    default_cookies, extract_audio, extract_frames, fmt, fmt_ts, log,
    make_session, resolve_share_url, run_cmd, save_outputs, slug,
    video_duration,
)
from .consensus import analyze_comments

# use the modules from the current venv (yt-dlp/gallery-dlp not on global PATH)
PY = sys.executable

REHYDRATION_RE = re.compile(
    r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">(.*?)</script>',
    re.S)


# ---------------------------------------------------------------------------
# Fetch + parse
# ---------------------------------------------------------------------------
def fetch_page(session, url: str) -> tuple[str, int] | None:
    """GET the video page. Bootstraps ttwid if missing. Returns (html, status) or None."""
    try:
        if "ttwid" not in session.cookies:
            session.get("https://www.tiktok.com/", impersonate=IMPERSONATE, timeout=20)
        r = session.get(url, impersonate=IMPERSONATE, timeout=30)
        return r.text, r.status_code
    except Exception as e:
        log(f"TikTok: page fetch error: {e}")
        return None


def parse_rehydration(html: str) -> dict | None:
    m = REHYDRATION_RE.search(html)
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
        return data.get("__DEFAULT_SCOPE__", {}).get("webapp.video-detail")
    except json.JSONDecodeError:
        return None


def extract_video_detail(scope: dict) -> dict | None:
    """Normalize itemInfo.itemStruct (or .item in older versions)."""
    info = scope.get("itemInfo") or {}
    item = info.get("itemStruct") or info.get("item")
    if not item:
        return None
    author = item.get("author") or {}
    stats = item.get("stats") or {}
    music = item.get("music") or {}
    tags = []
    for te in item.get("textExtra") or []:
        if te.get("hashtagName"):
            tags.append(te["hashtagName"])
    return {
        "id": str(item.get("id", "")),
        "desc": (item.get("desc") or "").strip(),
        "author": {
            "username": author.get("uniqueId"),
            "nickname": author.get("nickname"),
            "verified": author.get("verified", False),
        },
        "stats": {
            "plays": stats.get("playCount"),
            "likes": stats.get("diggCount"),
            "comments": stats.get("commentCount"),
            "shares": stats.get("shareCount"),
            "bookmarks": stats.get("collectCount"),
        },
        "hashtags": tags,
        "music": music.get("title"),
        "created_at": fmt_ts(item.get("createTime")),
    }


def extract_video_urls(scope: dict) -> list[tuple[int, str, str]]:
    """Direct video URLs from the embedded JSON.

    Returns a list of (size_bytes, label, url) sorted by size, descending.
    Keys may be camelCase (playAddr/bitrateInfo) or PascalCase
    (PlayAddr/UrlList) depending on the gear.
    """
    info = scope.get("itemInfo") or {}
    item = info.get("itemStruct") or info.get("item") or {}
    v = item.get("video") or {}
    gears: list[tuple[int, str, str]] = []
    for b in v.get("bitrateInfo") or []:
        pa = b.get("PlayAddr") or b.get("play_addr") or {}
        urls = pa.get("UrlList") or pa.get("url_list") or []
        if urls:
            gears.append((int(pa.get("DataSize") or pa.get("dataSize") or 0),
                          f"gear:{b.get('GearName') or b.get('gear_name')}", urls[0]))
    if v.get("playAddr") or v.get("play_addr"):
        gears.append((int(v.get("size") or 0), "playAddr",
                      v.get("playAddr") or v.get("play_addr")))
    gears = [g for g in gears if g[2] and g[2].startswith("http")]
    gears.sort(key=lambda g: g[0], reverse=True)
    return gears


def extract_page_comments(scope: dict) -> list[dict]:
    """In case the page contains embedded comments (varies by version/region).

    Comment signature: long numeric id + text + diggCount + userInfo/user.
    The strict signature avoids false positives on the video stats dict
    (which has diggCount but no id/text/userInfo).
    """
    found = []

    def walk(o):
        if isinstance(o, dict):
            if (isinstance(o.get("id"), (str, int)) and len(str(o["id"])) >= 10
                    and "text" in o and ("diggCount" in o or "digg_count" in o)
                    and ("userInfo" in o or "user" in o)):
                user = o.get("userInfo") or o.get("user") or {}
                found.append({
                    "username": user.get("uniqueId") or user.get("unique_id"),
                    "text": o.get("text", ""),
                    "likes": o.get("diggCount") or o.get("digg_count") or 0,
                    "replies": o.get("replyCommentTotal") or o.get("reply_comment_total") or 0,
                })
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(scope)
    # dedup by (username, text)
    seen, out = set(), []
    for c in found:
        k = (c["username"], (c["text"] or "")[:60])
        if k not in seen:
            seen.add(k)
            out.append(c)
    return out


def fetch_comments_api(session, video_id: str, page_html: str,
                       max_comments: int = 30) -> tuple[list[dict], str]:
    """Internal comments API with msToken/aid/region extracted from the page.

    Returns (comments, note). Comments are ordered by relevance
    (as TikTok serves them), not by likes: we sort them ourselves.
    """
    if not page_html:
        return [], "no page available"

    def grab(pattern, default=None):
        m = re.search(pattern, page_html)
        return m.group(1) if m else default

    ms_token = grab(r'"msToken":"([^"]+)"')
    aid = grab(r'"aid":(\d+)', "1988")
    region = grab(r'"region":"([^"]+)"', "US")

    all_comments, cursor, page_no = [], 0, 0
    while len(all_comments) < max_comments and page_no < (max_comments // 35 + 1):
        params = {
            "aweme_id": video_id, "cursor": str(cursor),
            "count": "35", "current_region": region or "US",
            "aid": aid, "item_id": video_id,
            "device_platform": "webapp", "app_language": "en",
        }
        if ms_token:
            params["msToken"] = ms_token
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"https://www.tiktok.com/api/comment/list/?{qs}"
        try:
            r = session.get(url, impersonate=IMPERSONATE, timeout=20,
                            headers={"referer": f"https://www.tiktok.com/video/{video_id}"})
            data = r.json()
        except Exception as e:
            return all_comments, f"API error page {page_no+1}: {e}"
        comments = data.get("comments") or []
        if not comments:
            code = data.get("status_code")
            return all_comments, (f"empty API (status_code={code}); "
                                  "likely IP block: use --cookies")
        for c in comments:
            user = c.get("user") or {}
            all_comments.append({
                "username": user.get("uniqueId") or user.get("unique_id"),
                "text": c.get("text", ""),
                "likes": c.get("diggCount") or c.get("digg_count") or 0,
                "replies": c.get("replyCommentTotal") or c.get("reply_comment_total") or 0,
            })
        if not data.get("hasMore"):
            break
        cursor = data.get("cursor") or 0
        if cursor == 0:
            break
        page_no += 1
        time.sleep(0.7)  # politeness / anti-rate-limit

    all_comments.sort(key=lambda c: c.get("likes") or 0, reverse=True)
    return all_comments[:max_comments], "ok"


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------
def _download_direct(session, gears: list[tuple[int, str, str]], outdir: Path,
                     video_id: str) -> tuple[str | None, str]:
    """Direct stream from the CDN. Requires the tiktok.com Referer (else: 403)."""
    outdir.mkdir(parents=True, exist_ok=True)
    dest = outdir / f"{video_id}.mp4"
    errors = []
    for size, label, url in gears:
        try:
            t0 = time.time()
            with session.stream("GET", url, impersonate=IMPERSONATE, timeout=180,
                                headers={"referer": "https://www.tiktok.com/"}) as rr:
                if rr.status_code != 200:
                    errors.append(f"{label}: HTTP {rr.status_code}")
                    continue
                n = 0
                with open(dest, "wb") as f:
                    for chunk in rr.iter_content(chunk_size=256 * 1024):
                        f.write(chunk)
                        n += len(chunk)
            if n > 100_000:  # sanity: no HTML responses / empty blobs
                return str(dest), (f"direct {label} {n/1e6:.1f}MB "
                                   f"in {time.time()-t0:.0f}s")
            errors.append(f"{label}: response too small ({n} B)")
        except Exception as e:
            errors.append(f"{label}: {e}")
            log(f"TikTok: direct download {label} failed: {e}")
    return None, "; ".join(errors) or "no gear available"


def _download_ytdlp(url: str, outdir: Path, cookies: str | None,
                    video_id: str) -> tuple[str | None, str]:
    """Fallback: yt-dlp with impersonation + cookies."""
    outdir.mkdir(parents=True, exist_ok=True)
    out_tmpl = str(outdir / "%(id)s.%(ext)s")
    cmd = [PY, "-m", "yt_dlp", "--impersonate", "chrome-136",
           "-f", "bv*+ba/b", "--merge-output-format", "mp4",
           "--no-playlist",
           "-o", out_tmpl, "--socket-timeout", "30"]
    if cookies and Path(cookies).exists():
        cmd += ["--cookies", str(cookies)]
    cmd.append(url)
    t0 = time.time()
    rc, out, err = run_cmd(cmd, timeout=900)
    if rc != 0:
        return None, f"yt-dlp failed: {err.strip()[-300:]}"
    dest = outdir / f"{video_id}.mp4"
    files = sorted(outdir.glob("*.mp4"), key=lambda p: p.stat().st_mtime)
    if not files:
        return None, "yt-dlp ok but no .mp4 found"
    path = str(files[-1])
    if path != str(dest):
        Path(path).replace(dest)
        path = str(dest)
    return path, f"yt-dlp ok in {time.time()-t0:.0f}s"


def download_video(session, url: str, outdir: Path, video_id: str,
                   cookies: str | None,
                   gears: list[tuple[int, str, str]]) -> tuple[str | None, str]:
    """Cascade download: direct (URLs from the page) -> yt-dlp."""
    if gears:
        p, note = _download_direct(session, gears, outdir, video_id)
        if p:
            return p, note
        log(f"TikTok: direct download failed ({note}); falling back to yt-dlp...")
    return _download_ytdlp(url, outdir, cookies, video_id)


# ---------------------------------------------------------------------------
# Full pipeline
# ---------------------------------------------------------------------------
def process(url: str, *, do_stt: bool = True, do_comments: bool = True,
            do_download: bool = True, vlm_cpu: bool = False,
            max_comments: int = 30,
            stt_model: str = "small", language: str | None = None,
            cookies: str | None = None) -> tuple[dict, str]:
    """Run the full pipeline. Returns (result_dict, markdown).

    vlm_cpu is reserved for the upcoming TikTok VLM (same semantics as in
    instagram.process: skip the GPU attempt when True).
    """
    info = classify_url(url)
    if not info:
        return {"error": f"Unrecognized TikTok URL: {url}"}, "Error: invalid URL"

    session = make_session()
    ck = cookies or default_cookies("tiktok")
    if ck and Path(ck).exists():
        n = add_netscape_cookies(session, ck)
        log(f"TikTok: {n} cookies loaded from {ck}")

    # resolve share links
    if info["kind"] == "share":
        log("TikTok: resolving short link...")
        final = resolve_share_url(session, url)
        if not final:
            return ({"error": "Could not resolve the short link (network error "
                              "or IP blocked by TikTok)"},
                    "Error: unresolvable short link")
        info = classify_url(final)
        if not info:
            # redirect landed on a non-video page (login wall / rate limit,
            # profile, dead link): report where it landed instead of crashing
            return ({"error": f"Short link landed on a non-video page (login "
                              f"wall, rate limit, profile or dead link): {final}"},
                    f"Error: short link -> {final}")
        log(f"TikTok: short link -> {final}")

    video_id = info["id"]
    result: dict = {"platform": "tiktok", "url": info["canonical"], "id": video_id,
                    "tiers": {}}
    outdir = DOWNLOADS_DIR / f"tiktok-{video_id}"

    # ---- Tier 1: page + embedded JSON ---------------------------------
    detail, comments_page, gears = None, [], []
    page = fetch_page(session, info["canonical"])
    if page:
        html, status = page
        scope = parse_rehydration(html)
        if scope:
            detail = extract_video_detail(scope)
            comments_page = extract_page_comments(scope)
            gears = extract_video_urls(scope)
            result["tiers"]["page"] = "ok"
        else:
            result["tiers"]["page"] = (f"page {status} without embedded data "
                                       "(anti-bot wall or shell page)")
            detail = None
    else:
        result["tiers"]["page"] = "fetch failed"

    # Metadata comes only from the page; without a logged-in session there is
    # no fallback, so stop here and point at cookies.
    if not detail:
        result["error"] = ("No method produced metadata "
                           "(blocked IP? try --cookies cookies.txt)")
        return result, render(result)

    result["metadata"] = detail

    # ---- Download + STT ---------------------------------------------------
    if do_download:
        log(f"TikTok: downloading video {video_id}...")
        vpath, note = download_video(session, info["canonical"], outdir,
                                     video_id, ck, gears)
        result["download"] = {"path": vpath, "note": note}
        if vpath:
            dur = round(video_duration(vpath), 1)
            result["video_duration_s"] = dur
            if dur <= 0:
                result["video_error"] = ("downloaded file is not a valid video "
                                         "(ffprobe found no duration) - corrupted "
                                         "download?")
            if do_stt:
                wav = str(outdir / f"{video_id}.wav")
                log("TikTok: extracting audio...")
                if extract_audio(vpath, wav):
                    log(f"TikTok: STT (model {stt_model})...")
                    from .stt import transcribe
                    result["transcript"] = transcribe(wav, model=stt_model, language=language)
                else:
                    result["transcript"] = {"error": "audio extraction failed"}
            # local paths for agents that have their own vision
            result["media_files"] = [vpath] + extract_frames(
                vpath, str(outdir / "frames"), n=3)
    elif do_stt:
        result["stt"] = {"skipped": "download disabled"}

    # ---- Comments ---------------------------------------------------------
    if do_comments:
        log(f"TikTok: comments (max {max_comments})...")
        comments = []
        if comments_page:
            comments = sorted(comments_page, key=lambda c: c.get("likes") or 0,
                              reverse=True)[:max_comments]
            result["comments_source"] = "embedded page"
        else:
            html_for_api = None
            if page:
                html_for_api = page[0]
            comments, note = fetch_comments_api(session, video_id, html_for_api,
                                                max_comments)
            result["comments_source"] = f"comment/list API ({note})"
        result["comments"] = comments
        result["consensus"] = analyze_comments(
            comments, (result["metadata"] or {}).get("stats", {}).get("likes"))
    else:
        result["comments"], result["consensus"] = [], {"available": False}

    result["files"] = {"video": str(outdir)}
    return result, render(result)


# ---------------------------------------------------------------------------
# Markdown rendering (format designed to be read by an LLM)
# ---------------------------------------------------------------------------
def render(r: dict) -> str:
    L = []
    if r.get("error"):
        return f"# TikTok\n\n**ERROR:** {r['error']}\n\n" + json.dumps(r, ensure_ascii=False, indent=2)

    m = r.get("metadata") or {}
    st = m.get("stats") or {}
    a = m.get("author") or {}
    L.append(f"# TikTok: @{a.get('username', '?')}{(' — verified' if a.get('verified') else '')}")
    L.append(f"\n- URL: {r['url']}")
    L.append(f"- ID: {r['id']}  |  {m.get('created_at', '?')}")
    if st:
        L.append(f"- Stats: **{fmt(st.get('plays'))} plays**, {fmt(st.get('likes'))} likes, "
                 f"{fmt(st.get('comments'))} comments, {fmt(st.get('shares'))} shares, "
                 f"{fmt(st.get('bookmarks'))} bookmarks")
    if m.get("hashtags"):
        L.append(f"- Hashtags: {', '.join('#'+h for h in m['hashtags'])}")
    if m.get("music"):
        L.append(f"- Music: {m['music']}")
    if m.get("desc"):
        L.append(f"\n## Caption\n\n{m['desc']}")

    t = r.get("transcript")
    if t and t.get("text"):
        load = f" (+{t['model_load_s']}s model load)" if (t.get("model_load_s") or 0) >= 1 else ""
        L.append(f"\n## Transcript (STT {t.get('model')}, lang={t.get('language')}, "
                 f"{t.get('realtime_factor', '?')}x realtime{load})\n\n{t['text']}")
    elif t and t.get("error"):
        L.append(f"\n## Transcript\n\n_ERROR: {t['error']}_")

    dl = r.get("download")
    if dl and not dl.get("path"):
        L.append(f"\n> ⚠️ Video download failed: {dl.get('note')}")
    if r.get("video_error"):
        L.append(f"\n> ⚠️ {r['video_error']}")

    mf = r.get("media_files") or []
    if mf:
        L.append("\n## Media files")
        for p in mf:
            L.append(f"- {p}")

    c = r.get("comments") or []
    if c:
        src = r.get("comments_source", "?")
        L.append(f"\n## Top {len(c)} comments (by likes, source: {src})")
        for i, cm in enumerate(c[:15], 1):
            extra = []
            if cm.get("likes"):
                extra.append(f"{fmt(cm['likes'])} likes")
            if cm.get("replies"):
                extra.append(f"{cm['replies']} replies")
            L.append(f"{i}. **@{cm.get('username')}**: {cm.get('text', '')}  "
                     f"_{', '.join(extra) if extra else '0 likes'}_")

    co = r.get("consensus") or {}
    if co.get("available"):
        rel = co.get("reliability") or {}
        L.append("\n## Comment consensus (likes-based)")
        L.append(f"- Reliability: {rel.get('high', 0)} high / {rel.get('medium', 0)} medium / "
                 f"{rel.get('low', 0)} low out of {co['total_analyzed']} analyzed")
        sig = [x for x in co["comments"][:15] if x["reliability"] in ("high", "medium")]
        if sig:
            L.append("- Comments most 'validated' by the community:")
            for x in sig[:5]:
                L.append(f"  - @{x['username']} ({fmt(x['likes'])} likes): "
                         f"{x['text'][:120]}")
        L.append(f"\n> Note: {co['note']}")
    return "\n".join(L)
