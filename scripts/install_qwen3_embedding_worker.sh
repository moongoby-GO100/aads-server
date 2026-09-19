#!/usr/bin/env bash
set -euo pipefail

site="${1:-}"
release_sha="${2:-}"
host="$(hostname)"
if [[ "${host,,}" == *contabo14* ]]; then
  echo "contabo14 is explicitly forbidden for Qwen3 embedding" >&2
  exit 64
fi
if [[ "$site" != "cafe24_114" && "$site" != "jinah244" ]]; then
  echo "usage: $0 <cafe24_114|jinah244> <40-char-release-sha>" >&2
  exit 64
fi
if [[ ! "$release_sha" =~ ^[0-9a-f]{40}$ ]]; then
  echo "release SHA must be a full 40-character lowercase Git SHA" >&2
  exit 64
fi

source_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
release_root="/opt/aads-qwen3-worker/releases"
release_dir="$release_root/$release_sha"
current_link="/opt/aads-qwen3-worker/current"
previous_target="$(readlink -f "$current_link" 2>/dev/null || true)"

install -d -m 0755 "$release_dir" "$release_root"
install -m 0755 "$source_dir/scripts/qwen3_embedding_worker.py" "$release_dir/qwen3_embedding_worker.py"
install -m 0644 "$source_dir/scripts/qwen3_embedding_worker.requirements.txt" "$release_dir/requirements.txt"
python3 -m venv "$release_dir/.venv"
"$release_dir/.venv/bin/pip" install --disable-pip-version-check -r "$release_dir/requirements.txt"

install -d -m 0750 /etc/aads
install -m 0644 "$source_dir/scripts/systemd/aads-qwen3-embedding-worker.service" \
  /etc/systemd/system/aads-qwen3-embedding-worker.service
if [[ ! -e /etc/aads/qwen3-embedding-worker.env ]]; then
  install -m 0600 "$source_dir/scripts/systemd/qwen3-embedding-worker.env.example" \
    /etc/aads/qwen3-embedding-worker.env
  sed -i "s/^QWEN_WORKER_SITE=.*/QWEN_WORKER_SITE=$site/" /etc/aads/qwen3-embedding-worker.env
fi

tmp_link="/opt/aads-qwen3-worker/.current.$release_sha"
ln -sfnT "$release_dir" "$tmp_link"
mv -Tf "$tmp_link" "$current_link"
if [[ -n "$previous_target" && "$previous_target" != "$release_dir" ]]; then
  printf '%s\n' "$previous_target" > /opt/aads-qwen3-worker/previous
fi
systemctl daemon-reload
echo "Installed release $release_sha for $site; enable/start is an explicit operator action."
