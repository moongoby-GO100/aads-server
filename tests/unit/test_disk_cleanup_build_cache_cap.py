"""disk_cleanup_v2.sh 빌드 캐시 상한 기본값(2GB)과 환경변수 덮어쓰기."""
import re
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "disk_cleanup_v2.sh"
LINE = re.compile(r'docker builder prune -f --keep-storage "\$\{AADS_BUILD_CACHE_KEEP:-([^}]+)\}"')


def _text():
    return SCRIPT.read_text(encoding="utf-8")


def test_default_keep_storage_is_2gb():
    m = LINE.search(_text())
    assert m, "keep-storage 줄이 AADS_BUILD_CACHE_KEEP 참조 형태가 아님"
    assert m.group(1) == "2GB"


def test_env_override_syntax_kept():
    assert "${AADS_BUILD_CACHE_KEEP:-" in _text()
    assert "8GB}" not in _text()
