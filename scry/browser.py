"""Browser tier: Camoufox (anti-detect Firefox) with the user's real cookies.

When curl_cffi is not enough (JS checkpoints, challenges, walls), we open a
real browser with a consistent fingerprint + the user's cookies. Same
behavior as a human user: same session, same profile.

Typical use (sporadic, few calls):
    html, final_url = fetch_page("https://www.instagram.com/reel/xxx/",
                                 "instagram_cookies.txt")

Notes:
- headless=False (default): visible window, maximum anti-detect
  compatibility. Fine on a desktop PC for sporadic use.
- geoip=True: Camoufox aligns timezone/locale with the IP (consistency).
- uBlock Origin included by default (reduces tracking noise).
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

from .common import log

# ---------------------------------------------------------------------------
# Cookies: Netscape -> Playwright format
# ---------------------------------------------------------------------------
def netscape_to_playwright(path: str | Path) -> list[dict]:
    out = []
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        dom, _subs, pth, sec, _exp, name, val = parts[:7]
        if not name:
            continue
        out.append({
            "name": name,
            "value": val,
            "domain": dom if dom.startswith(".") else "." + dom.lstrip("."),
            "path": pth or "/",
            "secure": sec == "TRUE",
            "httpOnly": False,
        })
    return out


# ---------------------------------------------------------------------------
# Fetch a page with the browser
# ---------------------------------------------------------------------------
class Session:
    """Reusable browser context (1 startup = navigation + downloads).

    Usage:
        with Session("instagram_cookies.txt") as s:
            html, url = s.open(post_url)
            comments = s.open_comments_popup() + s.extract_comments()
            s.download(media_url, dest)
    """
    def __init__(self, cookies_path: str | Path | None = None, *,
                 headless: bool = False, locale: str = "it-IT",
                 timezone: str = "Europe/Rome"):
        self.cookies_path = cookies_path
        self.headless = headless
        self.locale = locale
        self.timezone = timezone
        self._browser = None
        self.ctx = None
        self.page = None

    def __enter__(self):
        from camoufox.sync_api import Camoufox
        self._browser = Camoufox(headless=self.headless, geoip=True)
        browser = self._browser.__enter__()
        self.ctx = browser.new_context(locale=self.locale,
                                       timezone_id=self.timezone)
        if self.cookies_path and Path(self.cookies_path).exists():
            cookies = netscape_to_playwright(self.cookies_path)
            self.ctx.add_cookies(cookies)
            log(f"Browser: {len(cookies)} cookies loaded from {self.cookies_path}")
        self.page = self.ctx.new_page()
        return self

    def __exit__(self, *exc):
        try:
            self._browser.__exit__(*exc)
        finally:
            self.ctx = self.page = self._browser = None

    # -- navigation ------------------------------------------------------------
    def open(self, url: str, *, warmup: bool = True, wait_ms: int = 8000) -> tuple[str, str]:
        """Navigate to `url`. Returns (html, final_url).

        warmup: first visits the same host's home page (the site generates
        session cookies like ig_shield before serving real data).
        """
        page = self.page
        if warmup:
            home = re.match(r"(https?://[^/]+)", url)
            if home:
                try:
                    page.goto(home.group(1) + "/", wait_until="domcontentloaded",
                              timeout=60000)
                    page.wait_for_timeout(6000)
                except Exception as e:
                    log(f"Browser: home warmup failed ({e}); continuing")
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(wait_ms)
        return page.content(), page.url

    # -- comments (popup) --------------------------------------------------------
    def open_comments_popup(self, *, tries: int = 6, wait_ms: int = 4000) -> bool:
        """Click the comments icon to open the popup. Returns True if at
        least one comment ends up in the DOM.

        IG selectors change often: we try several candidates and verify the
        effect (presence of /p/.../c/<id>/ permalinks in the DOM).
        """
        page = self.page
        candidates = [
            'div[data-test-id="comment-icon"]',
            'svg[data-test-id="comment-icon"]',
            '[aria-label*="ommenti"]',
            '[aria-label*="omment"]',
            'div[placeholder="Commenta"]',
            'input[placeholder*="omment"]',
        ]
        for sel in candidates:
            try:
                loc = page.locator(sel)
                if loc.count() == 0:
                    continue
                loc.first.click(timeout=5000)
                # comments can take a few seconds to populate
                for _ in range(wait_ms // 2000 + 8):
                    page.wait_for_timeout(2000)
                    n = self.comment_count()
                    if n > 0:
                        log(f"Browser: comments popup open ({sel}, {n} elements)")
                        return True
            except Exception:
                continue
        return self.comment_count() > 0

    def comment_count(self) -> int:
        try:
            return self.page.evaluate(
                '''() => document.querySelectorAll('a[href*="/c/"]').length''')
        except Exception:
            return 0

    def extract_comments(self, *, max_scrolls: int = 3, scroll_ms: int = 2500) -> list[dict]:
        """Extract the comments visible in the popup (JS in the live DOM).

        Returns [{cid, username, text, when, likes, images, is_reply}].
        Scrolls the list to load more comments (new ones get appended).
        The scroll happens via JS on the popup's scrollable container
        (independent of the mouse position, which would otherwise scroll
        the feed behind the popup).
        """
        page = self.page
        results: list[dict] = []
        for i in range(max_scrolls + 1):
            batch = page.evaluate(EXTRACT_COMMENTS_JS)
            seen = {c["cid"] for c in results}
            for c in batch:
                if c["cid"] not in seen:
                    results.append(c)
            if i < max_scrolls:
                try:
                    page.evaluate(SCROLL_COMMENTS_JS)
                    page.wait_for_timeout(scroll_ms)
                except Exception:
                    break
        log(f"Browser: {len(results)} comments extracted from the DOM")
        return results

    # -- download -----------------------------------------------------------------
    def download(self, url: str, dest: str | Path, *, referer: str | None = None) -> tuple[bool, str]:
        """Download `url` via the browser's request context (same cookies/TLS)."""
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if referer:
            self.ctx.set_extra_http_headers({"referer": referer})
        try:
            resp = self.ctx.request.get(url, timeout=180_000)
            if not resp.ok:
                return False, f"HTTP {resp.status}"
            body = resp.body()
            if len(body) < 10_000:
                return False, f"response too small ({len(body)} B)"
            dest.write_bytes(body)
            return True, f"ok {len(body)/1e6:.1f}MB"
        except Exception as e:
            return False, f"error: {str(e)[:120]}"
        finally:
            self.ctx.set_extra_http_headers({})


