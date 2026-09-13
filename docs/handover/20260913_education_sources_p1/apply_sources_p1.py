#!/usr/bin/env python3
"""교육자료 8건 공식 출처 검수·보강 패치 (sources_p1, 2026-09-13 KST).

원칙(P0 검수와 동일):
  ① 법령·공식 문서·원 논문 등 1차 출처 우선
  ② 모든 인용을 클릭 가능한 링크로 전환
  ③ 확인하지 못한 항목은 삭제하지 않고 '미검증'으로 명시
  ④ 원문과 다른 서술은 원문 기준으로 교정하되, 초판 서술을 지우지 않고 교정 근거를 병기

이 스크립트는 되돌릴 수 있도록 '치환 쌍'만 담는다. 원본에서 치환 대상 문자열을
찾지 못하거나 2건 이상 발견되면 그 파일은 건드리지 않고 실패를 보고한다.
"""
import os
import pathlib
import sys

_env = os.environ.get("AADS_REPORTS_DIR")
BASE = (pathlib.Path(_env) if _env
        else pathlib.Path(__file__).resolve().parents[3] / "app/static/reports")

KST = "2026-09-13"

# 부록 공통 꼬리말 — HTTP 응답코드의 의미를 오해하지 않도록 P0와 동일 문구 사용
FOOT = ('<p style="font-size:.82rem;color:var(--text3)">※ 본 부록의 "확인"은 '
        f'{KST} KST에 해당 1차 출처 문서를 실제로 열어 해당 문장을 대조했다는 뜻이며, '
        '그 사이트에 실린 모든 수치를 본 문서가 검증했다는 뜻은 아닙니다. '
        '가격·모델 라인업처럼 자주 바뀌는 항목은 의사결정 직전 원문을 다시 확인하십시오.</p>')


def sources_table(rows):
    """이번 검수에서 실제로 열어본 1차 출처 목록을 표로 만든다.

    rows: (주제, 제목, URL, 등급) 튜플 목록. 등급은 '1차(공식 문서)' 등.
    """
    if not rows:
        return ""
    trs = "\n".join(
        f'        <tr><td>{topic}</td><td><a href="{url}">{title}</a></td>'
        f'<td>{tier}</td><td>{KST} KST</td></tr>'
        for topic, title, url, tier in rows)
    return f'''
  <h3>이번 검수에서 대조한 1차 출처</h3>
  <div class="table-wrap">
    <table>
      <thead><tr><th>주제</th><th>출처(클릭 가능)</th><th>등급</th><th>확인 시각</th></tr></thead>
      <tbody>
{trs}
      </tbody>
    </table>
  </div>
'''


def appendix(body_items, note=None, sources=None):
    """부록 섹션 HTML을 만든다. body_items 는 <li> 안에 들어갈 문자열 목록."""
    lis = "\n".join("    <li>%s</li>" % item for item in body_items)
    extra = ("\n  <p>%s</p>" % note) if note else ""
    extra += sources_table(sources or [])
    return f'''
<!-- ===== 부록. 출처 검수 이력 (sources_p1, {KST}) ===== -->
<div class="section" id="source-audit-20260913">
  <h2 class="section-title section-anchor"><span class="icon">🔍</span> 부록. 출처 검수 이력 ({KST} KST)</h2>
  <p>본 문서는 2026-09-12 초판 작성 이후, <strong>{KST} KST</strong>에 별도 출처 검수를 거쳤습니다. 검수 원칙은 ① 공식 문서·규격 원문·원 논문 등 <strong>1차 출처 우선</strong>, ② 모든 인용을 <strong>클릭 가능한 링크</strong>로 전환, ③ 확인하지 못한 내용은 삭제하지 않고 <span class="tag-unverified">미검증</span>으로 명시, ④ 원문과 다른 서술은 원문 기준으로 교정입니다. 아래는 이번 검수에서 <strong>실제로 바뀐 내용</strong>입니다.</p>
  <ul>
{lis}
  </ul>{extra}
  {FOOT}
</div>
'''


