FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ARG EMBEDDING_MODEL=sentence-transformers/all-MiniLM-L6-v2
ARG WHISPER_MODEL=base

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
RUN UV_COMPILE_BYTECODE=1 uv sync --locked --no-install-project --no-cache

RUN python -c "\
from sentence_transformers import SentenceTransformer; SentenceTransformer('${EMBEDDING_MODEL}', device='cpu');\
from faster_whisper import WhisperModel; WhisperModel('${WHISPER_MODEL}', device='cpu', compute_type='int8')" \
    && chmod -R a+rX /opt/models \
    && rm -rf /root/.cache

COPY . .

RUN DEBUG=True SECRET_KEY=build-only python manage.py collectstatic --noinput

RUN useradd -r -u 10001 -m -d /home/app app \
    && mkdir -p /app/media \
    && chown -R app:app /app /home/app
USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status==200 else 1)"

CMD ["gunicorn", "config.wsgi:application", \
     "--bind", "0.0.0.0:8000", \
     "--workers", "3", \
     "--timeout", "300", \
     "--access-logfile", "-", \
     "--error-logfile", "-"]
