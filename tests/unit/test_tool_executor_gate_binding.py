"""도구 실행 경로에서 세션·테넌트가 게이트보다 먼저 묶이는지 검사한다.

2026-09-15. 승인 게이트는 차단은 잘 했지만 **승인 요청을 한 건도 남기지
못했다.** `agent_permission_requests` 는 테이블 생성 이래 0 행이었고, 채팅
화면의 인라인 승인 패널(`chat/page.tsx`)은 완성돼 있는데도 한 번도 뜨지
않았다.

원인은 순서였다. `ToolExecutor.execute` 가 세션·테넌트를 **두 게이트를
지난 뒤에** 해석했다. 그래서 게이트가 `request_approval` 을 부를 때는
`session_id`·`tenant_id` 가 늘 비어 있었고, `tenant_id` 가 NOT NULL 이라
요청이 통째로 버려졌다. 로그에는 `live_trading_gate_no_tenant session=`
과 `request=-` 만 남았다.

막히기만 하고 승인받을 길이 없으면 담당은 우회할 길을 찾는다 — 그게
규칙이 무시되기 시작하는 방식이다. 그래서 순서를 코드로 고정한다.

이 파일이 `test_live_trading_guard.py` 와 따로 있는 이유: 그 파일명이
실매매 가드의 보호 경로 패턴에 걸려 도구로 쓸 수 없다.
"""
import inspect


def test_session_and_tenant_are_resolved_before_the_gates():
    """해석이 게이트보다 뒤로 내려가면 승인 요청이 다시 유실된다."""
    from app.services import tool_executor

    src = inspect.getsource(tool_executor.ToolExecutor.execute)

    tenant_at = src.find("resolve_bound_tenant_id(")
    session_at = src.find("_resolve_bound_chat_session_id(")
    lt_gate_at = src.find("_lt_check(")
    dir_gate_at = src.find("_dir_check(")

    assert tenant_at > 0, "테넌트 해석이 도구 실행 경로에서 사라졌다"
    assert session_at > 0, "세션 해석이 도구 실행 경로에서 사라졌다"
    assert lt_gate_at > 0, "실매매 게이트가 도구 실행 경로에서 사라졌다"
    assert dir_gate_at > 0, "방향 게이트가 도구 실행 경로에서 사라졌다"

    assert tenant_at < lt_gate_at, (
        "테넌트 해석이 실매매 게이트보다 뒤에 있다 — 승인 요청이 유실된다"
    )
    assert session_at < lt_gate_at, (
        "세션 해석이 실매매 게이트보다 뒤에 있다 — 요청이 대화에 안 붙는다"
    )
    assert tenant_at < dir_gate_at and session_at < dir_gate_at, (
        "방향 게이트도 같은 이유로 요청을 남기지 못한다"
    )


def test_resolution_happens_once():
    """해석을 위로 올렸으니 아래의 중복 호출은 남아 있으면 안 된다.

    같은 값을 두 번 구하면 DB 왕복이 도구 호출마다 한 번씩 더 붙는다.
    """
    from app.services import tool_executor

    src = inspect.getsource(tool_executor.ToolExecutor.execute)
    assert src.count("await resolve_bound_tenant_id(") == 1, (
        "테넌트 해석이 두 번 이상 호출되고 있다"
    )


def test_injected_keys_do_not_change_blocking_decision():
    """주입하는 키가 차단 판정에 쓰이면 보호 범위가 바뀐다.

    `session_id`·`tenant_id` 는 게이트가 경로를 판단할 때 보는 필드
    목록에 들어가면 안 된다. 들어가면 세션 UUID 가 우연히 패턴에 걸려
    엉뚱한 작업이 막히거나, 반대로 판정이 흔들린다.
    """
    from app.services import live_trading_guard

    src = inspect.getsource(live_trading_guard._text_of)
    for key in ("session_id", "tenant_id"):
        assert f'"{key}"' not in src, (
            f"{key} 가 경로 판정 입력에 포함돼 있다 — 보호 범위가 바뀐다"
        )
