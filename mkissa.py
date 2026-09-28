#!/usr/bin/env python3
"""
Search, list, play and download from mkissa.to by driving a real browser.

Anime and manga are the same site with two different tails: a show has a list of
parts (`episodes` for anime, `chapters` for manga), and a part is either one video
embed or a run of page images. The shape of that difference lives in `Mode`; the
browser, the Cloudflare handling and the downloaders are shared.

The site gates its API behind a client computed token, ships source lists
encrypted in the response body, and sits behind Cloudflare. Its player is a stack
of third party embeds, and those hosts return 403 unless Referer matches the
embed page origin. Manga pages come off a CDN that answers 403 without a Referer
too.

So this never speaks the site API. It drives Chromium the way a visitor does and
reads results from the rendered DOM, then captures whatever the embed player
assigns to <video> to get a direct file URL, or whatever the reader loaded to get
a chapter's page images.

Needs playwright and a Chromium build. See README for details and NOTE.md for
the measured facts about the site.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import sys
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Iterable, Optional

from playwright.sync_api import Error as PWError
from playwright.sync_api import sync_playwright

BASE = "https://mkissa.to"
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36"
)
HLS_RE = re.compile(r"\.m3u8(\?|$)", re.I)
# Reader pages live at /images1<NN>/<id>/<chapter>/<lang>/<n>.<ext>. The path
# segment varies (images133, images138), the extension varies per page, and
# covers and avatars are images too, so match the whole path.
PAGE_RE = re.compile(r"/images\d+/(?:[^/]+/){3}(\d+)\.(jpe?g|png|webp)(?:\?|$)", re.I)


@dataclass(frozen=True)
class Mode:
    """Everything that differs between the anime and the manga half of the site."""

    name: str       # "anime" | "manga", as in --anime / --manga
    route: str      # url segment: /anime/<id>, /manga/<id>
    tab: str        # aria-label prefix of the part list tab
    part: str       # how a part reads in urls: "p-3-sub" | "chapter-3-sub"
    word: str       # how a part reads in English
    short: str      # how a part reads in a listing: "ep" | "ch"
    part_re: re.Pattern

    def show_path(self, media_id: str) -> str:
        return f"/{self.route}/{media_id}"

    def search_path(self, query: str) -> str:
        return f"/search/{self.route}?query={query}"


ANIME = Mode("anime", "anime", "Episodes", "p", "episode", "ep",
             re.compile(r"/p-([0-9.]+)-(sub|dub)\b"))
MANGA = Mode("manga", "manga", "Chapters", "chapter", "chapter", "ch",
             re.compile(r"/chapter-([0-9.]+)-(sub|dub)\b"))


@dataclass
class Show:
    id: str
    title: str
    type: Optional[str] = None
    season: Optional[str] = None
    score: Optional[str] = None
    episodes: Optional[str] = None   # aired / total
    aired: Optional[str] = None       # "sub" or "dub"
    url: str = ""

    def __str__(self) -> str:
        bits = [f"{self.id}  {self.title}"]
        meta = []
        if self.type:
            meta.append(self.type)
        if self.season:
            meta.append(self.season)
        if self.score:
            meta.append(f"score {self.score}")
        if self.episodes:
            meta.append(f"parts {self.episodes}"
                        + (f" ({self.aired})" if self.aired else ""))
        if meta:
            bits.append("[" + ", ".join(meta) + "]")
        return "  ".join(bits)


@dataclass
class Part:
    """One episode of an anime, or one chapter of a manga."""

    number: str
    label: str
    kind: str  # "sub" | "dub"
    url: str = ""


@dataclass
class Media:
    """A resolved anime episode: one direct file url plus the host's referer rule."""

    url: str
    referer: str
    source: str
    kind: str  # "file" | "hls"

    @property
    def is_hls(self) -> bool:
        return self.kind == "hls" or bool(HLS_RE.search(self.url))


@dataclass
class Page:
    """One page image of a manga chapter."""

    number: int
    url: str
    ext: str


@dataclass
class Chapter:
    """A resolved manga chapter: the reader page, plus its page images in order."""

    url: str
    referer: str
    pages: list[Page] = field(default_factory=list)


# Captures the url the embed player assigns to <video>, plus the frame it came
# from. Runs in every frame before page scripts.
_CATCH_JS = r"""
(() => {
  const d = HTMLMediaElement.prototype;
  const sd = Object.getOwnPropertyDescriptor(d, 'src');
  if (sd && sd.set) {
    Object.defineProperty(d, 'src', {
      configurable: true, enumerable: sd.enumerable,
      get() { return sd.get.call(this); },
      set(v) {
        try {
          (window.__mkMedia = window.__mkMedia || []).push(
            { url: String(v), frame: location.href });
        } catch (e) {}
        return sd.set.call(this, v);
      },
    });
  }
  const cu = URL.createObjectURL;
  URL.createObjectURL = function (o) {
    try {
      (window.__mkMedia = window.__mkMedia || []).push(
        { url: (o && o.src) || 'blob:', frame: location.href });
    } catch (e) {}
    return cu.call(this, o);
  };
})();
"""

