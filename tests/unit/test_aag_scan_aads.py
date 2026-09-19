"""AAG L1/L2 아키텍처 추출기 단위테스트 — DB·파일시스템 없이 도는 순수 함수만.

여기 고정한 입력은 대부분 AADS 본체에서 **실제로 오탐/미탐을 냈던 문자열**이다.
추출기의 위험한 실패 모드는 터지는 것이 아니라 조용히 0건을 보고하거나,
반대로 산문에서 없는 테이블을 발명하는 것이다. 둘 다 여기서 막는다.
"""

import importlib.util
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

_spec = importlib.util.spec_from_file_location(
    "aag_scan_aads", ROOT / "tools" / "aag" / "scan_aads.py"
)
sc = importlib.util.module_from_spec(_spec)
# dataclass 는 생성 시 sys.modules 에서 자기 모듈을 되찾는다. 등록하지 않으면
# import 단계에서 AttributeError 로 죽는다 — 테스트가 아니라 로딩이 깨진다.
sys.modules["aag_scan_aads"] = sc
_spec.loader.exec_module(sc)


# ── 경로 합성 ──────────────────────────────────────────────────────────


def test_join_path_composes_mount_and_router_prefix():
    assert sc.join_path("/api/v1", "/chat") == "/api/v1/chat"
    assert sc.join_path("/api/v1", "/chat/messages/") == "/api/v1/chat/messages"


def test_join_path_skips_empty_parts():
    assert sc.join_path("", "/chat") == "/chat"
    assert sc.join_path("/api/v1", "") == "/api/v1"
    assert sc.join_path("/api/v1", "/") == "/api/v1"


def test_join_path_adds_missing_leading_slash():
    assert sc.join_path("/api/v1", "chat") == "/api/v1/chat"


def test_normalize_route_erases_param_names():
    """`{message_id}` 와 프런트의 `${msgId}` 는 같은 라우트다."""
    assert sc.normalize_route("/chat/messages/{message_id}") == "/chat/messages/{}"


def test_normalize_route_strips_trailing_slash_and_dedupes():
    assert sc.normalize_route("/chat//messages/") == "/chat/messages"
    assert sc.normalize_route("/") == "/"
    assert sc.normalize_route("") == "/"


# ── 네임스페이스 (DOUBLE_MOUNT 의 정본 기준) ──────────────────────────


def test_namespace_is_api_root_plus_first_segment():
    assert sc.namespace_key("/api/v1/chat/messages/{}", ["/api/v1"]) == "/api/v1/chat"
    assert sc.namespace_key("/api/v1/chat", ["/api/v1"]) == "/api/v1/chat"


def test_namespace_strips_the_longest_matching_api_root():
    """self-prefix 라우터를 `/api` 하나로 뭉치면 가짜 충돌이 쏟아진다."""
    roots = ["/api/v1", "/api"]
    assert sc.namespace_key("/api/v1/review/run", roots) == "/api/v1/review"
    assert sc.namespace_key("/api/v1/user/api-keys", roots) == "/api/v1/user"


def test_namespace_without_api_root_uses_first_segment():
    assert sc.namespace_key("/healthz", []) == "/healthz"


# ── 라우트 매칭 ────────────────────────────────────────────────────────


def test_backend_param_matches_frontend_literal():
    assert sc.route_matches("/chat/messages/abc", "/chat/messages/{}")


def test_frontend_param_does_not_match_backend_literal():
    """이걸 허용하면 `/chat/messages/search` 가 아무 변수 호출에나 걸려 드리프트를 놓친다."""
    assert not sc.route_matches("/chat/messages/{}", "/chat/messages/search")


def test_route_matches_requires_same_segment_count():
    assert not sc.route_matches("/chat/messages", "/chat/messages/{}")


# ── 백업 파일 ──────────────────────────────────────────────────────────


def test_backup_patterns_catch_real_aads_names():
    pats = sc.DEFAULT_RULES["backup_patterns"]
    for name in (
        "app/api/ceo_chat.py.bak",
        "app/api/chat.py.bak.T073",
        "app/api/llm_keys.py.bak_aads188",
        "app/api/memory.py.bak_20260305",
        "scripts/pipeline-runner.sh.bak_litellm_fix",
    ):
        assert sc.is_backup_path(name, pats), name


