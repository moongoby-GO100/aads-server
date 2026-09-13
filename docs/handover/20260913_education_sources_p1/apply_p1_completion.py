#!/usr/bin/env python3
"""sources_p1 잔여 2건(멀티모달·RAG) 출처 검수 완료 패치 (2026-09-13 KST, 재검수 대응).

배경
----
1차 작업에서 8건 중 6건은 본문 교정 + 부록까지 커밋됐으나, 멀티모달·RAG 2건은
목차에 부록 앵커(<a href="#source-audit-20260913">)만 들어가고 부록 본문이
누락된 채 커밋됐다. 그 결과 두 파일은 구조 검증에서 '깨진 앵커'로 FAIL 이었다.

이 스크립트가 하는 일
--------------------
  ① RAG      : 준비돼 있던 부록(appendix_content.json)을 실제로 삽입 — 깨진 앵커 해소
  ② 멀티모달 : 부록 삽입 + 초판이 "미검증"으로 남긴 이미지 토큰 환산 공식을
               Claude 공식 문서(1차 출처)로 확정하는 본문 교정
  ③ 두 파일  : 모바일 가로 스크롤(그리드 blowout) 방지 CSS 보강

원칙은 1차 작업과 동일하다 — 초판 서술을 지우지 않고, 교정 근거를 병기하며,
치환 대상이 정확히 1건이 아니면 그 파일은 건드리지 않고 실패를 보고한다.
"""
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
BASE = HERE.parents[2] / "app/static/reports"
KST = "2026-09-13"

MULTIMODAL = "20260912_multimodal_ai_education.html"
RAG = "20260912_rag_vectordb_embedding_education.html"

VISION_DOC = "https://platform.claude.com/docs/en/build-with-claude/vision"
PRICING_DOC = "https://platform.claude.com/docs/en/about-claude/pricing"
MODELS_DOC = "https://platform.claude.com/docs/en/about-claude/models/overview"

