# syntax=docker/dockerfile:1.7
FROM python:3.12-slim AS wheelhouse

WORKDIR /build

# Build wheels once, then install them into the runtime image without
# re-resolving dependency ranges.
ARG INSTALL_PLAYWRIGHT=false
COPY requirements.runtime.lock requirements.visual.lock ./
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/root/.cache/pip \
    apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev gcc curl git ca-certificates && \
    python -m pip install --upgrade pip wheel && \
    pip wheel --wheel-dir /wheels -r requirements.runtime.lock && \
    if [ "$INSTALL_PLAYWRIGHT" = "true" ]; then \
        pip wheel --wheel-dir /visual-wheels -r requirements.visual.lock; \
    else \
        mkdir -p /visual-wheels; \
    fi && \
    rm -rf /var/lib/apt/lists/*

FROM python:3.12-slim AS runtime

WORKDIR /app

ARG AADS_IMAGE_PROFILE=runtime
ARG INSTALL_PLAYWRIGHT=false

COPY requirements.runtime.lock requirements.visual.lock ./
COPY --from=wheelhouse /wheels /wheels
COPY --from=wheelhouse /visual-wheels /visual-wheels

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    apt-get update && apt-get install -y --no-install-recommends \
    libpq5 curl git openssh-client ca-certificates && \
    pip install --no-index --find-links=/wheels -r requirements.runtime.lock && \
    if [ "$INSTALL_PLAYWRIGHT" = "true" ]; then \
        pip install --no-index --find-links=/visual-wheels -r requirements.visual.lock && \
        playwright install chromium --with-deps; \
    fi && \
    rm -rf /var/lib/apt/lists/* /wheels /visual-wheels

ENV PATH="/usr/local/bin:${PATH}"
ENV AADS_IMAGE_PROFILE="${AADS_IMAGE_PROFILE}" \
    AADS_INSTALL_PLAYWRIGHT="${INSTALL_PLAYWRIGHT}"

COPY . .

RUN pip install --no-cache-dir --no-deps -e .

# MCP workspace 디렉터리 생성
RUN mkdir -p /tmp/aads_workspace /tmp/aads_workspace/screenshots /tmp/aads_workspace/baselines /var/log

EXPOSE 8080 8765 8766 8767

# supervisord로 API + MCP 서버 동시 기동
CMD ["supervisord", "-c", "/app/supervisord.conf"]