_STEALTH = [
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--disable-dev-shm-usage",
]


class CloudflareChallenge(RuntimeError):
    pass


class NoSuchPart(RuntimeError):
    """The catalogue does not list the part that was asked for. Retrying is pointless."""


class MKissa:
    """A persistent, warmed up Chromium session against mkissa.to."""

    def __init__(self, *, headless: bool = True, profile: Optional[Path] = None,
                 verbose: bool = True, timeout: int = 45_000,
                 browser: Optional[str] = None):
        self.headless = headless
        self.profile = Path(profile or Path(__file__).parent / ".mkissa-profile")
        self.verbose = verbose
        self.timeout = timeout
        self.browser = browser or os.environ.get("MKISSA_BROWSER")
        self._pw = None
        self.ctx = None
        self.page = None

    def _launch_target(self) -> dict:
        """Playwright's Chromium by default, or the binary given by --browser.

        Cloudflare's verdict varies by engine, so a different browser can get
        through where the default is refused. That is opt in rather than guessed
        per machine.
        """
        if not self.browser:
            return {"channel": "chromium"}
        exe = Path(self.browser).expanduser()
        if not exe.is_file():
            raise SystemExit(f"error: --browser: no such file: {exe}")
        if exe.read_bytes()[:2] == b"#!":
            raise SystemExit(
                f"error: --browser: {exe} is a shell wrapper, not the browser "
                f"binary. Point it at the real executable instead."
            )
        return {"executable_path": str(exe)}

    def __enter__(self) -> "MKissa":
        global UA
        self.profile.mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        self.ctx = self._pw.chromium.launch_persistent_context(
            str(self.profile),
            headless=self.headless,
            args=_STEALTH,
            viewport={"width": 1366, "height": 768},
            locale="en-US",
            timezone_id="Asia/Bangkok",
            user_agent=UA,
            ignore_https_errors=True,
            **self._launch_target(),
        )
        self.ctx.set_default_timeout(self.timeout)
        self.ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        self.ctx.add_init_script(_CATCH_JS)
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        # Adopt the browser's own user agent so the downloader does not send one
        # that disagrees with the session Cloudflare just allowed.
        try:
            UA = self.page.evaluate("() => navigator.userAgent") or UA
        except PWError:
            pass
        self.log(f"[*] browser: {self.browser or 'chromium (playwright)'}  ua: {UA[:60]}")
        self.warmup()
        return self

    def __exit__(self, *exc) -> None:
        try:
            if self.ctx:
                self.ctx.close()
        finally:
            if self._pw:
                self._pw.stop()

    def log(self, *a) -> None:
        if self.verbose:
            print(*a, file=sys.stderr, flush=True)

    # navigation
    def _is_challenged(self) -> bool:
        try:
            t = (self.page.title() or "").lower()
        except PWError:
            return False
        return "just a moment" in t or "performing security verification" in t

    def _wait_out_challenge(self, tries: int = 20, wait: int = 3) -> None:
        """Ride out the Cloudflare interstitial.

        Never reload here. The interstitial self solves in place, and a reload
        restarts it and reads as an attacker.
        """
        for i in range(tries):
            if not self._is_challenged():
                return
            if self._try_turnstile(wait_ms=1500):
                self.page.wait_for_timeout(2500)
                if not self._is_challenged():
                    self.log(f"  [cf] interstitial cleared on attempt {i + 1}")
                    return
                self.log(f"  [cf] click {i + 1} rejected, still challenged")
            else:
                self.page.wait_for_timeout(wait * 1000)
        raise CloudflareChallenge(
            "Cloudflare interstitial would not clear. It self solves only for "
            "sessions it trusts; retry later or solve it in a real browser."
        )

    def open(self, url: str, *, settle: int = 2500) -> None:
        self.page.goto(url, wait_until="domcontentloaded")
        self.page.wait_for_timeout(settle)
        self._wait_out_challenge()

    def warmup(self) -> None:
        self.log("[*] warming up session on", BASE)
        self.open(BASE + "/", settle=3500)
        try:
            self.page.mouse.wheel(0, 1200)
            self.page.wait_for_timeout(1200)
        except PWError:
            pass

    # search
    def search(self, query: str, mode: Mode, limit: int = 20) -> list[Show]:
        """Search either catalogue.

        The header search box opens a captcha panel instead of a search box, so
        this goes straight at the search route, which is not gated.
        """
        self.log(f"[*] searching {mode.name} for {query!r}")
        self.open(BASE + mode.search_path(query), settle=3000)
        return self._scrape_results(mode, limit)

    def _scrape_results(self, mode: Mode, limit: int) -> list[Show]:
        # Scope to .media-card. The page also carries a "Community picks" rail of
        # links that is not search output.
        self.page.wait_for_selector(".media-card", timeout=15000)
        self.page.wait_for_timeout(1000)
        raw = self.page.evaluate(
            """({route, limit}) => {
              const seen = new Map();
              const sel = `a[href^='/${route}/']`;
              for (const card of document.querySelectorAll('.media-card')) {
                const a = card.querySelector(sel) || card.closest(sel);
                if (!a) continue;
                const m = (a.getAttribute('href') || '')
                  .match(new RegExp('^/' + route + '/([A-Za-z0-9]+)'));
                if (!m || seen.has(m[1])) continue;
                const t = (q) => {
                  const e = card.querySelector(q);
                  return e ? e.textContent.trim() : null;
                };
                seen.set(m[1], {
                  id: m[1],
                  title: t('.media-card__title'),
                  subtitle: t('.media-card__subtitle') || '',
                  score: t('.media-card__score-value'),
                  progress: t('.media-card__latest-progress-nums'),
                  kind: t('.media-card__latest-progress-tr'),
                });
                if (seen.size >= limit) break;
              }
              return [...seen.values()];
            }""",
            {"route": mode.route, "limit": limit},
        )
        out = []
        for r in raw:
            # subtitle looks like "TV · Fall 2024" / "Manga · Summer 2004"
            bits = [x.strip() for x in (r.get("subtitle") or "").split("·")]
            kind = re.sub(r"[()]", "", (r.get("kind") or "")).strip().lower()
            out.append(Show(
                id=r["id"], title=r["title"] or "",
                type=bits[0] if bits else None,
                season=" · ".join(bits[1:]) or None,
                score=r.get("score"),
                episodes=re.sub(r"[^\d/.cp ]", "", r.get("progress") or "") or None,
                aired=kind or None,
                url=BASE + mode.show_path(r["id"]),
            ))
        self.log(f"[*] {len(out)} results")
        return out

    # show / parts
    def open_show(self, media_id: str, mode: Mode) -> None:
        self.open(BASE + mode.show_path(media_id), settle=4000)

    def _part_cards(self, media_id: str, mode: Mode) -> list[tuple[str, str]]:
        """Every (href, label) on the show page's part tab, across all ranges.

        A long list is paginated by chapter number range (`37 – 86`, `1 – 36`)
        and scrolling does not load the rest, so every range button is visited.
        """
        self.open_show(media_id, mode)
        tab = self.page.query_selector(f"button.tab-item[aria-label^='{mode.tab}']")
        if tab:
            tab.click()
            self.page.wait_for_timeout(2200)

        read = """() => [...document.querySelectorAll('.media-ep-item[data-href]')]
            .map(e => [e.getAttribute('data-href'),
                       (e.innerText || '').replace(/\\s+/g, ' ').trim()])"""
        cards: dict[str, str] = {}
        for href, label in self.page.evaluate(read):
            cards[href] = label

        ranges = self.page.query_selector_all(".media-ep-list__pagination button")
        for i in range(len(ranges)):
            self.page.query_selector_all(".media-ep-list__pagination button")[i].click()
            self.page.wait_for_timeout(2000)
            for href, label in self.page.evaluate(read):
                cards.setdefault(href, label)

        if not cards:
            raise RuntimeError(
                f"no {mode.word} list for {media_id} (page: {self.page.url})")
        return list(cards.items())

    def parts(self, media_id: str, mode: Mode) -> list[Part]:
        cards = self._part_cards(media_id, mode)
        out: list[Part] = []
        seen: set[tuple[str, str]] = set()
        for href, label in cards:
            # data-href looks like /anime/<id>/p-<number>-<sub|dub>; it is the
            # only field that reliably carries the number (and can be "12.5").
            m = mode.part_re.search(href)
            if not m:
                continue
            number, kind = m.group(1), m.group(2)
            if (number, kind) in seen:
                continue
            seen.add((number, kind))
            out.append(Part(number=number, label=label, kind=kind,
                            url=BASE + href.split("?")[0]))
        self.log(f"[*] {len(out)} {mode.word}s on {media_id}")
        return out

    def _find_card(self, media_id: str, mode: Mode, number: str,
                   kind: str) -> Optional[str]:
        """The href of the requested part, or None if the list does not have it."""
        for href, _ in self._part_cards(media_id, mode):
            m = mode.part_re.search(href)
            if m and m.group(1) == number and m.group(2) == kind:
                return href
        return None

    # playback
    def open_part(self, media_id: str, number: str, kind: str, mode: Mode,
                  attempts: int = 6) -> None:
        """Walk the normal navigation path, retrying while Cloudflare refuses.

        Each attempt re-walks show page, part tab, part card instead of reloading
        one url, and the gaps stay human sized. The walk matters: a direct
        navigation to a part url is a fresh document request and gets the
        interstitial, while the same page reached by clicking never does.
        """
        last = None
        for i in range(attempts):
            try:
                self._walk_to_part(media_id, number, kind, mode)
                if mode is ANIME:
                    self.wait_for_sources(timeout_s=45)
                else:
                    self._wait_for_reader(timeout_s=45)
                return
            except NoSuchPart:
                raise
            except (RuntimeError, PWError) as e:
                last = e
                if i < attempts - 1:
                    gap = 4 + i * 3
                    self.log(f"  [retry] {i + 1}/{attempts} failed "
                             f"({str(e).splitlines()[0][:60]}); retry in {gap}s")
                    self.page.wait_for_timeout(gap * 1000)
        raise last if last else RuntimeError("could not open part")

    def _walk_to_part(self, media_id: str, number: str, kind: str,
                      mode: Mode) -> None:
        # Only the click gets in. A direct navigation to a part url is a fresh
        # document request and Cloudflare holds it, so a part that is not in the
        # list is reported rather than fetched. Use `episodes` to see what exists.
        href = self._find_card(media_id, mode, number, kind)
        if not href:
            raise NoSuchPart(
                f"no {mode.short} {number} ({kind}) in the list for {media_id}. "
                f"Run `episodes {media_id}` to see the {mode.word}s that are there.")
        card = self.page.query_selector(f'.media-ep-item[data-href="{href}"]')
        if card:
            card.click()
        else:                           # the list moved under us, take the url
            self.open(BASE + href, settle=3000)
        self.page.wait_for_timeout(2500)
        self._wait_out_challenge()

    def sources(self) -> list[str]:
        return self.page.eval_on_selector_all(
            ".episode-page__source-btn", "els => els.map(e => e.innerText.trim())")

    def _try_turnstile(self, wait_ms: int = 8000) -> bool:
        """Click the "Verify you are human" checkbox if one is showing.

        It sits in a shadow root, so this clicks by geometry. Wait for a real
        bounding box first, the widget is not laid out immediately.
        """
        deadline = time.time() + wait_ms / 1000
        while time.time() < deadline:
            for fr in self.page.frames:
                if "challenges.cloudflare.com" not in (fr.url or ""):
                    continue
                try:
                    box = fr.frame_element().bounding_box()
                except PWError:
                    continue
                if not box or box["width"] < 50 or box["height"] < 20:
                    continue
                x = box["x"] + box["width"] * 0.065
                y = box["y"] + box["height"] * 0.48
                try:
                    self.page.mouse.move(x, y)
                    self.page.wait_for_timeout(120)
                    self.page.mouse.click(x, y)
                    self.log("  [cf] clicked the verification checkbox")
                    return True
                except PWError:
                    pass
            self.page.wait_for_timeout(500)
        return False

    def _captcha_shown(self) -> bool:
        try:
            return bool(self.page.query_selector(".captcha-panel, .captcha-body"))
        except PWError:
            return False

    def wait_for_sources(self, timeout_s: int = 120) -> list[str]:
        deadline = time.time() + timeout_s
        clicks, last_click, warned = 0, 0.0, False
        while time.time() < deadline:
            tabs = self.sources()
            if tabs:
                self.log(f"[*] sources: {tabs}")
                return tabs
            self.page.wait_for_timeout(2000)
            # A few clicks at a human cadence. Hammering it every two seconds
            # just looks like an attacker.
            now = time.time()
            if self._captcha_shown() and clicks < 3 and now - last_click > 20:
                if self._try_turnstile():
                    clicks, last_click = clicks + 1, now
            elif not warned and not self._captcha_shown():
                warned = True
                self.log("  [!] watch page has not produced sources yet; waiting")
        raise RuntimeError(
            "watch page never produced sources"
            + (" (Cloudflare verification still pending)" if self._captcha_shown() else "")
            + ". Try again in a few minutes."
        )

    def resolve_source(self, label: str, timeout_s: int = 45) -> Media:
        """Click a source tab, then capture the url the embed settles on."""
        btn = self.page.query_selector(f'.episode-page__source-btn:has-text("{label}")')
        if not btn:
            raise RuntimeError(f"no such source tab: {label}")
        btn.click()
        self.log(f"[*] source {label}: waiting for embed to resolve")

        deadline = time.time() + timeout_s
        seen: list[tuple[str, str]] = []
        while time.time() < deadline:
            for fr in self.page.frames:
                if fr == self.page.main_frame:
                    continue
                try:
                    for hit in fr.evaluate("() => window.__mkMedia || []"):
                        u = (hit.get("url") or "").strip()
                        if u and not u.startswith("blob:") and u not in dict(seen):
                            seen.append((u, hit.get("frame") or fr.url))
                except PWError:
                    pass
            if seen:
                break
            self.page.wait_for_timeout(500)

        if not seen:
            # Fallback: a <video> already in the DOM, in case the hook missed it.
            for fr in self.page.frames:
                if fr == self.page.main_frame:
                    continue
                try:
                    srcs = fr.evaluate(
                        "() => [...document.querySelectorAll('video')]"
                        ".map(v => v.currentSrc || v.src).filter(Boolean)")
                    for s in srcs:
                        if s not in dict(seen):
                            seen.append((s, fr.url))
                except PWError:
                    pass
        if not seen:
            raise RuntimeError(f"source {label}: embed never exposed a media URL")

        url, frame_url = seen[0]
        self.log(f"[*] resolved -> {url[:120]}")
        return Media(url=url, referer=_origin(frame_url), source=label,
                     kind="hls" if HLS_RE.search(url) else "file")

    def resolve_best(self, prefer: Optional[str] = None,
                     timeout_s: int = 45) -> Media:
        """Try preferred source first, then fall back through the other tabs."""
        available = self.sources()
        self.log(f"[*] sources available: {available}")
        errors = []
        for label in _source_order(available, prefer):
            try:
                return self.resolve_source(label, timeout_s=timeout_s)
            except Exception as e:      # noqa: BLE001 - try the next embed
                errors.append(f"{label}: {e}")
                self.log(f"  [warn] {label} failed: {e}")
        raise RuntimeError("no source resolved. " + " | ".join(errors))

    # manga reader
    def _wait_for_reader(self, timeout_s: int = 60) -> None:
        """Wait for the reader to mount its first page slots."""
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                if self.page.query_selector(".reader-page"):
                    return
            except PWError:
                pass
            if self._captcha_shown() and self._try_turnstile():
                pass
            self.page.wait_for_timeout(2000)
        raise RuntimeError(
            f"reader never mounted on {self.page.url}. Try again in a few minutes.")

    # How long to let pending page images land before deciding the scroll is done.
    _SETTLE_MS = 15_000
    _SETTLE_POLL_MS = 400

    def _pending_pages(self) -> int:
        """Page images the reader has in the DOM but has not finished loading.

        A slot is created before its request completes, so this is the honest
        measure of how far behind the scroll is running.
        """
        return self.page.evaluate(
            """() => [...document.querySelectorAll('.reader-page img')]
                .filter(i => i.currentSrc && !i.complete).length""")

    def _settle_reader(self) -> None:
        """Wait until the reader has no page image in flight."""
        deadline = time.time() + self._SETTLE_MS / 1000
        while time.time() < deadline:
            try:
                if self._pending_pages() == 0:
                    return
            except PWError:
                return
            self.page.wait_for_timeout(self._SETTLE_POLL_MS)

    def _scroll_reader(self) -> None:
        """Walk the chapter to its end so every page gets loaded at least once.

        The reader starts a page's request when its slot scrolls into view, so the
        scroll has to wait for the images in view to land. Stepping without
        waiting outruns the CDN: the slots go past, the requests never happen, and
        the chapter comes back with a hole in it. One big jump is worse still, it
        skips everything in between.
        """
        prev, stall = -1, 0
        while stall < 3:
            try:
                self._settle_reader()
                count = len(self.page.evaluate(
                    "() => performance.getEntriesByType('resource')"
                    ".filter(e => /\\/images\\d+\\//.test(e.name))"))
                at_end = self.page.evaluate(
                    "() => scrollY + innerHeight >= document.body.scrollHeight - 40")
            except PWError:
                return
            stall = stall + 1 if (count == prev and at_end) else 0
            prev = count
            self.page.mouse.wheel(0, 600)
            self.page.wait_for_timeout(350)

    def _probe_page(self, stem: str, number: int, exts: Iterable[str]) -> Optional[Page]:
        """Ask the image host whether a page exists. A miss is a small html error."""
        for ext in exts:
            url = f"{stem}{number}.{ext}"
            try:
                r = self.ctx.request.get(url, headers={"referer": BASE + "/"})
            except PWError:
                continue
            if r.status == 200 and r.headers.get("content-type", "").startswith("image/"):
                return Page(number=number, url=url, ext=ext)
        return None

    def pages(self, media_id: str, number: str, kind: str = "sub") -> list[Page]:
        """Every page image of a chapter, in order.

        The reader recycles its page slots as you scroll, so the live DOM is only
        a window onto the chapter. The browser's resource timeline keeps what was
        loaded, and anything the reader never showed is asked for by url.
        """
        self.open_part(media_id, number, kind, MANGA)
        self._scroll_reader()

        urls = self.page.evaluate(
            "() => performance.getEntriesByType('resource').map(e => e.name)")
        found: dict[int, Page] = {}
        for u in urls:
            m = PAGE_RE.search(u)
            if m and int(m.group(1)) not in found:
                found[int(m.group(1))] = Page(number=int(m.group(1)), url=u,
                                              ext=m.group(2).lower())
        if not found:
            raise RuntimeError(f"chapter {number} exposed no page images "
                               f"(page: {self.page.url})")
        self.log(f"[*] {len(found)} pages loaded by the reader")

        # The reader may have skipped a page, and the last slot may not be the
        # last page, so fill the gaps and keep going until the host says no. The
        # url is a plain path with a trailing slash, so <stem><n>.<ext> is right.
        last = max(found)
        stem = re.sub(r"/\d+\.\w+$/", "/", found[last].url)
        exts = [found[last].ext, "jpg", "png", "webp"]
        for n in range(1, last + 1):
            if n in found:
                continue
            page = self._probe_page(stem, n, exts)
            if page:
                self.log(f"  [gap] page {n} was not in the reader, fetched by url")
                found[n] = page
            else:
                self.log(f"  [warn] page {n} is missing from this chapter")
        n = last + 1
        while (page := self._probe_page(stem, n, exts)) is not None:
            found[n] = page
            n += 1
        return [found[k] for k in sorted(found)]

    def chapter(self, media_id: str, number: str, kind: str = "sub") -> Chapter:
        return Chapter(url=self.page.url, referer=_origin(self.page.url),
                       pages=self.pages(media_id, number, kind))

    def fetch_page(self, page: Page, referer: str) -> bytes:
        """Page bytes, through the session that was allowed past Cloudflare.

        The image host answers 403 to a bare request whatever the user agent, and
        replies with an html error page, so status and content type are both
        checked before these bytes are treated as an image.
        """
        r = self.ctx.request.get(page.url, headers={"referer": referer or BASE + "/"})
        body = r.body()
        if r.status != 200 or not r.headers.get("content-type", "").startswith("image/"):
            raise RuntimeError(
                f"page {page.number}: host refused the image ({r.status} "
                f"{r.headers.get('content-type')}). It gates on Referer and that may "
                f"have changed; re-run to resolve a fresh one.")
        return body


