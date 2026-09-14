"""문서 색인·검색 파이프라인 회귀 테스트.

2026-09-14. 대표가 "이거 왜 이렇게 돼 있지" 라고 물어도 문서가 근거로
잡히지 않았다. Auto-RAG 는 memory_facts 와 채팅 기록만 검색했고 저장소
문서 823건은 검색 대상이 아니었다.

이 테스트가 지키는 것은 셋이다.

1. **사본을 색인하지 않는다.** `/root/aads` 의 `.md` 13,219개 중 고유는
   823개였다. 릴리스 스냅숏·워크트리·go100 클론 6벌이 같은 문서를 33벌까지
   만든다. 제외 규칙이 풀리면 임베딩이 그만큼 낭비되고, 검색 결과가 같은
   문서의 사본으로 채워진다.
2. **Auto-RAG 가 문서를 본다.** 연결이 끊겨도 채팅은 멀쩡히 돌기 때문에
   증상이 "답이 좀 부실하다" 뿐이라 눈치채기 어렵다.
3. **출처 경로가 결과에 있다.** 근거 경로가 없으면 비전문가는 답을 검증할
   방법이 없다.
"""
import importlib.util
import inspect
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "index_docs.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("index_docs", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_clone_and_worktree_paths_are_excluded():
    m = _load_script()
    for path in (
        "/root/aads/.worktrees/x/docs/a.md",
        "/root/aads/aads-server-releases/autoheal/docs/a.md",
        "/root/aads/go100-token-opt/docs/a.md",
        "/root/aads/go100-direct-yBauuU/report/a.md",
        "/root/aads/aads-dashboard-unni/docs/a.md",
        "/root/aads/x/node_modules/pkg/readme.md",
        "/root/aads/aads-server-unified-p0/docs/a.md",
    ):
        assert m.excluded(path), f"제외돼야 하는데 색인 대상이다: {path}"


def test_canonical_paths_are_kept():
    m = _load_script()
    for path in (
        "/root/aads/aads-server/docs/chat/CHAT-BACKEND-SPEC.md",
        "/root/aads/aads-docs/reports/x.md",
        "/root/aads/go100/docs/reports/y.md",
    ):
        assert not m.excluded(path), f"정본인데 제외됐다: {path}"


def test_chunker_keeps_heading_context():
    m = _load_script()
    text = "# 제목\n\n" + ("본문 " * 400) + "\n\n## 두번째 절\n\n" + ("다른 본문 " * 400)
    chunks = m.chunk(text)
    assert len(chunks) >= 2
    headings = {h for h, _ in chunks}
    assert any("두번째" in h for h in headings), "절 제목이 청크에 안 붙었다"
    assert all(len(body) > 0 for _, body in chunks)


def test_chunker_caps_runaway_documents():
    """HANDOVER.md 는 1.67MB 다. 상한이 없으면 한 문서가 검색을 독점한다."""
    m = _load_script()
    chunks = m.chunk("본문 " * 400_000)
    assert len(chunks) <= m.MAX_CHUNKS_PER_DOC


def test_embed_helper_never_fabricates():
    """임베딩 실패는 실패로 둔다 — 더미를 만들면 안 된다."""
    m = _load_script()
    src = inspect.getsource(m.ollama_embed)
    assert "return None" in src
    assert "dummy" not in src.lower() and "hash" not in src.lower()


def test_auto_rag_searches_documents():
    from app.services import auto_rag

    src = inspect.getsource(auto_rag._search_relevant)
    assert "_search_documents" in src, "Auto-RAG 가 문서를 검색하지 않는다"


def test_doc_results_carry_source_path():
    from app.services import auto_rag

    src = inspect.getsource(auto_rag._search_documents)
    # 포맷터가 읽는 키를 그대로 채워야 컨텍스트에 들어간다.
    for key in ('"text"', '"source"', '"timestamp"', '"path"'):
        assert key in src, f"결과에 {key} 가 없다 — 포맷터가 읽지 못한다"


@pytest.mark.asyncio
async def test_doc_search_survives_db_failure(monkeypatch):
    """문서 검색이 죽어도 채팅은 멈추면 안 된다."""
    from app.services import doc_index

    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr("app.core.db_pool.get_pool", _boom)
    assert await doc_index.search_docs([0.0] * 768) == []


