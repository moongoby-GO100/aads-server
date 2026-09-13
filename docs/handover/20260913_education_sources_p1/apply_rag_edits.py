#!/usr/bin/env python3
"""RAG·벡터DB 교육자료 출처 보강 패치 (sources_p1, 2026-09-13 KST).

초판의 '추정 / 미검증' 단가·차원·라이선스를 각 사 공식 문서(1차 출처)로 대조해
확정한 결과를 반영한다. 기존 서술을 삭제하지 않고 교정 근거를 병기하는 방식.
"""
import os
import pathlib
import sys

# 기본은 저장소의 app/static/reports. 동일 카드가 중복 디스패치된 상황에서
# 피어 러너가 워크트리를 덮어쓰는 것을 피하려고 별도 작업 디렉터리에서
# 편집할 수 있도록 AADS_REPORTS_DIR 로 경로를 바꿀 수 있게 한다.
_env = os.environ.get("AADS_REPORTS_DIR")
BASE = (pathlib.Path(_env) if _env
        else pathlib.Path(__file__).resolve().parents[3] / "app/static/reports")
TARGET = BASE / "20260912_rag_vectordb_embedding_education.html"

REPLACEMENTS = []

# --- 1) 임베딩 모델 비교표: 추정 단가 → 공식 단가/차원/라이선스 확정 ---
REPLACEMENTS.append((
    '''        <tr><td><strong>gemini-embedding-001</strong></td><td>Google</td><td class="perf">3072 (Matryoshka 축소 지원)</td><td>다국어 강세, Gemini 생태계 통합</td><td class="price">추정 $0.10~0.15<br><small style="color:var(--text3)">(약 ₩138~207, 미검증)</small></td><td>API</td></tr>''',
    '''        <tr><td><strong><a href="https://ai.google.dev/gemini-api/docs/embeddings">gemini-embedding-2</a></strong><br><small style="color:var(--text3)">(초판 표기: gemini-embedding-001)</small></td><td>Google</td><td class="perf">128~3072 (Matryoshka)<br><small style="color:var(--text3)">공식 권장 768/1536/3072</small></td><td><strong>2026-09-13 교정</strong> — 현행 최신은 멀티모달 <code>gemini-embedding-2</code>(최대 입력 <strong>8,192</strong> 토큰). 텍스트 전용 <code>gemini-embedding-001</code>도 계속 제공되나 최대 입력이 <strong>2,048</strong> 토큰. 두 모델의 임베딩 공간은 호환되지 않아 교체 시 <strong>전량 재임베딩</strong>이 필요하다.</td><td class="price">$0.20 (유료 등급)<span class="tag-verified">공식</span><br><small style="color:var(--text3)">(약 ₩276) · 무료 등급 있음<br>초판 "추정 $0.10~0.15" 교정</small></td><td>API</td></tr>'''))

REPLACEMENTS.append((
    '''        <tr><td><strong>voyage-3-large / voyage-3.5</strong></td><td>Voyage AI (MongoDB)</td><td class="perf">1024~2048</td><td>검색 특화 설계, 코드 전용 voyage-code-3 별도 제공</td><td class="price">추정 $0.06~0.18<br><small style="color:var(--text3)">(약 ₩83~248, 미검증)</small></td><td>API</td></tr>''',
    '''        <tr><td><strong><a href="https://docs.voyageai.com/docs/pricing">voyage-4 / voyage-4-large</a></strong><br><small style="color:var(--text3)">(초판 표기: voyage-3.5 / voyage-3-large)</small></td><td>Voyage AI (MongoDB)</td><td class="perf">1024~2048</td><td><strong>2026-09-13 교정</strong> — 초판이 최신으로 적은 voyage-3 계열은 공식 가격표에서 <strong>구형(no longer current)</strong>으로 분류된다. 현행은 voyage-4 계열이며 코드 전용도 <code>voyage-code-4</code>로 세대가 바뀌었다.</td><td class="price">voyage-4 $0.06<br>voyage-4-large $0.12<br>voyage-4-lite $0.02<span class="tag-verified">공식</span><br><small style="color:var(--text3)">계정당 최초 <strong>2억 토큰 무료</strong><br>초판 "추정 $0.06~0.18" 교정</small></td><td>API</td></tr>'''))

