FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ARG USER

# ffmpeg/ffprobe drive the whole media pipeline; libgl1 and libglib2.0-0 are what
# opencv-python (pulled in by PySceneDetect) links against at import time.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libgl1 \
    libglib2.0-0 \
    curl \
    ca-certificates \
    git \
    zsh \
    openssh-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /backend

RUN useradd -ms /bin/bash $USER && \
    usermod -aG root $USER && \
    chown -R $USER:$USER /backend && \
    chmod -R 755 /backend

RUN chmod -R g+w /usr/local/

USER $USER

ENV PATH="/home/$USER/.local/bin/:$PATH" \
    # the uv cache and /usr/local are on different layers, so hardlinking cannot work
    UV_LINK_MODE=copy \
    # installed into the system prefix on purpose: /backend is bind-mounted from the
    # host, so a .venv there would collide with the host's (macOS arm64) one
    UV_PROJECT_ENVIRONMENT="/usr/local/" \
    # the bind mount also exposes .python-version (3.12); UV_PYTHON outranks it so uv
    # uses the interpreter this image already ships instead of fetching another
    UV_PYTHON=3.13 \
    # sentence-transformers and faster-whisper download ~200MB of weights on first use;
    # docker-compose keeps this on a named volume so they survive a container rebuild
    HF_HOME=/home/$USER/.cache/huggingface \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY pyproject.toml uv.lock ./
# no `uv lock` first — regenerating the lock is exactly what --locked exists to prevent.
# UV_COMPILE_BYTECODE only here, not as a persistent ENV: paired with
# PYTHONDONTWRITEBYTECODE below it recompiles all 15k files on every `uv run`, because
# the .pyc it just built are never written to disk.
RUN UV_COMPILE_BYTECODE=1 uv sync --locked

RUN sh -c "$(curl -L https://github.com/deluan/zsh-in-docker/releases/download/v1.2.1/zsh-in-docker.sh)" -- \
    -t robbyrussell \
    -p git \
    -p ssh-agent \
    -p 'history-substring-search' \
    -p https://github.com/zsh-users/zsh-autosuggestions \
    -p https://github.com/zsh-users/zsh-completions \
    -a 'bindkey "\$terminfo[kcuu1]" history-substring-search-up' \
    -a 'bindkey "\$terminfo[kcud1]" history-substring-search-down'
