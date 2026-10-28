"""Read-only comparison of relay code roots. Never opens credential files.

Pass the host checkout and an exported/mounted API release root. Hashes of the
fixed code allowlist are the revision evidence; no Docker or service mutation.
"""
import argparse
import hashlib
import json
from pathlib import Path

FILES = (
    'scripts/claude_relay_server.py',
    'scripts/claude-docker-wrapper-active.sh',
    'scripts/claude-docker-wrapper.sh',
    'scripts/claude-slot-credentials-wrapper.sh',
    'scripts/claude-oauth-wrapper.sh',
    'scripts/claude_slot_credential.py',
)


def inspect_parity(host_root, api_root):
    rows = []
    for relative in FILES:
        row = {'file': relative}
        for name, root in (('host', host_root), ('api', api_root)):
            path = Path(root).resolve() / relative
            row[name + '_path'] = str(path)
            try:
                row[name + '_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError:
                row[name + '_sha256'] = None
        row['ok'] = bool(row['host_sha256']) and row['host_sha256'] == row['api_sha256']
        rows.append(row)
    return {'ok': all(row['ok'] for row in rows), 'files': rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host-root', required=True)
    parser.add_argument('--api-root', required=True)
    args = parser.parse_args()
    report = inspect_parity(args.host_root, args.api_root)
    print(json.dumps(report, sort_keys=True))
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
