#!/usr/bin/env python3
"""언니냉면 정적 스냅샷·카페24 apache vhost 보조 도구 (deploy_unni_naengmyeon_cafe24.sh 전용).

    rewrite-html <in> <out>        Next 렌더 HTML 의 /_next/image?url=… 를 원본 경로로 바꾼다
                                   (카페24 에는 Next 이미지 최적화 서버가 없다).
    refs <html>...                 HTML 이 참조하는 스냅샷 자산 경로(/_next/static/, /brands/unni-naengmyeon/)
    render-vhost <template> <live> <out>
                                   live vhost 의 upstream 으로 template 을 렌더한다. 두 파일이 BEGIN-UNNI
                                   블록과 주석을 빼고 같을 때만 쓴다 — 다르면 손으로 바뀐 설정을 덮어쓰지
                                   않도록 종료코드 3.
    strip-unni <in> <out>          vhost 에서 BEGIN-UNNI..END-UNNI 블록을 뺀다(rollback).

종료코드: 0 성공 | 2 인자 오류 | 3 live 설정이 template 과 다름 | 4 검증 실패
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import unquote

# 쿼리 구분자는 HTML 속성(&amp;), 날것(&), RSC 페이로드 JSON(&) 세 가지로 나온다.
_SEP = r"(?:&amp;|&|\\u0026)"
_NEXT_IMAGE = re.compile(r"/_next/image\?url=([^&\"'\\\s,]+)(?:" + _SEP + r"(?:w|q)=\d+)*")
_ASSET = re.compile(r"""(/(?:_next/static|brands/unni-naengmyeon)/[^"'\\\s,()?#]+)""")
_UPSTREAM = re.compile(r"http://\d+\.\d+\.\d+\.\d+:\d+")
_PLACEHOLDER = "__FB_UPSTREAM__"


def rewrite_html(text: str) -> str:
    out = _NEXT_IMAGE.sub(lambda m: unquote(m.group(1)), text)
    if "/_next/image" in out:
        raise ValueError("/_next/image reference left after rewrite")
    return out


def asset_refs(text: str) -> list[str]:
    return sorted({unquote(m.group(1)) for m in _ASSET.finditer(text)})


def _upstream(live: str) -> str:
    found = {m.group(0) for line in live.splitlines() if not line.lstrip().startswith("#")
             for m in _UPSTREAM.finditer(line)}
    if len(found) != 1:
        raise ValueError(f"expected exactly one upstream in live vhost, found {sorted(found)}")
    return found.pop()


def _comparable(text: str) -> list[str]:
    """주석·빈 줄·BEGIN-UNNI 블록을 뺀 설정 줄."""
    lines, in_unni = [], False
    for raw in text.splitlines():
        line = raw.strip()
        if line == "# BEGIN-UNNI":
            in_unni = True
            continue
        if line == "# END-UNNI":
            in_unni = False
            continue
        if in_unni or not line or line.startswith("#"):
            continue
        lines.append(line)
    return lines


def strip_unni(text: str) -> str:
    out, in_unni = [], False
    for raw in text.splitlines(keepends=True):
        line = raw.strip()
        if line == "# BEGIN-UNNI":
            in_unni = True
        elif line == "# END-UNNI":
            in_unni = False
        elif not in_unni:
            out.append(raw)
    if in_unni:
        raise ValueError("unterminated BEGIN-UNNI block")
    return "".join(out)


def render_vhost(template: str, live: str) -> str:
    rendered = template.replace(_PLACEHOLDER, _upstream(live))
    if _comparable(rendered) != _comparable(live):
        raise LookupError("live vhost differs from the template outside BEGIN-UNNI")
    if rendered.count("# BEGIN-UNNI") != 2 or rendered.count("# END-UNNI") != 2:
        raise ValueError("template must carry BEGIN-UNNI..END-UNNI in both vhosts")
    return rendered


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    cmd, args = argv[0], argv[1:]
    try:
        if cmd == "rewrite-html" and len(args) == 2:
            Path(args[1]).write_text(rewrite_html(Path(args[0]).read_text(encoding="utf-8")), encoding="utf-8")
            return 0
        if cmd == "refs" and args:
            refs: set[str] = set()
            for name in args:
                refs.update(asset_refs(Path(name).read_text(encoding="utf-8")))
            print("\n".join(sorted(refs)))
            return 0
        if cmd == "render-vhost" and len(args) == 3:
            out = render_vhost(Path(args[0]).read_text(encoding="utf-8"), Path(args[1]).read_text(encoding="utf-8"))
            Path(args[2]).write_text(out, encoding="utf-8")
            return 0
        if cmd == "strip-unni" and len(args) == 2:
            Path(args[1]).write_text(strip_unni(Path(args[0]).read_text(encoding="utf-8")), encoding="utf-8")
            return 0
    except LookupError as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 3
    except ValueError as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 4
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