def test_live_source_is_not_a_backup():
    pats = sc.DEFAULT_RULES["backup_patterns"]
    for name in ("app/api/chat.py", "app/routers/chat.py", "tools/aag/scan_aads.py"):
        assert not sc.is_backup_path(name, pats), name


# ── SQL 테이블 추출 ────────────────────────────────────────────────────


def test_extract_tables_from_plain_select():
    assert sc.extract_sql_tables("SELECT id FROM chat_messages WHERE id = $1") == {
        "chat_messages"
    }


def test_prose_is_not_sql():
    """실측 오탐: 'update failed' 가 `failed` 라는 테이블을 만들어 냈다."""
    for prose in (
        "update failed",
        "update or append",
        "research_archive update error: ",
        "Create or update a tenant/project-scoped canonical handover entry.",
        "Create a Design Studio request card from chat/tool use.",
        "interrupt receipt session update did not match",
    ):
        assert sc.extract_sql_tables(prose) == set(), prose


def test_cte_names_are_not_tables_even_after_a_comment():
    """`),\\n -- 주석\\n eff AS (` 의 eff 가 테이블로 새어 나왔다."""
    sql = """
        WITH cur AS (
            SELECT id FROM agent_permission_requests WHERE id = $1
        ),
        -- 주문·자금은 대화 범위로 열지 않는다.
        eff AS (
            SELECT id FROM cur
        )
        SELECT * FROM eff
    """
    assert sc.extract_sql_tables(sql) == {"agent_permission_requests"}


def test_recursive_cte_name_is_not_a_table():
    sql = "WITH RECURSIVE child AS (SELECT id FROM datasets) SELECT * FROM child"
    assert sc.extract_sql_tables(sql) == {"datasets"}


def test_materialized_cte_names_are_not_tables():
    """GO100 쿼리의 AS MATERIALIZED/NOT MATERIALIZED CTE가 테이블로 새지 않는다."""
    sql = """
        SELECT * FROM (
            WITH active_symbols AS MATERIALIZED (SELECT code FROM stock_universe),
                 target AS NOT MATERIALIZED (SELECT code FROM active_symbols)
            SELECT * FROM target
        ) scoped JOIN real_table ON true
    """
    assert sc.extract_sql_tables(sql) == {"stock_universe", "real_table"}


def test_sql_keywords_after_from_are_not_tables():
    assert sc.extract_sql_tables("SELECT TRIM(BOTH ' ' FROM code) FROM stocks") == {"stocks"}
    assert sc.extract_sql_tables("SELECT * FROM stocks WHERE code BETWEEN '1' AND '9'") == {"stocks"}


def test_string_literal_inside_sql_is_not_a_table():
    """`'Runner/review primary from settings order'` 가 `settings` 를 만들었다."""
    sql = (
        "INSERT INTO model_routing_preferences (notes) "
        "SELECT x FROM seed_values WHERE note = 'primary from settings order'"
    )
    assert sc.extract_sql_tables(sql) == {"model_routing_preferences", "seed_values"}


def test_for_update_of_alias_is_not_a_table():
    """`FOR UPDATE OF m` 의 of 가 테이블로 잡혔다."""
    sql = "SELECT m.* FROM chat_messages m WHERE m.id = $1 FOR UPDATE OF m"
    assert sc.extract_sql_tables(sql) == {"chat_messages"}


def test_extract_and_distinct_from_are_not_table_positions():
    """`EXTRACT(EPOCH FROM ts)` 와 `IS DISTINCT FROM x.y` 의 FROM."""
    sql = (
        "SELECT EXTRACT(EPOCH FROM (NOW() - te.updated_at))::int AS age "
        "FROM chat_turn_executions te "
        "WHERE te.id IS DISTINCT FROM te_latest.user_message_id"
    )
    assert sc.extract_sql_tables(sql) == {"chat_turn_executions"}


def test_set_returning_function_is_not_a_table():
    sql = "SELECT v FROM jsonb_array_elements(logs) AS v"
    assert sc.extract_sql_tables(sql) == set()


def test_ignore_list_removes_system_catalogs():
    sql = "SELECT 1 FROM pg_proc WHERE proname = $1"
    assert sc.extract_sql_tables(sql, ignore=["pg_proc"]) == set()


