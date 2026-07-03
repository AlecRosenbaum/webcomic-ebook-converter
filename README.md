# Webcomic → ebook

Turn a webcomic's pages into an e-reader-ready book (EPUB / CBZ, or a Kobo
`.kepub.epub` optimized with [KCC](https://github.com/ciromattia/kcc) — tuned by
default for the **Kobo Libra Colour**). A local server does the heavy lifting —
it fetches the pages, downloads the images, assembles the book, and runs KCC. The
browser is only the control panel (paste URLs, pick & reorder), so it stays light
even for thousands of pages.

Everything runs locally — nothing is uploaded anywhere.

## Run it

The easiest way is the prebuilt container (see [Self-hosting](#self-hosting)):

```bash
docker run --rm -p 8788:8788 -v webcomic-cache:/data/cache \
  ghcr.io/alecrosenbaum/webcomic-ebook-converter:latest
```

Then open <http://127.0.0.1:8788/>. For local development without Docker, use
`./run.sh` (needs **nix**, **uv/uvx**, and **python3** on PATH).

Once the page is open:

1. Paste one or more **chapter/page URLs** (one per line) — they're fetched in
   order and merged into a single book. For "single image + next button" comics,
   tick **Crawl** and paste just the first page; it follows the `next` link to the
   end (starting a new chapter each time the URL path changes, e.g.
   `…/The_Hook/2` → `…/Rushes/1`). If it hits the page limit, bump the number and
   click **Resume crawl** to continue from where it stopped — same chapter, no
   re-finding the last page.
2. Click **Fetch all**. Thumbnails appear, grouped by chapter (only the first
   chapter is expanded — click a header, or Expand all, to see the rest).
3. **Pick & order**: untick junk (ads/banners), drag tiles to reorder. Pages are
   numbered continuously across all chapters.
4. Choose a **Format** (optionally tick **Webtoon mode** and/or **Split into
   volumes**) and click the build button. The server downloads, assembles, and
   converts; a progress bar tracks it. Each finished file streams straight to your
   downloads folder — copy it onto your Kobo over USB (or Send-to-Kindle for the
   plain EPUB/CBZ formats).

`./run.sh` requires **nix**, **uv/uvx**, and **python3** on your PATH. Stop the
server with `Ctrl+C`. Use a different port with `PORT=9000 ./run.sh`.

## Output formats

| Format | What you get |
| --- | --- |
| **E-reader EPUB — KCC optimized (KoLC)** | Run through KCC on the server: downscaled to the device screen, color preserved, gamma-corrected, output as a Kobo `.kepub.epub`. Best for an e-ink reader. |
| **EPUB (fixed layout)** | Fixed-layout EPUB 3, one image per page sized to each image. |
| **CBZ** | Plain comic archive (zipped images). |

The **Right-to-left (manga)** checkbox applies to the EPUB and KCC options. The
**Webtoon mode** checkbox (KCC only) splits tall continuous strips into
device-height pages for vertical-scroll manhwa; it supersedes RTL (a vertical
webtoon has no left/right direction), so the two are mutually exclusive.

**Split into volumes** partitions the book into ~N MB volumes (whole chapters
kept together), each downloaded as its own file — much friendlier than one
multi-GB book, and nothing to extract. (The container image ships with KCC
pre-installed; local `./run.sh` builds KCC on first use via `uvx` — ~450 MB, then
cached.)

## How it works

The browser only collects URLs and selections. Everything else is server-side, so
the browser never holds the images or the (potentially multi-GB) output in memory.
`server.py` exposes:

| Route | Purpose |
| --- | --- |
| `GET /` | serves `index.html` |
| `GET /proxy?url=…` | fetches a remote page/image with a browser-like User-Agent + Referer (for scraping image URLs off the pages) |
| `POST /build` | JSON `{images:[{url,chapter}], format, name, …}` → starts a background job that downloads, assembles, splits, and runs KCC. Returns `{job}` |
| `GET /build/status?job=…` | progress JSON (phase, done/total) |
| `GET /build/result?job=…&vol=N` | streams volume N of the finished book (each volume is a separate download) |
| `GET /health` | reports whether `uv`/`uvx` and `7zz` are available |

The result is downloaded via a plain link, so the browser streams it to disk
rather than buffering it. `run.sh` wraps the server in `nix shell nixpkgs#_7zz` so
the `7zz` binary KCC needs for extraction is on PATH, then opens the browser.

## KCC settings

The e-reader option converts with these:

```
kcc-c2e -p KoLC -f EPUB --forcecolor -u [-m | -w] [-a AUTHOR] -t TITLE
```

- `KoLC` — Kobo Libra Colour profile. `--forcecolor` keeps color (KCC defaults
  to grayscale). Without `--nokepub`, KCC names the output `.kepub.epub` so Kobo
  loads it through its native kepub renderer (better fixed-layout/image handling
  than the generic EPUB path). `-u` upscales small pages; `-m` is manga/RTL;
  `-w` is webtoon mode (splits tall strips into device-height pages).

Override via environment variables when launching:

| Var | Default | Meaning |
| --- | --- | --- |
| `HOST` | `127.0.0.1` | bind address (the container image sets `0.0.0.0`) |
| `PORT` | `8788` | server port |
| `PROFILE` | `KoLC` | KCC device profile (see `kcc-c2e --help` for the full list) |
| `KCC_FORMAT` | `EPUB` | KCC output format |
| `KCC_REF` | pinned commit | KCC git commit for the `uvx` fallback (local dev only; the image bakes KCC in) |
| `KCC_PY` | `3.12` | Python version `uv` builds KCC with |
| `KCC_TIMEOUT` | `3600` | KCC subprocess time budget per volume (seconds) |
| `DL_WORKERS` | `6` | concurrent image downloads during a build |
| `CACHE_DIR` | `.proxy-cache` | on-disk download cache location |
| `CACHE_TTL` | `604800` | cache lifetime since last use, seconds (7 days); `0` disables |
| `CACHE_MAX_MB` | `4096` | cache size cap (LRU eviction); `0` disables the cap |
| `CACHE_SWEEP_SEC` | `3600` | how often the background cache sweep runs |

Example: `PROFILE=KPW5 ./run.sh` targets a Paperwhite 5.

### Download cache

Fetched pages and images are cached on disk under `.proxy-cache/` (keyed by URL)
so re-running a chapter doesn't re-download every image. A background sweep
(every `CACHE_SWEEP_SEC`, and right after each build) reclaims space: it deletes
entries not used within `CACHE_TTL` and, if the cache still exceeds `CACHE_MAX_MB`,
evicts least-recently-used entries until it's under the cap. A cache hit refreshes
an entry's "last used" time, so actively-used images stick around. You can still
delete `.proxy-cache/` by hand, or set `CACHE_TTL=0` to disable caching. During a
build each image is streamed to a temp file (never all held in RAM), so multi-GB
sources don't blow up server memory.

## Self-hosting

A container image is built and pushed to GHCR by GitHub Actions on every push to
`main` (and on `v*` tags) — no local Docker needed. It bundles Python, `7z`, and
KCC (pinned), and runs a build-time smoke test so a broken image fails CI.

`docker run`:

```bash
docker run -d --name webcomic-converter \
  -p 8788:8788 \
  -v webcomic-cache:/data/cache \
  -e PROFILE=KoLC \
  ghcr.io/alecrosenbaum/webcomic-ebook-converter:latest
```

`docker-compose.yml`:

```yaml
services:
  webcomic-converter:
    image: ghcr.io/alecrosenbaum/webcomic-ebook-converter:latest
    ports:
      - "8788:8788"
    environment:
      - PROFILE=KoLC            # KCC device profile; see kcc-c2e --help
      # - KCC_TIMEOUT=7200       # bump for very large webtoons
    volumes:
      - webcomic-cache:/data/cache   # persist the download cache across restarts
    restart: unless-stopped

volumes:
  webcomic-cache:
```

Then open `http://<host>:8788/`. The image binds `0.0.0.0` and reads/writes its
download cache under `/data/cache` (mount a volume to keep it across restarts).

Notes:
- The GHCR package is private by default. Either make it public in the repo's
  package settings, or `docker login ghcr.io` with a token that has `read:packages`.
- **Security**: `/proxy` is an open forward-proxy (it fetches whatever URL it's
  given) and there's no auth. Keep it on your LAN / behind a VPN or an
  authenticating reverse proxy — don't expose it to the public internet.

## Files

| File | Role |
| --- | --- |
| `index.html` | the UI — paste URLs, select/reorder, kick off the build (self-contained, no CDN) |
| `server.py` | fetch proxy + `/build` (download, assemble, split, run KCC), stdlib only |
| `run.sh` | local-dev launcher: provides `7z` via nix, starts the server, opens the browser |
| `Dockerfile` | production image (Python + `7z` + KCC baked in) |
| `.github/workflows/` | CI that builds and publishes the image to GHCR |

## Notes & limitations

- Pages are grouped by **fetch order**, so list chapter URLs in reading order.
- Sites that build their image list with JavaScript (lazy loading) may return few
  or no images from the raw HTML — use the **Add image URLs manually** box for
  those.
- **Crawl mode** detects the "next" link via `rel="next"`, then `title`/`aria-label`/
  `class`/`id`/child-`<img>` or link-text signals. Repeated nav icons dedupe across
  pages; **Deselect small (<300px)** clears them in one click. It stops at the last
  page (no next / self-link), an already-visited page, or the page limit. If a site's
  "next" isn't detected, fall back to listing chapter URLs (crawl off).
- WebP isn't a core EPUB image type; JPEG/PNG (the usual webcomic case) are safest
  for older e-readers.
- **Webtoon mode**: KCC merges each folder of images into one strip and rejects any
  strip taller than 524288 px. The tool auto-groups a long source into height-capped
  subfolders (breaking at chapter boundaries) so this never trips. A *single* image
  taller than that cap is still rejected by KCC — split it before importing.
- **Failed downloads**: transient failures (rate limits, 5xx, network errors) are
  retried with backoff, then any stragglers are retried once more sequentially. If
  an image still can't be fetched (e.g. a dead link), it's reported — the count in
  the result and the exact URLs in the log — rather than silently leaving a gap.