def test_index_and_query_prefixes_stay_paired():
    """색인 접두어와 질문 접두어는 짝이 맞아야 한다.

    nomic-embed-text 는 작업 접두어를 요구한다. 한쪽만 바꾸면 검색이
    **조용히** 나빠진다 — 오류도 없고 결과도 나온다. 순위만 틀어진다.

    2026-09-14 실측. 질문 "채팅 응답이 왜 느려졌지" 에 대해
    정답 문단과 무관한 계약서 문단의 유사도 차이가
    접두어 없이 0.015, 접두어를 붙이면 0.061 이었다. 4배다.
    """
    from app.services import doc_index

    script = _load_script()
    assert script.DOC_PREFIX == doc_index.DOC_PREFIX, (
        "색인 스크립트와 서비스의 문서 접두어가 다르다 — 백필 경로별로 "
        "다른 벡터가 저장된다"
    )
    assert doc_index.QUERY_PREFIX != doc_index.DOC_PREFIX
    assert doc_index.QUERY_PREFIX.startswith("search_query")
    assert doc_index.DOC_PREFIX.startswith("search_document")


def test_query_prefix_applied_in_one_place():
    """호출자가 각자 붙이면 한 곳이 빠졌을 때 그 경로만 나빠진다.

    2026-09-14 `chat_messages` 도 접두어 세대(embedding_ver=2)로 넘어오면서
    접두어 붙은 질문 벡터를 쓰는 곳이 둘이 됐다(문서·채팅). 그래서 만드는
    자리를 `build_auto_rag_context` 한 곳으로 올리고 아래로 넘긴다 —
    각자 만들면 CPU Ollama 에 같은 문장을 두 번 태운다(요청당 약 20초).
    """
    from app.services import doc_index

    assert inspect.iscoroutinefunction(doc_index.embed_query)
    src = inspect.getsource(doc_index.embed_query)
    assert "QUERY_PREFIX" in src

    from app.services import auto_rag

    top = inspect.getsource(auto_rag.build_auto_rag_context)
    assert "embed_query" in top, "접두어 붙은 질문 벡터를 만드는 곳이 없다"
    assert top.count("embed_query(") == 1, "질문 벡터를 두 번 만들고 있다"

    for fn in (auto_rag._search_documents, auto_rag._search_chat_messages):
        assert "embed_query" not in inspect.getsource(fn), (
            f"{fn.__name__} 이 자기 벡터를 또 만든다 — 넘겨받아야 한다"
        )


def test_chat_message_search_only_reads_current_generation():
    """세대가 섞인 벡터를 한 검색에서 비교하면 조용히 나빠진다.

    2026-09-14 실측. `chat_messages` 의 벡터 33,140건 중 진짜는 65건이었고
    나머지는 임베딩 경로가 끊긴 채 저장된 해시 더미였다. 더미는 값이
    난수라 **어떤 질문과도 적당히 비슷하게** 나온다 — 그래서 섞어 놓으면
    상위를 더미가 차지한다. 오류도 없고 결과도 나온다. 순위만 무의미하다.
    """
    from app.services import auto_rag
    from app.services import chat_embedding_service as ces

    assert ces.CHAT_EMBED_VER == 2
    assert ces.CHAT_DOC_PREFIX.startswith("search_document")
    assert ces.CHAT_QUERY_PREFIX.startswith("search_query")

    src = inspect.getsource(auto_rag._search_chat_messages)
    assert src.count("embedding_ver = 2") == 3, (
        "크로스 세션 검색 경로 3개(오케스트레이터·프로젝트·워크스페이스) "
        "전부에 세대 조건이 있어야 한다 — 하나만 빠져도 그 경로가 더미를 읽는다"
    )
    assert "embedding_ver = 2" in inspect.getsource(auto_rag._detect_reask)

    # 저장하는 쪽도 같은 세대를 적어야 짝이 맞는다.
    store = inspect.getsource(ces.embed_and_store_message)
    assert "CHAT_DOC_PREFIX" in store and "embedding_ver" in store
    assert "embed_texts_strict" in store, "더미를 저장하면 안 된다"


