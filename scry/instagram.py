"""Instagram pipeline: link -> media (photo/video/carousel) -> OCR/STT -> (+ comments).

Tiered strategy:
  Tier 1: GET the post page with curl_cffi (chrome impersonation)
          -> window.__additionalData (JSON) containing shortcode_media:
          caption, stats, top comments, direct media URLs
          (works on public posts without a wall; if the session is in a
           JS checkpoint, the page fetch contains no data)
  Tier 2: browser (Camoufox, anti-detect Firefox) with the user's real
          cookies: same behavior as a human visitor. Sporadic use: opens a
          real window, navigates, reads __additionalData / XDT blocks.
  Tier 3: og: meta tags (caption) + /embed/captioned/ fallback
  Media: direct download of the URLs with curl_cffi; if the CDN refuses,
         download via the browser request context; last resort gallery-dl
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

from .common import (
    DOWNLOADS_DIR, IMPERSONATE, add_netscape_cookies, classify_url,
    default_cookies, extract_audio, fmt, fmt_ts, log, make_session, run_cmd,
    video_duration,
)
from .consensus import analyze_comments

PY = sys.executable  # current venv modules (gallery-dl not on global PATH)

ADD_DATA_RE = re.compile(r'window\.__additionalData\s*=\s*(\{.*?\})\s*</script>', re.S)
XDT_TAG_RE = re.compile(r'<script[^>]*data-content-len="(\d+)"[^>]*>'
                        r'(\{.*?\})\s*</script>', re.S)


# ---------------------------------------------------------------------------
# Fetch + parse
# ---------------------------------------------------------------------------
def fetch_page(session, url: str) -> str | None:
    try:
        r = session.get(url, impersonate=IMPERSONATE, timeout=30,
                        headers={"accept-language": "en-US,en;q=0.9"})
        if r.status_code == 200:
            return r.text
        log(f"Instagram: page HTTP {r.status_code}")
        return None
    except Exception as e:
        log(f"Instagram: fetch error: {e}")
        return None


def _find_media_object(obj, code: str):
    """Recursively find the post's media object (contains 'shortcode')."""
    if isinstance(obj, dict):
        if obj.get("shortcode") == code and (
                "video_url" in obj or "image_versions2" in obj
                or "carousel_media" in obj or "display_url" in obj):
            return obj
        for v in obj.values():
            r = _find_media_object(v, code)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_media_object(v, code)
            if r:
                return r
    return None


def parse_additional_data(html: str, code: str) -> dict | None:
    m = ADD_DATA_RE.search(html)
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None
    return _find_media_object(data, code)


# ---------------------------------------------------------------------------
# XDT (new 2025/26 format): JSON in <script data-content-len> with
# "code" instead of "shortcode"
# ---------------------------------------------------------------------------
def _find_xdt_media(obj, code: str, found: list | None = None) -> list:
    if found is None:
        found = []
    if isinstance(obj, dict):
        if obj.get("code") == code and (
                "video_versions" in obj or "image_versions2" in obj
                or "image_versions" in obj):
            found.append(obj)
        for v in obj.values():
            _find_xdt_media(v, code, found)
    elif isinstance(obj, list):
        for v in obj:
            _find_xdt_media(v, code, found)
    return found


def parse_xdt_page(html: str, code: str) -> dict | None:
    """Scans all server-side JSON blocks in the page and returns the most
    complete media object for `code`. None if not found."""
    best = None
    for m in re.finditer(r'<script[^>]*data-content-len="(\d+)"[^>]*>', html):
        n = int(m.group(1))
        if n < 5000:
            continue
        start = m.end()
        try:
            data = json.loads(html[start:start + n])
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        for med in _find_xdt_media(data, code):
            if best is None or len(med.keys()) > len(best.keys()):
                best = med
    return best