def test_ddl_extraction_covers_table_and_view():
    text = (
        "CREATE TABLE IF NOT EXISTS chat_messages (id uuid);\n"
        "CREATE MATERIALIZED VIEW deploy_recent_durations AS SELECT 1;\n"
    )
    assert sc.extract_ddl_tables(text) == {"chat_messages", "deploy_recent_durations"}


def test_fstring_placeholder_prevents_alias_becoming_a_table():
    """보간을 지워 `FROM  e` 가 되면 별칭 e 가 테이블로 잡힌다 — 자리표시자를 남긴다."""
    source = 'q = f"SELECT e.id FROM {table} e JOIN {other} d ON d.id = e.id"\n'
    tables, notes = sc.collect_sql_tables(source, [])
    assert tables == set()
    assert notes and "런타임 보간" in notes[0]["detail"]


def test_collect_sql_tables_reads_only_string_constants():
    """`app/api/yeoljeong_accounting.py:5` 의 독스트링 함정과 같은 이유다."""
    source = (
        '"""Docs mentioning select id from not_a_table."""\n'
        'SQL = "SELECT id FROM real_table WHERE id = $1"\n'
    )
    tables, _ = sc.collect_sql_tables(source, [])
    assert tables == {"real_table"}


# ── 라우터 모듈 파싱 (ast) ─────────────────────────────────────────────


def test_router_prefix_is_composed_into_route_paths():
    source = (
        "from fastapi import APIRouter\n"
        'router = APIRouter(prefix="/browser-tasks")\n'
        '@router.get("/{task_id}")\n'
        "def get_task(task_id): ...\n"
    )
    info = sc.parse_router_module("app/api/browser_tasks.py", source)
    assert info.defines_router
    assert [(r.method, r.path) for r in info.routes] == [("GET", "/browser-tasks/{task_id}")]


def test_docstring_route_example_is_not_a_route():
    source = (
        '"""예시:\n\n    @router.get("/fake")\n"""\n'
        "from fastapi import APIRouter\n"
        "router = APIRouter()\n"
    )
    info = sc.parse_router_module("app/api/x.py", source)
    assert info.routes == []


def test_non_constant_route_path_goes_to_unresolved():
    source = (
        "from fastapi import APIRouter\n"
        "router = APIRouter()\n"
        "@router.get(PATH)\n"
        "def f(): ...\n"
    )
    info = sc.parse_router_module("app/api/x.py", source)
    assert info.routes == []
    assert info.unresolved[0]["kind"] == "ROUTE_PATH"


def test_entrypoint_include_router_resolves_both_import_styles():
    source = (
        "from app.api import health, chat\n"
        "from app.routers.chat import router as chat_v2_router\n"
        'app.include_router(chat.router, prefix="/api/v1")\n'
        'app.include_router(chat_v2_router, prefix="/api/v1")\n'
        "app.include_router(health.router)\n"
    )
    includes, _aliases, err = sc.parse_entrypoint("app/main.py", source)
    assert err == ""
    assert [(i.target_module, i.prefix) for i in includes] == [
        ("app.api.chat", "/api/v1"),
        ("app.routers.chat", "/api/v1"),
        ("app.api.health", ""),
    ]


def test_include_router_inside_a_docstring_is_invisible_to_ast():
    """실측 함정: `app/api/yeoljeong_accounting.py:5` 는 독스트링 안의 예시다."""
    source = (
        '"""Not registered yet — wire up with:\n\n'
        '    app.include_router(yeoljeong_accounting.router, prefix="/api/v1")\n"""\n'
        "from fastapi import APIRouter\n"
        "router = APIRouter()\n"
    )
    includes, _aliases, err = sc.parse_entrypoint("app/main.py", source)
    assert err == ""
    assert includes == []


# ── 프런트엔드 URL 해석 ────────────────────────────────────────────────


def test_env_default_resolves_to_base_path():
    expr = 'process.env.NEXT_PUBLIC_API_URL || "https://aads.newtalk.kr/api/v1"'
    assert sc.resolve_base_expr(expr, ["NEXT_PUBLIC_API_URL"]) == "/api/v1"


def test_empty_env_default_resolves_to_empty_base():
    """`|| ""` 는 해석 실패가 아니라 '접두가 빠진 경로' 다 — 그게 잡을 드리프트다."""
    expr = 'process.env.NEXT_PUBLIC_API_URL || ""'
    assert sc.resolve_base_expr(expr, ["NEXT_PUBLIC_API_URL"]) == ""