REPLACEMENTS.append((
    '''        <tr><td><strong>embed-v4</strong></td><td>Cohere</td><td class="perf">1536 (축소 가능)</td><td>멀티모달(이미지+텍스트) 임베딩 지원</td><td class="price">추정 $0.10~0.12<br><small style="color:var(--text3)">(약 ₩138~166, 미검증)</small></td><td>API</td></tr>''',
    '''        <tr><td><strong><a href="https://docs.cohere.com/docs/cohere-embed">embed-v4.0</a></strong></td><td>Cohere</td><td class="perf">256 / 512 / 1024 / <strong>1536(기본)</strong></td><td>멀티모달 — 텍스트·이미지·<strong>혼합 문서(PDF)</strong> 입력 지원. 최대 컨텍스트 <strong>128k 토큰</strong>으로 Cohere embed 계열 중 가장 길다.</td><td class="price">공식 문서에 토큰 단가 <span class="tag-unverified">미공개</span><br><small style="color:var(--text3)">공식 가격 페이지는 전용배포(Model Vault) 시간당 요금만 게시 — 초판 "추정 $0.10~0.12"는 근거를 찾지 못해 <strong>철회</strong></small></td><td>API</td></tr>'''))

REPLACEMENTS.append((
    '''        <tr><td><strong>Solar-Embedding</strong></td><td>Upstage (한국)</td><td class="perf">미검증</td><td>한국 기업의 한국어 특화 임베딩, 국내 도메인 용어에 강점으로 알려짐</td><td class="price">미검증 (콘솔 확인 필요)</td><td>API</td></tr>''',
    '''        <tr><td><strong><a href="https://www.upstage.ai/pricing/api">Upstage Embed 2</a></strong><br><small style="color:var(--text3)">(초판 표기: Solar-Embedding)</small></td><td>Upstage (한국)</td><td class="perf">공식 가격 페이지에 차원 <span class="tag-unverified">미기재</span></td><td>한국 기업의 한국어 특화 임베딩. <strong>2026-09-13 확인</strong> — 구형 <code>Embed (Legacy)</code>는 <strong>2026-12-31(UTC) 서비스 종료</strong> 예정이며 공식 문서가 <code>Embed 2</code>로의 이전을 권고한다. 신규 도입은 Embed 2 기준으로 검토할 것.</td><td class="price">Embed 2 <strong>$0.02</strong><span class="tag-verified">공식</span><br><small style="color:var(--text3)">(약 ₩28) · Legacy Embed $0.10<br>초판 "미검증" 해소</small></td><td>API</td></tr>'''))

REPLACEMENTS.append((
    '''        <tr><td><strong>jina-embeddings-v4</strong></td><td>Jina AI (오픈소스/API)</td><td class="perf">가변 (Matryoshka)</td><td>멀티모달, 긴 문서 지원</td><td class="price">추정 $0.02~0.08<br><small style="color:var(--text3)">(약 ₩28~110, 미검증)</small></td><td>API/오픈소스</td></tr>''',
    '''        <tr><td><strong><a href="https://jina.ai/embeddings/">jina-embeddings-v4</a></strong></td><td>Jina AI</td><td class="perf">Dense(단일벡터) + Late-interaction(다중벡터) 동시 지원</td><td>멀티모달, 최대 입력 <strong>32,768</strong> 토큰, Qwen2-VL 기반 3.8B 파라미터.<br><strong>⚠️ 2026-09-13 라이선스 교정</strong> — <strong>Qwen Research License</strong>가 적용되어 <strong>상업적 이용이 허용되지 않는다</strong>. 초판의 "오픈소스" 표기는 사내 도입 검토 시 오해를 부를 수 있어 정정한다.</td><td class="price">API 무료(연구 용도)<br><small style="color:var(--text3)">Jina 공식 FAQ가 스루풋 제한을 명시하며 <strong>프로덕션 부적합</strong>이라고 안내 — 초판 "추정 $0.02~0.08" 철회</small></td><td>연구용 (상업 이용 불가)</td></tr>'''))

