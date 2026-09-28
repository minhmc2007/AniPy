# AniPy

Search, list episodes, play and download from mkissa.to by driving a real browser.

```
python mkissa.py search "boku no hero"
python mkissa.py episodes yWgQprmrSYKy2LmYR
python mkissa.py download yWgQprmrSYKy2LmYR 1 --name bnha-s1e1
python mkissa.py play yWgQprmrSYKy2LmYR 1
```

## Why not a plain HTTP scraper

| Defence | What it is |
| --- | --- |
| `x-aa-boot` | 32 byte value the page computes after fetching `client-crypto/v1/bootstrap`. Missing it returns `{"error":"invalid_boot_token"}` |
| `aaReq` | the source query carries an encrypted blob in `extensions` |
| `tobeparsed` | even a good source query returns an encrypted body, the client decrypts it |
| `window.aaJp0` | a pristine `JSON.parse` pinned from a throwaway iframe realm, so an injected `Proxy` (the comment names a Tachiyomi extension) cannot forge data |
| Cloudflare | interstitial on deep urls, a checkbox gating the source list |

So the tool never speaks that protocol. The page does the cryptography and results are read from the rendered DOM:

1. `search` clicks the header search box, types with a delay, presses Enter, reads the `.media-card` grid.
2. `episodes` opens the show, clicks the Episodes tab, reads each card's `data-href` (`/anime/<id>/p-<ep>-<sub|dub>`), the only field that reliably carries the number.
3. `url`, `download` and `play` walk to the watch page, click a source tab, let the embed boot, then capture what it assigns to `<video>` from inside the iframe. That yields a direct file url.

The player is a stack of third party embeds (`filelotion.fyi` wrapping `mp4upload`, `ok.ru` and HLS hosts). Those return 403 unless `Referer` matches the embed page origin, so the referer is captured with the url and sent on download.

## Setup

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
```

`mpv` is only needed for `play`

## Cloudflare

A watch url stacks two challenges: the interstitial that replaces the navigation, then the in page checkbox. `open_episode` clicks the interstitial widget once it is laid out, waits rather than reloading, then clicks the panel at a human cadence. The verdict is intermittent, so the run makes a few short attempts in one session, each re walking the navigation path.

Two things that mattered, both found by measurement:

* Do not pass `--disable-gpu`. It removes WebGL, and Cloudflare fingerprints the renderer string. A missing renderer is a stronger tell than an ordinary one.
* Do not reload during a challenge. The interstitial self solves in place.

If an IP is being challenged hard, a different engine gets through. `--browser` points at another Chromium based binary, and the session adopts that browser's own user agent so the downloader does not send a mismatched one.

```bash
python mkissa.py --browser /opt/brave-bin/brave download <anime_id> 1
```

Some distributions ship `/usr/bin/brave` as a shell wrapper. The tool rejects a wrapper and asks for the real executable, so resolve it first with `readlink -f "$(which brave)"`.

`search` and `episodes` never touch the watch route and work regardless.

## Commands

| Command | Purpose |
| --- | --- |
| `search <query> [-n N]` | Search. Prints id, title, format, season, score, episode count. |
| `episodes <anime_id>` | List episodes with sub or dub kind. |
| `sources <anime_id> <ep>` | List the source tabs an episode offers. |
| `url <anime_id> <ep> [--json]` | Print the direct media url, download nothing. |
| `download <anime_id> <ep>` | Resolve and download. Resumable. |
| `play <anime_id> <ep>` | Resolve and hand to `mpv`. |
| `clear <anime_id>` | Open a real window and wait for a human click. |

Flags: `--source <tab>`, `--kind sub|dub`, `--timeout`, `--headed`, `--profile`, `--browser`, `--mpv-args`, `-o/--out`, `-d/--dir`, `-q`.

## Source tabs

The set changes per episode. `Mp4`, `Sl-mp4`, `Uv-mp4`, `Ok`, `Yt`, `Fm-Hls` and `Vg` have all been seen. Tabs are ordered by what the name promises (`mp4` or `file` first, since those hand back a whole file) rather than a hardcoded list, then the tool falls back through the rest until one resolves. `--source <tab>` pins one.

## Notes

* If a source only offers HLS, `download` writes a `.url` file with the playlist and referer and prints the `mpv` or `ffmpeg` line to use.
* Downloads resume from `<file>.part` over HTTP `Range`.
* A fresh profile gets challenged on deep urls. Visiting the homepage first, then navigating by clicking, keeps the session looking like a person. That happens in `warmup()` and `_walk_to_episode()`.