def test_base_and_params_compose_into_a_backend_path():
    consts = {"BASE": "/api/v1"}
    status, path, _ = sc.resolve_call_url(
        "`${BASE}/chat/workspaces/${activeWs}/roles`", consts, ["NEXT_PUBLIC_API_URL"]
    )
    assert (status, path) == ("resolved", "/api/v1/chat/workspaces/{}/roles")


def test_variable_base_plus_variable_path_is_unresolved():
    """`${BASE_URL}${path}` 는 경로를 확정할 수 없다 — 억지로 세면 통계가 오염된다."""
    status, _path, why = sc.resolve_call_url(
        "`${BASE_URL}${path}`", {"BASE_URL": "/api/v1"}, ["NEXT_PUBLIC_API_URL"]
    )
    assert status == "unresolved"
    assert "세그먼트" in why


def test_unknown_base_variable_is_unresolved():
    status, _path, why = sc.resolve_call_url(
        "`${BASE_URL}/image/generate`", {}, ["NEXT_PUBLIC_API_URL"]
    )
    assert status == "unresolved"
    assert "base" in why


def test_query_only_interpolation_does_not_block_resolution():
    status, path, _ = sc.resolve_call_url(
        '`/lessons${qs ? "?" + qs : ""}`', {}, ["NEXT_PUBLIC_API_URL"], allow_relative=True
    )
    assert (status, path) == ("resolved", "/lessons")


def test_interpolation_that_can_produce_a_path_segment_is_not_query_only():
    assert not sc.interpolation_is_query_only('flag ? "/extra" : ""')
    assert sc.interpolation_is_query_only('qs ? "?" + qs : ""')
    assert sc.interpolation_is_query_only(
        'project && project !== "all" ? `?project=${p}` : ""'
    )
    assert not sc.interpolation_is_query_only("qs")


def test_string_concatenation_resolves_to_a_param_segment():
    status, path, _ = sc.resolve_call_url(
        '"/documents/" + encodeURIComponent(docId)', {}, ["NEXT_PUBLIC_API_URL"],
        allow_relative=True,
    )
    assert (status, path) == ("resolved", "/documents/{}")


def test_absolute_url_reduces_to_its_path():
    status, path, _ = sc.resolve_call_url(
        '"https://aads.newtalk.kr/api/v1/health"', {}, ["NEXT_PUBLIC_API_URL"]
    )
    assert (status, path) == ("resolved", "/api/v1/health")


def test_split_call_args_survives_commas_inside_template_literals():
    text = 'fetch(`${a},${b}/x`, { method: "POST" })'
    args, close = sc.split_call_args(text, text.index("("))
    assert args[0] == "`${a},${b}/x`"
    assert args[1] == '{ method: "POST" }'
    assert text[close] == ")"


def test_find_ts_calls_ignores_the_helper_declaration():
    text = (
        "async function request<T>(path: string) {\n"
        "  return fetch(`${BASE_URL}${path}`);\n"
        "}\n"
        'const a = request("/health");\n'
    )
    calls = sc.find_ts_calls(text, ["fetch", "request"])
    exprs = sorted(c["url_expr"] for c in calls)
    assert exprs == ['"/health"', "`${BASE_URL}${path}`"]  # 선언 1건은 빠졌다


def test_method_defaults_to_get_and_reads_literals():
    assert sc.method_of("") == ("GET", True)
    assert sc.method_of('{ method: "DELETE" }') == ("DELETE", True)
    assert sc.method_of("{ method: verb }") == ("GET", False)


def test_ts_named_imports_are_collected():
    text = 'import { BASE_URL, getToken } from "./api";\n'
    assert sc.parse_ts_imports(text) == {"BASE_URL": "./api", "getToken": "./api"}


def test_ts_module_resolution_handles_relative_and_alias():
    known = {"web/src/lib/api.ts", "web/src/app/chat/api.ts"}
    aliases = {"@/": "web/src/"}
    assert sc.resolve_ts_module("./api", "web/src/app/chat/page.tsx", known, aliases) == \
        "web/src/app/chat/api.ts"
    assert sc.resolve_ts_module("@/lib/api", "web/src/app/page.tsx", known, aliases) == \
        "web/src/lib/api.ts"
    assert sc.resolve_ts_module("./nope", "web/src/app/page.tsx", known, aliases) == ""


# ── 프런트 호출 판정 ───────────────────────────────────────────────────


