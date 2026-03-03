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

# MCP workspace 디렉터리 생성
RUN mkdir -p /tmp/aads_workspace /var/log

EXPOSE 8080 8765 8766 8767

# supervisord로 API + MCP 서버 동시 기동
CMD ["supervisord", "-c", "/app/supervisord.conf"]
