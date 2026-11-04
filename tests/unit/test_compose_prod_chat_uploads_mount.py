"""운영 compose 에 채팅 업로드 폴더 바인드가 있는지 검사한다."""
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
BIND = "/root/aads/uploads/chat:/root/aads/uploads/chat:rw"
SERVICES = ["aads-server", "aads-server-green"]


def _compose():
    return yaml.safe_load((ROOT / "docker-compose.prod.yml").read_text(encoding="utf-8"))


def test_chat_uploads_bind_exactly_once():
    services = _compose()["services"]
    for name in SERVICES:
        vols = [str(v) for v in services[name]["volumes"]]
        assert vols.count(BIND) == 1, name


def test_chat_files_dir_default_under_bind_target():
    src = (ROOT / "app/services/chat_service.py").read_text(encoding="utf-8")
    m = re.search(r'CHAT_FILES_DIR\s*=\s*Path\(os\.getenv\("CHAT_FILES_DIR",\s*"([^"]+)"\)\)', src)
    assert m, "CHAT_FILES_DIR 기본값을 찾지 못함"
    target = BIND.split(":")[1]
    assert m.group(1) == target or m.group(1).startswith(target + "/")