PATCHES = {}

# =====================================================================
# 1) RAG · 벡터DB · 임베딩
#    본문 교정은 apply_rag_edits.py 가 먼저 수행한다. 여기서는 부록만 붙인다.
# =====================================================================
PATCHES["20260912_rag_vectordb_embedding_education.html"] = []

# =====================================================================
# 2) MCP · 도구 호출
# =====================================================================
PATCHES["20260912_mcp_tool_calling_education.html"] = [
    # 초판이 '확인 필요'로 남긴 Client Features 항목 — 규격 원문에서 폐기예정으로 확정
    ("""        <tr><td>MCP 스펙 2026-07-28의 Client Features 요약에서 Sampling/Roots 누락</td><td style="color:var(--warning);font-weight:600">미검증(확인 필요)</td><td>개요 페이지 요약 문구 변경인지 실제 폐지인지 changelog 원문 대조 필요</td></tr>""",
     f"""        <tr><td>MCP 스펙 2026-07-28의 Client Features 요약에서 Sampling/Roots 누락</td><td><span class="tag-verified">해소</span> ({KST})</td><td><strong>요약 문구 변경이 아니라 실제 폐기예정(deprecated)이었음.</strong> 규격 원문 확인 결과 <a href="https://modelcontextprotocol.io/specification/2026-07-28/client/sampling">Sampling</a>·<a href="https://modelcontextprotocol.io/specification/2026-07-28/client/roots">Roots</a> 두 기능 모두 프로토콜 버전 <code>2026-07-28</code>부터 deprecated로 표시되며 근거는 <a href="https://github.com/modelcontextprotocol/modelcontextprotocol/pull/2577">SEP-2577</a>. 기능 수명주기 정책상 <strong>이 개정판 공개 후 최소 12개월</strong>은 규격에 남아 있다가 제거 대상이 된다. 원문은 "신규 구현은 채택하지 말 것(SHOULD NOT)"을 명시하고, 대체 경로로 Roots는 <strong>도구 파라미터·리소스 URI·서버 설정</strong>, Sampling은 <strong>LLM 공급사 API 직접 연동</strong>을 제시한다. 따라서 현행 Client Features는 <strong>Elicitation 하나</strong>다.</td></tr>"""),
]

# =====================================================================
# 3) 파인튜닝 · LoRA
#    초판이 arXiv ID·공식 문서 주소를 '맨 텍스트'로만 적어 두어 클릭이 안 됐다.
#    수치 자체는 원문과 일치하므로(부록 참조) 링크화 + URL 이전 반영만 한다.
# =====================================================================
PATCHES["20260912_finetuning_lora_practice_education.html"] = [
    ("""  <h3>핵심 논문 (arXiv, 2026-09-12 원문 확인)</h3>""",
     f"""  <h3>핵심 논문 (arXiv, 2026-09-12 원문 확인 · {KST} KST 링크화·제목 대조)</h3>"""),
    # Claude 공식 문서 URL 이전(302) 반영
    ("""<strong>Claude Glossary(파인튜닝 항목)</strong> — docs.claude.com/en/docs/about-claude/glossary""",
     """<strong>Claude Glossary(파인튜닝 항목)</strong> — <a href="https://platform.claude.com/docs/en/about-claude/glossary">platform.claude.com/docs/en/about-claude/glossary</a><br><small style="color:var(--text3)">(2026-09-13 확인: 초판이 적은 <code>docs.claude.com/…</code>은 <code>platform.claude.com/…</code>으로 302 리다이렉트된다)</small>"""),
    ("""<strong>OpenAI 파인튜닝 가이드</strong> — developers.openai.com/api/docs/guides/supervised-fine-tuning""",
     """<strong>OpenAI 파인튜닝 가이드</strong> — <a href="https://developers.openai.com/api/docs/guides/supervised-fine-tuning">developers.openai.com/api/docs/guides/supervised-fine-tuning</a>"""),
    ("""<strong>Gemini API 파인튜닝 안내</strong> — ai.google.dev/gemini-api/docs/model-tuning""",
     """<strong>Gemini API 파인튜닝 안내</strong> — <a href="https://ai.google.dev/gemini-api/docs/model-tuning">ai.google.dev/gemini-api/docs/model-tuning</a>"""),
    ("""<strong>Hugging Face PEFT</strong> — huggingface.co/docs/peft""",
     """<strong>Hugging Face PEFT</strong> — <a href="https://huggingface.co/docs/peft">huggingface.co/docs/peft</a>"""),
    ("""<strong>Hugging Face TRL</strong> — huggingface.co/docs/trl""",
     """<strong>Hugging Face TRL</strong> — <a href="https://huggingface.co/docs/trl">huggingface.co/docs/trl</a>"""),
]