def _origin(url: str) -> str:
    m = re.match(r"^(https?://[^/]+)", url or "")
    return m.group(1) if m else ""


def _source_order(available: list[str], prefer: Optional[str] = None) -> list[str]:
    """Put the tabs that hand back a whole file ahead of the stream only ones.

    The tab set changes per episode, so order by what the name promises instead of
    a hardcoded list that goes stale.
    """
    if prefer and prefer in available:
        return [prefer] + [t for t in available if t != prefer]
    direct = [t for t in available if any(k in t.lower() for k in ("mp4", "file"))]
    rest = [t for t in available if t not in direct]
    return direct + rest


def download(media: Media, dest: Path, *, resume: bool = True,
             progress: bool = True) -> Path:
    """Stream to disk, keeping a <dest>.part until it finishes.

    The embed hosts return 403 unless Referer matches the page they were served
    from, so it goes on every request. Resume uses Range.
    """
    dest = Path(dest).expanduser()
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_suffix(dest.suffix + ".part")

    have = partial.stat().st_size if (resume and partial.exists()) else 0
    headers = {"User-Agent": UA, "Referer": media.referer or BASE + "/"}
    if have:
        headers["Range"] = f"bytes={have}-"
    req = urllib.request.Request(media.url, headers=headers)
    try:
        r = urllib.request.urlopen(req, timeout=60)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise RuntimeError(
                f"host refused the download ({e.code}). These embed hosts gate on "
                f"Referer and it may have changed; re-run to resolve a fresh one."
            ) from e
        raise
    with r:
        total = None
        if r.headers.get("Content-Range"):
            total = have + int(r.headers["Content-Range"].rsplit("/", 1)[-1] or 0)
        elif r.headers.get("Content-Length"):
            total = (have if r.status == 206 else 0) + int(r.headers["Content-Length"])
        mode = "ab" if (r.status == 206 and have) else "wb"
        if mode == "wb":
            have = 0
        t0 = time.time()
        with open(partial, mode) as out:
            while True:
                b = r.read(1 << 20)
                if not b:
                    break
                out.write(b)
                have += len(b)
                if progress:
                    el = max(time.time() - t0, 1e-6)
                    pct = f"{have / total * 100:5.1f}%" if total else "  ?  "
                    print(f"\r  {pct}  {have / 1e6:8.1f} MB  {have / el / 1e6:5.1f} MB/s",
                          end="", file=sys.stderr, flush=True)
    if progress:
        print(file=sys.stderr)
    partial.replace(dest)
    return dest