def test_prompt_layer_templates_render():
    """프롬프트 템플릿에 escape 안 된 중괄호가 들어가면 레이어가 통째로 빠진다.

    2026-09-14. 지침 예시로 `todo_write(update={"원인 지점 확인": "completed"})`
    를 LAYER4 템플릿 본문에 넣었는데, `str.format()` 이 그걸 치환 필드로 읽어
    `build_layer4()` 가 **항상** KeyError 를 던졌다.

    그 예외는 `build_messages_context` 전체를 무너뜨리고, 호출부의 fallback 이
    담당 역할 프롬프트·Auto-RAG·메모리를 통째로 버린 채 "You are a helpful AI
    assistant." 로 답하게 만든다. 답은 나오니까 겉으로는 멀쩡해 보인다.

    템플릿을 `.format()` 으로 렌더하는 한 이 함정은 계속 있다. 그래서 렌더
    자체를 테스트한다 — 필드 목록을 눈으로 세지 말고 실제로 돌려 본다.
    """
    import string

    from app.core.prompts import system_prompt_v2 as sp

    # 렌더 자체가 통과해야 한다. 이것 하나가 실제 사고를 잡는다.
    rendered = sp.build_layer4()
    assert rendered.strip()
    # 예시는 escape 를 풀고 사람이 읽는 모양으로 나와야 한다.
    assert 'todo_write(update={"원인 지점 확인": "completed"})' in rendered

    # 치환 필드는 진화 통계 5개뿐이어야 한다. 늘어나면 호출부도 같이 고쳐야 한다.
    # `string.Formatter` 로 읽는다 — 정규식으로 세면 escape 된 `{{...}}` 안쪽을
    # 필드로 잘못 읽는다(이 테스트를 처음 쓸 때 그렇게 틀렸다).
    fields = {
        f for _, f, _, _ in string.Formatter().parse(sp.LAYER4_SELF_AWARENESS_TEMPLATE)
        if f
    }
    assert fields == {
        "fact_count", "obs_count", "avg_quality", "quality_count", "error_pattern_count",
    }, f"LAYER4 템플릿에 예상 밖 치환 필드: {sorted(fields)}"

    # 통계를 넘기는 경로도 렌더돼야 한다.
    assert sp.build_layer4({
        "fact_count": 1, "obs_count": 2, "avg_quality": 3,
        "quality_count": 4, "error_pattern_count": 5,
    })

    # Layer 1 도 같은 함정을 쓴다.
    assert sp.build_layer1("CEO", "", intent="analysis")


def test_context_builder_passes_session_role_to_compiler():
    """담당 역할 프롬프트는 role_key 를 넘겨야 붙는다.

    `PromptCompiler` 는 `role_scope` 가 지정된 자산을 넘겨받은 role 로 거른다.
    빈 문자열을 넘기면 그런 자산은 **하나도** 선택되지 않는다.

    2026-09-14 실측. `build_messages_context` 에 `role=""` 이 박혀 있어서,
    #310 주도 세션을 이 경로로 조립하면 시스템 프롬프트에 `StrategyCardLead`
    도 `ask_session` 도 없었다 — 담당이 자기가 누구인지, 누구를 부를 수 있는지
    모르는 채로 이어쓰기를 한다. 오류는 없다. 그냥 역할이 빠진다.
    """
    import inspect

    from app.services import context_builder

    src = inspect.getsource(context_builder.build_messages_context)
    # 주석은 뺀다 — 왜 이렇게 고쳤는지 설명하느라 옛 코드를 인용하기 때문이다.
    code = "\n".join(
        line for line in src.splitlines() if not line.lstrip().startswith("#")
    )
    assert 'role=""' not in code, "role 을 빈 문자열로 넘기면 역할 자산이 전부 걸러진다"
    assert "role_key" in code, "세션의 role_key 를 찾아 넘겨야 한다"
    assert "role=_role_key" in code

    # 워크스페이스도 같은 함정이다. 자산의 `workspace_scope` 는 `{GO100}` 인데
    # 세션 이름은 `[GO100] 백억이` 다. 컴파일러는 정규화하지 않으므로 표시명을
    # 넘기면 그 프로젝트 자산이 하나도 안 걸린다 — 오류 없이 0건이다.
    #
    # 2026-09-14 실측:
    #   ws='[GO100] 백억이' → 12,656자, 팀 명단 없음
    #   ws='GO100'         → 15,459자, 팀 명단 있음
    assert "workspace_name=ws_key" in code, (
        "표시명 대신 정규화된 프로젝트 키를 넘겨야 한다"
    )