def extract_media_xdt(obj: dict) -> dict:
    """Normalize the XDT media object to the same shape as extract_media()."""
    urls, kind = [], None
    media_type = obj.get("media_type")  # 1=image, 2=video, 8/... = carousel?
    carousel = obj.get("carousel_media") or []
    if obj.get("video_versions"):
        kind = "video"
        urls.append(obj["video_versions"][0]["url"])
    if carousel:
        kind = "carousel"
        urls = []
        for item in carousel:
            if item.get("video_versions"):
                urls.append(("video", item["video_versions"][0]["url"]))
            elif (item.get("image_versions2") or {}).get("candidates"):
                urls.append(("image", item["image_versions2"]["candidates"][0]["url"]))
    elif (obj.get("image_versions2") or {}).get("candidates"):
        kind = kind or "image"
        urls.append(obj["image_versions2"]["candidates"][0]["url"])
    elif obj.get("display_url"):
        kind = kind or "image"
        urls.append(obj["display_url"])

    if kind is None and urls:
        kind = "video" if obj.get("video_duration") else "image"

    caption = (obj.get("caption") or {}).get("text", "") or ""
    stats = {
        "likes": obj.get("like_count"),
        "comments": obj.get("comment_count"),
    }
    if obj.get("view_count"):
        stats["views"] = obj["view_count"]
    user = (obj.get("user") or {}).get("username")
    location = (obj.get("location") or {}).get("name")

    return {
        "type": kind,
        "caption": caption,
        "stats": stats,
        "taken_at": fmt_ts(obj.get("taken_at")),
        "author": user,
        "location": location,
        "link": obj.get("link"),
        "comments": [],  # XDT comments are paginated: they come from the DOM
        "media_urls": urls,
    }


def extract_media(obj: dict) -> dict:
    """Normalize shortcode_media -> dict with type/caption/stats/comments/media_urls."""
    media_urls, kind = [], None
    if obj.get("video_url"):
        kind = "video"
        media_urls.append(obj["video_url"])
    if obj.get("carousel_media"):
        kind = "carousel"
        for item in obj["carousel_media"]:
            if item.get("video_url"):
                media_urls.append(("video", item["video_url"]))
            elif (item.get("image_versions2") or {}).get("candidates"):
                media_urls.append(("image", item["image_versions2"]["candidates"][0]["url"]))
    elif (obj.get("image_versions2") or {}).get("candidates"):
        kind = kind or "image"
        media_urls.append(obj["image_versions2"]["candidates"][0]["url"])
    elif obj.get("display_url"):
        kind = kind or "image"
        media_urls.append(obj["display_url"])

    if kind is None and media_urls:
        kind = "video" if obj.get("video_duration") else "image"

    caption = (obj.get("caption") or {}).get("text", "").strip()
    likes = obj.get("like_count")
    if likes is None:
        likes = (obj.get("edge_liked_by") or {}).get("count")
    comments_raw = (obj.get("edge_media_to_comment") or {}).get("edges") or []
    comments = []
    for e in comments_raw:
        node = e.get("node") or {}
        owner = node.get("owner") or {}
        clikes = node.get("like_count")
        if clikes is None:
            clikes = (node.get("edge_liked_by") or {}).get("count", 0)
        replies = (node.get("reply") or {}).get("comment_count") or 0
        if node.get("text"):
            comments.append({
                "username": owner.get("username"),
                "text": node["text"],
                "likes": clikes or 0,
                "replies": replies,
                "is_author": bool(node.get("owned_by_post")),
            })
    comments.sort(key=lambda c: c["likes"], reverse=True)

    return {
        "type": kind,
        "caption": caption,
        "stats": {
            "likes": likes,
            "comments": (obj.get("edge_media_to_comment") or {}).get("count")
                        or obj.get("comment_count"),
        },
        "taken_at": fmt_ts(obj.get("taken_at_timestamp")),
        "comments": comments,
        "media_urls": media_urls,
    }


def og_meta(html: str) -> dict:
    out = {}
    m = re.search(r'<meta content="([^"]*)" property="og:description"/>', html)
    if m:
        out["caption"] = m.group(1)
    m = re.search(r'<meta content="([^"]+)" property="og:image"/>', html)
    if m:
        out["image"] = m.group(1)
    m = re.search(r'"video_url":"([^"]+)"', html)
    if m:
        out["video"] = m.group(1)
    return out


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------
def _download_url(session, url: str, dest: Path) -> bool:
    try:
        with session.stream(url, impersonate=IMPERSONATE, timeout=60) as r:
            dest.write_bytes(r.content)
        return dest.exists() and dest.stat().st_size > 0
    except Exception:
        return False