def download_chapter(session: MKissa, chapter: Chapter, dest: Path, *,
                     images: bool = False, progress: bool = True) -> Path:
    """Save a chapter as one .cbz, or with --images as loose numbered pages.

    Stored, not deflated: the pages are already compressed, and readers want the
    bytes as they came. An .cbz is written in one pass, so it is not resumable;
    the loose page form skips pages already on disk.
    """
    dest = Path(dest).expanduser()
    dest.parent.mkdir(parents=True, exist_ok=True)
    # An .cbz is a zip of the pages as they came, so it is written in one pass and
    # is not resumable. The loose form writes into a folder and keeps what is
    # already there, which is how a part finished download is picked up again.
    zipper = None if images else zipfile.ZipFile(dest, "w", zipfile.ZIP_STORED)
    if images:
        dest.mkdir(parents=True, exist_ok=True)

    total_bytes = 0
    try:
        for i, page in enumerate(chapter.pages, 1):
            name = f"page-{page.number:03d}.{page.ext}"
            if images and (dest / name).exists() and (dest / name).stat().st_size:
                if progress:
                    print(f"  {i:>3}/{len(chapter.pages)}  {name}  have it",
                          file=sys.stderr, flush=True)
                continue
            body = session.fetch_page(page, chapter.referer)
            total_bytes += len(body)
            if zipper is not None:
                zipper.writestr(name, body)
            else:
                (dest / name).write_bytes(body)
            if progress:
                print(f"  {i:>3}/{len(chapter.pages)}  {name}  "
                      f"{len(body) / 1e6:.2f} MB", file=sys.stderr, flush=True)
    finally:
        if zipper is not None:
            zipper.close()
    if progress:
        print(f"[+] {len(chapter.pages)} pages, {total_bytes / 1e6:.1f} MB -> {dest}",
              file=sys.stderr, flush=True)
    return dest


