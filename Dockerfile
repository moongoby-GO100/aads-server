FROM python:3.12-slim

WORKDIR /app

# psycopg binary 의존성 + Rust(tiktoken 빌드) + supervisord + git
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev gcc curl git supervisor && \
    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y && \
    rm -rf /var/lib/apt/lists/*

ENV PATH="/root/.cargo/bin:${PATH}"

COPY pyproject.toml ./
RUN pip install --no-cache-dir -e .

COPY . .

# Playwright chromium 설치 (T-024 Visual QA)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libxkbcommon0 libxcomposite1 libxdamage1 libxrandr2 libgbm1 \
    libxshmfence1 libasound2 && \
    rm -rf /var/lib/apt/lists/*
RUN playwright install chromium --with-deps 2>/dev/null || playwright install chromium

# MCP workspace 디렉터리 생성
RUN mkdir -p /tmp/aads_workspace /tmp/aads_workspace/screenshots /tmp/aads_workspace/baselines /var/log

EXPOSE 8080 8765 8766 8767

# supervisord로 API + MCP 서버 동시 기동
CMD ["supervisord", "-c", "/app/supervisord.conf"]
