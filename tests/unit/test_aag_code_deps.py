"""AAG S1 — import·tests 간선, GO100 Next.js 화면 노드, 플래그 off 불변.

가짜 저장소를 tmp 에 만들어 실제 Scan 을 돌린다. 운영 /root/aads 는 건드리지 않는다.
"""

import importlib.util
import json
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


cd = _load("aag_code_deps_t", "tools/aag/code_deps.py")
sc = _load("aag_scan_aads_cd", "tools/aag/scan_aads.py")


def _w(base: Path, rel: str, body: str = "") -> None:
    p = base / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(body), encoding="utf-8")


# ── Python import ──────────────────────────────────────────────────────


def _py_tree(base: Path) -> None:
    _w(base, "app/__init__.py")
    _w(base, "app/core/__init__.py", "from .util import helper\n")
    _w(base, "app/core/util.py", "def helper():\n    return 1\n")
    _w(base, "app/services/__init__.py")
    _w(base, "app/services/a.py", """
        import os
        import requests
        import app.core.util
        from app.core import util
        from app.services import b
        from . import c
        from .b import thing
        from ..core.util import helper
        from app.missing import nothing
        """)
    _w(base, "app/services/b.py", "thing = 1\n")
    _w(base, "app/services/c.py", "import importlib\nm = importlib.import_module('app.core.util')\n")
    _w(base, "app/broken.py", "def (:\n")


def _build_py(base: Path, test_roots=("tests",)):
    files = sorted(
        p.relative_to(base).as_posix() for p in base.rglob("*.py")
    )
    return cd.build_py_deps(files, lambda r: (base / r).read_text(encoding="utf-8"), [""], list(test_roots))


def _pairs(result, kind):
    return {(e["from"], e["to"]) for e in result["edges"] if e["kind"] == kind}


def test_python_absolute_relative_and_external(tmp_path):
    _py_tree(tmp_path)
    res = _build_py(tmp_path)
    imports = _pairs(res, "imports")
    # 절대 import: `import a.b.c` 와 `from a.b import c`
    assert ("app/services/a.py", "app/core/util.py") in imports
    assert ("app/services/a.py", "app/services/b.py") in imports
    # 상대 import: `from . import c`, `from .b import`, `from ..core.util import`
    assert ("app/services/a.py", "app/services/c.py") in imports
    # 동적 import_module 상수
    assert ("app/services/c.py", "app/core/util.py") in imports
    # __init__ 재수출을 상대 import 로 따라간다
    assert ("app/core/__init__.py", "app/core/util.py") in imports
    # 외부 패키지(os/requests)는 간선 없이 집계만
    assert not any("requests" in e["to"] or e["to"] == "os" for e in res["edges"])
    assert res["external"]["requests"] == 1 and res["external"]["os"] == 1
    # 같은 (from,to) 는 한 줄
    assert len(res["edges"]) == len({(e["from"], e["to"], e["kind"]) for e in res["edges"]})


def test_python_unresolved_and_parse_failures_are_counted_not_guessed(tmp_path):
    _py_tree(tmp_path)
    res = _build_py(tmp_path)
    bad = {(u["file"], u["import"]) for u in res["unresolved"]}
    assert ("app/services/a.py", "app.missing") in bad
    assert res["parse_failed"] == ["app/broken.py"]
    # 해석 실패한 import 는 상위 패키지로 대신 잇지 않는다
    assert ("app/services/a.py", "app/__init__.py") not in _pairs(res, "imports")


def test_relative_import_above_root_is_unresolved(tmp_path):
    _w(tmp_path, "app/x.py", "from ... import y\n")
    res = _build_py(tmp_path)
    assert res["unresolved"][0]["reason"] == "relative_beyond_root"


# ── tests 간선 ─────────────────────────────────────────────────────────


