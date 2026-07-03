#!/usr/bin/env python3
# /// script
# requires-python = ">=3.8"
# dependencies = []
# ///
"""
Unified local server for the Webcomic -> ebook tool.

One process does everything (no public CORS proxy, no separate scripts):

  GET  /              -> serves index.html
  GET  /proxy?url=..  -> CORS proxy: fetches a remote page/image server-side
  POST /build         -> JSON {images:[{url,chapter}], format, name, ...}; downloads,
                         assembles (+ splits into volumes), runs KCC — all server-side
                         so the browser never holds the images. Returns {job}.
  GET  /build/status?job=..  -> progress JSON
  GET  /build/result?job=..&vol=N -> streams volume N of the finished book (epub/cbz)
  GET  /health        -> {"ok":true, "kcc":bool, "sevenzip":bool, ...}

Start it with ./run.sh, which provides the `7zz` binary (via nix) that KCC needs
and opens your browser. Standard library only.

Env overrides:
  HOST        bind address                      [127.0.0.1; container: 0.0.0.0]
  PORT        listen port                       [8788]
  PROFILE     KCC device profile                [KoLC  = Kobo Libra Colour]
  KCC_FORMAT  KCC output format                 [EPUB]
  KCC_REF     KCC git commit for the uvx fallback (local dev only)
  KCC_PY      python uv builds KCC with         [3.12]
  KCC_TIMEOUT KCC subprocess time budget, sec   [3600]
  CACHE_DIR   on-disk proxy cache dir           [<here>/.proxy-cache]
  CACHE_TTL   proxy cache lifetime, seconds     [604800 = 7 days; 0 disables]
"""

import os
import re
import sys
import shutil
import subprocess
import tempfile
import glob
import json
import time
import datetime
import hashlib
import threading
import uuid
import zipfile
import urllib.request
import urllib.error
from email.utils import parsedate_to_datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(HERE, "index.html")

HOST        = os.environ.get("HOST", "127.0.0.1")  # container/k8s: set 0.0.0.0
PORT        = int(os.environ.get("PORT", "8788"))
KCC_PROFILE = os.environ.get("PROFILE", "KoLC")
KCC_FORMAT  = os.environ.get("KCC_FORMAT", "EPUB")
KCC_REF     = os.environ.get("KCC_REF", "1e57da08a9560a10bcbd8bba6c7d2f7e898b59d2")
KCC_PY      = os.environ.get("KCC_PY", "3.12")

# Rate-limit (HTTP 429 / 503) handling for the proxy.
MAX_RETRIES     = int(os.environ.get("MAX_RETRIES", "4"))
MAX_RETRY_WAIT  = float(os.environ.get("MAX_RETRY_WAIT", "30"))  # cap any single wait, seconds

# KCC subprocess time budget (webtoons with thousands of pages are slow).
KCC_TIMEOUT     = int(os.environ.get("KCC_TIMEOUT", "3600"))     # seconds

# On-disk proxy cache so re-runs don't re-download the same images.
CACHE_DIR       = os.environ.get("CACHE_DIR", os.path.join(HERE, ".proxy-cache"))
CACHE_TTL       = float(os.environ.get("CACHE_TTL", str(7 * 24 * 3600)))  # seconds; 0 disables

# Server-side build (/build): download images + assemble + KCC without the browser
# ever holding gigabytes.
DL_WORKERS      = int(os.environ.get("DL_WORKERS", "6"))     # concurrent image downloads
WEBTOON_CAP     = 480000                                      # KCC per-directory merge cap (px)
JOBS = {}                                                     # job_id -> state dict
JOBS_LOCK = threading.Lock()

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def have(cmd):
    return shutil.which(cmd) is not None


def _cache_paths(url):
    h = hashlib.sha256(url.encode("utf-8")).hexdigest()
    base = os.path.join(CACHE_DIR, h)
    return base + ".bin", base + ".ct"


def cache_get(url):
    """Return (body, content_type) if a fresh cached copy exists, else None."""
    if not CACHE_DIR or CACHE_TTL <= 0:
        return None
    binp, ctp = _cache_paths(url)
    try:
        if (time.time() - os.stat(binp).st_mtime) > CACHE_TTL:
            return None
        with open(binp, "rb") as f:
            body = f.read()
        ct = "application/octet-stream"
        try:
            with open(ctp, "r") as f:
                ct = f.read().strip() or ct
        except OSError:
            pass
        return body, ct
    except OSError:
        return None