def test_prompt_compiler_warns_when_role_selects_nothing():
    """범위가 어긋나 자산이 0건이면 조용히 끝나면 안 된다.

    2026-09-14 같은 함정에 두 번 빠졌다 — 한 번은 role 을 빈 문자열로, 한 번은
    workspace 를 표시명으로 넘겨서. 둘 다 오류 없이 "자산 0건" 으로 끝났고,
    프롬프트는 3만 자가 넘으니 길이만 봐서는 붙은 줄 안다.
    """
    import inspect

    from app.services.prompt_compiler import PromptCompiler

    src = inspect.getsource(PromptCompiler.compile)
    assert "prompt_assets_none_selected" in src
    assert "if role_key and not rows:" in src


def test_orchestrator_projects_single_source():
    """통합지시 검색 범위는 한 곳에서만 정한다.

    2026-09-07 에 만들어진 목록이 네 곳에 복사됐고 이미 갈라져 있었다 —
    auto_rag 7개, memory_recall 7개, autonomous_executor 6개(CEO 없음),
    subagent_service 5개(NAS·CEO 없음). 사본을 만들면 한쪽이 반드시 낡는다.
    """
    import inspect

    from app.core import memory_recall, project_config
    from app.services import auto_rag

    assert project_config.ORCHESTRATOR_PROJECTS
    assert auto_rag._CEO_ORCHESTRATOR_PROJECTS is project_config.ORCHESTRATOR_PROJECTS
    assert memory_recall._CEO_ORCHESTRATOR_PROJECTS is project_config.ORCHESTRATOR_PROJECTS

    for mod in (auto_rag, memory_recall):
        src = inspect.getsource(mod)
        assert '["AADS", "KIS", "GO100"' not in src, (
            f"{mod.__name__} 에 목록 사본이 다시 생겼다"
        )


def test_orchestrator_projects_env_override(monkeypatch):
    """환경변수로 바꿀 수 있고, 오타 하나로 통합 검색이 죽지 않는다."""
    from app.core import project_config

    monkeypatch.setenv("CEO_ORCHESTRATOR_PROJECTS", "AADS, GO100 ,FOOD")
    assert project_config._parse_orchestrator_projects() == ["AADS", "GO100", "FOOD"]

    # 모르는 이름은 버리고 나머지를 쓴다.
    monkeypatch.setenv("CEO_ORCHESTRATOR_PROJECTS", "AADS,없는프로젝트,CEO")
    assert project_config._parse_orchestrator_projects() == ["AADS", "CEO"]

    # 전부 걸러지면 기본값. 빈 목록으로 검색이 조용히 죽으면 안 된다.
    monkeypatch.setenv("CEO_ORCHESTRATOR_PROJECTS", "오타1,오타2")
    assert (
        project_config._parse_orchestrator_projects()
        == project_config._DEFAULT_ORCHESTRATOR_PROJECTS
    )

    # 미설정이면 기본값.
    monkeypatch.delenv("CEO_ORCHESTRATOR_PROJECTS", raising=False)
    assert (
        project_config._parse_orchestrator_projects()
        == project_config._DEFAULT_ORCHESTRATOR_PROJECTS
    )


def test_healer_resolves_servers_from_registry():
    """감시기는 서버 지도를 코드에 두지 않는다.

    2026-09-14 실측. `unified_healer` 에 `{"211": ..., "114": ...}` 지도가
    박혀 있었는데 `monitored_services.server` 값은 `contabo14` 였다.
    매칭이 안 되니 SSH 를 **시도조차 하지 않고** 곧바로 fail 로 기록했다.

        contabo14  go100-relay    fail  연속 실패 110,370회
        contabo14  nginx          fail  연속 실패 106,709회

    전부 정상 가동 중이었다. 고친 뒤 전체 26건을 재검사한 결과
    **거짓 경보 8건이 해소되고 진짜 실패 4건이 드러났다** —
    go100-ws-krx, kis-v41-minute-collector 등 수집 계통이다.

    거짓 경보는 경보가 없는 것보다 나쁘다. 10만 건에 묻혀 진짜가 안 보인다.
    """
    import inspect

    from app.services import unified_healer

    src = inspect.getsource(unified_healer)
    assert "server_registry" in src, "레지스트리를 봐야 한다"
    assert "ssh_host_map" not in src, "코드에 서버 지도 사본이 다시 생겼다"

    resolve_src = inspect.getsource(unified_healer._resolve_server)
    assert "SELECT ip" in resolve_src

    # 레지스트리에 없는 서버는 조용히 fail 하지 말고 왜인지 남겨야 한다.
    exec_src = inspect.getsource(unified_healer._execute_command)
    assert "monitored_server_not_in_registry" in exec_src

    # registry.port 는 SSH 포트가 아니다 — cafe24_114 는 7916(웹)이고 SSH 는 22.
    assert "-p {port}" not in exec_src


