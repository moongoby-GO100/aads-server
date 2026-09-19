#!/usr/bin/env bash
set -euo pipefail

host="$(hostname)"
if [[ "${host,,}" == *contabo14* ]]; then
  echo "contabo14 is explicitly forbidden for Qwen3 embedding" >&2
  exit 64
fi

install -d -m 0750 /etc/aads
install -m 0644 scripts/systemd/aads-qwen3-embedding-worker.service \
  /etc/systemd/system/aads-qwen3-embedding-worker.service
if [[ ! -e /etc/aads/qwen3-embedding-worker.env ]]; then
  install -m 0600 scripts/systemd/qwen3-embedding-worker.env.example \
    /etc/aads/qwen3-embedding-worker.env
fi
systemctl daemon-reload
echo "Installed only; enable/start is an explicit operator action."