def cache_put(url, body, ctype):
    if not CACHE_DIR or CACHE_TTL <= 0 or not body:
        return
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        binp, ctp = _cache_paths(url)
        tmp = binp + ".tmp"
        with open(tmp, "wb") as f:
            f.write(body)
        os.replace(tmp, binp)                       # atomic swap into place
        with open(ctp, "w") as f:
            f.write(ctype or "application/octet-stream")
    except OSError:
        pass


def retry_after_seconds(headers):
    """Parse a Retry-After header (delta-seconds or HTTP-date) into seconds, or None."""
    if not headers:
        return None
    ra = headers.get("Retry-After")
    if not ra:
        return None
    ra = ra.strip()
    if ra.isdigit():
        return float(ra)
    try:
        dt = parsedate_to_datetime(ra)
        if dt is None:
            return None
        now = datetime.datetime.now(dt.tzinfo) if dt.tzinfo else datetime.datetime.now()
        return max(0.0, (dt - now).total_seconds())
    except Exception:
        return None


def safe_name(s, default="comic"):
    s = re.sub(r"[\\/:*?\"<>|]+", " ", s or "").strip()
    s = re.sub(r"\.(cbz|epub)$", "", s, flags=re.I).strip()
    return s[:80] or default


def slog(msg):
    sys.stderr.write("%s %s\n" % (time.strftime("%d/%b/%Y %H:%M:%S"), msg))


# ---------- server-side fetch (shared by /proxy and /build) ----------

# Statuses worth retrying: rate limits, request timeout, and 5xx / Cloudflare origin
# errors — all typically transient under concurrent load. (403/404 are persistent, so
# not retried.) Network-level errors (status 0) are retried too.
RETRYABLE = {408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524, 525, 526, 527}


def do_fetch(url, referer=None):
    """Fetch a URL server-side, retrying transient failures with backoff.
    Returns (status, body, headers, neterr); status 0 = network error (neterr=msg)."""
    origin = "{0.scheme}://{0.netloc}".format(urlparse(url))
    ref = referer if (referer or "").startswith("http") else origin + "/"
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "*/*", "Referer": ref,
    })
    body = headers = None
    status, neterr = 0, None
    for attempt in range(1, MAX_RETRIES + 2):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                body, headers, status, neterr = resp.read(), resp.headers, resp.status, None
        except urllib.error.HTTPError as e:
            body = (e.read() if e.fp else b"") or b""
            headers, status, neterr = e.headers, e.code, None
        except Exception as e:
            status, neterr, body, headers = 0, str(e), b"", None
        if (status == 0 or status in RETRYABLE) and attempt <= MAX_RETRIES:
            wait = retry_after_seconds(headers)
            if wait is None:
                wait = 2 ** (attempt - 1)
            time.sleep(min(wait, MAX_RETRY_WAIT))
            continue
        break
    return status, body, headers, neterr


def fetch_cached(url, referer=None):
    """Cache-aware fetch. Returns (status, body, content_type, neterr)."""
    c = cache_get(url)
    if c is not None:
        return 200, c[0], c[1], None
    status, body, headers, neterr = do_fetch(url, referer)
    ctype = headers.get("Content-Type", "application/octet-stream") if headers else "application/octet-stream"
    if status == 200 and body:
        cache_put(url, body, ctype)
    return status, body, ctype, neterr


# ---------- image helpers ----------

def ext_for(ctype, url):
    t = (ctype or "").lower()
    for key, ext in (("jpeg", "jpg"), ("jpg", "jpg"), ("png", "png"), ("webp", "webp"),
                     ("gif", "gif"), ("avif", "avif"), ("bmp", "bmp")):
        if key in t:
            return ext
    m = re.search(r"\.([a-z0-9]+)(?:$|[?#])", url.split("?")[0], re.I)
    return m.group(1).lower() if m else "jpg"


def mime_for(ext):
    return {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "gif": "image/gif",
            "webp": "image/webp", "avif": "image/avif", "bmp": "image/bmp"}.get((ext or "").lower(), "image/jpeg")