# =====================================================================
# 4) LLMOps · 관측성
# =====================================================================
PATCHES["20260912_llmops_observability_education.html"] = [
    ("""        <tr><td><code>gen_ai.system</code></td><td>모델 공급사</td><td>anthropic, openai</td></tr>""",
     f"""        <tr><td><code>gen_ai.provider.name</code><br><small style="color:var(--text3)">(초판 표기: <code>gen_ai.system</code>)</small></td><td>모델 공급사<span class="tag-verified">교정</span></td><td>anthropic, openai<br><small style="color:var(--text3)"><strong>{KST} 교정</strong> — 현행 규격 문서에 <code>gen_ai.system</code>은 더 이상 등장하지 않으며, 공급사 식별자는 <code>gen_ai.provider.name</code>이다(추론 오퍼레이션에서 <strong>Required</strong>).</small></td></tr>"""),
    ("""        <tr><td><code>gen_ai.usage.output_tokens</code></td><td>출력 토큰 수</td><td>512</td></tr>""",
     f"""        <tr><td><code>gen_ai.usage.output_tokens</code></td><td>출력 토큰 수</td><td>512</td></tr>
        <tr><td><code>gen_ai.usage.cache_read.input_tokens</code><br><code>gen_ai.usage.cache_write.input_tokens</code></td><td>캐시 읽기/쓰기 토큰 수<span class="tag-verified">신규</span></td><td>—<br><small style="color:var(--text3)"><strong>{KST} 추가</strong> — 프롬프트 캐싱 비용을 분리 계측하려면 이 두 속성이 필요하다. 규격에는 <code>gen_ai.usage.reasoning.output_tokens</code>(추론 토큰)와 text/image/audio 모달리티별 변형도 정의되어 있다.</small></td></tr>"""),
    ("""여기에 생성형 AI 전용 속성 이름을 표준화한 것이 <strong>GenAI 시맨틱 컨벤션</strong>입니다.</p>""",
     f"""여기에 생성형 AI 전용 속성 이름을 표준화한 것이 <strong>GenAI 시맨틱 컨벤션</strong>입니다.</p>
  <div class="alert alert-warn">
    <strong>⚠️ {KST} KST 검수 — 규격 위치가 옮겨졌고, 아직 '개발(Development)' 단계다</strong>
    GenAI 시맨틱 컨벤션은 OpenTelemetry 본체 문서에서 <strong>전용 저장소 <a href="https://github.com/open-telemetry/semantic-conventions-genai">open-telemetry/semantic-conventions-genai</a>로 이전</strong>되었습니다(기존 <code>opentelemetry.io/docs/specs/semconv/gen-ai/</code> 페이지는 이전 안내만 표시). 이 저장소는 GenAI 클라이언트뿐 아니라 <strong>MCP</strong>와 공급사별 규약까지 함께 정의합니다.
    <br>또한 <a href="https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/gen-ai-spans.md">규격 원문</a> 기준으로 <code>gen_ai.*</code> 속성 대부분은 아직 <strong>Development(개발) 안정성 등급</strong>이며, <code>server.address</code>·<code>server.port</code> 같은 공용 속성만 Stable입니다. <strong>속성명이 앞으로도 바뀔 수 있다</strong>는 뜻이므로, 대시보드 쿼리를 속성명에 직접 하드코딩하기보다 수집 계층에서 한 번 매핑해 두는 편이 안전합니다.
  </div>"""),
]

