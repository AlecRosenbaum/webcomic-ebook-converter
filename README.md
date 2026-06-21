# Webcomic → Kindle

Turn a webcomic's pages into a Kindle-ready book. A single local server serves a
web page, proxies the cross-origin fetches the browser can't do itself, and (for
the Kindle option) runs [KCC](https://github.com/ciromattia/kcc) to optimize the
output for an e-ink device.

Everything runs locally — nothing is uploaded anywhere.

## Quick start

```bash
./run.sh
```

This opens <http://127.0.0.1:8788/> in your browser. Then:

1. Paste one or more **chapter/page URLs** (one per line) — they're fetched in
   order and merged into a single book.
2. Click **Fetch all**. Thumbnails appear, grouped by chapter.
3. **Pick & order**: untick junk (ads/banners), drag tiles to reorder. Pages are
   numbered continuously across all chapters.
4. Choose a **Format** and click the build button. The file downloads to your
   browser's downloads folder; copy it to your reader (Send to Kindle, or drag
   onto a Kobo over USB).

`./run.sh` requires **nix**, **uv/uvx**, and **python3** on your PATH. Stop the
server with `Ctrl+C`. Use a different port with `PORT=9000 ./run.sh`.

## Output formats

| Format | What you get |
| --- | --- |
| **E-reader EPUB — KCC optimized (KoLC)** | CBZ built in-browser, then run through KCC on the server: downscaled to the device screen, color preserved, gamma-corrected, output as a Kobo `.kepub.epub`. Best for an e-ink reader. |
| **EPUB (fixed layout)** | Self-contained fixed-layout EPUB 3 built entirely in-browser (no server needed). One image per page, sized to each image. |
| **CBZ** | Plain comic archive (zipped images), built in-browser. |

The **Right-to-left (manga)** checkbox applies to the EPUB and KCC options.

The KCC option needs the server running (it shows **● server connected · KCC
ready** in the header). The first KCC conversion builds KCC and its
dependencies (~450 MB, via `uvx`) and caches them; later runs are fast. Opened as
a plain `file://` instead of through the server, the page still produces EPUB/CBZ
in-browser and falls back to public CORS proxies for fetching.

## How it works

The browser's same-origin policy blocks a page from reading another site's HTML
or image bytes. `server.py` sidesteps this by fetching server-side and returning
the bytes with permissive CORS headers. It exposes:

| Route | Purpose |
| --- | --- |
| `GET /` | serves `webcomic-to-cbz.html` |
| `GET /proxy?url=…` | fetches a remote page/image with a browser-like User-Agent + Referer |
| `POST /kcc?name=…` | runs KCC on an uploaded CBZ, returns an optimized EPUB |
| `GET /health` | reports whether `uv`/`uvx` and `7zz` are available |

`run.sh` wraps the server in `nix shell nixpkgs#_7zz` so the `7zz` binary KCC
needs for extraction is on PATH, then opens the browser.

## KCC settings

The e-reader option converts with these:

```
kcc-c2e -p KoLC -f EPUB --forcecolor -u [-m] [-a AUTHOR] -t TITLE
```

- `KoLC` — Kobo Libra Colour profile. `--forcecolor` keeps color (KCC defaults
  to grayscale). Without `--nokepub`, KCC names the output `.kepub.epub` so Kobo
  loads it through its native kepub renderer (better fixed-layout/image handling
  than the generic EPUB path). `-u` upscales small pages; `-m` is manga/RTL.

Override via environment variables when launching:

| Var | Default | Meaning |
| --- | --- | --- |
| `PORT` | `8788` | server port |
| `PROFILE` | `KoLC` | KCC device profile (see `kcc-c2e --help` for the full list) |
| `KCC_FORMAT` | `EPUB` | KCC output format |
| `KCC_REF` | pinned commit | KCC git tag/branch/commit to build |
| `KCC_PY` | `3.12` | Python version `uv` builds KCC with |

Example: `PROFILE=KPW5 ./run.sh` targets a Paperwhite 5.

## Files

| File | Role |
| --- | --- |
| `webcomic-to-cbz.html` | the UI — scrape, select/reorder, build (self-contained, no CDN) |
| `server.py` | page server + CORS proxy + KCC runner (stdlib only) |
| `run.sh` | launcher: provides `7zz` via nix, starts the server, opens the browser |

## Notes & limitations

- Pages are grouped by **fetch order**, so list chapter URLs in reading order.
- Sites that build their image list with JavaScript (lazy loading) may return few
  or no images from the raw HTML — use the **Add image URLs manually** box for
  those.
- WebP isn't a core EPUB image type; JPEG/PNG (the usual webcomic case) are safest
  for older Kindles.
