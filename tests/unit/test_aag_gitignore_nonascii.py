"""AAG 무시 목록이 한글 파일명에서 새지 않는지 지킨다.

`git ls-files` 는 기본값(`core.quotePath=true`)에서 비ASCII 경로를
`"app/static/reports/\\355\\225\\234\\352\\270\\200.html.bak_aads"` 처럼
따옴표 + 8진 이스케이프로 내놓는다. 그 문자열은 실제 경로와 한 글자도 맞지
않으므로 **한글 이름 파일만 무시 목록에서 빠진다.**

2026-09-16 실측으로 잡혔다. 깨끗한 체크아웃의 STALE_BACKUP 은 34 인데
116 워킹트리에서는 37 이 나왔고, 벌어진 3개가 전부 한글 파일명의
`.bak_aads` 였다. 같은 커밋이 실행 위치에 따라 다른 결과를 내면
고정선은 의미가 없고, pre-commit 게이트는 무관한 커밋을 영원히 막는다.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

_spec = importlib.util.spec_from_file_location(
    "aag_scan_nonascii", ROOT / "tools" / "aag" / "scan_aads.py"
)
sc = importlib.util.module_from_spec(_spec)
sys.modules["aag_scan_nonascii"] = sc
_spec.loader.exec_module(sc)


def test_gitignored_nonascii_backup_is_recognised(tmp_path):
    repo = tmp_path / "repo"
    (repo / "app").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True,
                   capture_output=True, text=True)

    (repo / ".gitignore").write_text("*.bak_aads\n", encoding="utf-8")
    ascii_bak = repo / "app" / "plain.py.bak_aads"
    hangul_bak = repo / "app" / "열정국밥_체크리스트.html.bak_aads"
    ascii_bak.write_text("x", encoding="utf-8")
    hangul_bak.write_text("x", encoding="utf-8")

    sc._IGNORED_CACHE.clear()
    ignored = sc.git_ignored_files(repo)

    assert ascii_bak.as_posix() in ignored, "ASCII 경로부터 새고 있다"
    assert hangul_bak.as_posix() in ignored, (
        "한글 파일명이 무시 목록에서 빠졌다 — core.quotePath=false 가 사라졌나"
    )


def test_scan_reads_ignore_list_with_quotepath_disabled():
    """구현이 바뀌어도 이 플래그만은 남아야 한다."""
    source = (ROOT / "tools" / "aag" / "scan_aads.py").read_text(encoding="utf-8")
    start = source.index("def git_ignored_files(")
    end = source.index("\ndef ", start + 1)
    body = source[start:end]

    assert "core.quotePath=false" in body
    assert '"ls-files", "--others", "--ignored"' in body