ROUTES = {
    "GET": ["/api/v1/chat/messages", "/api/v1/chat/messages/{}", "/api/v1/chat/executions/{}/events"],
    "POST": ["/api/v1/chat/messages/send"],
}


def test_exact_match_is_ok():
    assert sc.classify_frontend_call("GET", "/api/v1/chat/messages", ROUTES)[0] == "OK"


def test_method_mismatch_is_path_drift():
    """실측: 대시보드가 POST /chat/messages 를 부르는데 백엔드는 GET 뿐이다."""
    verdict, why = sc.classify_frontend_call("POST", "/api/v1/chat/messages", ROUTES)
    assert verdict == "PATH_DRIFT"
    assert "GET" in why


def test_missing_api_prefix_is_path_drift_not_route_missing():
    """`NEXT_PUBLIC_API_URL || ""` 로 접두가 빠진 호출."""
    verdict, why = sc.classify_frontend_call("GET", "/chat/executions/{}/events", ROUTES)
    assert verdict == "PATH_DRIFT"
    assert "/api/v1" in why


def test_unknown_path_is_route_missing():
    verdict, _ = sc.classify_frontend_call("GET", "/api/v1/ops/qa-results", ROUTES)
    assert verdict == "ROUTE_MISSING"


# ── 규칙 ───────────────────────────────────────────────────────────────


def test_dup_module_finds_the_chat_pair():
    findings = sc.find_dup_modules(
        ["app/api/chat.py", "app/routers/chat.py", "app/api/health.py",
         "app/api/__init__.py", "app/routers/__init__.py"],
        ["app/api", "app/routers"],
    )
    assert len(findings) == 1
    assert findings[0]["modules"] == ["app/api/chat.py", "app/routers/chat.py"]


def test_dup_module_ignores_package_inits():
    findings = sc.find_dup_modules(
        ["app/api/__init__.py", "app/routers/__init__.py"], ["app/api", "app/routers"]
    )
    assert findings == []


def _route(module, method, path, entry="app/main.py"):
    return {"entrypoint": entry, "module": module, "method": method,
            "mount_prefix": "/api/v1", "full_path": path, "lineno": 1}


def test_double_mount_allows_distinct_routers_to_share_a_namespace():
    routes = [
        _route("app/api/chat.py", "POST", "/api/v1/chat"),
        _route("app/api/chat.py", "GET", "/api/v1/chat/intents"),
        _route("app/routers/chat.py", "GET", "/api/v1/chat/messages"),
    ]
    assert sc.find_double_mounts(routes, ["/api/v1"]) == []


def test_double_mount_ignores_route_conflicts_between_distinct_modules():
    routes = [
        _route("app/api/a.py", "GET", "/api/v1/x/y"),
        _route("app/api/b.py", "GET", "/api/v1/x/y"),
    ]
    assert sc.find_double_mounts(routes, ["/api/v1"]) == []


def test_double_mount_reports_the_same_router_registered_twice():
    route = _route("app/api/a.py", "GET", "/api/v1/x/y")
    findings = sc.find_double_mounts([route, route], ["/api/v1"])
    assert len(findings) == 1
    assert findings[0]["namespace"] == "/api/v1/x"
    assert findings[0]["owners"] == ["app/api/a.py"]
    assert findings[0]["exact_conflicts"] == [
        {"method": "GET", "path": "/api/v1/x/y", "modules": ["app/api/a.py"]}
    ]


def test_single_owner_namespace_is_not_reported():
    routes = [_route("app/api/ops.py", "GET", "/api/v1/ops/health")]
    assert sc.find_double_mounts(routes, ["/api/v1"]) == []


def test_routes_on_different_entrypoints_do_not_collide():
    routes = [
        _route("app/api/auth.py", "POST", "/api/v1/auth/login"),
        _route("app/api/auth2.py", "GET", "/api/v1/auth/me", entry="app/yeoljeong_main.py"),
    ]
    assert sc.find_double_mounts(routes, ["/api/v1"]) == []