def test_tests_edges_link_test_files_to_target_modules(tmp_path):
    _py_tree(tmp_path)
    _w(tmp_path, "tests/unit/test_util.py", "from app.core.util import helper\nimport pytest\n")
    _w(tmp_path, "tests/unit/test_none.py", "import json\n")
    _w(tmp_path, "tests/helpers.py", "from app.services import b\n")
    _w(tmp_path, "tests/unit/test_helpers.py", "from tests import helpers\nfrom app.services import b\n")
    res = _build_py(tmp_path)
    assert ("tests/unit/test_util.py", "app/core/util.py") in _pairs(res, "tests")
    # 테스트→테스트 헬퍼는 tests 간선이 아니다
    assert ("tests/unit/test_helpers.py", "tests/helpers.py") not in _pairs(res, "tests")
    assert ("tests/unit/test_helpers.py", "app/services/b.py") in _pairs(res, "tests")
    # 테스트 파일은 imports 간선의 출발점이 되지 않는다
    assert not any(f.startswith("tests/") for f, _ in _pairs(res, "imports"))
    assert "tests/unit/test_none.py" in res["test_files"]
    assert "tests/unit/test_none.py" not in res["linked_tests"]
    assert "tests/unit/test_util.py" in res["linked_tests"]


def test_test_path_by_name_outside_test_roots():
    assert cd.is_test_path("backend/app/test_x.py", ["tests"])
    assert cd.is_test_path("pkg/conftest.py", [])
    assert not cd.is_test_path("app/core/util.py", ["tests"])


# ── TS import / tsconfig ───────────────────────────────────────────────


def _ts_tree(base: Path) -> None:
    _w(base, "web/tsconfig.json", """
        {
          // comment
          "compilerOptions": {
            "paths": { "@/*": ["./src/*"], },
          },
        }
        """)
    _w(base, "web/src/app/page.tsx", """
        import { Button } from "@/components/Button";
        import type { Row } from '../lib/types'
        import {
          a,
          b
        } from "./local";
        import React from "react";
        import "./styles.css";
        export * from "@/lib/types";
        const Lazy = () => import("@/components/Button");
        """)
    _w(base, "web/src/app/local.ts", "export const a = 1, b = 2\n")
    _w(base, "web/src/components/Button.tsx", "import { x } from '@scope/pkg/deep'\nimport { y } from '@/nope/missing'\n")
    _w(base, "web/src/lib/types.ts", "export type Row = {}\n")
    _w(base, "web/src/lib/index.ts", "")


def test_ts_imports_alias_relative_and_external(tmp_path):
    _ts_tree(tmp_path)
    keys = sorted(p.relative_to(tmp_path).as_posix() for p in (tmp_path / "web/src").rglob("*.ts*"))
    res = cd.build_ts_deps(
        keys, lambda k: (tmp_path / k).as_posix(), lambda a: a,
        lambda k: Path(k if k.startswith("/") else tmp_path / k).read_text(encoding="utf-8"),
        (tmp_path / "web/tsconfig.json").as_posix(),
    )
    pairs = {(e["from"], e["to"]) for e in res["edges"]}
    assert ("web/src/app/page.tsx", "web/src/components/Button.tsx") in pairs   # @/ alias (+ dynamic)
    assert ("web/src/app/page.tsx", "web/src/lib/types.ts") in pairs            # ../ + export * from
    assert ("web/src/app/page.tsx", "web/src/app/local.ts") in pairs            # 다중 행 import
    assert res["external"] == {"react": 1, "@scope/pkg": 1}
    assert res["assets_skipped"] == 1
    assert [u["import"] for u in res["unresolved"]] == ["@/nope/missing"]
    assert all(e["kind"] == "imports" and e["lang"] == "ts" for e in res["edges"])


def test_tsconfig_jsonc_comments_and_trailing_commas():
    cfg = cd.parse_tsconfig('{ /* c */ "a": "http://x", // t\n "b": [1,2,], }')
    assert cfg == {"a": "http://x", "b": [1, 2]}