# ---------------------------------------------------------------------------
# 1) 본문 교정 — 멀티모달만 해당 (RAG 본문은 1차 작업에서 이미 교정 완료)
# ---------------------------------------------------------------------------
PATCHES = {
    MULTIMODAL: [
        # 2장: "환산 공식은 미검증" → Claude 공식 문서로 확정. 타사는 여전히 미검증으로 남긴다.
        ('정확한 타일 크기·토큰 환산 공식은 제공사·모델별로 다르고 수시로 바뀌므로 '
         '<span class="unverified">본 문서에서는 구체적 환산표를 확정값으로 제시하지 않고 '
         '"미검증"으로 남겨두며, 실제 예산 산정 시 각사 공식 문서의 최신 계산기를 사용해야 합니다.'
         '</span></p>',

         f'정확한 타일 크기·토큰 환산 공식은 제공사·모델별로 다릅니다. '
         f'<strong>{KST} KST 재검수에서 Claude 계열만은 공식 문서로 확정했습니다</strong> — '
         f'<a href="{VISION_DOC}">Claude Vision 공식 문서</a>는 이미지를 픽셀이 아니라 '
         f'<strong>28×28 픽셀 블록(=비주얼 토큰 1개)</strong>으로 나눠 보며, 이미지 1장의 비용은 '
         f'<code>⌈가로÷28⌉ × ⌈세로÷28⌉</code> 토큰이라고 명시합니다. '
         f'예를 들어 1000×1000 이미지는 <strong>1,296 토큰</strong>입니다. '
         f'<span class="tag-verified">공식</span> '
         f'다만 <strong>Google·OpenAI의 환산 공식은 이번 검수 범위 밖이라 여전히 '
         f'<span class="unverified">미검증</span></strong>이며, 예산 산정 시에는 각사 공식 문서를 '
         f'직접 확인해야 합니다.</p>'),

        # 8장 제목: 인용 주소가 바뀐 사실을 반영하고 재확인 날짜를 남긴다.
        ('<h3>Anthropic Claude — 공식 가격 (anthropic.com/pricing 2026-09-12 확인)</h3>',
         f'<h3>Anthropic Claude — 공식 가격 '
         f'(<a href="{PRICING_DOC}">공식 가격 문서</a> · 2026-09-12 확인, {KST} KST 재확인)</h3>'),

        # 8장 본문: 이미지 과금 방식 + 2장에서 확정한 공식을 실제 금액으로 연결한다.
        ('<p>Claude 계열은 <strong>이미지 입력에 별도 단가를 매기지 않습니다.</strong> '
         '이미지를 토큰으로 환산한 뒤 위 "입력 토큰" 단가로 동일하게 과금합니다. '
         '즉 이미지 1장이 몇 토큰으로 환산되는지가 실질 비용을 좌우하며, 이 환산 공식 자체는 '
         '2장에서 언급했듯 미검증(공식 계산기 재확인 필요) 영역입니다.</p>',

         f'<p>Claude 계열은 <strong>이미지 입력에 별도 단가를 매기지 않습니다.</strong> '
         f'이미지를 토큰으로 환산한 뒤 위 "입력 토큰" 단가로 동일하게 과금합니다. '
         f'즉 이미지 1장이 몇 토큰으로 환산되는지가 실질 비용을 좌우하는데, '
         f'<strong>{KST} KST 재검수에서 이 환산 공식을 공식 문서로 확정했습니다</strong> '
         f'(2장 참조). <a href="{VISION_DOC}">Claude Vision 공식 문서</a> 기준으로 정리하면:</p>\n'
         f'  <ul>\n'
         f'    <li>이미지 1장 = <code>⌈가로÷28⌉ × ⌈세로÷28⌉</code> 비주얼 토큰 '
         f'(28×28 픽셀 = 토큰 1개) <span class="tag-verified">공식</span></li>\n'
         f'    <li>모델별 해상도 상한이 있어 초과분은 자동 축소된다 — '
         f'<strong>Claude 4.7 이후 모델</strong>은 장변 2,576px·최대 4,784 토큰, '
         f'<strong>그 외 모델</strong>은 장변 1,568px·최대 1,568 토큰. '
         f'즉 고해상도 티어는 같은 이미지에 최대 약 3배 토큰을 쓴다.</li>\n'
         f'    <li>공식 문서의 환산 예시(1,000장 기준): 1000×1000 이미지는 '
         f'<strong>Haiku 4.5에서 약 $1.30</strong>, <strong>Opus 5(고해상도 티어)에서 약 $6.48</strong>, '
         f'4K(3840×2160) 이미지는 <strong>Opus 5에서 약 $23.92</strong>.</li>\n'
         f'    <li>입력 한도: 이미지 최대 <strong>8000×8000px</strong>, 장당 <strong>10MB</strong>'
         f'(Amazon Bedrock·Google Cloud는 5MB), 요청당 <strong>100장</strong>'
         f'(200k 컨텍스트 모델) 또는 <strong>600장</strong>, claude.ai는 메시지당 20장. '
         f'JPEG·PNG·GIF·WebP만 지원하며 애니메이션은 첫 프레임만 사용된다.</li>\n'
         f'  </ul>\n'
         f'  <p>따라서 "이미지 몇 장을 분석시키면 얼마인가"는 이제 <strong>추정이 아니라 계산</strong>으로 '
         f'답할 수 있습니다. 대량 이미지 파이프라인은 전송 전에 해상도를 줄이는 것만으로 '
         f'토큰 비용이 직접 줄어듭니다.</p>'),
    ],
    RAG: [],
}