def download_media(media: dict, code: str, session, cookies: str | None,
                   use_browser: bool = True, headless: bool = False) -> dict:
    """Download the post's media. Returns {files:[{kind,path}], video_path, note}.

    Order: direct curl_cffi -> browser request context -> gallery-dl.
    """
    outdir = DOWNLOADS_DIR / f"instagram-{code}"
    outdir.mkdir(parents=True, exist_ok=True)
    files, note, failed = [], [], []
    urls = media["media_urls"]
    is_carousel = media["type"] == "carousel"

    for i, u in enumerate(urls):
        if isinstance(u, tuple):
            kind, u = u
        else:
            kind = "video" if media["type"] == "video" else "image"
        ext = "mp4" if kind == "video" else "jpg"
        name = f"{code}-{i+1}.{ext}" if (is_carousel or len(urls) > 1) else f"{code}.{ext}"
        dest = outdir / name
        if _download_url(session, u, dest):
            files.append({"kind": kind, "path": str(dest)})
        else:
            failed.append((kind, u, dest))

    # retry the failures via the browser request context (same cookies/TLS)
    if failed and cookies and use_browser:
        from . import browser as browser_mod
        for kind, u, dest in list(failed):
            log(f"Instagram: download retry via browser: {u[:60]}")
            ok, why = browser_mod.download_url(u, dest, cookies_path=cookies,
                                               headless=headless,
                                               referer="https://www.instagram.com/")
            if ok:
                files.append({"kind": kind, "path": str(dest)})
                failed.remove((kind, u, dest))
            else:
                note.append(f"browser download {why}: {u[:60]}")

    if failed:
        note.extend(f"download failed: {u[:60]}" for _, u, _ in failed)

    if not files and cookies:
        # gallery-dl fallback
        log("Instagram: gallery-dl fallback...")
        cmd = [PY, "-m", "gallery_dl", "-D", str(outdir), "--write-metadata", "--range", "1-20"]
        if Path(cookies).exists():
            cmd += ["--cookies", str(cookies)]
        cmd.append(f"https://www.instagram.com/p/{code}/")
        rc, out, err = run_cmd(cmd, timeout=600)
        for f in sorted(outdir.iterdir()) if outdir.exists() else []:
            if f.suffix.lower() in (".jpg", ".jpeg", ".mp4", ".png", ".webp"):
                files.append({"kind": "video" if f.suffix == ".mp4" else "image",
                              "path": str(f)})
        if rc != 0:
            note.append(f"gallery-dl: {err.strip()[-200:]}")
    return {"files": files,
            "video_path": next((f["path"] for f in files if f["kind"] == "video"), None),
            "note": "; ".join(note) or "ok"}