def test_tsconfig_extends_supplies_paths(tmp_path):
    _w(tmp_path, "base.json", '{"compilerOptions": {"baseUrl": ".", "paths": {"~/*": ["src/*"]}}}')
    _w(tmp_path, "tsconfig.json", '{"extends": "./base.json"}')
    paths, base_url = cd.load_tsconfig_paths(
        (tmp_path / "tsconfig.json").as_posix(),
        lambda p: Path(p).read_text(encoding="utf-8") if Path(p).exists() else "",
    )
    assert paths == {"~/*": [(tmp_path / "src/*").as_posix()]}
    assert base_url == tmp_path.as_posix()


# ── Next.js 화면 route ─────────────────────────────────────────────────


@pytest.mark.parametrize("key,expected", [
    ("frontend/src/app/portfolio/page.tsx", {"role": "page", "route": "/portfolio"}),
    ("frontend/src/app/page.tsx", {"role": "page", "route": "/"}),
    ("frontend/src/app/(protected)/feed/[id]/page.tsx", {"role": "page", "route": "/feed/{id}"}),
    ("frontend/src/app/docs/[...slug]/page.tsx", {"role": "page", "route": "/docs/{slug*}"}),
    ("frontend/src/app/(protected)/layout.tsx", {"role": "layout", "route": "/"}),
    ("frontend/src/app/go100-api/health/route.ts", {"role": "route_handler", "route": "/go100-api/health"}),
    ("frontend/src/app/portfolio/loading.tsx", {"role": "special", "route": "/portfolio"}),
    ("frontend/src/pages/index.tsx", {"role": "page", "route": "/"}),
    ("frontend/src/pages/trade/[code].tsx", {"role": "page", "route": "/trade/{code}"}),
    ("frontend/src/pages/api/ping.ts", {"role": "route_handler", "route": "/api/ping"}),
    ("frontend/src/pages/_error.tsx", {"role": "special"}),
    ("frontend/src/components/ui/Card.tsx", {"role": "component"}),
    ("frontend/src/lib/api/client.ts", {"role": "module"}),
])
def test_classify_next_file(key, expected):
    assert cd.classify_next_file(key) == expected


# ── Scan 통합: GO100 형 저장소, 플래그 on/off ───────────────────────────


GO100_RULES = textwrap.dedent("""
    version: 1
    scan:
      router_dirs: [backend/app/api]
      app_roots: [backend/app]
      backup_roots: [backend/app]
      schema_roots: [migrations]
      exclude_dir_names: [node_modules, .git, .next, __pycache__]
    entrypoints:
      primary: backend/app/main.py
      secondary: []
    frontend:
      roots: []
      extensions: [".js"]
      base_env_names: []
      fallback_base: ""
      call_names: [fetch]
    code_deps:
      python_roots: [backend, tests]
      python_import_roots: ["", backend]
      test_roots: [tests]
      frontend:
        roots: [frontend/src]
        extensions: [".ts", ".tsx"]
        base_env_names: [NEXT_PUBLIC_API_URL]
        call_names: [fetch]
        tsconfig: frontend/tsconfig.json
        contract_findings: false
    sql:
      ignore_tables: []
      known_external_tables: []
""")


def _go100_repo(base: Path) -> Path:
    _w(base, "backend/__init__.py")
    _w(base, "backend/app/__init__.py")
    _w(base, "backend/app/main.py", """
        from fastapi import FastAPI
        from app.api import orders
        app = FastAPI()
        app.include_router(orders.router, prefix="/api/v1")
        """)
    _w(base, "backend/app/api/__init__.py")
    _w(base, "backend/app/api/orders.py", """
        from fastapi import APIRouter
        from app.services.pricing import quote
        router = APIRouter(prefix="/orders")
        @router.get("")
        def list_orders():
            return quote()
        """)
    _w(base, "backend/app/services/__init__.py")
    _w(base, "backend/app/services/pricing.py", "def quote():\n    return 1\n")
    _w(base, "tests/go100/test_pricing.py", "from backend.app.services.pricing import quote\n")
    _w(base, "frontend/tsconfig.json", '{"compilerOptions": {"paths": {"@/*": ["./src/*"],}},}')
    _w(base, "frontend/src/app/orders/page.tsx", """
        import { OrderTable } from "@/components/OrderTable";
        export default function Page() { return null }
        """)
    _w(base, "frontend/src/components/OrderTable.tsx", """
        const BASE_URL = process.env.NEXT_PUBLIC_API_URL || "http://127.0.0.1:8002/api/v1";
        export const OrderTable = () => fetch(`${BASE_URL}/orders`);
        """)
    rules = base / "rules.yml"
    rules.write_text(GO100_RULES, encoding="utf-8")
    return rules