def img_dims(d):
    """(width, height) from image header bytes for PNG/GIF/WebP/JPEG, else None."""
    try:
        n = len(d)
        if n > 24 and d[:8] == b"\x89PNG\r\n\x1a\n":
            return int.from_bytes(d[16:20], "big"), int.from_bytes(d[20:24], "big")
        if n > 10 and d[:3] == b"GIF":
            return int.from_bytes(d[6:8], "little"), int.from_bytes(d[8:10], "little")
        if n > 30 and d[:4] == b"RIFF" and d[8:12] == b"WEBP":
            cc = d[12:16]
            if cc == b"VP8 ":
                return (int.from_bytes(d[26:28], "little") & 0x3fff,
                        int.from_bytes(d[28:30], "little") & 0x3fff)
            if cc == b"VP8L":
                b0, b1, b2, b3 = d[21], d[22], d[23], d[24]
                return (1 + (((b1 & 0x3f) << 8) | b0),
                        1 + (((b3 & 0x0f) << 10) | (b2 << 2) | ((b1 & 0xc0) >> 6)))
            if cc == b"VP8X":
                return (1 + (d[24] | d[25] << 8 | d[26] << 16),
                        1 + (d[27] | d[28] << 8 | d[29] << 16))
        if n > 4 and d[0] == 0xFF and d[1] == 0xD8:  # JPEG: scan SOF markers
            o = 2
            while o + 9 < n:
                if d[o] != 0xFF:
                    o += 1; continue
                m = d[o + 1]
                if m == 0xFF:
                    o += 1; continue
                if 0xD0 <= m <= 0xD9:
                    o += 2; continue
                ln = int.from_bytes(d[o + 2:o + 4], "big")
                if 0xC0 <= m <= 0xCF and m not in (0xC4, 0xC8, 0xCC):
                    return int.from_bytes(d[o + 7:o + 9], "big"), int.from_bytes(d[o + 5:o + 7], "big")
                o += 2 + ln
    except Exception:
        pass
    return None


