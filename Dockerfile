FROM python:3.12-slim

WORKDIR /app

# psycopg binary 의존성 + Rust(tiktoken 빌드)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev gcc curl && \
    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y && \
    rm -rf /var/lib/apt/lists/*

ENV PATH="/root/.cargo/bin:${PATH}"

COPY pyproject.toml ./
RUN pip install --no-cache-dir -e .

COPY . .

EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