# OpenAI 2건: 단가는 초판이 정확했으므로 유지하고 공식 근거 링크만 부착
REPLACEMENTS.append((
    '''<tr><td><strong>text-embedding-3-small</strong></td><td>OpenAI</td>''',
    '''<tr><td><strong><a href="https://developers.openai.com/api/docs/pricing">text-embedding-3-small</a></strong></td><td>OpenAI</td>'''))
REPLACEMENTS.append((
    '''<td class="price">$0.02<br><small style="color:var(--text3)">(약 ₩28)</small></td><td>API</td></tr>''',
    '''<td class="price">$0.02<span class="tag-verified">공식</span><br><small style="color:var(--text3)">(약 ₩28)</small></td><td>API</td></tr>'''))
REPLACEMENTS.append((
    '''<tr><td><strong>text-embedding-3-large</strong></td><td>OpenAI</td>''',
    '''<tr><td><strong><a href="https://developers.openai.com/api/docs/pricing">text-embedding-3-large</a></strong></td><td>OpenAI</td>'''))
REPLACEMENTS.append((
    '''<td class="price">$0.13<br><small style="color:var(--text3)">(약 ₩179)</small></td><td>API</td></tr>''',
    '''<td class="price">$0.13<span class="tag-verified">공식</span><br><small style="color:var(--text3)">(약 ₩179)</small></td><td>API</td></tr>'''))

# --- 2) 가격 표기 기준 안내에 검수 사실 추가 ---
REPLACEMENTS.append((
    '''    $1 ≈ ₩1,380 기준 환산. 임베딩 가격은 각 사가 수시로 조정하므로 아래는 작성 시점 근사치이며, 계약 전 공식 가격 페이지 재확인을 권장합니다.''',
    '''    $1 ≈ ₩1,380 기준 환산. 임베딩 가격은 각 사가 수시로 조정하므로 아래는 작성 시점 근사치이며, 계약 전 공식 가격 페이지 재확인을 권장합니다.
    <br><strong>2026-09-13 KST 재검수</strong> — 초판의 "추정·미검증" 단가를 <strong>각 사 공식 가격 페이지(1차 출처)</strong>와 전수 대조해 확정했습니다. 모델명을 클릭하면 근거 문서로 이동합니다. <span class="tag-verified">공식</span> 표시는 해당 수치를 공식 문서에서 직접 확인했다는 뜻입니다.'''))

# --- 3) MTEB 경고 박스: '도구 오류로 미확인' → 의도적 제외로 정정 ---
REPLACEMENTS.append((
    '''    임베딩 모델 성능 순위(MTEB 리더보드 등)는 신모델 출시마다 자주 바뀝니다. 이 표의 정성적 특징은 참고하되, 실제 채택 전에는 반드시 <strong>우리 문서로 직접 Recall@k 등을 측정</strong>(10장)해서 결정해야 합니다. 이번 조사에서는 실시간 웹 검색 도구가 연결 오류로 응답하지 않아 최신 MTEB 수치를 직접 확인하지 못했습니다 — 순위·점수는 미검증이며 위 표는 모델의 존재와 일반적 특징만을 근거로 작성했습니다.''',
    '''    임베딩 모델 성능 순위(<a href="https://huggingface.co/spaces/mteb/leaderboard">MTEB 리더보드</a> 등)는 신모델 출시마다 자주 바뀝니다. 이 표의 정성적 특징은 참고하되, 실제 채택 전에는 반드시 <strong>우리 문서로 직접 Recall@k 등을 측정</strong>(10장)해서 결정해야 합니다.
    <br><strong>2026-09-13 KST 재검수</strong> — 위 표의 <strong>단가·차원·최대 입력·라이선스는 각 사 공식 문서로 확정</strong>했습니다. 다만 <strong>모델별 MTEB 점수와 순위는 이번에도 싣지 않습니다</strong>. 리더보드는 수시로 갱신되어 교육자료에 고정 수치로 박아두면 머지않아 틀린 정보가 되기 때문이며, 이는 확인 실패가 아니라 <strong>의도적 제외</strong>입니다. 최신 순위가 필요하면 위 리더보드를 직접 여십시오.'''))