# ---------------------------------------------------------------------------
# Full pipeline
# ---------------------------------------------------------------------------
def process(url: str, *, do_stt: bool = True, do_ocr: bool = True,
            do_comments: bool = True, max_comments: int = 30,
            stt_model: str = "small", language: str | None = None,
            cookies: str | None = None, use_browser: bool = True,
            headless: bool = False) -> tuple[dict, str]:
    info = classify_url(url)
    if not info:
        return {"error": f"Unrecognized Instagram URL: {url}"}, "Error: invalid URL"

    session = make_session()
    ck = cookies or str(default_cookies("instagram"))
    if ck and Path(ck).exists():
        n = add_netscape_cookies(session, ck)
        log(f"Instagram: {n} cookies loaded from {ck}")

    code = info["id"]
    result: dict = {"platform": "instagram", "url": info["canonical"],
                    "shortcode": code, "tiers": {}}
    outdir = DOWNLOADS_DIR / f"instagram-{code}"
    outdir.mkdir(parents=True, exist_ok=True)

    # ---- Tier 1: __additionalData ----------------------------------------
    html = fetch_page(session, info["canonical"])
    media_obj, media = None, None
    if html:
        media_obj = parse_additional_data(html, code)
        if media_obj:
            media = extract_media(media_obj)
            result["tiers"]["additional_data"] = "ok"
        else:
            result["tiers"]["additional_data"] = ("page 200 but no post data "
                                                  "(login wall? use --cookies)")
            meta = og_meta(html)
            if meta.get("caption") or meta.get("video"):
                result["tiers"]["og_meta"] = "ok"
                media = {"type": "video" if meta.get("video") else "image",
                         "caption": meta.get("caption", ""),
                         "stats": {}, "taken_at": None, "comments": [],
                         "media_urls": [meta.get("video") or meta.get("image")]
                         if (meta.get("video") or meta.get("image")) else []}
    else:
        result["tiers"]["page"] = "fetch failed"

    if not media and use_browser:
        # ---- Tier 2: browser (Camoufox) ------------------------------------
        log("Instagram: curl tier without data, opening browser (Camoufox)...")
        try:
            from . import browser as browser_mod
            with browser_mod.Session(ck, headless=headless) as s:
                bhtml, burl = s.open(info["canonical"], wait_ms=8000)
                media_obj = parse_xdt_page(bhtml, code) \
                    or parse_additional_data(bhtml, code)
                if media_obj:
                    media = (extract_media_xdt(media_obj)
                             if "video_versions" in media_obj
                             or "image_versions2" in media_obj
                             else extract_media(media_obj))
                    result["tiers"]["browser"] = "ok"
                    log("Browser: post data (XDT) found in page")
                    # comments: popup + DOM
                    if do_comments:
                        if s.open_comments_popup():
                            dom_comments = s.extract_comments(max_scrolls=2)
                            media["comments"] = [
                                {"username": c["username"], "text": c["text"],
                                 "likes": c["likes"], "replies": 0,
                                 "when": c["when"], "is_author": False,
                                 "images": c["images"]}
                                for c in dom_comments]
                            result["comments_source"] = (
                                f"browser DOM ({len(media['comments'])} visible)")
                        else:
                            result["tiers"]["comments_popup"] = (
                                "comments popup not opened")
                else:
                    result["tiers"]["browser"] = (
                        "browser: page loaded but no post data "
                        f"(final url: {burl}); the post may require login")
        except Exception as e:
            result["tiers"]["browser"] = f"error: {type(e).__name__}: {e}"

    if not media:
        result["error"] = ("No method produced post data. "
                           "A valid login is probably needed: export your cookies from the "
                           "browser ('Get cookies.txt LOCALLY' extension) and retry "
                           "with --cookies cookies.txt")
        return result, render(result)

    result["metadata"] = {
        "type": media["type"], "caption": media["caption"],
        "stats": media["stats"], "taken_at": media["taken_at"],
    }

    # ---- Media download ----------------------------------------------------
    log(f"Instagram: downloading media ({media['type']})...")
    dl = download_media(media, code, session, ck,
                        use_browser=use_browser, headless=headless)
    result["download"] = dl

    video_path = dl.get("video_path")
    images = [f["path"] for f in dl["files"] if f["kind"] == "image"]

    # ---- STT (video) --------------------------------------------------------
    if video_path and do_stt:
        wav = str(outdir / f"{code}.wav")
        log("Instagram: extracting audio...")
        if extract_audio(video_path, wav):
            result["video_duration_s"] = round(video_duration(video_path), 1)
            log(f"Instagram: STT (model {stt_model})...")
            from .stt import transcribe
            result["transcript"] = transcribe(wav, model=stt_model, language=language)
        else:
            result["transcript"] = {"error": "audio extraction failed"}

    # ---- OCR (photos / video frames) ------------------------------------------
    if do_ocr:
        ocr_out = {}
        if images:
            from .ocr import ocr_image
            for i, img in enumerate(images, 1):
                log(f"Instagram: OCR image {i}/{len(images)}...")
                r = ocr_image(img)
                if r["text"]:
                    ocr_out[f"image_{i}"] = r["text"]
        if video_path:
            from .ocr import ocr_video
            log("Instagram: OCR video frames...")
            vr = ocr_video(video_path, n_frames=3)
            if vr["text"]:
                ocr_out["video_frames"] = vr["text"]
        if ocr_out:
            result["ocr"] = ocr_out

    # ---- Comments -------------------------------------------------------------
    if do_comments:
        comments = sorted(media["comments"],
                          key=lambda c: c.get("likes") or 0, reverse=True)
        comments = comments[:max_comments]
        result["comments"] = comments
        result.setdefault("comments_source", "post page (top comments)")
        result["consensus"] = analyze_comments(
            comments, (media["stats"] or {}).get("likes"))
    else:
        result["comments"], result["consensus"] = [], {"available": False}

    result["files"] = {"dir": str(outdir)}
    return result, render(result)


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------
def render(r: dict) -> str:
    L = []
    if r.get("error"):
        return (f"# Instagram\n\n**ERROR:** {r['error']}\n\n"
                + json.dumps(r, ensure_ascii=False, indent=2))

    m = r.get("metadata") or {}
    st = m.get("stats") or {}
    L.append(f"# Instagram ({m.get('type', '?')})")
    L.append(f"\n- URL: {r['url']}")
    if m.get("taken_at"):
        L.append(f"- Date: {m['taken_at']}")
    if st:
        parts = [f"**{fmt(st.get('likes'))} likes**"]
        if st.get("comments"):
            parts.append(f"{fmt(st['comments'])} comments")
        L.append(f"- Stats: {', '.join(parts)}")
    if m.get("caption"):
        L.append(f"\n## Caption\n\n{m['caption']}")

    t = r.get("transcript")
    if t and t.get("text"):
        L.append(f"\n## Transcript (STT {t.get('model')}, lang={t.get('language')}, "
                 f"{t.get('realtime_factor', '?')}x realtime)\n\n{t['text']}")
    elif t and t.get("error"):
        L.append(f"\n## Transcript\n\n_ERROR: {t['error']}_")

    dl = r.get("download")
    if dl and dl.get("note") and dl["note"] != "ok":
        L.append(f"\n> ⚠️ {dl['note']}")

    o = r.get("ocr") or {}
    if o:
        L.append("\n## Text in images (OCR)")
        for k, v in o.items():
            L.append(f"\n**{k}:**\n\n{v}")

    c = r.get("comments") or []
    if c:
        src = r.get("comments_source")
        L.append(f"\n## Top {len(c)} comments (by likes)" +
                 (f" — source: {src}" if src else ""))
        for i, cm in enumerate(c[:15], 1):
            extra = []
            if cm.get("likes"):
                extra.append(f"{fmt(cm['likes'])} likes")
            if cm.get("replies"):
                extra.append(f"{cm['replies']} replies")
            if cm.get("is_author"):
                extra.append("post author")
            if cm.get("images"):
                extra.append(f"📷 {len(cm['images'])} image(s) attached (sticker/photo)")
            L.append(f"{i}. **@{cm.get('username')}**: {cm.get('text', '')}  "
                     f"_{', '.join(extra) if extra else '0 likes'}_")

    co = r.get("consensus") or {}
    if co.get("available"):
        L.append("\n## Comment consensus (heuristic)")
        L.append(f"- {co['agree']} agree / {co['disagree']} disagree / "
                 f"{co['neutral']} neutral out of {co['total_analyzed']} analyzed")
        if co.get("agreement_ratio") is not None:
            L.append(f"- Agreement ratio among those with a clear opinion: "
                     f"**{co['agreement_ratio']:.0%}**")
        sig = [x for x in co["comments"][:15] if x["reliability"] in ("high", "medium")]
        if sig:
            L.append("- Comments most 'validated' by the community:")
            for x in sig[:5]:
                L.append(f"  - @{x['username']} ({fmt(x['likes'])} likes, {x['stance']}): "
                         f"{x['text'][:120]}")
        L.append(f"\n> Note: {co['note']}")
    return "\n".join(L)
