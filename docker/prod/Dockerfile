FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ARG EMBEDDING_MODEL=sentence-transformers/all-MiniLM-L6-v2
ARG WHISPER_MODEL=base

# ffmpeg/ffprobe run the media pipeline; libgl1 and libglib2.0-0 are what opencv-python
# (via PySceneDetect) links against at import time.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

ENV UV_PROJECT_ENVIRONMENT="/usr/local/" \
    UV_LINK_MODE=copy \
    UV_PYTHON=3.13 \
    HF_HOME=/opt/models \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml uv.lock ./
# --no-cache: with UV_LINK_MODE=copy the cache is a byte-for-byte duplicate of
# site-packages, and it was shipping 1.4GB of dead weight inside the image
RUN UV_COMPILE_BYTECODE=1 uv sync --locked --no-install-project --no-cache

# Bake the model weights in. Downloading ~250MB from HuggingFace on first request would
# make the first video of every deploy crawl, and rate-limit when several workers start.
RUN python -c "\
from sentence_transformers import SentenceTransformer; SentenceTransformer('${EMBEDDING_MODEL}', device='cpu');\
from faster_whisper import WhisperModel; WhisperModel('${WHISPER_MODEL}', device='cpu', compute_type='int8')" \
    && chmod -R a+rX /opt/models \
    && rm -rf /root/.cache

COPY . .

# collectstatic only needs the settings to import, not a database or real secrets
RUN DEBUG=True SECRET_KEY=build-only python manage.py collectstatic --noinput

# -m for a real home: gunicorn's control server writes there and dies without it.
# /app/media must exist in the image, otherwise docker creates the named volume's
# mountpoint as root and the non-root process cannot write uploads into it.
RUN useradd -r -u 10001 -m -d /home/app app \
    && mkdir -p /app/media \
    && chown -R app:app /app /home/app
USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status==200 else 1)"

# The worker runs this same image with the command overridden — see docker-compose.yaml.
# Long timeout: an upload of a few hundred MB has to finish streaming to storage.
CMD ["gunicorn", "config.wsgi:application", \
     "--bind", "0.0.0.0:8000", \
     "--workers", "3", \
     "--timeout", "300", \
     "--access-logfile", "-", \
     "--error-logfile", "-"]