def test_layer2_injects_live_server_facts():
    """서버 사실은 정적 프롬프트가 아니라 Layer 2 에 실측으로 들어간다.

    2026-09-14 대표님 지적 — "서버정보 등 최신 변경사항이 세션에 주입이
    안 되나? 왜 옛날 정보를 보고하지". 실측하니 Layer 2 에는 시각과 지시
    건수뿐이고 서버에 관한 것이 하나도 없었고, Layer 1 에는 "## 3개 서버"
    가 손으로 적혀 있었다. 등록부에는 4대가 있었다.
    """
    import inspect

    from app.core.prompts import system_prompt_v2 as sp
    from app.services import context_builder

    # Layer 1 은 프롬프트 캐시용이다. 변하는 사실을 두면 안 된다.
    rendered = sp.build_layer1("CEO", "", intent="analysis")
    for ip in ("5.104.86.116", "5.104.86.14", "114.207.244.86"):
        assert ip not in rendered, f"Layer 1 에 서버 IP({ip})가 다시 박혔다"

    src = inspect.getsource(context_builder._build_layer2_dynamic)
    assert "server_registry" in src, "서버 목록을 등록부에서 읽어야 한다"
    assert "monitored_services" in src, "죽은 서비스를 주입해야 한다"

    # 오래된 판정을 현재처럼 말하면 고치려던 문제를 되풀이한다.
    assert "interval '15 minutes'" in src, "최근 검사만 읽어야 한다"
    assert "consecutive_failures" in src and "1000" in src, (
        "연속 실패가 많은 행은 '방금 죽은 것' 과 구분해야 한다 — "
        "감시 주기 30초에 10만 회면 35일이다"
    )


def test_direction_guard_catches_what_trading_guard_misses():
    """방향 변경은 실매매 게이트가 못 잡는다.

    `live_trading_guard` 는 파일 쓰기·명령 실행만 본다. 마일스톤·목표 기준·
    담당 배정을 바꾸는 것은 그냥 지나가므로, 방향은 자유롭게 바뀌고 그
    방향대로 파일을 고치려는 순간에야 멈춘다 — 이미 담당 여럿이 그 방향으로
    몇 시간 일한 뒤다.
    """
    from app.services.direction_guard import classify

    for sql in (
        "UPDATE milestones SET status='completed' WHERE id=1",
        "INSERT INTO goal_task_links (goal_id) VALUES (1)",
        "UPDATE goals SET title='기준 변경' WHERE id=1",
        "UPDATE chat_sessions SET role_key='X' WHERE id=1",
        "INSERT INTO prompt_assets (slug) VALUES ('x')",
    ):
        blocked, why = classify("db_safe_write", {"sql": sql})
        assert blocked, f"방향 변경을 놓쳤다: {sql}"
        assert why

    # 확실할 때만 막는다. 정상 작업을 막으면 게이트를 우회할 길을 찾는다.
    for tool, sql in (
        ("db_safe_write", "SELECT * FROM milestones"),
        ("db_safe_write", "UPDATE card_trades SET qty=1"),  # 실매매 게이트 몫
        ("query_database", "UPDATE milestones SET x=1"),     # 조회 도구
    ):
        blocked, _ = classify(tool, {"sql": sql})
        assert not blocked, f"정상 작업을 막았다: {tool} {sql}"


def test_dispatch_does_not_give_up_silently():
    """응답이 끊긴 마일스톤이 조용히 방치되면 안 된다.

    2026-09-14 실측. 운영인프라담당에게 지시를 넣은 직후 배포로 슬롯이
    바뀌며 스트림이 끊겼고 세션에는 이것만 남았다:

        ⚠️ 응답 생성이 중단되어 여기까지 보존된 내용이 없습니다.

    보낸 기록만 남기는 설계였다면 그 마일스톤은 영원히 멈춘다.
    """
    import inspect

    from app.services import goal_dispatch, goal_report

    src = inspect.getsource(goal_dispatch.dispatch_pending_milestones)

    # 진행중·중단 표시를 답으로 세면 안 된다 — 그게 바로 끊긴 경우다.
    assert "⏳" in src and "응답 생성이" in src
    # 재시도 한도와 간격이 있어야 한다.
    assert "_MAX_DISPATCH" in src and "_RETRY_AFTER_MIN" in inspect.getsource(goal_dispatch)
    # 발송 실패도 횟수를 올려야 매 사이클 재시도 폭주를 막는다.
    assert "dispatch_count = dispatch_count + 1" in src
    # 한도를 넘긴 건은 기록에 남아 사람이 볼 수 있어야 한다.
    assert "dispatch_note" in src

    # 보고는 사건 기준이고 중복을 막아야 한다.
    rep = inspect.getsource(goal_report)
    assert "goal_report_log" in rep and "UNIQUE" in rep
    assert "milestone_stuck" in rep


