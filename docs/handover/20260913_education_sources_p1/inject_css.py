#!/usr/bin/env python3
"""교육자료 8건에 인용 링크·검증 태그 CSS를 주입한다 (sources_p1, 2026-09-13 KST).

초판 8건은 외부 링크가 단 한 건도 없어 링크 스타일이 정의되어 있지 않았다.
또 8건 중 7건에는 <span class="tag-verified"> / <span class="tag-unverified">
스타일 자체가 없어(본문에서 '미검증'을 맨 텍스트로만 표기) 함께 넣는다.

</style> 직전에 넣는다 — 파일마다 CSS 구성이 달라 특정 규칙을 앵커로 쓰면
한 건이라도 어긋나기 때문이다.
"""
import pathlib
import sys

LINK_CSS = '''/* --- 2026-09-13 출처 검수(sources_p1): 인용 링크·검증 태그 스타일 --- */
.section a[href^="http"],.footer a[href^="http"],.alert a[href^="http"],td a[href^="http"],li a[href^="http"]{color:var(--accent3);text-decoration:none;border-bottom:1px dashed var(--accent3);transition:.2s;word-break:break-word}
.section a[href^="http"]:hover,.footer a[href^="http"]:hover,.alert a[href^="http"]:hover,td a[href^="http"]:hover{color:var(--accent);border-bottom-style:solid}
.section a[href^="http"]::after,.footer a[href^="http"]::after{content:" \\2197";font-size:.75em;opacity:.7}
/* 긴 URL이 모바일에서 가로 스크롤을 만들지 않도록 줄바꿈 허용 */
.alert,.info-card,.checkpoint,td,li,p{overflow-wrap:break-word}
/* 모바일 가로 스크롤 제거: grid/flex 항목은 기본값이 min-width:auto 라서
   안에 든 <pre>·긴 토큰이 트랙을 밀어내 문서 폭을 넘긴다(그리드 blowout).
   compare-row 는 768px 이하에서 이미 1단으로 접히지만 이 규칙이 없으면 여전히 넘쳤다. */
.compare-row>*,.card-grid>*,.toc-grid>*,.compare-box,.info-card{min-width:0}
pre{max-width:100%}
'''

TAG_CSS = '''.tag-verified{display:inline-block;font-size:.72rem;font-weight:700;padding:2px 8px;border-radius:6px;background:#10b98120;color:#34d399;margin-left:6px}
.tag-unverified{display:inline-block;font-size:.72rem;font-weight:700;padding:2px 8px;border-radius:6px;background:#f59e0b20;color:#fbbf24;margin-left:6px}
'''


def main(argv):
    out = pathlib.Path(argv[0] if argv else "/root/edusrc-p1-scratch/reports")
    for path in sorted(out.glob("20260912_*_education.html")):
        src = path.read_text(encoding="utf-8")
        if "sources_p1" in src:
            print(f"  SKIP {path.name} (이미 주입됨)")
            continue
        if src.count("</style>") != 1:
            print(f"  FAIL {path.name}: </style> 앵커 특정 실패")
            return 1
        add = LINK_CSS if "tag-unverified{" in src else LINK_CSS + TAG_CSS
        path.write_text(src.replace("</style>", add + "</style>"), encoding="utf-8")
        print(f"  CSS  {path.name}" + ("" if "tag-unverified{" in src else " (+검증 태그)"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
