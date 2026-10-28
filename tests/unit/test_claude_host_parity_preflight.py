import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path('scripts/claude_host_parity_preflight.py').resolve()
spec = importlib.util.spec_from_file_location('parity', SCRIPT)
parity = importlib.util.module_from_spec(spec)
spec.loader.exec_module(parity)


@pytest.mark.parametrize('defect', [None, 'old-wrapper', 'missing-helper'])
def test_cli_parity(tmp_path, defect):
    host, api = tmp_path / 'host', tmp_path / 'api'
    for root in (host, api):
        (root / 'scripts').mkdir(parents=True)
        for name in parity.FILES:
            (root / name).write_text('same revision code\n')
        (root / '.credentials.json').write_text('DO_NOT_OUTPUT_SECRET')
    if defect == 'old-wrapper':
        (host / 'scripts/claude-docker-wrapper.sh').write_text('legacy lock lifetime')
    elif defect == 'missing-helper':
        (host / 'scripts/claude_slot_credential.py').unlink()
    result = subprocess.run([sys.executable, str(SCRIPT), '--host-root', str(host), '--api-root', str(api)], text=True, capture_output=True, check=False)
    assert result.returncode == (1 if defect else 0)
    report = json.loads(result.stdout)
    assert report['ok'] is (defect is None)
    assert 'DO_NOT_OUTPUT_SECRET' not in result.stdout
    assert all(row['host_path'].startswith(str(host)) for row in report['files'])
