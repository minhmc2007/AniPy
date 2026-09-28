#!/usr/bin/env python3
"""
Search, list episodes, play and download from mkissa.to by driving a real browser.

The site gates its API behind a client computed token, ships source lists
encrypted in the response body, and sits behind Cloudflare. Its player is a stack
of third party embeds, and those hosts return 403 unless Referer matches the
embed page origin.

So this never speaks the site API. It drives Chromium the way a visitor does and
reads results from the rendered DOM, then captures whatever the embed player
assigns to <video> to get a direct file URL.

Needs playwright and a Chromium build. See README for the mpv extra.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, asdict
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
            meta.append(f"ep {self.episodes}" + (f" ({self.aired})" if self.aired else ""))
        if meta:
            bits.append("[" + ", ".join(meta) + "]")
        return "  ".join(bits)


@dataclass
class Episode:
    number: str
    label: str
    kind: str  # "sub" | "dub"
    url: str = ""


@dataclass
class Media:
    url: str
    referer: str
    source: str
    kind: str  # "file" | "hls"

    @property
    def is_hls(self) -> bool:
        return self.kind == "hls" or bool(HLS_RE.search(self.url))


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

    # -- navigation --------------------------------------------------------
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

    # -- search ------------------------------------------------------------
    def search(self, query: str, limit: int = 20) -> list[Show]:
        self.log(f"[*] searching {query!r}")
        self.open(BASE + "/", settle=3000)

        trigger = self.page.query_selector(".header-search__trigger")
        if trigger:
            trigger.click()
            self.page.wait_for_timeout(600)
        else:
            self.open(f"{BASE}/search/anime?query={query}", settle=3000)
            return self._scrape_results(limit)

        box = self.page.wait_for_selector(
            '[role="dialog"] input[type="search"], [role="dialog"] input',
            timeout=8000)
        box.click()
        box.type(query, delay=70)          # human-ish keystrokes
        self.page.wait_for_timeout(400)
        self.page.keyboard.press("Enter")
        self.page.wait_for_timeout(3500)
        self._wait_out_challenge()
        return self._scrape_results(limit)

    def _scrape_results(self, limit: int) -> list[Show]:
        # Scope to .media-card. The page also carries a "Community picks" rail of
        # /anime/ links that is not search output.
        self.page.wait_for_selector(".media-card", timeout=15000)
        self.page.wait_for_timeout(1000)
        raw = self.page.evaluate(
            """(limit) => {
              const seen = new Map();
              for (const card of document.querySelectorAll('.media-card')) {
                const a = card.querySelector("a[href^='/anime/']")
                       || card.closest('a[href^="/anime/"]');
                if (!a) continue;
                const m = (a.getAttribute('href') || '').match(/^\\/anime\\/([A-Za-z0-9]+)/);
                if (!m || seen.has(m[1])) continue;
                const t = (sel) => {
                  const e = card.querySelector(sel);
                  return e ? e.textContent.trim() : null;
                };
                const sub = t('.media-card__subtitle') || '';
                seen.set(m[1], {
                  id: m[1],
                  title: t('.media-card__title'),
                  subtitle: sub,
                  score: t('.media-card__score-value'),
                  progress: t('.media-card__latest-progress-nums'),
                  kind: t('.media-card__latest-progress-tr'),
                });
                if (seen.size >= limit) break;
              }
              return [...seen.values()];
            }""",
            limit,
        )
        out = []
        for r in raw:
            # subtitle looks like "TV · Fall 2024" / "Special · Fall 2024"
            subtitle = (r.get("subtitle") or "")
            bits = [x.strip() for x in subtitle.split("·")]
            kind = re.sub(r"[()]", "", (r.get("kind") or "")).strip().lower()
            out.append(Show(
                id=r["id"], title=r["title"] or "",
                type=bits[0] if bits else None,
                season=" · ".join(bits[1:]) or None,
                score=r.get("score"),
                episodes=re.sub(r"[^\d/]", "", r.get("progress") or "") or None,
                aired=kind or None,
                url=f"{BASE}/anime/{r['id']}",
            ))
        self.log(f"[*] {len(out)} results")
        return out

    # -- show / episodes ---------------------------------------------------
    def open_show(self, anime_id: str) -> None:
        self.open(f"{BASE}/anime/{anime_id}", settle=4000)
    def episodes(self, anime_id: str) -> list[Episode]:
        self.open_show(anime_id)
        tab = self.page.query_selector("button.tab-item[aria-label^='Episodes']")
        if tab:
            tab.click()
            self.page.wait_for_timeout(2200)
        cards = self.page.query_selector_all(".media-ep-item")
        if not cards:
            raise RuntimeError(
                f"no episode list for {anime_id} (page: {self.page.url})")
        eps: list[Episode] = []
        seen: set[str] = set()
        for c in cards:
            # data-href looks like /anime/<id>/p-<number>-<sub|dub>; it is the
            # only field that reliably carries the number (and can be "12.5").
            href = c.get_attribute("data-href") or ""
            m = re.search(r"/p-([0-9.]+)-(sub|dub)\b", href)
            if m:
                number, kind = m.group(1), m.group(2)
                url = BASE + href.split("?")[0]
            else:
                text = c.inner_text().strip()
                number = re.sub(r"(?i)^ep\.?\s*", "", text).strip().split("\n")[0]
                kind = "dub" if c.evaluate(
                    "e => !!e.closest('[class*=dub], [data-kind=dub]')") else "sub"
                url = f"{BASE}/anime/{anime_id}/p-{number}-{kind}"
            if not number or (number, kind) in seen:
                continue
            seen.add((number, kind))
            eps.append(Episode(number=number, label=c.inner_text().strip().replace("\n", " "),
                               kind=kind, url=url))
        self.log(f"[*] {len(eps)} episodes on {anime_id}")
        return eps

    # -- playback ----------------------------------------------------------
    def open_episode(self, anime_id: str, number: str, kind: str = "sub",
                     attempts: int = 6) -> None:
        """Walk the normal navigation path, retrying while Cloudflare refuses.

        Each attempt re-walks show page, Episodes tab, episode card instead of
        reloading one url, and the gaps stay human sized.
        """
        last = None
        for i in range(attempts):
            try:
                self._walk_to_episode(anime_id, number, kind)
                self.wait_for_sources(timeout_s=45)
                return
            except (RuntimeError, PWError) as e:
                last = e
                if i < attempts - 1:
                    gap = 4 + i * 3
                    self.log(f"  [retry] {i + 1}/{attempts} failed "
                             f"({str(e).splitlines()[0][:60]}); retry in {gap}s")
                    self.page.wait_for_timeout(gap * 1000)
        raise last if last else RuntimeError("could not open episode")

    def _walk_to_episode(self, anime_id: str, number: str, kind: str) -> None:
        self.open_show(anime_id)
        tab = self.page.query_selector("button.tab-item[aria-label^='Episodes']")
        if tab:
            tab.click()
            self.page.wait_for_timeout(1500)
        card = self.page.query_selector(f'.media-ep-item:has-text("{number}")')
        if card:
            card.click()
        else:
            self.open(f"{BASE}/anime/{anime_id}/p-{number}-{kind}", settle=3000)
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


def play_with_mpv(media: Media, *, extra: Iterable[str] = ()) -> int:
    import shutil
    import subprocess
    if not (mpv := shutil.which("mpv")):
        raise RuntimeError("mpv not found, install it or use `download` instead")
    cmd = [mpv, "--referrer=" + (media.referer or BASE + "/"),
           "--user-agent=" + UA, *extra, media.url]
    return subprocess.call(cmd)


def _safe(name: str) -> str:
    return re.sub(r"[^\w.\-]+", "_", name).strip("_")[:80] or "video"


def cmd_search(a: MKissa, args) -> int:
    for s in a.search(args.query, limit=args.limit):
        print(s)
    return 0


def cmd_episodes(a: MKissa, args) -> int:
    for e in a.episodes(args.anime_id):
        print(f"{e.kind:3} {e.number:>5}  {e.label}")
    return 0


def cmd_sources(a: MKissa, args) -> int:
    a.open_episode(args.anime_id, args.episode, args.kind)
    for s in a.sources():
        print(s)
    return 0


def _get_media(a: MKissa, args) -> Media:
    a.open_episode(args.anime_id, args.episode, args.kind)
    return a.resolve_best(prefer=args.source, timeout_s=args.timeout)


def cmd_url(a: MKissa, args) -> int:
    m = _get_media(a, args)
    if args.json:
        print(json.dumps(asdict(m), indent=2))
    else:
        print(m.url)
        print(f"# referer: {m.referer}", file=sys.stderr)
    return 0


def cmd_download(a: MKissa, args) -> int:
    m = _get_media(a, args)
    if m.is_hls:
        stem = args.out or str(Path(args.dir) /
                               f"{_safe(args.name or args.anime_id + '_ep' + args.episode)}")
        note = Path(stem + ".url")
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text(m.url + "\n# referer: " + m.referer + "\n")
        print(f"[!] source {m.source} is an HLS playlist, not a single file.\n"
              f"    playlist written to: {note}\n"
              f"    play it:   mpv --referrer='{m.referer}' '{m.url}'\n"
              f"    remux it:  ffmpeg -headers $'Referer: {m.referer}\\r\\n' "
              f"-i '{m.url}' -c copy out.mp4", file=sys.stderr)
        return 2
    dest = Path(args.out) if args.out else Path(
        args.dir) / f"{_safe(args.name or args.anime_id + '_ep' + args.episode)}.mp4"
    print(f"[+] source {m.source}\n[+] {m.url}\n[+] saving to {dest}", file=sys.stderr)
    download(m, dest, resume=not args.no_resume)
    print(f"[+] done: {dest}", file=sys.stderr)
    return 0


def cmd_play(a: MKissa, args) -> int:
    m = _get_media(a, args)
    return play_with_mpv(m, extra=args.mpv_args.split() if args.mpv_args else ())


def cmd_clear(args) -> int:
    """Open a real window and wait for a human to click the checkbox.

    The clearance cookie lands in the profile and gets reused after that.
    """
    profile = args.profile or Path(__file__).parent / ".mkissa-profile"
    print("A browser window will open on a watch page.", file=sys.stderr)
    print("Click the 'Verify you are human' checkbox when it appears.", file=sys.stderr)
    print("The clearance is stored in", profile, file=sys.stderr)
    with MKissa(headless=False, profile=profile, verbose=True) as b:
        b.open_show(args.anime_id)
        tab = b.page.query_selector("button.tab-item[aria-label^='Episodes']")
        if tab:
            tab.click()
            b.page.wait_for_timeout(2000)
        card = b.page.query_selector(".media-ep-item")
        if card:
            card.click()
        else:
            b.open(f"{BASE}/anime/{args.anime_id}/p-{args.episode}-sub", settle=5000)
        try:
            b.wait_for_sources(timeout_s=args.timeout)
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
    p.add_argument("--headed", action="store_true",
                   help="show the browser (helps when headless is detected)")
    p.add_argument("--profile", type=Path, help="persistent profile dir")
    p.add_argument("--browser", metavar="PATH",
                   help="browser binary to drive, e.g. /opt/brave-bin/brave")
    p.add_argument("-q", "--quiet", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("search", help="search the catalogue")
    s.add_argument("query")
    s.add_argument("-n", "--limit", type=int, default=20)
    s.set_defaults(fn=cmd_search)

    s = sub.add_parser("episodes", help="list an anime's episodes")
    s.add_argument("anime_id")
    s.set_defaults(fn=cmd_episodes)

    s = sub.add_parser("sources", help="list a watchable episode's source tabs")
    s.add_argument("anime_id")
    s.add_argument("episode")
    s.add_argument("--kind", choices=["sub", "dub"], default="sub")
    s.set_defaults(fn=cmd_sources)

    def add_watch_opts(sp):
        sp.add_argument("anime_id")
        sp.add_argument("episode")
        sp.add_argument("--kind", choices=["sub", "dub"], default="sub")
        sp.add_argument("--source", help="prefer a source tab (default: auto)")
        sp.add_argument("--timeout", type=int, default=45,
                        help="seconds to let an embed resolve")
        sp.add_argument("--name", help="filename stem")

    s = sub.add_parser("url", help="print the resolved direct media URL")
    add_watch_opts(s)
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_url)

    s = sub.add_parser("download", help="download an episode")
    add_watch_opts(s)
    s.add_argument("-o", "--out", help="output file path")
    s.add_argument("-d", "--dir", default=".", help="output directory")
    s.add_argument("--no-resume", action="store_true")
    s.set_defaults(fn=cmd_download)

    s = sub.add_parser("play", help="stream an episode with mpv")
    add_watch_opts(s)
    s.add_argument("--mpv-args", default="")
    s.set_defaults(fn=cmd_play)

    s = sub.add_parser("clear", help="solve the Cloudflare checkbox by hand")
    s.add_argument("anime_id")
    s.add_argument("episode", nargs="?", default="1")
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