# JS executed in the live DOM: extracts comments (text, user, likes, time, images).
# Stable structural anchors (not ephemeral CSS classes):
#   - permalink  a[href*="/p/<code>/c/<id>/"]  (1 per comment, including replies)
#   - username   a[href="/username/"] in the permalink row
#   - text       next sibling of the row
#   - likes      span "Mi piace: N" / "Likes: N" in the actions row
#                (one level above the username+time row; absent when 0)
#   - is_reply   heuristic on DOM depth
EXTRACT_COMMENTS_JS = r"""
() => {
  const parseN = (s) => {
    s = (s||'').trim();
    let mult = 1;
    if (/k$/i.test(s)) { mult = 1e3; s = s.replace(/k$/i,''); }
    else if (/m$/i.test(s)) { mult = 1e6; s = s.replace(/m$/i,''); }
    s = s.replace(/\s/g,'');
    if (/\d[.,]\d{3}$/.test(s)) s = s.replace(/[.,]/g,'');   // 1.234 / 1,234 -> 1234
    else s = s.replace(',', '.');                              // 1,5 -> 1.5
    const n = parseFloat(s) * mult;
    return isFinite(n) ? Math.round(n) : 0;
  };
  const out = [];
  const seen = new Set();
  const links = Array.from(document.querySelectorAll('a[href]'));
  for (const a of links) {
    const h = a.getAttribute('href') || '';
    const m = h.match(/^\/(?:p|reel)\/[^/]+\/c\/(\d+)\//);
    if (!m || seen.has(m[1])) continue;
    seen.add(m[1]);
    const permalinkSpan = a.parentElement;
    const row = permalinkSpan ? permalinkSpan.parentElement : null;
    if (!row) continue;
    // comment container: walk up until it includes the actions row (likes/heart)
    let cont = row;
    for (let i = 0; i < 6 && cont.parentElement; i++) {
      cont = cont.parentElement;
      const tx = cont.textContent || '';
      if (tx.includes('Mi piace:') || tx.includes('Likes:') || tx.includes('Liked:')
          || cont.querySelector('svg[aria-label="Mi piace"], svg[aria-label="Like"]')) break;
    }
    let depth = 0, pp = row;
    while (pp && pp !== document.body) { depth++; pp = pp.parentElement; }
    // username
    let username = null;
    const uA = Array.from(row.querySelectorAll('a[href]')).find(x => {
      const hh = x.getAttribute('href') || '';
      return /^\/[^/]+\/?$/.test(hh);
    });
    if (uA) username = uA.textContent.trim();
    // time
    const t = row.querySelector('time[datetime]');
    const when = t ? t.getAttribute('datetime') : null;
    // text: siblings after the row, until we find non-empty text
    let text = '';
    let node = row.nextElementSibling;
    while (node) {
      const tt = (node.textContent || '').trim();
      if (tt && !/^(Rispondi|Reply|Inizia a commentare|Commenta)$/i.test(tt)) {
        text = tt; break;
      }
      node = node.nextElementSibling;
    }
    // likes: the count lives in a "Mi piace: N" span ("Likes: N" in EN)
    // in the comment's actions row; absent when 0 likes.
    let likes = 0;
    if (cont) {
      for (const sp of cont.querySelectorAll('span')) {
        const t = (sp.textContent || '').trim();
        const lm = t.match(/^(?:Mi piace|Liked|Likes?)\s*:\s*([\d.,]+[KM]?)/i);
        if (lm) { likes = parseN(lm[1]); break; }
      }
    }
    // images/stickers (exclude the s150x150 profile pic)
    const images = cont
      ? Array.from(cont.querySelectorAll('img'))
          .map(im => im.currentSrc || im.src || '')
          .filter(s => s.includes('cdninstagram') && !s.includes('s150x150') && !s.includes('profile_pic'))
      : [];
    out.push({ cid: m[1], username, text: text.slice(0, 2000), when,
               likes, images: images.slice(0, 4), depth });
  }
  const minD = out.length ? Math.min.apply(null, out.map(o => o.depth)) : 0;
  out.forEach(o => { o.is_reply = o.depth > minD + 2; delete o.depth; });
  return out;
}
"""