def open_in_desktop(target: str) -> int:
    """Hand a url to the desktop's own handler.

    No assumption about which app that is, which is the point: mpv for a stream,
    a browser for a reader page, a picture viewer for a page image, whatever the
    user has wired up.
    """
    import shutil
    import subprocess
    if sys.platform == "darwin":
        cmd = ["open", target]
    elif os.name == "nt":
        cmd = ["cmd", "/c", "start", "", target]
    else:
        if not (xdg := shutil.which("xdg-open")):
            raise RuntimeError("xdg-open not found, open the url by hand: " + target)
        cmd = [xdg, target]
    return subprocess.call(cmd)


def _safe(name: str) -> str:
    return re.sub(r"[^\w.\-]+", "_", name).strip("_")[:80] or "video"


def _stem(args) -> str:
    return _safe(args.name or f"{args.media_id}_{args.mode.short}{args.part}")


def cmd_search(a: MKissa, args) -> int:
    for s in a.search(args.query, args.mode, limit=args.limit):
        print(s)
    return 0


def cmd_episodes(a: MKissa, args) -> int:
    for p in a.parts(args.media_id, args.mode):
        print(f"{p.kind:3} {p.number:>5}  {p.label}")
    return 0


def cmd_sources(a: MKissa, args) -> int:
    _need_anime(args)
    a.open_part(args.media_id, args.part, args.kind, ANIME)
    for s in a.sources():
        print(s)
    return 0