def test_route_shadowed_catches_a_duplicate_inside_one_module():
    """AADS 실측 — `app/api/ops.py` 가 `GET /ops/codex-usage` 를 두 번 등록했다.

    함수 이름이 달라 ruff F811 도 조용하므로 ROUTE_SHADOWED가 실제 충돌을
    별도로 보고해야 한다.
    """
    routes = [
        dict(_route("app/api/ops.py", "GET", "/api/v1/ops/codex-usage"), lineno=2677),
        dict(_route("app/api/ops.py", "GET", "/api/v1/ops/codex-usage"), lineno=2877),
    ]
    findings = sc.find_route_shadowed(routes)
    assert len(findings) == 1
    assert findings[0]["method"] == "GET"
    assert findings[0]["path"] == "/api/v1/ops/codex-usage"
    assert findings[0]["same_module"] is True
    assert findings[0]["winner"] == {"module": "app/api/ops.py", "lineno": 2677}
    assert findings[0]["shadowed"] == [{"module": "app/api/ops.py", "lineno": 2877}]


def test_route_shadowed_keeps_the_first_registered_route():
    """FastAPI 는 먼저 등록된 라우트를 쓴다 — 순서를 뒤집으면 승자도 뒤집힌다."""
    routes = [
        dict(_route("app/api/b.py", "GET", "/api/v1/x/y"), lineno=9),
        dict(_route("app/api/a.py", "GET", "/api/v1/x/y"), lineno=3),
    ]
    findings = sc.find_route_shadowed(routes)
    assert findings[0]["winner"]["module"] == "app/api/b.py"
    assert findings[0]["same_module"] is False


def test_route_shadowed_ignores_different_methods_on_the_same_path():
    routes = [
        _route("app/api/ops.py", "GET", "/api/v1/ops/thing"),
        _route("app/api/ops.py", "POST", "/api/v1/ops/thing"),
    ]
    assert sc.find_route_shadowed(routes) == []


def test_route_shadowed_ignores_the_same_path_on_another_entrypoint():
    """`app/main.py` 와 `app/yeoljeong_main.py` 는 서로 다른 ASGI 앱이다."""
    routes = [
        _route("app/api/a.py", "GET", "/api/v1/shared/thing"),
        _route("app/api/b.py", "GET", "/api/v1/shared/thing",
               entry="app/yeoljeong_main.py"),
    ]
    assert sc.find_route_shadowed(routes) == []


def test_route_shadowed_matches_paths_after_param_normalization():
    """`{id}` 와 `{task_id}` 는 같은 라우트다 — 이름만 다르면 여전히 가려진다."""
    routes = [
        dict(_route("app/api/a.py", "PATCH", "/api/v1/tasks/{id}"), lineno=10),
        dict(_route("app/api/a.py", "PATCH", "/api/v1/tasks/{task_id}"), lineno=40),
    ]
    findings = sc.find_route_shadowed(routes)
    assert len(findings) == 1
    assert findings[0]["shadowed"] == [{"module": "app/api/a.py", "lineno": 40}]


def test_parsed_routes_come_back_in_source_order():
    """ast.walk 는 너비 우선이라 중첩 함수가 섞이면 소스 순서가 깨진다.

    순서가 깨지면 ROUTE_SHADOWED 가 "어느 쪽이 죽었나" 를 거꾸로 말한다.
    """
    info = sc.parse_router_module("app/api/s.py", textwrap.dedent("""
        from fastapi import APIRouter
        router = APIRouter()

        @router.get("/a")
        async def a():
            def inner():
                pass
            return {}

        @router.get("/b")
        async def b():
            return {}
    """))
    assert [r.path for r in info.routes] == ["/a", "/b"]
    assert [r.lineno for r in info.routes] == sorted(r.lineno for r in info.routes)


def test_orphan_router_is_the_unmounted_module():
    findings = sc.find_orphan_routers(
        ["app/api/ceo_chat.py", "app/api/health.py"],
        {"app/api/health.py": ["app/main.py"]},
    )
    assert [f["module"] for f in findings] == ["app/api/ceo_chat.py"]


def test_router_mounted_on_secondary_entrypoint_is_not_orphan():
    findings = sc.find_orphan_routers(
        ["app/api/yeoljeong_finance.py"],
        {"app/api/yeoljeong_finance.py": ["app/yeoljeong_main.py"]},
    )
    assert findings == []


def test_table_without_ddl_is_reported():
    findings = sc.find_table_no_model(
        {"pipeline_jobs": ["app/api/admin.py"], "chat_messages": ["app/routers/chat.py"]},
        {"chat_messages"},
    )
    assert [f["table"] for f in findings] == ["pipeline_jobs"]
    assert findings[0]["user_count"] == 1