def test_goal_scheduler_covers_every_project():
    """프로젝트를 박아 두면 나머지가 통째로 방치된다.

    2026-09-14 실측. 골 제어 사이클 세 곳이 `"AADS"` 로 고정돼 있어서
    GO100 의 #310 은 물론 NAS·NTV2·SF 의 진행 중 목표도 스케줄러가 아예
    건드리지 않고 있었다.
    """
    import re
    from pathlib import Path

    src = Path("/app/app/main.py").read_text(encoding="utf-8")
    i = src.find("_run_goal_control_cycle")
    assert i > 0
    # 고정 길이로 자르면 사이클에 단계를 더할 때마다 검사 범위 밖으로
    # 밀려난다. 로그 한 줄까지를 사이클로 본다.
    end = src.find("goal_control_cycle_done", i)
    assert end > i, "사이클 끝을 찾지 못했다"
    cycle = src[i:end + 400]
    assert not re.search(r'\(\s*"AADS"', cycle), "골 사이클에 프로젝트가 다시 박혔다"
    assert "dispatch_pending_milestones" in cycle, "담당에게 말을 걸어야 한다"
    assert "report_goal_events" in cycle, "대표님께 보고해야 한다"


def test_milestone_completion_has_a_judge():
    """완료를 누가 판정하는지 없으면 첫 마일스톤에서 영원히 멈춘다.

    `check_milestone_completion` 은 `goal_task_links` 의 상태로 판정한다.
    담당 세션으로 굴러가는 마일스톤은 묶인 작업이 0건이라 `no_linked_tasks`
    가 돌아오고 `in_progress` 에 영원히 남는다. 담당은 답을 했으니 재알림도
    안 간다 — 조용한 영구 정지다.
    """
    import inspect

    from app.services import milestone_review

    report = inspect.getsource(milestone_review.report_done)
    # 신고는 완료가 아니다. 확인 대기다.
    assert "'review'" in report
    # 근거 없는 완료는 받지 않는다.
    assert "evidence_required" in report and "numbers_required" in report

    confirm = inspect.getsource(milestone_review.confirm)
    # 반려하면 발송 기록을 지워야 다시 지시가 나간다.
    assert "dispatch_count = 0" in confirm
    # 음성 결과는 반려와 다르다.
    assert "failed" in confirm

    ask = inspect.getsource(milestone_review.ask_pending_reviews)
    # 주도가 확인을 안 하면 또 멈춘다 — 재알림과 승격이 있어야 한다.
    assert "review_ask_count" in ask
    assert "_telegram" in ask


def test_ab_variants_open_together():
    """A/B 는 같은 순서의 변형을 함께 열어야 비교가 된다.

    2026-09-14 이전에는 `ORDER BY sequence_order LIMIT 1` 이라 하나만
    열렸다. 하나만 끝내고 다음으로 넘어가면 비교할 대상이 없다.
    """
    import inspect

    from app.services.goal_manager import GoalStateMachine

    src = inspect.getsource(GoalStateMachine.advance_goal)
    assert "pending_rows" in src, "변형을 전부 가져와야 한다"
    assert "id = ANY($1::uuid[])" in src, "변형을 함께 착수해야 한다"
    # review 도 열린 것으로 봐야 한다 — 반려될 수 있다.
    assert "'in_progress', 'review'" in src


