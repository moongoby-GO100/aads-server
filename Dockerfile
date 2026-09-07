# syntax=docker/dockerfile:1.7
FROM python:3.12-slim

WORKDIR /app

# psycopg binary 의존성 + Rust(tiktoken 빌드) + supervisord + git.
# Rust is needed only while installing Python wheels, so remove it in the same
# layer to keep the runtime image bounded.
COPY pyproject.toml ./
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/root/.cache/pip \
    apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev gcc curl git supervisor openssh-client && \
    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y && \
    PATH="/root/.cargo/bin:${PATH}" pip install -e ".[dev]" && \
    rm -rf /root/.cargo /root/.rustup /var/lib/apt/lists/*

ENV PATH="/usr/local/bin:${PATH}"

COPY . .

# Playwright chromium 설치 (T-024 Visual QA). Let Playwright install OS deps
# once, then drop apt indexes from the same layer.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    playwright install chromium --with-deps && \
    rm -rf /var/lib/apt/lists/*

# MCP workspace 디렉터리 생성
RUN mkdir -p /tmp/aads_workspace /tmp/aads_workspace/screenshots /tmp/aads_workspace/baselines /var/log

EXPOSE 8080 8765 8766 8767

# supervisord로 API + MCP 서버 동시 기동
CMD ["supervisord", "-c", "/app/supervisord.conf"]