def cmd_pages(a: MKissa, args) -> int:
    _need_manga(args)
    for p in a.pages(args.media_id, args.part, args.kind):
        print(f"{p.number:>4}  {p.url}")
    return 0


def _need_anime(args) -> None:
    if args.mode is not ANIME:
        raise RuntimeError(f"`{args.cmd}` is anime only. Drop --manga, or use "
                           f"`--manga url|download|play`.")


def _need_manga(args) -> None:
    if args.mode is not MANGA:
        raise RuntimeError(f"`{args.cmd}` is manga only. Add --manga.")


def _get_media(a: MKissa, args) -> Media:
    a.open_part(args.media_id, args.part, args.kind, ANIME)
    return a.resolve_best(prefer=args.source, timeout_s=args.timeout)


def cmd_url(a: MKissa, args) -> int:
    if args.mode is MANGA:
        pages = a.pages(args.media_id, args.part, args.kind)
        referer = _origin(a.page.url)
        if args.json:
            print(json.dumps({"reader": a.page.url, "referer": referer,
                              "pages": [asdict(p) for p in pages]}, indent=2))
        else:
            print(pages[0].url)
            print(f"# {len(pages)} pages, referer: {referer}", file=sys.stderr)
        return 0
    m = _get_media(a, args)
    if args.json:
        print(json.dumps(asdict(m), indent=2))
    else:
        print(m.url)
        print(f"# referer: {m.referer}", file=sys.stderr)
    return 0