# =====================================================================
# 5) 멀티모달 AI  (구조 버그 1건 포함)
# =====================================================================
PATCHES["20260912_multimodal_ai_education.html"] = [
    # 구조 버그: alert div 가 </p> 로 닫혀 있어 문서 트리가 어긋남
    ("""외부에서 받은 이미지(사용자 업로드, 웹 크롤링 결과)를 비전 모델에 넣을 때는 이 위험을 감안한 시스템 프롬프트 방어가 필요합니다.</p>""",
     """외부에서 받은 이미지(사용자 업로드, 웹 크롤링 결과)를 비전 모델에 넣을 때는 이 위험을 감안한 시스템 프롬프트 방어가 필요합니다.</div>"""),
]

# =====================================================================
# 6) AI 코딩 에이전트  (구조 버그 1건 포함)
# =====================================================================
PATCHES["20260912_ai_coding_agent_practice_education.html"] = [
    # 구조 버그: 열린 적 없는 </p> 가 경고 박스 div 를 먼저 닫아버려,
    # 바로 다음 줄의 </div> 가 11장 section 을 통째로 조기 종료시키고 있었다.
    # (그 결과 문서 끝에서 <body> 가 </div> 로 닫히는 연쇄 오류가 났다.)
    # 뒤따르는 </div> 가 본래의 닫는 태그이므로, 군더더기 </p> 만 제거한다.
    ("""병렬 job이 같은 프로젝트의 같은 파일을 동시에 건드리지 않도록 작업지시서 설계로 보완해야 합니다.</p>""",
     """병렬 job이 같은 프로젝트의 같은 파일을 동시에 건드리지 않도록 작업지시서 설계로 보완해야 합니다."""),
]

# =====================================================================
# 7) AI 제품 PRD · AX
# =====================================================================
PATCHES["20260912_ai_product_prd_ax_education.html"] = []

# =====================================================================
# 8) AI 비용 · 토큰 이코노믹스  (구조 버그 2건 포함)
# =====================================================================
PATCHES["20260912_ai_cost_token_economics_education.html"] = [
    # 구조 버그 ①: 열린 적 없는 </p> 가 div 를 먼저 닫아버림
    ("""도구 조합을 잘 설계하면 특정 오버헤드를 회피할 수 있다는 뜻입니다.</p></div>""",
     """도구 조합을 잘 설계하면 특정 오버헤드를 회피할 수 있다는 뜻입니다.</div>"""),
    # 구조 버그 ②: 닫는 태그 오타 </strong하며,>
    ("""<strong>호출량(26,760건)과 성공률(99.39%)만 실측 가능</strong하며,""",
     """<strong>호출량(26,760건)과 성공률(99.39%)만 실측 가능</strong>하며,"""),
    # 공식 문서 URL 이전(302) 반영 + 링크화
    ("""본 장의 배수(1.25x / 2.0x / 0.1x)는 2026-09-12 <code>fetch_url</code>로 확인한 Anthropic 공식 문서(docs.claude.com/en/docs/about-claude/pricing)의 "Prompt caching" 섹션을 그대로 인용했습니다.""",
     f"""본 장의 배수(1.25x / 2.0x / 0.1x)는 2026-09-12 <code>fetch_url</code>로 확인한 Anthropic 공식 문서의 "Prompt caching" 섹션을 그대로 인용했으며, <strong>{KST} KST에 원문을 다시 열어 값이 그대로임을 재확인</strong>했습니다(<a href="https://platform.claude.com/docs/en/about-claude/pricing">공식 가격 문서</a>). <strong>다만 인용 URL이 바뀌었습니다</strong> — 초판이 적은 <code>docs.claude.com/en/docs/about-claude/pricing</code>은 현재 <code>platform.claude.com/docs/en/about-claude/pricing</code>으로 <strong>302 리다이렉트</strong>되므로, 사내 문서·북마크는 새 주소로 갱신하십시오."""),
]