def test_orchestration_limits_defer_rather_than_burn_retries():
    """비용·부하·멈춤은 **미루기**지 포기가 아니다.

    미룬 것을 재시도로 세면 한도가 헛되이 닳는다. 2026-09-14 contabo14
    부하 29 의 주범이 백테스트 4개 동시 실행이었고, A/B 는 그걸 설계로
    한다 — 오케스트레이션이 수집기를 죽일 수 있다.
    """
    import inspect

    from app.services import goal_dispatch, orchestration_limits

    src = inspect.getsource(goal_dispatch.dispatch_pending_milestones)
    for gate in ("owner_paused", "cost_gate", "load_gate"):
        assert gate in src, f"{gate} 를 발송 전에 봐야 한다"

    # 게이트에 걸린 경로는 dispatch_count 를 올리지 않고 skip 으로 빠져야 한다.
    gated = src[src.index("owner_paused"):src.index("# 보낸 뒤에")]
    assert "dispatch_count + 1" not in gated, "미룬 것이 재시도 한도를 깎는다"

    # 비용은 오케스트레이션이 쓴 것만 센다 — 대표님 대화까지 세면 안 된다.
    cost = inspect.getsource(orchestration_limits.refresh_goal_cost)
    assert "MIN(dispatched_at)" in cost

    # 부하를 못 읽으면 통과시킨다. 못 읽는다고 진행을 막으면 안 된다.
    load = inspect.getsource(orchestration_limits.load_gate)
    assert "return True" in load

    # 기한은 알리되 멈추지 않는다.
    dl = inspect.getsource(orchestration_limits.check_deadlines)
    assert "status" not in dl.split("UPDATE")[0] or "UPDATE milestones" not in dl


def test_every_context_caller_passes_db_conn():
    """`db_conn` 을 안 넘기면 Layer 2 의 DB 조회가 통째로 건너뛰어진다.

    서버 목록, 죽은 서비스, 같이 일하는 담당 명단이 전부 빠진다.
    **오류는 안 난다. 그냥 없다.**

    2026-09-14 실측. 세 호출부 중 이어쓰기만 안 넘기고 있었다 — 오늘 이
    경로에서 세 번째로 나온 누락이다(예외로 통째 실패 → 역할 프롬프트
    없음 → 이것).
    """
    import re
    from pathlib import Path

    src = Path("/app/app/services/chat_service.py").read_text(encoding="utf-8")
    calls = [m.start() for m in re.finditer(r"await build_messages_context\(", src)]
    assert calls, "호출부를 찾지 못했다"
    for pos in calls:
        block = src[pos:pos + 600]
        end = block.find(")\n")
        assert "db_conn=" in block[:end if end > 0 else 600], (
            f"db_conn 을 안 넘기는 호출부가 있다 (offset {pos})"
        )


def test_turn_timing_measures_what_the_user_feels():
    """체감 시간을 재지 않으면 고칠 곳을 못 찾는다.

    2026-09-14 대표님 질문 "채팅창이 너처럼 즉시 응답이 왜 안되지?" 에
    답하려고 로그를 뒤졌는데 **계측 자체가 없었다.** 손으로 재 보니
    컨텍스트 조립 2.2초(대부분 CPU Ollama 임베딩)였고 릴레이 구간은
    못 쟀다. 못 재는 구간은 고칠 수도 없다.
    """
    import inspect

    from app.services import turn_timing

    src = inspect.getsource(turn_timing)
    # 하트비트를 첫 토큰으로 세면 계측이 거짓말을 한다.
    chat = inspect.getsource(
        __import__("app.services.chat_service", fromlist=["x"]).send_message_stream
    )
    assert '_timer.mark("first_token")' in chat
    i = chat.index('_timer.mark("first_token")')
    assert "heartbeat" not in chat[i - 260:i], "하트비트를 첫 토큰으로 세고 있다"

    # 실패한 턴이 오래 걸린 경우가 가장 알고 싶은 것이다.
    assert "_timer.save()" in chat

    # 평균은 긴 꼬리에 끌려간다.
    summary = inspect.getsource(turn_timing.recent_summary)
    assert "percentile_disc" in summary and "avg(" not in summary.lower()

    # 계측이 응답 경로를 붙잡으면 본말전도다.
    assert "asyncio.create_task" in src