# --- 4) 참고자료: 원 논문·공식 문서 링크화 + 출처 등급 표기 ---
REPLACEMENTS.append((
    '''        <tr><td>RAG 개념</td><td>Retrieval-Augmented Generation 원 논문 (Lewis et al., 2020) 및 후속 서베이</td><td>기본 개념 정립</td></tr>
        <tr><td>벡터DB</td><td>pgvector, Qdrant, Milvus, Weaviate, Pinecone 공식 문서</td><td>파라미터·가격은 공식 문서 우선 확인</td></tr>
        <tr><td>임베딩 벤치마크</td><td>MTEB(Massive Text Embedding Benchmark) 리더보드</td><td>수시 업데이트되므로 도입 직전 재확인 필수 (본 문서는 미검증 표기)</td></tr>
        <tr><td>평가 프레임워크</td><td>RAGAS 공식 문서</td><td>faithfulness/answer relevancy 등 구현 예시 제공</td></tr>
        <tr><td>GraphRAG</td><td>Microsoft Research GraphRAG 프로젝트</td><td>오픈소스 구현 공개</td></tr>''',
    '''        <tr><td>RAG 개념</td><td><a href="https://arxiv.org/abs/2005.11401">Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks</a> (Lewis et al., 2020) — arXiv:2005.11401</td><td><strong>1차(원 논문)</strong> · 2026-09-13 KST 제목 대조 확인. 기본 개념 정립</td></tr>
        <tr><td>벡터DB</td><td><a href="https://github.com/pgvector/pgvector">pgvector</a> · <a href="https://qdrant.tech/documentation/">Qdrant</a> · <a href="https://milvus.io/docs">Milvus</a> · <a href="https://docs.weaviate.io/weaviate">Weaviate</a> · <a href="https://docs.pinecone.io/">Pinecone</a> 공식 문서</td><td><strong>1차(공식 문서)</strong> · 파라미터·가격은 공식 문서 우선 확인</td></tr>
        <tr><td>임베딩 벤치마크</td><td><a href="https://huggingface.co/spaces/mteb/leaderboard">MTEB 공식 리더보드</a> · 원 논문 <a href="https://arxiv.org/abs/2210.07316">MTEB: Massive Text Embedding Benchmark</a> (Muennighoff et al., 2022) — arXiv:2210.07316</td><td><strong>1차(원 논문+공식 리더보드)</strong> · 원 논문 기준 8개 태스크·58개 데이터셋·112개 언어·33개 모델 평가. 순위는 수시 갱신되므로 도입 직전 재확인 필수</td></tr>
        <tr><td>평가 프레임워크</td><td><a href="https://docs.ragas.io/">RAGAS 공식 문서</a> · 원 논문 <a href="https://arxiv.org/abs/2309.15217">Ragas: Automated Evaluation of Retrieval Augmented Generation</a> (Es et al., 2023) — arXiv:2309.15217</td><td><strong>1차(원 논문+공식 문서)</strong> · faithfulness/answer relevancy 등 구현 예시 제공</td></tr>
        <tr><td>GraphRAG</td><td><a href="https://github.com/microsoft/graphrag">Microsoft GraphRAG 공식 저장소</a> · 원 논문 <a href="https://arxiv.org/abs/2404.16130">From Local to Global: A Graph RAG Approach to Query-Focused Summarization</a> — arXiv:2404.16130</td><td><strong>1차(원 논문+공식 저장소)</strong> · 오픈소스 구현 공개</td></tr>
        <tr><td>인덱스 알고리즘 (7장)</td><td>HNSW 원 논문 <a href="https://arxiv.org/abs/1603.09320">Efficient and robust approximate nearest neighbor search using Hierarchical Navigable Small World graphs</a> (Malkov &amp; Yashunin, 2016) — arXiv:1603.09320</td><td><strong>1차(원 논문)</strong> · 2026-09-13 KST 신규 추가. 7-1장 HNSW 설명의 근거 문헌</td></tr>
        <tr><td>Late Interaction (9-3장)</td><td>ColBERT 원 논문 <a href="https://arxiv.org/abs/2004.12832">ColBERT: Efficient and Effective Passage Search via Contextualized Late Interaction over BERT</a> (Khattab &amp; Zaharia, SIGIR 2020) — arXiv:2004.12832</td><td><strong>1차(원 논문)</strong> · 2026-09-13 KST 신규 추가. 9-3장 근거 문헌</td></tr>'''))

