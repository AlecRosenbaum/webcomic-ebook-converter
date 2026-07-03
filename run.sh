#!/usr/bin/env bash
#
# run.sh — launch the unified Webcomic -> ebook server (local dev).
#
# It serves the web page, proxies the page/image fetches, and runs Kindle Comic
# Converter (KCC) for the "e-reader EPUB" option — all from one local server.
# (For deployment, use the Docker image instead — see the README.)
#
# nix supplies the `7zz` binary KCC needs to unpack archives; the server spawns
# `uvx` to build & run KCC on demand (first conversion downloads ~450MB, cached after).
#
# Usage:
#   ./run.sh                 # http://127.0.0.1:8788
#   PORT=9000 ./run.sh       # different port
#   PROFILE=KPW5 ./run.sh    # different KCC device profile (see kcc-c2e --help)
#
# Requirements on PATH: nix, uv/uvx, python3.

set -euo pipefail
cd "$(dirname "$0")"

export PORT="${PORT:-8788}"
URL="http://127.0.0.1:${PORT}/"

for cmd in nix uvx python3; do
  command -v "$cmd" >/dev/null 2>&1 || { echo "error: '$cmd' not found on PATH" >&2; exit 1; }
done

echo ">> Webcomic -> ebook server starting on ${URL}"
echo ">> serves the page + CORS proxy + KCC. First KCC run builds KCC (~450MB), then cached."

# Open the browser once the server answers.
( for _ in $(seq 1 60); do
    if curl -fsS "$URL" -o /dev/null 2>/dev/null; then
      command -v open >/dev/null 2>&1 && open "$URL"
      break
    fi
    sleep 0.25
  done ) &

# nix shell puts 7zz on PATH for the server (and the KCC subprocess it spawns).
exec nix shell nixpkgs#_7zz -c python3 server.py