def cmd_download(a: MKissa, args) -> int:
    if args.mode is MANGA:
        ch = a.chapter(args.media_id, args.part, args.kind)
        dest = Path(args.out) if args.out else Path(
            args.dir) / (_stem(args) if args.images else _stem(args) + ".cbz")
        print(f"[+] {len(ch.pages)} pages from {ch.url}\n[+] saving to {dest}",
              file=sys.stderr)
        download_chapter(a, ch, dest, images=args.images)
        print(f"[+] done: {dest}", file=sys.stderr)
        return 0
    m = _get_media(a, args)
    if m.is_hls:
        stem = args.out or str(Path(args.dir) / _stem(args))
        note = Path(stem + ".url")
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text(m.url + "\n# referer: " + m.referer + "\n")
        print(f"[!] source {m.source} is an HLS playlist, not a single file.\n"
              f"    playlist written to: {note}\n"
              f"    play it:   mpv --referrer='{m.referer}' '{m.url}'\n"
              f"    remux it:  ffmpeg -headers $'Referer: {m.referer}\\r\\n' "
              f"-i '{m.url}' -c copy out.mp4", file=sys.stderr)
        return 2
    dest = Path(args.out) if args.out else Path(args.dir) / f"{_stem(args)}.mp4"
    print(f"[+] source {m.source}\n[+] {m.url}\n[+] saving to {dest}", file=sys.stderr)
    download(m, dest, resume=not args.no_resume)
    print(f"[+] done: {dest}", file=sys.stderr)
    return 0