def _run(base: Path, rules: Path, monkeypatch, flag: str):
    if flag is None:
        monkeypatch.delenv(cd.FLAG_ENV, raising=False)
    else:
        monkeypatch.setenv(cd.FLAG_ENV, flag)
    loaded, warnings = sc.load_rules(rules)
    scan = sc.Scan(base.resolve(), loaded)
    scan.warnings.extend(warnings)
    scan.scan_backend()
    scan.scan_frontend()
    scan.scan_code_deps()
    scan.apply_rules()
    scan.build_graph()
    return scan, sc.build_baseline(scan)


def test_go100_scan_adds_screen_nodes_and_edges(tmp_path, monkeypatch):
    rules = _go100_repo(tmp_path)
    scan, baseline = _run(tmp_path, rules, monkeypatch, None)
    nodes = {n["id"]: n for n in scan.nodes}
    page = nodes["frontend/src/app/orders/page.tsx"]
    assert page["kind"] == "frontend" and page["role"] == "page" and page["route"] == "/orders"
    assert nodes["frontend/src/components/OrderTable.tsx"]["role"] == "component"
    assert nodes["tests/go100/test_pricing.py"]["role"] == "test"

    by_kind = {}
    for e in scan.edges:
        by_kind.setdefault(e["kind"], set()).add((e["from"], e["to"]))
    assert ("backend/app/api/orders.py", "backend/app/services/pricing.py") in by_kind["imports"]
    assert ("backend/app/main.py", "backend/app/api/orders.py") in by_kind["imports"]
    assert ("frontend/src/app/orders/page.tsx", "frontend/src/components/OrderTable.tsx") in by_kind["imports"]
    assert ("tests/go100/test_pricing.py", "backend/app/services/pricing.py") in by_kind["tests"]
    # 화면→API calls 간선 (AADS 와 같은 규칙)
    assert any(f == "frontend/src/components/OrderTable.tsx" for f, _ in by_kind["calls"])

    cov = baseline["scope"]["coverage"]
    assert cov["frontend_files_total"] == 2 and cov["frontend_files_as_nodes"] == 2
    assert cov["test_files_total"] == 1 and cov["test_files_linked"] == 1
    assert cov["unresolved_imports"] == 0
    assert cov["py_files_as_nodes"] == cov["py_files_total"]
    assert baseline["scope"]["edges_by_kind"]["tests"] == 1
    assert baseline["scope"]["code_deps"]["edges"]["imports_ts"] == 1
    # contract_findings:false — 새 calls 가 결함(P0)으로 번지지 않는다
    assert not [f for f in scan.findings if f["rule"] in ("ROUTE_MISSING", "PATH_DRIFT")]


