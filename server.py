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
  PORT        listen port              [8788]
  PROFILE     KCC device profile       [KoLC  = Kobo Libra Colour]
  KCC_FORMAT  KCC output format        [EPUB]
  KCC_REF     KCC git tag/branch/commit pinned for reproducible builds
  KCC_PY      python uv builds KCC with [3.12]
"""

import os
import re
import shutil
import subprocess
import tempfile
import glob
import json
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(HERE, "webcomic-to-cbz.html")

PORT        = int(os.environ.get("PORT", "8788"))
KCC_PROFILE = os.environ.get("PROFILE", "KoLC")
KCC_FORMAT  = os.environ.get("KCC_FORMAT", "EPUB")
KCC_REF     = os.environ.get("KCC_REF", "1e57da08a9560a10bcbd8bba6c7d2f7e898b59d2")
KCC_PY      = os.environ.get("KCC_PY", "3.12")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def have(cmd):
    return shutil.which(cmd) is not None


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
        self.send_header("Access-Control-Allow-Private-Network", "true")

    def _send(self, status, body, ctype, extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", ctype)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
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
                                    "sevenzip": have("7zz") or have("7z")})
        return self._text(404, "Not found")

    def do_POST(self):
        if urlparse(self.path).path == "/kcc":
            return self._kcc()
        return self._text(404, "Not found")

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

        origin = "{0.scheme}://{0.netloc}".format(urlparse(target))
        req = urllib.request.Request(target, headers={
            "User-Agent": UA, "Accept": "*/*", "Referer": origin + "/",
        })
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                body, headers, status = resp.read(), resp.headers, resp.status
        except urllib.error.HTTPError as e:
            body = (e.read() if e.fp else b"") or str(e).encode()
            headers, status = e.headers, e.code
        except Exception as e:
            return self._text(502, "Proxy error: %s" % e)

        ctype = headers.get("Content-Type", "application/octet-stream") if headers else "application/octet-stream"
        extra = {}
        cenc = headers.get("Content-Encoding") if headers else None
        if cenc:
            extra["Content-Encoding"] = cenc
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

        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return self._text(400, "Empty upload (expected a CBZ body).")
        data = self.rfile.read(length)

        tmp = tempfile.mkdtemp(prefix="kcc_")
        try:
            outdir = os.path.join(tmp, "out"); os.makedirs(outdir)
            infile = os.path.join(tmp, name + ".cbz")
            with open(infile, "wb") as f:
                f.write(data)

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
                                  timeout=600, env=os.environ.copy())
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
    print("  Ctrl+C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