def xml_escape(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&apos;"))


# ---------- KCC command ----------

def kcc_available():
    """True if we can run KCC — either a pre-installed kcc-c2e (container) or uv/uvx
    (local dev, which builds KCC on demand)."""
    return have("kcc-c2e") or have("uvx") or have("uv")


def kcc_base_cmd():
    """Prefer a pre-installed kcc-c2e (baked into the Docker image); otherwise run it
    on demand via uvx from the pinned commit (local dev)."""
    if have("kcc-c2e"):
        return ["kcc-c2e"]
    if have("uvx"):
        return ["uvx", "--python", KCC_PY,
                "--from", "git+https://github.com/ciromattia/kcc@%s" % KCC_REF, "kcc-c2e"]
    return None


def run_kcc(inpath, outdir, name, author, manga, webtoon, log=slog):
    """Run kcc-c2e on a folder or CBZ. Returns the subprocess result."""
    base = kcc_base_cmd()
    if base is None:
        raise RuntimeError("KCC unavailable: neither kcc-c2e nor uv/uvx on PATH.")
    cmd = base + ["-p", KCC_PROFILE, "-f", KCC_FORMAT, "--forcecolor", "-u",
                  "-t", name, "-o", outdir]
    if author:
        cmd += ["-a", author]
    if webtoon:
        cmd.append("-w")
    elif manga:
        cmd.append("-m")
    cmd.append(inpath)
    log("running KCC: %s" % " ".join(cmd))
    return subprocess.run(cmd, cwd=os.path.dirname(outdir), capture_output=True, text=True,
                          timeout=KCC_TIMEOUT, env=os.environ.copy())


# ---------- output assembly ----------

def group_webtoon(items):
    """Group ordered images into sub-lists so each group's cumulative source height
    stays under KCC's per-directory merge cap; also break at chapter boundaries."""
    groups, cur, cum, last = [], [], 0, None
    for m in items:
        ch = m.get("chapter")
        if cur and (cum + m["h"] > WEBTOON_CAP or (ch is not None and ch != last)):
            groups.append(cur); cur, cum = [], 0
        cur.append(m); cum += m["h"]; last = ch
    if cur:
        groups.append(cur)
    return groups


def assemble_cbz(items, path):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as zf:
        pad = max(3, len(str(len(items))))
        for i, m in enumerate(items):
            zf.write(m["path"], "page-%s.%s" % (str(i + 1).zfill(pad), m["ext"]))


def assemble_kcc_input(items, root, webtoon):
    """Lay images out as a folder tree for KCC (webtoon -> height-capped subfolders)."""
    os.makedirs(root, exist_ok=True)
    if webtoon:
        for gi, g in enumerate(group_webtoon(items)):
            gd = os.path.join(root, str(gi + 1).zfill(4)); os.makedirs(gd)
            for si, m in enumerate(g):
                shutil.copy(m["path"], os.path.join(gd, "%s.%s" % (str(si + 1).zfill(4), m["ext"])))
        return
    pad = max(4, len(str(len(items))))
    for i, m in enumerate(items):
        shutil.copy(m["path"], os.path.join(root, "page-%s.%s" % (str(i + 1).zfill(pad), m["ext"])))


def split_volumes(items, target_bytes):
    """Partition ordered images into ~target_bytes volumes, keeping whole chapters
    together (a single oversized chapter becomes its own volume). target_bytes<=0
    => one volume (no split)."""
    if target_bytes <= 0:
        return [items]
    chapters, cur, last = [], [], object()
    for m in items:                          # consecutive runs of the same chapter
        ch = m.get("chapter")
        if cur and ch != last:
            chapters.append(cur); cur = []
        cur.append(m); last = ch
    if cur:
        chapters.append(cur)
    vols, v, vsize = [], [], 0
    for chap in chapters:
        csize = sum(m.get("size", 0) for m in chap)
        if v and vsize + csize > target_bytes:
            vols.append(v); v, vsize = [], 0
        v.extend(chap); vsize += csize
    if v:
        vols.append(v)
    return vols


def produce_output(items, fmt, voldir, outname, author, rtl, webtoon):
    """Build ONE output file (cbz/epub/kcc) for a list of images. Returns (path, ext)."""
    os.makedirs(voldir, exist_ok=True)
    if fmt == "cbz":
        out = os.path.join(voldir, outname + ".cbz")
        assemble_cbz(items, out)
        return out, ".cbz"
    if fmt == "epub":
        out = os.path.join(voldir, outname + ".epub")
        assemble_epub(items, out, outname, author, rtl)
        return out, ".epub"
    # kcc
    kin = os.path.join(voldir, "kin")
    outdir = os.path.join(voldir, "out"); os.makedirs(outdir, exist_ok=True)
    assemble_kcc_input(items, kin, webtoon)
    proc = run_kcc(kin, outdir, outname, author, manga=(not webtoon and rtl), webtoon=webtoon,
                   log=slog)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-1500:]
        raise RuntimeError("KCC exited %d:\n%s" % (proc.returncode, tail))
    outs = (glob.glob(os.path.join(outdir, "*.kepub.epub")) or
            glob.glob(os.path.join(outdir, "*.epub")) or
            glob.glob(os.path.join(outdir, "*")))
    if not outs:
        raise RuntimeError("KCC produced no output.")
    produced = outs[0]
    return produced, (".kepub.epub" if produced.endswith(".kepub.epub")
                      else (os.path.splitext(produced)[1] or ".epub"))