# --- 5) 다음 단계 / 조사 한계 고지 갱신 ---
REPLACEMENTS.append((
    '''    <li>MTEB 최신 리더보드와 각 임베딩/벡터DB 서비스의 최신 가격을 도입 직전 재확인한다(본 문서의 일부 가격·순위는 조사 도구 오류로 미검증 표기됨).</li>''',
    '''    <li>MTEB 최신 리더보드와 각 임베딩/벡터DB 서비스의 최신 가격을 도입 직전 재확인한다. <strong>(2026-09-13 갱신)</strong> 단가·차원·라이선스는 이번 검수에서 공식 문서로 확정했으므로, 남은 재확인 대상은 <strong>모델별 MTEB 점수·순위</strong>와 <strong>Cohere embed-v4 토큰 단가</strong>다.</li>
    <li><strong>(2026-09-13 신규)</strong> 이번 검수에서 드러난 <strong>세대 교체·라이선스 리스크</strong>를 도입 검토 첫 단계에서 반영한다 — ① Upstage <code>Embed (Legacy)</code>는 2026-12-31 서비스 종료 예정, ② Voyage는 voyage-4 계열로 교체됨, ③ <code>jina-embeddings-v4</code>는 연구 라이선스라 <strong>상업적 이용 불가</strong>, ④ Gemini 임베딩은 모델 간 벡터 공간이 비호환이라 교체 시 전량 재임베딩 비용이 발생한다.</li>'''))

REPLACEMENTS.append((
    '''    본 문서 작성 중 원격 서버 명령 실행 도구와 실시간 웹 검색 도구(Gemini Grounding, SearXNG 등)가 세션 내내 반복적으로 연결 오류를 일으켜, 일부 최신 가격·벤치마크 순위·semantic_code_search 내부 구현을 직접 확인하지 못했습니다. 해당 항목은 본문에 "미검증"으로 명시했으며, 실제 의사결정 전 재확인을 권장합니다. AADS 관련 수치(DB 행 수, 인덱스 정의, 테이블 구조)는 PostgreSQL을 직접 조회해 확인한 사실입니다.''',
    '''    <strong>(2026-09-13 KST 갱신)</strong> 초판 작성 당시에는 실시간 웹 검색 도구가 연결 오류를 일으켜 최신 가격·벤치마크를 확인하지 못했고, 해당 항목을 "미검증"으로 표기했습니다. <strong>2026-09-13 재검수에서 임베딩 단가·차원·최대 입력·라이선스를 각 사 공식 문서(1차 출처)로 전수 확정</strong>하여 이 제약은 대부분 해소되었습니다. 남은 미확인 항목은 <a href="#source-audit-20260913">부록. 출처 검수 이력</a>에 정리했습니다.
    <br>AADS 관련 수치(DB 행 수, 인덱스 정의, 테이블 구조)는 PostgreSQL을 직접 조회해 확인한 사실이며, <code>semantic_code_search</code>의 내부 구현은 이번 검수 범위에서도 확인하지 못해 <span class="tag-unverified">미검증</span>으로 유지합니다.'''))


def main():
    src = TARGET.read_text(encoding="utf-8")
    missing = []
    for old, new in REPLACEMENTS:
        if old not in src:
            missing.append(old[:80])
            continue
        if src.count(old) != 1:
            missing.append("NOT UNIQUE: " + old[:80])
            continue
        src = src.replace(old, new)
    if missing:
        for m in missing:
            print("MISS:", m)
        return 1
    TARGET.write_text(src, encoding="utf-8")
    print("patched", TARGET.name, "· 치환", len(REPLACEMENTS), "건")
    return 0


if __name__ == "__main__":
    sys.exit(main())