def test_stale_cleanup_does_not_kill_long_turns():
    """정리 작업이 살아 있는 턴을 죽이면 안 된다.

    2026-09-14. 대표님 세션(bf6f097c)의 턴이
    `force_interrupted_stale_placeholder_cleanup` 으로 죽었다.

    기본값이 90초였는데 이 서버 채팅은 **2~61분** 걸린다. 죽이지 않는
    방패가 `_get_live_streaming_session_ids()` 인데 **프로세스 메모리**라
    블루/그린 컷오버 뒤 새 슬롯에서는 비어 있다 — 방패가 사라진다.
    """
    import inspect

    from app.services import chat_service

    assert chat_service._STALE_PLACEHOLDER_TIMEOUT_SEC_DEFAULT >= 600, (
        "이 서버 채팅은 2~61분 걸린다. 90초는 정상적인 턴을 죽인다"
    )

    src = inspect.getsource(chat_service._live_session_ids_with_db)
    assert "heartbeat_at" in src, "DB 하트비트를 봐야 슬롯이 바뀌어도 유지된다"
    # 조회가 실패하면 메모리 목록만이라도 돌려준다. 빈 집합을 주면
    # 정리 작업이 전부 죽인다 — 실패 방향이 정반대다.
    assert "_get_live_streaming_session_ids()" in src

    # 90초 정리 경로가 DB 병용 함수를 써야 한다.
    cleanup = inspect.getsource(chat_service)
    assert "_live_session_ids_with_db(conn)" in cleanup


def test_failed_command_is_not_replayed_forever():
    """실패한 명령을 무한 재생하면 새 턴이 아예 시작되지 않는다.

    2026-09-14. 07:48 에 실패한 `resume` 명령이 하루 종일 같은 실패로
    재생됐다. 오류 사전에 재발 89회로 올라 있던 항목이다.

    되살리지는 않는다 — DB 트리거 `aads_chat_command_transition` 이 종결
    상태를 불변으로 두는 것은 **의도된 설계**다. 되살리면 같은 키가 두
    결과를 갖게 되고 멱등성이 무너진다. 대신 새 키를 요구한다.
    """
    import inspect

    from app.services import chat_commands

    src = inspect.getsource(chat_commands)
    assert "chat_command_failed_use_new_key" in src
    i = src.index("chat_command_failed_use_new_key")
    assert 'record.status == "failed"' in src[i - 900:i]
    # 되살리기를 다시 넣으면 트리거가 막고, 막히면 조용히 실패한다.
    assert "SET status = 'pending', error = NULL" not in src


def test_auto_rag_does_not_block_first_token():
    """근거를 다 모을 때까지 첫 글자도 못 내면 안 된다.

    2026-09-14 실측. 질문 임베딩 한 번이 CPU Ollama 에서 2,558ms 다.
    그동안 화면은 비어 있다. 그런데 **근거가 첫 문장에 필요한 경우는
    드물다** — 대개 답을 시작한 뒤 중간에 쓰인다.

    실측(상한 700ms):
        1회차  701 ms  상한에 걸려 다음 턴으로   129자
        2회차  296 ms  지난 근거 이어받음        1,936자
    """
    import inspect

    from app.services import context_builder as cb

    assert cb._AUTO_RAG_WAIT_MS <= 2000, "상한이 임베딩 시간(2.5초)보다 크면 의미가 없다"

    src = inspect.getsource(cb._build_auto_rag_layer_bounded)
    # 주석은 뺀다 — 왜 그렇게 고쳤는지 설명하느라 옛 코드를 인용한다.
    # (2026-09-14 이 검사를 처음 쓸 때 주석의 `wait_for` 를 잡았다.)
    code = "\n".join(
        line for line in src.splitlines() if not line.lstrip().startswith("#")
    )

    # 늦은 근거를 **버리지 않는다.** 버리면 그 질문에 대한 근거가 영영 안 붙는다.
    assert "_late_rag" in src and "add_done_callback" in src

    # 작업을 취소하면 안 된다 — 뒤에서 마저 끝내야 다음 턴에 쓴다.
    assert "wait_for" not in code, "wait_for 는 상한에서 작업을 취소한다"
    assert "asyncio.wait(" in code

    # 조용히 빼면 안 된다. 근거 없이 답한 것을 대표님이 모르시면 안 된다.
    assert "늦어" in src

    # CancelledError 는 Exception 이 아니다. 콜백에서 새어 나가면 루프
    # 예외 핸들러에 찍힌다 — 실측에서 그렇게 됐다.
    stash = src[src.index("def _stash"):]
    assert "except BaseException" in stash

    # 두 호출 경로 모두 상한을 거쳐야 한다.
    whole = inspect.getsource(cb)
    assert "_build_auto_rag_layer(_last_user_msg" not in whole
    assert "_build_auto_rag_layer(last_user_message, session_id, _project)" not in whole