def assemble_epub(items, path, title, author, rtl):
    """Fixed-layout EPUB 3, one image per page sized to that image (mirrors the
    in-browser builder)."""
    bid = "urn:uuid:" + str(uuid.uuid4())
    modified = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    t, a = xml_escape(title or "Comic"), xml_escape(author or "Unknown")
    pad = max(3, len(str(len(items))))
    num = lambda i: str(i + 1).zfill(pad)
    manifest = spine = ""
    for i, m in enumerate(items):
        nm = num(i)
        manifest += '    <item id="page-%s" href="page-%s.xhtml" media-type="application/xhtml+xml"/>\n' % (nm, nm)
        manifest += '    <item id="img-%s" href="images/img-%s.%s" media-type="%s"%s/>\n' % (
            nm, nm, m["ext"], mime_for(m["ext"]), ' properties="cover-image"' if i == 0 else "")
        spine += '    <itemref idref="page-%s"/>\n' % nm
    opf = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid" '
           'prefix="rendition: http://www.idpf.org/vocab/rendition/#">\n'
           '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
           '    <dc:identifier id="bookid">%s</dc:identifier>\n'
           '    <dc:title>%s</dc:title>\n    <dc:creator>%s</dc:creator>\n    <dc:language>en</dc:language>\n'
           '    <meta property="dcterms:modified">%s</meta>\n'
           '    <meta property="rendition:layout">pre-paginated</meta>\n'
           '    <meta property="rendition:orientation">auto</meta>\n'
           '    <meta property="rendition:spread">auto</meta>\n'
           '    <meta name="cover" content="img-%s"/>\n  </metadata>\n'
           '  <manifest>\n    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>\n'
           '    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>\n%s  </manifest>\n'
           '  <spine toc="ncx"%s>\n%s  </spine>\n</package>\n') % (
        bid, t, a, modified, num(0), manifest,
        ' page-progression-direction="rtl"' if rtl else "", spine)
    nav = ('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE html>\n'
           '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="en">\n'
           '<head><meta charset="utf-8"/><title>%s</title></head>\n<body>\n'
           '  <nav epub:type="toc" id="toc"><h1>%s</h1><ol><li><a href="page-%s.xhtml">Start</a></li></ol></nav>\n'
           '  <nav epub:type="landmarks" hidden="hidden"><ol>'
           '<li><a epub:type="bodymatter" href="page-%s.xhtml">Start</a></li></ol></nav>\n'
           '</body>\n</html>\n') % (t, t, num(0), num(0))
    ncx = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">\n'
           '  <head><meta name="dtb:uid" content="%s"/><meta name="dtb:depth" content="1"/>'
           '<meta name="dtb:totalPageCount" content="0"/><meta name="dtb:maxPageNumber" content="0"/></head>\n'
           '  <docTitle><text>%s</text></docTitle>\n'
           '  <navMap><navPoint id="np-1" playOrder="1"><navLabel><text>Start</text></navLabel>'
           '<content src="page-%s.xhtml"/></navPoint></navMap>\n</ncx>\n') % (bid, t, num(0))
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml",
                    '<?xml version="1.0" encoding="UTF-8"?>\n'
                    '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
                    '  <rootfiles><rootfile full-path="OEBPS/content.opf" '
                    'media-type="application/oebps-package+xml"/></rootfiles>\n</container>\n',
                    compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("OEBPS/content.opf", opf, compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("OEBPS/nav.xhtml", nav, compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("OEBPS/toc.ncx", ncx, compress_type=zipfile.ZIP_DEFLATED)
        for i, m in enumerate(items):
            nm = num(i)
            w, h = m.get("w") or 800, m.get("h") or 1200
            page = ('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE html>\n'
                    '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">\n'
                    '<head><meta charset="utf-8"/><title>Page %d</title>\n'
                    '<meta name="viewport" content="width=%d, height=%d"/>\n'
                    '<style>html,body{margin:0;padding:0}body{width:%dpx;height:%dpx}'
                    'img{width:100%%;height:100%%;display:block}</style></head>\n'
                    '<body><img src="images/img-%s.%s" alt="Page %d"/></body>\n</html>\n') % (
                i + 1, w, h, w, h, nm, m["ext"], i + 1)
            zf.writestr("OEBPS/page-%s.xhtml" % nm, page, compress_type=zipfile.ZIP_DEFLATED)
            zf.write(m["path"], "OEBPS/images/img-%s.%s" % (nm, m["ext"]))


# ---------- build job worker ----------

def run_build_job(job_id):
    job = JOBS[job_id]
    p = job["params"]
    workdir = job["workdir"]
    images = p.get("images") or []
    fmt = p.get("format", "kcc")
    webtoon = bool(p.get("webtoon"))
    rtl = bool(p.get("rtl"))
    name = safe_name(p.get("name") or "comic")
    author = (p.get("author") or "").strip()
    split_bytes = int(float(p.get("split_mb") or 0) * 1024 * 1024)
    total = len(images)

    def upd(**kw):
        with JOBS_LOCK:
            job.update(kw)

    try:
        # 1. Download every image to disk (bounded memory: N workers hold N images).
        upd(phase="download", total=total, done=0, message="Downloading images…")
        imgdir = os.path.join(workdir, "img"); os.makedirs(imgdir, exist_ok=True)
        results = [None] * total
        failed = []

        reasons = {}  # index -> failure reason (for reporting)

        def dl(i):
            url = images[i].get("url")
            status, body, ctype, neterr = fetch_cached(url, images[i].get("chapterUrl"))
            if status != 200 or not body:
                reasons[i] = ("net: " + neterr) if status == 0 else ("HTTP %d" % status)
                return i, None
            ext = ext_for(ctype, url)
            dims = img_dims(body)
            path = os.path.join(imgdir, "%06d.%s" % (i, ext))
            with open(path, "wb") as f:
                f.write(body)
            reasons.pop(i, None)
            return i, {"path": path, "ext": ext, "size": len(body),
                       "w": dims[0] if dims else 0, "h": dims[1] if dims else 0,
                       "chapter": images[i].get("chapter")}

        with ThreadPoolExecutor(max_workers=DL_WORKERS) as ex:
            for fut in as_completed([ex.submit(dl, i) for i in range(total)]):
                i, meta = fut.result()
                if meta is not None:
                    results[i] = meta
                with JOBS_LOCK:
                    job["done"] = job.get("done", 0) + 1

        # Retry stragglers sequentially — concurrency is the usual cause of transient
        # blocks, so a slow single-threaded pass recovers most of them (no gaps).
        missing = [i for i in range(total) if results[i] is None]
        if missing:
            upd(message="Retrying %d failed download(s)…" % len(missing))
            slog("build %s: retrying %d stragglers sequentially" % (job_id, len(missing)))
            for i in missing:
                time.sleep(0.25)
                _, meta = dl(i)
                if meta is not None:
                    results[i] = meta

        failed = [i for i in range(total) if results[i] is None]
        ok = [m for m in results if m]
        upd(failed=len(failed))
        if not ok:
            upd(phase="error", error="All %d image downloads failed." % total)
            return
        if failed:
            sample = ["  #%d %s (%s)" % (i + 1, images[i].get("url"), reasons.get(i, "?"))
                      for i in failed[:30]]
            slog("build %s: %d/%d images FAILED after retries:\n%s" %
                 (job_id, len(failed), total, "\n".join(sample)))
            upd(failed_urls=[images[i].get("url") for i in failed[:50]])

        # 2. Partition into ~split_mb volumes (whole chapters together), build each.
        vols = split_volumes(ok, split_bytes)
        nvol = len(vols)
        volumes = []  # {path, filename, ctype}
        for vi, vol in enumerate(vols):
            label = name if nvol == 1 else "%s - Vol %s" % (name, str(vi + 1).zfill(2))
            upd(phase="build",
                message=("Building %s (%d/%d)…" % ("volume" if nvol > 1 else "book", vi + 1, nvol)),
                vol_done=vi, vol_total=nvol)
            voldir = os.path.join(workdir, "vol_%03d" % vi)
            path, ext = produce_output(vol, fmt, voldir, safe_name(label), author, rtl, webtoon)
            fname = safe_name(label) + ext
            volumes.append({"path": path, "filename": fname,
                            "ctype": ("application/vnd.comicbook+zip" if ext == ".cbz"
                                      else "application/epub+zip")})

        # 3. Deliver volumes as individual downloads (no zip -> nothing to extract).
        skipped = (" · %d SKIPPED (see log)" % len(failed)) if failed else ""
        vol_note = "" if nvol == 1 else " in %d volumes" % nvol
        upd(phase="done", ready=True, volumes=volumes,
            volume_names=[v["filename"] for v in volumes], nvol=nvol,
            message="Done — %d images%s%s." % (len(ok), vol_note, skipped))
    except subprocess.TimeoutExpired:
        upd(phase="error", error="KCC timed out after %ds." % KCC_TIMEOUT)
    except Exception as e:
        upd(phase="error", error="Build error: %s" % e)


def cleanup_jobs(max_age=3600):
    """Drop finished/abandoned jobs and their temp dirs after max_age seconds."""
    now = time.time()
    with JOBS_LOCK:
        stale = [jid for jid, j in JOBS.items()
                 if now - j.get("created", now) > max_age]
        for jid in stale:
            shutil.rmtree(JOBS[jid].get("workdir", ""), ignore_errors=True)
            JOBS.pop(jid, None)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # ---- helpers ----
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Expose-Headers", "Retry-After")
        self.send_header("Access-Control-Allow-Private-Network", "true")

    def _spool_body(self):
        """Stream the request body to a temp file and return (path, bytes_written).
        Streaming (not buffering in RAM) is essential: a webtoon CBZ can be many GB,
        and reading it all into memory would MemoryError -> empty body -> spurious 400.
        Handles Content-Length and chunked. Fully consuming the body also prevents a
        leftover-body keep-alive desync."""
        fd, path = tempfile.mkstemp(prefix="upload_", suffix=".cbz")
        total, CH = 0, 1 << 20  # 1 MiB chunks
        try:
            with os.fdopen(fd, "wb") as out:
                te = (self.headers.get("Transfer-Encoding") or "").lower()
                if "chunked" in te:
                    while True:
                        line = self.rfile.readline(65537).split(b";", 1)[0].strip()
                        try:
                            size = int(line, 16)
                        except ValueError:
                            break
                        if size == 0:
                            while True:               # consume trailers up to blank line
                                t = self.rfile.readline(65537)
                                if t in (b"\r\n", b"\n", b""):
                                    break
                            break
                        rem = size
                        while rem > 0:
                            buf = self.rfile.read(min(rem, CH))
                            if not buf:
                                break
                            out.write(buf); total += len(buf); rem -= len(buf)
                        self.rfile.read(2)            # trailing CRLF after each chunk
                else:
                    rem = int(self.headers.get("Content-Length", "0") or "0")
                    while rem > 0:
                        buf = self.rfile.read(min(rem, CH))
                        if not buf:
                            break
                        out.write(buf); total += len(buf); rem -= len(buf)
        except Exception:
            try:
                os.remove(path)
            except OSError:
                pass
            raise
        return path, total

    def _send(self, status, body, ctype, extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", ctype)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        if getattr(self, "close_connection", False):
            self.send_header("Connection", "close")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def _send_file(self, status, path, ctype, extra=None):
        """Stream a file as the response body (Content-Length from the file size,
        copied in chunks). Essential for multi-GB KCC output: reading it into RAM
        and writing in one shot blows up memory and, if the single write breaks
        partway, leaves the client with fewer bytes than Content-Length declared.
        Returns True if the full body was sent, False if the client went away."""
        size = os.path.getsize(path)
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", ctype)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        if getattr(self, "close_connection", False):
            self.send_header("Connection", "close")
        self.send_header("Content-Length", str(size))
        self.end_headers()
        try:
            with open(path, "rb") as f:
                shutil.copyfileobj(f, self.wfile, 1 << 20)  # 1 MiB chunks
            return True
        except (BrokenPipeError, ConnectionResetError, OSError) as e:
            self.close_connection = True                    # can't recover mid-body
            self.log_message("response send interrupted after headers: %s", e)
            return False

    def _text(self, status, msg):
        self._send(status, msg, "text/plain; charset=utf-8")

    def _json(self, status, obj):
        self._send(status, json.dumps(obj), "application/json")

    # ---- routes ----
    def do_OPTIONS(self):
        self.send_response(204); self._cors()
        self.send_header("Content-Length", "0"); self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self._serve_page()
        if path == "/proxy":
            return self._proxy()
        if path == "/health":
            return self._json(200, {"ok": True,
                                    "kcc": kcc_available(),
                                    "sevenzip": have("7zz") or have("7z"),
                                    "cache": bool(CACHE_DIR and CACHE_TTL > 0),
                                    "build": True})
        if path == "/build/status":
            return self._build_status()
        if path == "/build/result":
            return self._build_result()
        return self._text(404, "Not found")

    def do_POST(self):
        # Stream the body to disk up front (before any early return) and don't reuse
        # the connection for a POST — makes both OOM on huge uploads and a leftover-body
        # keep-alive desync impossible.
        self.close_connection = True
        self._body_path, self._body_len = None, 0
        try:
            self._body_path, self._body_len = self._spool_body()
        except Exception as e:
            self.log_message("body read failed: %s", e)
        self.log_message("POST %s  CL=%s TE=%s Expect=%s -> spooled %d bytes",
                         urlparse(self.path).path,
                         self.headers.get("Content-Length"),
                         self.headers.get("Transfer-Encoding"),
                         self.headers.get("Expect"), self._body_len)
        try:
            if urlparse(self.path).path == "/build":
                return self._build_start()
            return self._text(404, "Not found")
        finally:
            if self._body_path and os.path.exists(self._body_path):
                try:
                    os.remove(self._body_path)
                except OSError:
                    pass

    # ---- /build: server-side download + assemble + KCC (background job) ----
    def _build_start(self):
        cleanup_jobs()
        try:
            params = json.loads(open(self._body_path, "rb").read().decode("utf-8")) if self._body_path else {}
        except Exception as e:
            return self._text(400, "Invalid JSON body: %s" % e)
        if not params.get("images"):
            return self._text(400, "No images to build.")
        if params.get("format") == "kcc" and not (kcc_available() and (have("7zz") or have("7z"))):
            return self._text(503, "KCC unavailable (need kcc-c2e or uv/uvx, plus 7z/7zz).")
        job_id = uuid.uuid4().hex[:12]
        workdir = tempfile.mkdtemp(prefix="build_")
        with JOBS_LOCK:
            JOBS[job_id] = {"phase": "queued", "done": 0, "total": len(params["images"]),
                            "failed": 0, "ready": False, "error": None, "message": "Queued…",
                            "params": params, "workdir": workdir, "created": time.time()}
        threading.Thread(target=run_build_job, args=(job_id,), daemon=True).start()
        return self._json(200, {"job": job_id})

    def _build_status(self):
        jid = (parse_qs(urlparse(self.path).query).get("job") or [""])[0]
        with JOBS_LOCK:
            job = JOBS.get(jid)
            if not job:
                return self._json(404, {"error": "unknown job"})
            out = {k: job.get(k) for k in ("phase", "done", "total", "failed", "ready", "error",
                                           "message", "volume_names", "nvol", "vol_done",
                                           "vol_total", "failed_urls")}
        return self._json(200, out)

    def _build_result(self):
        q = parse_qs(urlparse(self.path).query)
        jid = (q.get("job") or [""])[0]
        try:
            idx = int((q.get("vol") or ["0"])[0])
        except ValueError:
            idx = 0
        with JOBS_LOCK:
            job = JOBS.get(jid)
            if not job or not job.get("ready"):
                return self._text(404, "Result not ready.")
            vols = job.get("volumes") or []
            workdir = job.get("workdir")
        if idx < 0 or idx >= len(vols):
            return self._text(404, "No such volume.")
        v = vols[idx]
        if not os.path.exists(v["path"]):
            return self._text(410, "Result no longer available.")
        sent = self._send_file(200, v["path"], v["ctype"],
                               {"Content-Disposition": 'attachment; filename="%s"' % v["filename"]})
        if sent:  # clean up once every volume has been fetched
            with JOBS_LOCK:
                job.setdefault("fetched", set()).add(idx)
                done = len(job["fetched"]) >= len(vols)
            if done:
                shutil.rmtree(workdir, ignore_errors=True)
                with JOBS_LOCK:
                    JOBS.pop(jid, None)

    def _serve_page(self):
        try:
            with open(PAGE, "rb") as f:
                body = f.read()
        except FileNotFoundError:
            return self._text(500, "index.html not found next to server.py")
        self._send(200, body, "text/html; charset=utf-8")

    def _proxy(self):
        target = (parse_qs(urlparse(self.path).query).get("url") or [None])[0]
        if not target:
            return self._text(400, "Missing ?url= parameter")
        if not target.startswith(("http://", "https://")):
            return self._text(400, "URL must start with http:// or https://")

        cached = cache_get(target)
        if cached is not None:
            body, ctype = cached
            return self._send(200, body, ctype, {"X-Cache": "HIT"})

        status, body, headers, neterr = do_fetch(target)
        if status == 0:
            return self._text(502, "Proxy error: %s" % neterr)

        ctype = headers.get("Content-Type", "application/octet-stream") if headers else "application/octet-stream"
        extra = {}
        cenc = headers.get("Content-Encoding") if headers else None
        if cenc:
            extra["Content-Encoding"] = cenc
        # If we still got rate-limited after our retries, pass Retry-After through so the
        # client can keep respecting it.
        if status in (429, 503) and headers and headers.get("Retry-After"):
            extra["Retry-After"] = headers.get("Retry-After")
        if status == 200:
            cache_put(target, body, ctype)
            extra["X-Cache"] = "MISS"
        self._send(status, body, ctype, extra)

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.log_date_time_string(), fmt % args))


def main():
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print("Webcomic -> ebook server: http://%s:%d/" % (HOST, PORT))
    print("  build: POST /build (server-side download+assemble+KCC)   profile=%s format=%s" % (KCC_PROFILE, KCC_FORMAT))
    print("  KCC available: %s | 7z/7zz on PATH: %s" % (kcc_available(), have("7zz") or have("7z")))
    if CACHE_DIR and CACHE_TTL > 0:
        print("  proxy cache: %s (TTL %.0fh) — delete it to clear" % (CACHE_DIR, CACHE_TTL / 3600))
    print("  Ctrl+C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