# ---------------------------------------------------------------------------
# 2) 부록 — 멀티모달은 1차 초안의 장 번호 오기를 바로잡고 이번 검수 결과를 반영해 교체
# ---------------------------------------------------------------------------
MULTIMODAL_ITEMS = [
    f'<strong>확인(유지) — Anthropic 라인업</strong>: <strong>1장</strong> 비교표의 '
    f'"Opus 5 / Sonnet 5 / Haiku 4.5 / Fable 5.1 — 모두 이미지·문서 입력 지원, '
    f'<strong>자체 이미지/영상 생성 모델은 없음</strong>" 서술을 '
    f'<a href="{MODELS_DOC}">공식 모델 개요</a>와 '
    f'<a href="{VISION_DOC}">Vision 공식 문서</a>로 대조해 확인했다. '
    f'Vision 문서 FAQ는 "Claude는 이미지 이해 전용 모델이며 이미지를 생성·편집할 수 없다"고 '
    f'명시하고 있어 초판 서술이 정확하다. <span class="tag-verified">공식</span>',

    f'<strong>해소(미검증 → 확정) — 이미지 토큰 환산 공식</strong>: 초판이 <strong>2장·8장</strong>에서 '
    f'"환산 공식은 미검증, 공식 계산기 재확인 필요"로 남겨둔 항목을 '
    f'<a href="{VISION_DOC}">Claude Vision 공식 문서</a>로 확정했다 — '
    f'<strong>28×28 픽셀 = 비주얼 토큰 1개</strong>, 이미지 1장 = '
    f'<code>⌈가로÷28⌉ × ⌈세로÷28⌉</code> 토큰. 해상도 티어는 '
    f'<strong>Claude 4.7 이후 장변 2,576px·최대 4,784토큰 / 그 외 1,568px·최대 1,568토큰</strong>이며, '
    f'초과 이미지는 거부가 아니라 <strong>자동 축소</strong>된다. '
    f'이 문서에서 가장 컸던 미검증 구멍이 메워진 항목이다. <span class="tag-verified">공식</span>',

    f'<strong>확인(유지) — 8장 Claude 단가표</strong>: Fable 5.1 $10/$50, Opus 5 $5/$25, '
    f'Sonnet 5 $2/$10, Haiku 4.5 $1/$5와 캐시 읽기·쓰기 단가까지 '
    f'<a href="{PRICING_DOC}">공식 가격 문서</a>와 <strong>전 행 일치</strong>함을 확인해 그대로 두고 '
    f'근거 링크만 붙였다. 덧붙여 공식 문서는 Sonnet 5의 $2/$10이 <strong>도입 특가가 아니라 정식 가격</strong>이며 '
    f'2026-09-01로 예정됐던 $3/$15 인상은 <strong>시행되지 않는다</strong>고 명시한다 — '
    f'초판 작성 시점 이후 바뀐 사실이므로 예산 계획에 반영할 것. '
    f'<strong>인용 주소도 갱신했다</strong>: <code>anthropic.com/pricing</code> → '
    f'<code>platform.claude.com/docs/en/about-claude/pricing</code>.',

    f'<strong>신규 — 이미지 입력 한도(초판에 없던 운영 제약)</strong>: 최대 '
    f'<strong>8000×8000px</strong>, 장당 <strong>10MB</strong>(Bedrock·Google Cloud 5MB), '
    f'요청당 <strong>100장</strong>(200k 컨텍스트 모델) 또는 <strong>600장</strong>, '
    f'claude.ai 메시지당 20장. 지원 형식은 JPEG·PNG·GIF·WebP이며 <strong>애니메이션은 첫 프레임만</strong> '
    f'사용된다. 한 요청에 이미지가 20장을 넘으면 장당 해상도 제한이 더 엄격해져(권장: 각 변 2,000px 이하) '
    f'초과 시 <code>invalid_request_error</code>로 거부된다 — 대량 처리 파이프라인 설계 시 먼저 확인할 값이다. '
    f'<span class="tag-verified">공식</span>',

    f'<strong>신규 — 공식 문서가 명시한 비전 한계</strong>: '
    f'<a href="{VISION_DOC}">Vision 공식 문서</a>는 Claude가 '
    f'<strong>이미지 속 인물을 식별하지 않으며(정책상 거부)</strong>, '
    f'<strong>어떤 이미지가 AI 생성물인지 판별할 수 없다</strong>고 명시한다. '
    f'10장의 딥페이크·신원 오용 리스크를 "모델로 걸러내면 된다"고 오해하지 않도록 덧붙인다.',

    '<strong>구조 교정</strong>: <strong>10장</strong> "이미지 속 프롬프트 인젝션 주의" 경고 박스가 '
    '<code>&lt;div&gt;</code>로 열린 뒤 <code>&lt;/p&gt;</code>로 닫혀 HTML 트리가 어긋나 있던 문제를 '
    '바로잡았다(브라우저는 관대하게 렌더링하지만, 이후 섹션이 이 박스 안에 중첩된 것으로 해석될 수 있었다).',

    '<strong>라벨링 — 타사 제품명은 시점 표기</strong>: <strong>1장</strong> 비교표의 Google·OpenAI '
    '제품명(Gemini 3.8 Flash, Nano Banana Pro, GPT-6 Astra, Sora 2 등)은 '
    '<strong>2026-09-12 초판 작성 시점의 라인업</strong>이다. 생성형 모델 라인업은 분기 단위로 바뀌므로 '
    '<strong>제품명·버전 자체가 빠르게 낡는다</strong>. 본 검수에서는 Anthropic 라인업만 1차 출처로 '
    '재확인했고, 나머지는 시점 표기를 유지한다.',

    '<strong>유지</strong>: AADS 내부 미디어 공급자 표(kling, black-forest-labs/flux, stability/SD 3.5, '
    'genspark_ui 폴백 등)는 초판이 저장소 설정을 직접 조회해 기록한 <strong>내부 실측</strong>이므로 '
    '그대로 둔다. 다만 이는 <strong>AADS 환경의 설정값</strong>이지 각 사 공식 제품 사양이 아니다.',

    '<strong>미해소</strong>: <strong>Google·OpenAI의 이미지 토큰 환산 단가</strong>(8장 미검증 표기 유지)와 '
    '타사 생성 모델의 <strong>라이선스·상업적 이용 조건</strong>은 이번 검수 범위 밖이다. '
    '특히 생성물의 <strong>저작권·상업적 이용 가능 범위</strong>는 공급사 약관마다 다르므로, '
    '마케팅·제품에 실제 사용하기 전 <strong>법무 검토</strong>가 필요하다.',
]