def test_known_external_tables_are_excluded():
    findings = sc.find_table_no_model(
        {"remote_only": ["app/x.py"]}, set(), known_external=["remote_only"]
    )
    assert findings == []


def test_stale_backup_lists_only_backup_files():
    findings = sc.find_stale_backups(
        ["app/api/chat.py", "app/api/chat.py.bak.T073", "app/main.py.bak_pool_fix"],
        sc.DEFAULT_RULES["backup_patterns"],
    )
    assert [f["path"] for f in findings] == [
        "app/api/chat.py.bak.T073", "app/main.py.bak_pool_fix"
    ]


# ── 집계·고정선 ────────────────────────────────────────────────────────


def test_counts_by_rule_reports_zero_for_untriggered_rules():
    counts = sc.counts_by_rule([{"rule": "DUP_MODULE"}, {"rule": "DUP_MODULE"}])
    assert counts["DUP_MODULE"] == 2
    assert counts["ROUTE_MISSING"] == 0
    assert set(counts) == set(sc.RULE_ORDER)


def test_baseline_comparison_flags_only_increases():
    current = {"findings_by_rule": {"DUP_MODULE": 2, "STALE_BACKUP": 10}}
    saved = {"findings_by_rule": {"DUP_MODULE": 1, "STALE_BACKUP": 34}}
    regressions = sc.compare_baseline(current, saved)
    assert regressions == ["DUP_MODULE: 1 → 2 (+1)"]


def test_baseline_comparison_flags_a_rule_missing_from_the_baseline():
    current = {"findings_by_rule": {"NEW_RULE": 3}}
    regressions = sc.compare_baseline(current, {"findings_by_rule": {}})
    assert regressions == ["NEW_RULE: 고정선에 없던 규칙 (현재 3건)"]


# ── 0건 가드 ───────────────────────────────────────────────────────────


class _FakeScan:
    """zero_target_guard 는 Scan 의 속성만 본다 — FS 없이 확인할 수 있다."""

    def __init__(self, py_files, router_dir_files, modules, entrypoints_present,
                 fe_roots=None, fe_files=None):
        self.py_files = py_files
        self.router_dir_files = router_dir_files
        self.modules = modules
        self.entrypoints_present = entrypoints_present
        self.rules = {"frontend": {"roots": list(fe_roots or [])}}
        self.fe_files = list(fe_files or [])


def test_zero_target_guard_is_silent_when_everything_was_scanned():
    scan = _FakeScan(["app/main.py"], ["app/api/x.py"], {"app/api/x.py": object()},
                     ["app/main.py"])
    assert sc.zero_target_guard(scan) == []


def test_zero_target_guard_fires_when_frontend_is_configured_but_empty():
    """2026-09-16: 유령 미러를 지우자 프런트 0파일이 됐고 PATH_DRIFT/ROUTE_MISSING 이
    영원히 0 으로 나올 뻔했다. 백엔드가 멀쩡하면 전체 가드는 통과하므로 별도로 잡는다."""
    scan = _FakeScan(["app/main.py"], ["app/api/x.py"], {"app/api/x.py": object()},
                     ["app/main.py"], fe_roots=["../aads-dashboard/src"], fe_files=[])

    reasons = sc.zero_target_guard(scan)

    assert len(reasons) == 1
    assert "frontend.roots" in reasons[0]


def test_zero_target_guard_silent_when_frontend_has_files():
    scan = _FakeScan(["app/main.py"], ["app/api/x.py"], {"app/api/x.py": object()},
                     ["app/main.py"], fe_roots=["../aads-dashboard/src"],
                     fe_files=["/root/aads/aads-dashboard/src/lib/api.ts"])

    assert sc.zero_target_guard(scan) == []


def test_zero_target_guard_names_every_empty_input():
    """'위반 0건' 과 '아무것도 못 읽었다' 를 같은 코드로 내보내지 않기 위한 마지막 방벽."""
    reasons = sc.zero_target_guard(_FakeScan([], [], {}, []))
    assert len(reasons) == 4
    assert any("app_roots" in r for r in reasons)
    assert any("엔트리포인트" in r for r in reasons)


def test_zero_target_guard_fires_when_only_routers_are_missing():
    scan = _FakeScan(["app/main.py"], [], {}, ["app/main.py"])
    reasons = sc.zero_target_guard(scan)
    assert any("router_dirs" in r for r in reasons)


