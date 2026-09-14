#!/usr/bin/env python3
"""문서 표류 검사 — 문서가 주장하는 숫자와 코드의 실제값을 대조한다.

2026-09-14 감사에서 나온 것들이다.

    문서                          실제
    page.tsx 4,501줄              12,865줄
    ChatArtifactPanel 513줄       2,928줄
    chat_service.py 4,158줄       14,855줄
    라우터 엔드포인트 "30+"        77개
    프론트 sseTimeout 90s          150s

`docs/chat/*` 최종 수정이 2026-07-15 인데 그 뒤로 채팅 코드 커밋만 서버
175 / 대시보드 160건이 쌓였다. 코드 주석조차 안 맞았다 — page.tsx 는
"90초 비활성 / 절대 300초" 라고 써놓고 값은 150000 / 3600000 이었다.

**손으로 적은 숫자는 반드시 틀어진다.** 문서를 다시 손으로 맞춰도 160커밋
뒤에 또 벌어진다. 그래서 사람이 아니라 이 스크립트가 본다.

검사 대상 문서를 목록으로 갖고 있지 않다. 문서 안의 표기에서 찾는다 —
목록을 두면 새 문서가 검사 밖에 남는다.

    doc_drift_check.py            표류 목록 출력, 있으면 종료코드 1
    doc_drift_check.py --warn     항상 0 으로 끝낸다 (도입 1단계)
    doc_drift_check.py --paths A  검사할 문서 경로 제한

## 시행 단계

1단계는 경고다. 기존 표류가 이미 여러 건이라 곧바로 차단하면 무관한 커밋이
전부 막힌다. 기존 항목을 고친 뒤 `--warn` 을 떼고 차단으로 올린다.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
# 검색 뿌리는 **정본 저장소만** 둔다. /root/aads 전체를 뒤지면 릴리스 스냅숏과
# go100 클론 6벌에 같은 파일이 있어서 전부 "모호함" 으로 떨어지고, 훑는 데도
# 오래 걸린다(실측: `.md` 만 13,219개).
ROOTS = [REPO, REPO.parent / "aads-dashboard"]

# "page.tsx  (4501L)" / "### 2.1 page.tsx (4,501줄)" / "| `x.py` | 4,158줄 |"
_CLAIM = re.compile(
    r"(?P<file>[A-Za-z0-9_./-]+\.(?:py|tsx|ts|jsx|js|md|sh))"
    r"[^\n(|]{0,80}?"
    r"[(|]\s*(?P<num>\d[\d,]*)\s*(?:L|줄|lines?)\b"
)
# "chat.py — Chat V2 Router (30+ endpoints)" — 파일이 같은 줄에 있을 때만 센다.
# 파일 없이 "엔드포인트 30개" 라고만 적힌 곳은 무엇의 개수인지 알 수 없어서
# 대조하면 오탐이 난다. 실제로 이 검사기를 설명하는 PRD 문장이 걸렸다.
_ENDPOINTS = re.compile(
    r"(?P<file>[A-Za-z0-9_./-]+\.py)"
    r"[^\n]{0,90}?"
    r"\(\s*(?P<num>\d+)\s*\+?\s*(?:개\s*)?(?:endpoints?|엔드포인트)"
)

_SKIP_DIR_PARTS = {
    ".git", "node_modules", ".worktrees", "generated", ".venvs", "backups",
    ".next", "dist", "build", "__pycache__",
}
# 사본이 사는 디렉터리. 정본과 같은 파일이 들어 있어 모두 "모호함" 을 만든다.
_SKIP_NAME_HINTS = ("-releases", "-unified-p0", "aads-dashboard-unni",
                    "claude-model-release-", ".tmp-")


# 표류를 기록한 문장은 검사 대상이 아니다. 감사 보고서가 "문서 513줄 →
# 실제 2,928줄" 이라고 적으면 그 513이 다시 주장으로 잡힌다. 실제로 이
# 검사기가 자기 감사 보고서와 기획서를 걸었다.
_REPORTING = ("→", "->", "실제", "actual", "vs ", "이었다", "였다")


_TWO_COUNTS = re.compile(r"\d[\d,]*\s*(?:L\b|줄|lines?\b)")


def _is_reporting_line(text: str, pos: int) -> bool:
    start = text.rfind("\n", 0, pos) + 1
    end = text.find("\n", pos)
    line = text[start: end if end != -1 else len(text)]
    if any(tok in line for tok in _REPORTING):
        return True
    # 표 한 행에 숫자가 둘 이상이면 "문서 N줄 / 실제 M줄" 대조표다.
    #   | `ChatArtifactPanel.tsx` | 513줄 | 2,928줄 | 5.7× |
    return len(_TWO_COUNTS.findall(line)) >= 2


def _iter_docs(paths: list[str]) -> list[Path]:
    out: list[Path] = []
    for raw in paths:
        base = Path(raw)
        if not base.is_absolute():
            base = REPO / base
        if base.is_file():
            out.append(base)
            continue
        if not base.is_dir():
            continue
        for p in base.rglob("*.md"):
            if _SKIP_DIR_PARTS & set(p.parts):
                continue
            out.append(p)
    return sorted(set(out))


_INDEX: dict[str, list[Path]] | None = None


def _file_index() -> dict[str, list[Path]]:
    """파일명 → 경로 목록. 한 번만 만든다.

    이름마다 rglob 을 돌리면 저장소를 수십 번 훑는다(실측 42초). 커밋 훅에서
    돌 것이라 한 번에 끝내야 한다.
    """
    global _INDEX
    if _INDEX is not None:
        return _INDEX
    import os

    idx: dict[str, list[Path]] = {}
    exts = {".py", ".tsx", ".ts", ".jsx", ".js", ".sh"}
    for root in ROOTS:
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            # 들어가기 **전에** 쳐낸다. rglob 은 node_modules 안까지 다 훑고
            # 나서 걸러서 느리다(실측 42초 → 이 방식으로 수 초).
            dirnames[:] = [
                d for d in dirnames
                if d not in _SKIP_DIR_PARTS
                and not any(h in d for h in _SKIP_NAME_HINTS)
            ]
            if any(h in dirpath for h in _SKIP_NAME_HINTS):
                continue
            for fn in filenames:
                if os.path.splitext(fn)[1].lower() in exts:
                    idx.setdefault(fn, []).append(Path(dirpath) / fn)
    _INDEX = idx
    return idx


# "src/app/chat/" 처럼 트리 블록 머리에 적히는 디렉터리 경로
_DIR_HINT = re.compile(r"(?P<dir>(?:[A-Za-z0-9_.-]+/){1,6})\s*$")


def _dir_hints(text: str, upto: int) -> list[str]:
    """주장 위치 바로 앞에서 디렉터리 힌트를 모은다.

    문서는 보통 트리 블록 머리에 `src/app/chat/` 을 적고 그 아래에 파일만
    나열한다. 이름만으로는 `page.tsx` 가 저장소에 여러 개라 모호한데,
    이 힌트를 붙이면 정확히 짚을 수 있다. 힌트가 없으면 검사하지 않는다 —
    잘못 짚은 대조는 없느니만 못하다.
    """
    head = text[:upto].split("\n")
    hints: list[str] = []
    for line in reversed(head[-40:]):
        m = _DIR_HINT.search(line.strip().rstrip("`").strip())
        if m:
            hints.append(m.group("dir").rstrip("/"))
        if len(hints) >= 3:
            break
    return hints


def _resolve(name: str, doc: Path, hints: list[str]) -> Path | None:
    """문서가 가리키는 파일을 찾는다. 끝까지 모호하면 포기한다.

    멀쩡한 문서를 틀렸다고 하면 다음 사람이 이 검사기를 안 믿게 된다.
    """
    cand = (doc.parent / name).resolve()
    if cand.is_file():
        return cand
    hits = _file_index().get(Path(name).name, [])
    if not hits:
        return None
    if len(hits) == 1:
        return hits[0]
    for hint in hints:
        narrowed = [p for p in hits if f"/{hint}/" in str(p)]
        if len(narrowed) == 1:
            return narrowed[0]
    return None


def _line_count(p: Path) -> int | None:
    try:
        return sum(1 for _ in p.open(encoding="utf-8", errors="ignore"))
    except OSError:
        return None


def _router_decorators(p: Path) -> int:
    """그 파일이 실제로 여는 창구 수."""
    try:
        return sum(1 for l in p.read_text(encoding="utf-8", errors="ignore").split("\n")
                   if l.startswith("@router."))
    except OSError:
        return 0


def _router_endpoints() -> int:
    total = 0
    d = REPO / "app" / "routers"
    if not d.is_dir():
        return 0
    for f in d.glob("*.py"):
        try:
            total += sum(1 for l in f.read_text(encoding="utf-8", errors="ignore").split("\n")
                         if l.startswith("@router."))
        except OSError:
            continue
    return total


def check(paths: list[str], tolerance: float) -> list[dict]:
    findings: list[dict] = []

    for doc in _iter_docs(paths):
        try:
            text = doc.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue

        for m in _CLAIM.finditer(text):
            if _is_reporting_line(text, m.start()):
                continue
            name = m.group("file")
            if name.endswith(".md"):
                continue
            claimed = int(m.group("num").replace(",", ""))
            target = _resolve(name, doc, _dir_hints(text, m.start()))
            if target is None:
                continue
            actual = _line_count(target)
            if actual is None or actual == 0:
                continue
            drift = abs(actual - claimed) / actual
            if drift > tolerance:
                findings.append({
                    "doc": str(doc.relative_to(REPO)) if str(doc).startswith(str(REPO)) else str(doc),
                    "line": text[: m.start()].count("\n") + 1,
                    "kind": "줄 수",
                    "what": name,
                    "claimed": f"{claimed:,}줄",
                    "actual": f"{actual:,}줄",
                    "drift": f"{drift*100:.0f}%",
                })

        for m in _ENDPOINTS.finditer(text):
            if _is_reporting_line(text, m.start()):
                continue
            claimed = int(m.group("num"))
            name = m.group("file")
            target = _resolve(name, doc, _dir_hints(text, m.start()))
            if target is None:
                continue
            actual = _router_decorators(target)
            if actual == 0:
                continue
            drift = abs(actual - claimed) / actual
            if drift > tolerance:
                findings.append({
                    "doc": str(doc.relative_to(REPO)) if str(doc).startswith(str(REPO)) else str(doc),
                    "line": text[: m.start()].count("\n") + 1,
                    "kind": "엔드포인트 수",
                    "what": name,
                    "claimed": f"{claimed}개",
                    "actual": f"{actual}개",
                    "drift": f"{drift*100:.0f}%",
                })
    return findings


def main() -> None:
    ap = argparse.ArgumentParser(description="문서 표류 검사")
    ap.add_argument("--paths", nargs="*", default=["docs"], help="검사할 문서 경로")
    ap.add_argument("--warn", action="store_true", help="표류가 있어도 0 으로 끝낸다")
    ap.add_argument("--tolerance", type=float, default=0.15,
                    help="허용 오차 비율 (기본 0.15 — 코드는 조금씩 늘 변한다)")
    args = ap.parse_args()

    findings = check(args.paths, args.tolerance)
    if not findings:
        print("[doc_drift] 표류 없음")
        return

    print(f"[doc_drift] 표류 {len(findings)}건 — 문서가 주장하는 숫자가 코드와 다르다\n")
    for f in findings:
        print(f"  {f['doc']}:{f['line']}")
        print(f"     {f['kind']} · {f['what']}")
        print(f"     문서 {f['claimed']}  →  실제 {f['actual']}   (차이 {f['drift']})")
        print()
    print("고칠 때: 문서의 숫자를 실제값으로 바꾸거나, 그 숫자를 문서에서 빼고")
    print("`scripts/generate_now.py` 가 만드는 생성물을 가리켜라.")
    if not args.warn:
        sys.exit(1)


if __name__ == "__main__":
    main()
