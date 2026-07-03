# syntax=docker/dockerfile:1
FROM python:3.12-slim

# Runtime deps:
#   p7zip-full -> the `7z` binary KCC uses to extract archives
#   libgl1 / libglib2.0-0 -> shared libs PySide6 (a KCC dependency) links against
#   git -> only needed to pip-install KCC from its repo (purged afterwards)
# QT_QPA_PLATFORM=offscreen so any Qt init works with no display.
ENV QT_QPA_PLATFORM=offscreen \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8788 \
    CACHE_DIR=/data/cache

# KCC isn't on PyPI — install from its repo, pinned to a known-good commit.
ARG KCC_REF=1e57da08a9560a10bcbd8bba6c7d2f7e898b59d2

RUN apt-get update \
    && apt-get install -y --no-install-recommends git p7zip-full libgl1 libglib2.0-0 \
    && pip install --no-cache-dir "git+https://github.com/ciromattia/kcc@${KCC_REF}" \
    && apt-get purge -y git && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

# Build-time smoke test: actually convert a 1-page CBZ so the build FAILS here if
# KCC, 7z, or the Qt libs are broken — rather than discovering it at runtime.
RUN python -c "import io,zipfile;from PIL import Image;b=io.BytesIO();Image.new('RGB',(600,800),(180,180,180)).save(b,'JPEG');z=zipfile.ZipFile('/tmp/s.cbz','w');z.writestr('001.jpg',b.getvalue());z.close()" \
    && kcc-c2e -p KoLC -f EPUB -o /tmp/out /tmp/s.cbz \
    && ls /tmp/out/*.epub >/dev/null \
    && rm -rf /tmp/s.cbz /tmp/out \
    && echo "KCC smoke test passed"

WORKDIR /app
COPY server.py index.html ./
RUN mkdir -p /data/cache
VOLUME ["/data/cache"]
EXPOSE 8788

# Simple liveness check for orchestrators.
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8788')+'/health',timeout=3)"

CMD ["python", "server.py"]