# ── 규칙 파일 ──────────────────────────────────────────────────────────


def test_shipped_rules_file_loads_without_warnings():
    rules, warnings = sc.load_rules(ROOT / "tools" / "aag" / "rules_aads.yml")
    assert warnings == []
    assert rules["entrypoints"]["primary"] == "app/main.py"
    assert "app/routers" in rules["scan"]["router_dirs"]
    assert set(sc.RULE_ORDER) <= set(rules["severity"])


def test_missing_rules_file_warns_instead_of_silently_defaulting():
    rules, warnings = sc.load_rules(ROOT / "tools" / "aag" / "no-such-rules.yml")
    assert warnings and "기본값" in warnings[0]
    assert rules["entrypoints"]["primary"] == "app/main.py"


# ── 2026-09-16 ROUTE_MISSING 오탐 4/18 제거 ────────────────────────────
# 라이브 OpenAPI(696 경로) 대조로 확인한 결과 18건 중 4건이 우리 백엔드 계약과
# 무관했다. P0 에 오탐이 섞이면 진짜 P0 까지 같이 무시된다.

HOSTS = ["aads.newtalk.kr", "newtalk.kr", "localhost", "127.0.0.1"]


def test_external_origin_is_not_measured_against_backend_routes():
    """`fetch("https://raw.githubusercontent.com/...")` 가 P0 로 잡혔던 건."""
    status, path, why = sc.resolve_call_url(
        '"https://raw.githubusercontent.com/moongoby-GO100/aads-docs/main/x.json"',
        {}, ["NEXT_PUBLIC_API_URL"], internal_hosts=HOSTS,
    )
    assert status == "external"
    assert "raw.githubusercontent.com" in why
    assert path == ""


def test_our_own_absolute_url_is_still_measured():
    status, path, _ = sc.resolve_call_url(
        '"https://aads.newtalk.kr/api/v1/ops/health-check"',
        {}, ["NEXT_PUBLIC_API_URL"], internal_hosts=HOSTS,
    )
    assert status == "resolved"
    assert path == "/api/v1/ops/health-check"


def test_subdomain_of_internal_host_counts_as_ours():
    status, _, _ = sc.resolve_call_url(
        '"https://pick.newtalk.kr/api/v1/x"', {}, [], internal_hosts=HOSTS
    )
    assert status == "resolved"


def test_next_route_handlers_become_frontend_served_paths():
    """`/runtime/dashboard-slot` 은 대시보드가 스스로 서빙한다(백엔드에 없다)."""
    routes = sc.collect_next_route_handlers([
        "/root/aads/aads-dashboard/src/app/runtime/dashboard-slot/route.ts",
        "/root/aads/aads-dashboard/src/app/(admin)/loops/[id]/route.ts",
        "/root/aads/aads-dashboard/src/app/ops/page.tsx",
        "/root/aads/aads-dashboard/src/lib/api.ts",
    ])
    assert routes == ["/loops/{}", "/runtime/dashboard-slot"]


def test_variable_segment_is_unresolved_not_route_missing():
    """`/api/v1/loops/{}/{}` 의 실제 action 은 pause|resume|cancel 셋뿐이고
    셋 다 백엔드에 있다. 값을 모른다는 이유로 없는 결함을 세지 않는다."""
    routes = {"POST": ["/api/v1/loops/{}/pause", "/api/v1/loops/{}/cancel"]}
    verdict, why = sc.classify_frontend_call("POST", "/api/v1/loops/{}/{}", routes)
    assert verdict == "VAR_SEGMENT"
    assert "확정 불가" in why


def test_empty_internal_hosts_hides_nothing():
    """규칙에서 internal_hosts 가 빠져도 부채가 조용히 사라지면 안 된다."""
    status, _, _ = sc.resolve_call_url(
        '"https://raw.githubusercontent.com/x/y.json"', {}, [], internal_hosts=[]
    )
    assert status == "resolved"


def test_variable_segment_with_no_candidate_is_still_route_missing():
    """후보가 하나도 없으면 변수여도 결함이다 — 면죄부로 쓰이면 안 된다."""
    routes = {"POST": ["/api/v1/other/{}/pause"]}
    verdict, _ = sc.classify_frontend_call("POST", "/api/v1/loops/{}/{}", routes)
    assert verdict == "ROUTE_MISSING"