MULTIMODAL_SOURCES = [
    ["모델 사양", "Claude 공식 모델 개요", MODELS_DOC, "1차(공식 문서)"],
    ["이미지 과금·한도", "Claude Vision 공식 문서", VISION_DOC, "1차(공식 문서)"],
    ["토큰 단가", "Claude 공식 가격 문서", PRICING_DOC, "1차(공식 가격표)"],
    ["모델 사양", "Gemini API 공식 문서", "https://ai.google.dev/gemini-api/docs", "1차(공식 문서)"],
    ["원 논문", "CLIP — arXiv:2103.00020", "https://arxiv.org/abs/2103.00020", "1차(원 논문)"],
    ["원 논문", "Whisper — arXiv:2212.04356", "https://arxiv.org/abs/2212.04356", "1차(원 논문)"],
]

# 모바일 가로 스크롤 방지 — grid/flex 항목의 기본 min-width:auto 때문에
# 안에 든 <pre>·긴 토큰이 트랙을 밀어내 문서 폭을 넘기는 현상(그리드 blowout)을 막는다.
CSS_FIX = """/* 모바일 가로 스크롤 제거(2026-09-13 sources_p1 완료 패치): grid/flex 항목은 기본값이
   min-width:auto 라서 안에 든 <pre>·긴 토큰이 트랙을 밀어내 문서 폭을 넘긴다. */
.compare-row>*,.card-grid>*,.toc-grid>*,.compare-box,.info-card{min-width:0}
pre{max-width:100%}
"""


def inject_css(src):
    """</style> 직전에 가로 스크롤 방지 규칙을 넣는다. 이미 있으면 건드리지 않는다."""
    if '.compare-row>*' in src:
        return src, False
    if src.count('</style>') != 1:
        raise ValueError("</style> 를 1건으로 특정하지 못함")
    return src.replace('</style>', CSS_FIX + '</style>'), True


def main():
    sys.path.insert(0, str(HERE))
    from apply_sources_p1 import appendix, insert_appendix  # noqa: E402

    spec = json.loads((HERE / "appendix_content.json").read_text(encoding="utf-8"))
    # 멀티모달 부록은 장 번호 오기 교정 + 이번 검수 결과 반영본으로 교체한다.
    spec[MULTIMODAL]["items"] = MULTIMODAL_ITEMS
    spec[MULTIMODAL]["sources"] = MULTIMODAL_SOURCES

    failed = []
    for name, pairs in PATCHES.items():
        path = BASE / name
        src = original = path.read_text(encoding="utf-8")

        for old, new in pairs:
            n = src.count(old)
            if n != 1:
                failed.append(f"{name}: 치환 대상 {n}건 — {old[:60]}")
                src = original
                break
            src = src.replace(old, new)
        else:
            entry = spec[name]
            src, warn = insert_appendix(
                src, appendix(entry["items"], entry.get("note"), entry.get("sources")),
                entry["toc"])
            if warn:
                failed.append(f"{name}: {warn}")
                continue
            src, css_added = inject_css(src)
            path.write_text(src, encoding="utf-8")
            print(f"OK  {name} · 본문 치환 {len(pairs)}건 · 부록 추가"
                  f"{' · CSS 보강' if css_added else ''}")
            continue
        print(f"FAIL {name}")

    if failed:
        print("\n--- 실패 ---")
        for item in failed:
            print(" ", item)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