# 맨 텍스트로 적힌 arXiv 주소를 클릭 가능한 링크로 바꿀 파일
LINKIFY_ARXIV = {"20260912_finetuning_lora_practice_education.html"}


def linkify_arxiv(src):
    """<td>arxiv.org/abs/2106.09685</td> 형태를 앵커로 바꾼다.

    이미 <a> 안에 있는 주소는 건드리지 않도록 여는 태그 직후 패턴만 잡는다.
    """
    import re
    pattern = re.compile(r'(<td>)(arxiv\.org/abs/(\d{4}\.\d{4,5}))(</td>)')
    return pattern.subn(
        lambda m: f'{m.group(1)}<a href="https://arxiv.org/abs/{m.group(3)}">'
                  f'{m.group(2)}</a>{m.group(4)}', src)


def insert_appendix(src, html, toc_label):
    """부록 섹션과 목차 항목을 넣는다."""
    if 'id="source-audit-20260913"' in src:
        return src, "이미 부록 있음"
    # 1) 부록 본문: footer 직전에 삽입
    marker = '<div class="footer">'
    if src.count(marker) != 1:
        return src, "footer 앵커를 1건으로 특정하지 못함"
    src = src.replace(marker, html + "\n" + marker)
    # 2) 목차: 마지막 <a href="#chNN"> 항목 뒤에 추가
    import re
    hits = list(re.finditer(r'[ \t]*<a href="#ch\d+">.*?</a>\n', src))
    if not hits:
        return src, "목차 앵커를 찾지 못함(부록 본문만 삽입됨)"
    last = hits[-1]
    entry = ('    <a href="#source-audit-20260913"><span class="num">★</span> '
             f'{toc_label}</a>\n')
    src = src[:last.end()] + entry + src[last.end():]
    return src, None


def main():
    import json
    appendices = json.loads(
        (pathlib.Path(__file__).parent / "appendix_content.json").read_text(encoding="utf-8"))
    failed = []
    for name, pairs in PATCHES.items():
        path = BASE / name
        src = path.read_text(encoding="utf-8")
        if 'id="source-audit-20260913"' in src:
            # 이미 이 검수가 반영된 파일 — 두 번 적용하지 않는다(스크립트는 멱등하지 않다)
            print(f"SKIP {name} · 이미 검수본")
            continue
        original = src
        for old, new in pairs:
            n = src.count(old)
            if n != 1:
                failed.append(f"{name}: 치환 대상 {n}건 — {old[:60]}")
                src = original
                break
            src = src.replace(old, new)
        else:
            if name in LINKIFY_ARXIV:
                src, n_links = linkify_arxiv(src)
                print(f"    arXiv 링크화 {n_links}건")
            spec = appendices.get(name)
            if spec:
                src, warn = insert_appendix(
                    src,
                    appendix(spec["items"], spec.get("note"), spec.get("sources")),
                    spec["toc"])
                if warn:
                    failed.append(f"{name}: {warn}")
            path.write_text(src, encoding="utf-8")
            print(f"OK  {name} · 치환 {len(pairs)}건 · 부록 {'추가' if spec else '없음'}")
            continue
        print(f"FAIL {name}")
    if failed:
        print("\n--- 실패 ---")
        for f in failed:
            print(" ", f)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
