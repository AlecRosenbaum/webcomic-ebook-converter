#!/usr/bin/env python3
# /// script
# requires-python = ">=3.8"
# dependencies = []
# ///
"""
Unified local server for the Webcomic -> Kindle tool.

One process does everything (no public CORS proxy, no separate scripts):

  GET  /              -> serves webcomic-to-cbz.html
  GET  /proxy?url=..  -> CORS proxy: fetches a remote page/image server-side
  POST /kcc?name=..   -> runs Kindle Comic Converter on an uploaded CBZ and
                         returns a device-optimized EPUB
  GET  /health        -> {"ok":true, "kcc":bool, "sevenzip":bool}

Start it with ./run.sh, which provides the `7zz` binary (via nix) that KCC needs
and opens your browser. Standard library only.

Env overrides:
  PORT        listen port                       [8788]
  PROFILE     KCC device profile                [KoLC  = Kobo Libra Colour]
  KCC_FORMAT  KCC output format                 [EPUB]
  KCC_REF     KCC git tag/branch/commit pinned for reproducible builds
  KCC_PY      python uv builds KCC with         [3.12]
  KCC_TIMEOUT KCC subprocess time budget, sec   [3600]
  CACHE_DIR   on-disk proxy cache dir           [<here>/.proxy-cache]
  CACHE_TTL   proxy cache lifetime, seconds     [604800 = 7 days; 0 disables]
"""

import os
import re
import shutil
import subprocess
import tempfile
import glob
import json
import time
import datetime
import hashlib
import urllib.request
import urllib.error
from email.utils import parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(HERE, "webcomic-to-cbz.html")

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
                                    "kcc": have("uvx") or have("uv"),
                                    "sevenzip": have("7zz") or have("7z"),
                                    "cache": bool(CACHE_DIR and CACHE_TTL > 0)})
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
            if urlparse(self.path).path == "/kcc":
                return self._kcc()
            return self._text(404, "Not found")
        finally:
            if self._body_path and os.path.exists(self._body_path):
                try:
                    os.remove(self._body_path)
                except OSError:
                    pass

    def _serve_page(self):
        try:
            with open(PAGE, "rb") as f:
                body = f.read()
        except FileNotFoundError:
            return self._text(500, "webcomic-to-cbz.html not found next to server.py")
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

        origin = "{0.scheme}://{0.netloc}".format(urlparse(target))
        req = urllib.request.Request(target, headers={
            "User-Agent": UA, "Accept": "*/*", "Referer": origin + "/",
        })

        body = headers = status = None
        for attempt in range(1, MAX_RETRIES + 2):  # initial try + MAX_RETRIES retries
            try:
                with urllib.request.urlopen(req, timeout=45) as resp:
                    body, headers, status = resp.read(), resp.headers, resp.status
            except urllib.error.HTTPError as e:
                body = (e.read() if e.fp else b"") or str(e).encode()
                headers, status = e.headers, e.code
            except Exception as e:
                return self._text(502, "Proxy error: %s" % e)

            # Respect rate limiting: back off and retry on 429 (and transient 503).
            if status in (429, 503) and attempt <= MAX_RETRIES:
                wait = retry_after_seconds(headers)
                if wait is None:
                    wait = 2 ** (attempt - 1)          # 1, 2, 4, 8 … backoff
                wait = min(wait, MAX_RETRY_WAIT)
                self.log_message("upstream %d (rate limit); waiting %.1fs, retry %d/%d: %s",
                                 status, wait, attempt, MAX_RETRIES, target)
                time.sleep(wait)
                continue
            break

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

    def _kcc(self):
        if not (have("uvx") or have("uv")):
            return self._text(503, "uv/uvx not found on PATH (needed to run KCC).")
        if not (have("7zz") or have("7z")):
            return self._text(503, "7zz not found on PATH. Launch via ./run.sh so nix provides it.")

        q = parse_qs(urlparse(self.path).query)
        name = safe_name((q.get("name") or ["comic"])[0])
        author = ((q.get("author") or [""])[0]).strip()
        manga = (q.get("manga") or ["0"])[0] in ("1", "true", "yes")
        webtoon = (q.get("webtoon") or ["0"])[0] in ("1", "true", "yes")

        if not self._body_path or self._body_len <= 0:
            return self._text(400, "Empty upload (expected a CBZ body).")

        tmp = tempfile.mkdtemp(prefix="kcc_")
        try:
            outdir = os.path.join(tmp, "out"); os.makedirs(outdir)
            infile = os.path.join(tmp, name + ".cbz")
            shutil.move(self._body_path, infile)   # hand the streamed upload straight to KCC

            # No --nokepub -> KCC emits a Kobo .kepub.epub. --forcecolor keeps color
            # (KCC converts to grayscale by default) for the color e-ink screen.
            cmd = ["uvx", "--python", KCC_PY,
                   "--from", "git+https://github.com/ciromattia/kcc@%s" % KCC_REF,
                   "kcc-c2e", "-p", KCC_PROFILE, "-f", KCC_FORMAT, "--forcecolor", "-u",
                   "-t", name, "-o", outdir]
            if author:
                cmd += ["-a", author]
            if webtoon:
                cmd.append("-w")   # split tall continuous strips into device-height pages
            elif manga:
                cmd.append("-m")   # RTL doesn't apply to a vertical webtoon, so these are exclusive
            cmd.append(infile)

            self.log_message("running KCC: %s", " ".join(cmd))
            proc = subprocess.run(cmd, cwd=tmp, capture_output=True, text=True,
                                  timeout=KCC_TIMEOUT, env=os.environ.copy())
            if proc.returncode != 0:
                tail = (proc.stderr or proc.stdout or "").strip()[-1500:]
                return self._text(500, "KCC exited %d:\n%s" % (proc.returncode, tail))

            outs = (glob.glob(os.path.join(outdir, "*.kepub.epub"))
                    or glob.glob(os.path.join(outdir, "*.epub"))
                    or glob.glob(os.path.join(outdir, "*")))
            if not outs:
                return self._text(500, "KCC produced no output.\n" + (proc.stdout or "")[-1000:])
            produced = outs[0]
            with open(produced, "rb") as f:
                epub = f.read()

            # Kobo only treats the file as a kepub if the name ends in .kepub.epub.
            dl_ext = ".kepub.epub" if produced.endswith(".kepub.epub") else (os.path.splitext(produced)[1] or ".epub")
            self._send(200, epub, "application/epub+zip",
                       {"Content-Disposition": 'attachment; filename="%s%s"' % (name, dl_ext)})
        except subprocess.TimeoutExpired:
            return self._text(504, "KCC timed out.")
        except Exception as e:
            return self._text(500, "KCC error: %s" % e)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def log_message(self, fmt, *args):
        import sys
        sys.stderr.write("%s %s\n" % (self.log_date_time_string(), fmt % args))


def main():
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print("Webcomic -> Kindle server: http://127.0.0.1:%d/" % PORT)
    print("  proxy: /proxy?url=...   KCC: POST /kcc   (profile=%s format=%s)" % (KCC_PROFILE, KCC_FORMAT))
    print("  7zz on PATH: %s | uv/uvx on PATH: %s" % (have("7zz") or have("7z"), have("uvx") or have("uv")))
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