def cmd_play(a: MKissa, args) -> int:
    if args.mode is MANGA:
        # The reader page is the chapter: one scrollable document of pictures,
        # and the desktop opens it with whatever it opens pages with.
        a.open_part(args.media_id, args.part, args.kind, MANGA)
        target = a.page.url
        a.log(f"[*] opening the reader; `--manga url` prints the page image urls")
    else:
        m = _get_media(a, args)
        target = m.url
        a.log(f"[*] source {m.source}  referer: {m.referer}\n"
              f"    (that host gates on Referer; a player that cannot set it "
              f"will show nothing)")
    print(f"[+] {target}", file=sys.stderr)
    return open_in_desktop(target)


def cmd_clear(args) -> int:
    """Open a real window and wait for a human to click the checkbox.

    The clearance cookie lands in the profile and gets reused after that.
    """
    profile = args.profile or Path(__file__).parent / ".mkissa-profile"
    print("A browser window will open on a part page.", file=sys.stderr)
    print("Click the 'Verify you are human' checkbox when it appears.", file=sys.stderr)
    print("The clearance is stored in", profile, file=sys.stderr)
    with MKissa(headless=False, profile=profile, verbose=True) as b:
        b.open_part(args.media_id, args.part, args.kind, args.mode)
        try:
            b.wait_for_sources(timeout_s=args.timeout) if args.mode is ANIME \
                else b._wait_for_reader(timeout_s=args.timeout)
        except RuntimeError:
            print("\nNot cleared. Keep the window open, click the checkbox, and "
                  "re-run `clear` if it did not stick.", file=sys.stderr)
            return 3
        print("\nCleared. You can close the window.", file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mkissa", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--anime", dest="mode", action="store_const", const=ANIME,
                   help="search and fetch anime (default)")
    p.add_argument("--manga", dest="mode", action="store_const", const=MANGA,
                   help="search and fetch manga")
    p.add_argument("--headed", action="store_true",
                   help="show the browser (helps when headless is detected)")
    p.add_argument("--profile", type=Path, help="persistent profile dir")
    p.add_argument("--browser", metavar="PATH",
                   help="browser binary to drive, e.g. /opt/brave-bin/brave")
    p.add_argument("-q", "--quiet", action="store_true")
    p.set_defaults(mode=ANIME)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("search", help="search the anime or manga catalogue")
    s.add_argument("query")
    s.add_argument("-n", "--limit", type=int, default=20)
    s.set_defaults(fn=cmd_search)

    s = sub.add_parser("episodes",
                       help="list an anime's episodes, or a manga's chapters")
    s.add_argument("media_id")
    s.set_defaults(fn=cmd_episodes)

    s = sub.add_parser("sources", help="list a watchable episode's source tabs (anime)")
    s.add_argument("media_id")
    s.add_argument("part")
    s.add_argument("--kind", choices=["sub", "dub"], default="sub")
    s.set_defaults(fn=cmd_sources)

    s = sub.add_parser("pages", help="list a chapter's page image urls (manga)")
    s.add_argument("media_id")
    s.add_argument("part")
    s.add_argument("--kind", choices=["sub", "dub"], default="sub")
    s.set_defaults(fn=cmd_pages)

    def add_part_opts(sp):
        sp.add_argument("media_id")
        sp.add_argument("part", help="episode number, or chapter number")
        sp.add_argument("--kind", choices=["sub", "dub"], default="sub")
        sp.add_argument("--timeout", type=int, default=45,
                        help="seconds to let a part load")
        sp.add_argument("--name", help="filename stem")

    s = sub.add_parser("url", help="print the resolved direct media url")
    add_part_opts(s)
    s.add_argument("--source", help="prefer a source tab, anime only (default: auto)")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_url)

    s = sub.add_parser("download", help="download an episode, or a chapter")
    add_part_opts(s)
    s.add_argument("--source", help="prefer a source tab, anime only (default: auto)")
    s.add_argument("-o", "--out", help="output file path")
    s.add_argument("-d", "--dir", default=".", help="output directory")
    s.add_argument("--no-resume", action="store_true", help="anime only")
    s.add_argument("--images", action="store_true",
                   help="manga only: write loose page files instead of one .cbz")
    s.set_defaults(fn=cmd_download)

    s = sub.add_parser("play", help="hand the part to the desktop's own player")
    add_part_opts(s)
    s.add_argument("--source", help="prefer a source tab, anime only (default: auto)")
    s.set_defaults(fn=cmd_play)

    s = sub.add_parser("clear", help="solve the Cloudflare checkbox by hand")
    s.add_argument("media_id")
    s.add_argument("part", nargs="?", default="1")
    s.add_argument("--kind", choices=["sub", "dub"], default="sub")
    s.add_argument("--timeout", type=int, default=300)
    s.set_defaults(fn=cmd_clear)
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "clear":
        return cmd_clear(args)
    with MKissa(headless=not args.headed, profile=args.profile,
                browser=args.browser, verbose=not args.quiet) as session:
        return args.fn(session, args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except CloudflareChallenge as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(3)
    except (RuntimeError, PWError) as e:
        # Expected operational failures (captcha, dead source, a markup change).
        first = str(e).strip().splitlines()[0] if str(e).strip() else e.__class__.__name__
        print(f"error: {first}", file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        sys.exit(130)