def test_code_deps_exclude_keeps_data_dirs_that_global_exclude_drops(tmp_path, monkeypatch):
    rules = _go100_repo(tmp_path)
    _w(tmp_path, "backend/app/services/data/__init__.py")
    _w(tmp_path, "backend/app/services/data/router.py", "def route():\n    return 1\n")
    _w(tmp_path, "backend/app/api/feed.py", "from app.services.data.router import route\n")
    _w(tmp_path, "frontend/src/app/admin/data/page.tsx", "export default function P() { return null }\n")
    rules.write_text(
        GO100_RULES.replace("[node_modules, .git, .next, __pycache__]",
                            "[node_modules, .git, .next, __pycache__, data]")
        + "\n", encoding="utf-8")

    scan, _ = _run(tmp_path, rules, monkeypatch, None)
    ids = {n["id"] for n in scan.nodes}
    assert "backend/app/services/data/router.py" not in ids
    assert "frontend/src/app/admin/data/page.tsx" not in ids

    text = GO100_RULES.replace("[node_modules, .git, .next, __pycache__]",
                               "[node_modules, .git, .next, __pycache__, data]")
    text = text.replace("code_deps:\n", "code_deps:\n  exclude_dir_names: [node_modules, .git, .next, __pycache__]\n", 1)
    rules.write_text(text, encoding="utf-8")
    scan, baseline = _run(tmp_path, rules, monkeypatch, None)
    nodes = {n["id"]: n for n in scan.nodes}
    assert nodes["frontend/src/app/admin/data/page.tsx"]["route"] == "/admin/data"
    assert "backend/app/services/data/router.py" in nodes
    pairs = {(e["from"], e["to"]) for e in scan.edges if e["kind"] == "imports"}
    assert ("backend/app/api/feed.py", "backend/app/services/data/router.py") in pairs
    assert baseline["scope"]["coverage"]["unresolved_imports"] == 0

    monkeypatch.setenv(cd.FLAG_ENV, "0")
    off, _ = _run(tmp_path, rules, monkeypatch, "0")
    assert "backend/app/services/data/router.py" not in {n["id"] for n in off.nodes}


def test_flag_off_matches_pre_s1_snapshot(tmp_path, monkeypatch):
    """AAG_SCAN_CODE_DEPS=0 → 노드·간선·stats·결함이 S1 이전 스캐너와 똑같다."""
    rules = _go100_repo(tmp_path)
    scan, baseline = _run(tmp_path, rules, monkeypatch, "0")
    assert scan.code_deps_enabled is False
    kinds = {e["kind"] for e in scan.edges}
    assert kinds <= {"queries", "calls", "owns", "mounts"}
    assert not any("role" in n for n in scan.nodes)
    assert not scan.fe_files
    assert set(baseline["scope"]) == {
        "app_py_files", "router_dir_files", "router_modules", "include_router_calls",
        "mounted_routes", "namespaces", "frontend_files", "frontend_calls_resolved",
        "sql_tables_referenced", "graph_nodes", "graph_edges", "coverage",
    }
    assert "test_files_total" not in baseline["scope"]["coverage"]

    # 플래그 on 의 기존 요소는 off 결과의 상위집합이어야 한다 (제거·변경 없음)
    on, _ = _run(tmp_path, rules, monkeypatch, "1")
    off_nodes = {n["id"]: n for n in scan.nodes}
    on_nodes = {n["id"]: n for n in on.nodes}
    for nid, node in off_nodes.items():
        assert on_nodes[nid]["kind"] == node["kind"]
    assert all(e in on.edges for e in scan.edges)
    assert on.findings == scan.findings


def test_section_missing_means_no_extraction(tmp_path, monkeypatch):
    """code_deps 섹션이 없는 프로젝트(NAS/SF 등)는 플래그가 켜져도 S1 을 하지 않는다."""
    rules = _go100_repo(tmp_path)
    text = rules.read_text(encoding="utf-8")
    rules.write_text(text[:text.index("code_deps:")] + "sql:\n  ignore_tables: []\n  known_external_tables: []\n",
                     encoding="utf-8")
    scan, baseline = _run(tmp_path, rules, monkeypatch, None)
    assert scan.code_deps_enabled is False
    assert "edges_by_kind" not in baseline["scope"]


def test_code_deps_failure_does_not_abort_scan(tmp_path, monkeypatch):
    rules = _go100_repo(tmp_path)

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(sc.code_deps, "build_py_deps", boom)
    scan, baseline = _run(tmp_path, rules, monkeypatch, None)
    assert scan.code_deps_stats["error"].startswith("RuntimeError")
    assert any("code_deps 추출 실패" in w for w in scan.warnings)
    assert not [e for e in scan.edges if e["kind"] in ("imports", "tests")]
    json.dumps(baseline)


def test_flag_parsing():
    assert cd.enabled({}) is True
    assert cd.enabled({cd.FLAG_ENV: "1"}) is True
    for off in ("0", "false", "OFF", "no"):
        assert cd.enabled({cd.FLAG_ENV: off}) is False