# Scroll of the popup's comments container via JS (mouse-independent):
# walks up from the first permalink to the parent with
# scrollHeight > clientHeight and scrolls it. Without this, scrolling would
# land on the feed behind the popup.
SCROLL_COMMENTS_JS = r"""
() => {
  const anchors = document.querySelectorAll('a[href*="/c/"]');
  if (!anchors.length) { window.scrollBy(0, 1200); return false; }
  let el = anchors[0].parentElement;
  while (el && el !== document.documentElement) {
    if (el.scrollHeight > el.clientHeight + 100) {
      el.scrollBy(0, 1200);
      return true;
    }
    el = el.parentElement;
  }
  window.scrollBy(0, 1200);
  return false;
}
"""


def fetch_page(url: str, cookies_path: str | Path | None = None, *,
               headless: bool = False, wait_ms: int = 8000,
               warmup: bool = True, locale: str = "it-IT",
               timezone: str = "Europe/Rome") -> tuple[str, str, list[dict]]:
    """Open `url` with Camoufox + cookies. Returns (html, final_url, cookies).

    warmup: first visits the same host's home page (the site generates
    session cookies like ig_shield before serving real data).
    """
    from camoufox.sync_api import Camoufox

    kwargs = {"headless": headless, "geoip": True}
    with Camoufox(**kwargs) as browser:
        ctx = browser.new_context(locale=locale, timezone_id=timezone)
        if cookies_path and Path(cookies_path).exists():
            cookies = netscape_to_playwright(cookies_path)
            ctx.add_cookies(cookies)
            log(f"Browser: {len(cookies)} cookies loaded from {cookies_path}")
        page = ctx.new_page()

        if warmup:
            home = re.match(r"(https?://[^/]+)", url)
            if home:
                try:
                    page.goto(home.group(1) + "/", wait_until="domcontentloaded",
                              timeout=60000)
                    page.wait_for_timeout(6000)
                except Exception as e:
                    log(f"Browser: home warmup failed ({e}); continuing")

        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(wait_ms)
        html = page.content()
        final_url = page.url
        page_cookies = ctx.cookies()
    return html, final_url, page_cookies


def fetch_and_save_page(url: str, cookies_path: str | Path | None, out_html: Path,
                        **kwargs) -> tuple[str, str]:
    """fetch_page + save raw HTML to disk (for debug/re-analysis)."""
    html, final_url, _ = fetch_page(url, cookies_path, **kwargs)
    out_html = Path(out_html)
    out_html.parent.mkdir(parents=True, exist_ok=True)
    out_html.write_text(html)
    return html, final_url


# ---------------------------------------------------------------------------
# Media download via browser context (browser cookies+headers)
# ---------------------------------------------------------------------------
def download_url(url: str, dest: str | Path, *, cookies_path: str | Path | None,
                 headless: bool = False, referer: str | None = None,
                 timeout_ms: int = 180_000) -> tuple[bool, str]:
    """Download `url` using the browser's request context (same cookies/TLS).

    Useful for CDNs that require the browser session (e.g. Instagram's CDN).
    Returns (ok, note).
    """
    from camoufox.sync_api import Camoufox
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with Camoufox(headless=headless, geoip=True) as browser:
        ctx = browser.new_context(locale="it-IT", timezone_id="Europe/Rome")
        if cookies_path and Path(cookies_path).exists():
            ctx.add_cookies(netscape_to_playwright(cookies_path))
        if referer:
            ctx.set_extra_http_headers({"referer": referer})
        req = ctx.request
        try:
            resp = req.get(url, timeout=timeout_ms)
            if not resp.ok:
                return False, f"HTTP {resp.status}"
            body = resp.body()
            if len(body) < 10_000:
                return False, f"response too small ({len(body)} B)"
            dest.write_bytes(body)
            return True, f"ok {len(body)/1e6:.1f}MB"
        finally:
            req.dispose()
