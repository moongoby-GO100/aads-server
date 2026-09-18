#!/usr/bin/env python3
"""AAG L1/L2 추출기 자가검증 — 합성 저장소를 만들어 규칙 7종이 실제로 걸리는지 본다.

단위테스트(`tests/unit/test_aag_scan_aads.py`)가 순수 함수를 보는 것과 달리
여기서는 **스캐너를 통째로** 돌린다. 규칙이 하나씩은 맞는데 조립하면 0건이
나오는 실패 모드가 있기 때문이다 — 그리고 0건은 "깨끗하다" 와 구분되지 않는다.

합성 저장소에는 실제 AADS 에서 실패를 냈던 함정을 그대로 심는다.
  - 독스트링 안의 `include_router` (grep 파서라면 여기서 오탐)
  - exact 충돌 0건인 네임스페이스 공유 (exact 만 세면 못 잡는다)
  - 한 모듈이 같은 METHOD+경로를 두 번 등록 (소유자가 1개라 DOUBLE_MOUNT 밖)
  - `NEXT_PUBLIC_API_URL` 기본값 합성이 필요한 프런트 호출

종료코드: 0 전부 통과 / 1 실패 있음.
"""

from __future__ import annotations

import importlib.util
import io
import json
import shutil
import sys
import tempfile
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("aag_scan_aads", _HERE / "scan_aads.py")
scan_aads = importlib.util.module_from_spec(_spec)
sys.modules["aag_scan_aads"] = scan_aads  # dataclass 가 모듈을 되찾을 수 있어야 한다
_spec.loader.exec_module(scan_aads)


RULES_YML = """
version: 1
scan:
  router_dirs: [app/api, app/routers]
  app_roots: [app]
  backup_roots: [app, migrations]
  schema_roots: [migrations]
  exclude_dir_names: [__pycache__]
entrypoints:
  primary: app/main.py
  secondary: [app/side_main.py]
frontend:
  roots: [web/src]
  extensions: [".ts", ".tsx"]
  base_env_names: [NEXT_PUBLIC_API_URL]
  fallback_base: ""
  call_names: [fetch]
  path_helpers: {request: BASE_URL}
  path_aliases: {"@/": "web/src/"}
backup_patterns: ['\\.bak$', '\\.bak[._-].*$']
sql:
  ignore_tables: [pg_catalog]
  known_external_tables: []
severity:
  DUP_MODULE: P1
  DOUBLE_MOUNT: P1
  ROUTE_SHADOWED: P0
  ORPHAN_ROUTER: P2
  TABLE_NO_MODEL: P1
  PATH_DRIFT: P1
  ROUTE_MISSING: P0
  STALE_BACKUP: P2
output:
  graph_json: out/graph.json
  mermaid: out/arch.mmd
  findings_md: out/findings.md
  mermaid_max_namespaces: 10
"""

FILES: dict[str, str] = {
    "app/main.py": '''
from fastapi import FastAPI
from app.api import alpha, dup, notes, shadow
from app.routers.dup import router as dup_v2_router

app = FastAPI()
app.include_router(alpha.router, prefix="/api/v1")
app.include_router(alpha.router, prefix="/api/v1")
app.include_router(dup.router, prefix="/api/v1")
app.include_router(dup_v2_router, prefix="/api/v1")
app.include_router(notes.router, prefix="/api/v1")
app.include_router(shadow.router, prefix="/api/v1")
''',
    "app/side_main.py": '''
from fastapi import FastAPI
from app.api import sidecar, sidecar_same

app = FastAPI()
app.include_router(sidecar.router, prefix="/api/v1")
app.include_router(sidecar_same.router, prefix="/api/v1")
''',
    "app/api/__init__.py": "",
    "app/api/alpha.py": '''
from fastapi import APIRouter

router = APIRouter()


@router.get("/alpha")
async def list_alpha():
    return []


@router.get("/shared/first")
async def shared_first():
    return {}
''',
    # exact 충돌 0건으로 /api/v1/shared 네임스페이스를 나눠 갖는다.
    "app/api/dup.py": '''
from fastapi import APIRouter

router = APIRouter()


@router.post("/shared/second")
async def shared_second():
    return {}
''',
    "app/routers/__init__.py": "",
    "app/routers/dup.py": '''
from fastapi import APIRouter

router = APIRouter(prefix="/dupns")


@router.get("/items/{item_id}")
async def get_item(item_id: str):
    return {}
''',
    # 독스트링 함정 — grep 파서라면 여기서 가짜 마운트를 읽는다.
    "app/api/orphan.py": '''
"""아직 등록하지 않은 라우터.

    from app.api import orphan
    app.include_router(orphan.router, prefix="/api/v1")
"""
from fastapi import APIRouter

router = APIRouter(prefix="/orphan")


@router.get("/ping")
async def ping():
    return {}
''',
    "app/api/sidecar.py": '''
from fastapi import APIRouter

router = APIRouter(prefix="/sidecar")


@router.get("/status")
async def status():
    return {}
''',
    "app/api/notes.py": '''
from fastapi import APIRouter

router = APIRouter()


@router.get("/notes")
async def list_notes(conn):
    await conn.fetch("SELECT id FROM known_table WHERE id = $1")
    await conn.fetch("SELECT id FROM ghost_table WHERE id = $1")
    await conn.fetch(f"SELECT id FROM {'dynamic_name'} d WHERE d.id = $1")
    return "update failed"
''',
    "app/api/old.py.bak": "router = None\n",
    # 한 모듈이 같은 METHOD+경로를 두 번 등록한다 — 뒤엣것은 등록조차 되지
    # 않고 죽는다. 함수 이름이 다르므로 ruff F811 로는 잡히지 않는다.
    "app/api/shadow.py": '''
from fastapi import APIRouter

router = APIRouter(prefix="/shadow")


@router.get("/dup")
async def dup_winner():
    return {"which": "first"}


@router.post("/dup")
async def other_method_is_fine():
    return {}


@router.get("/dup")
async def dup_loser():
    return {"which": "second"}
''',
    # 다른 엔트리포인트(= 다른 ASGI 앱)의 같은 경로는 충돌이 아니다.
    "app/api/sidecar_same.py": '''
from fastapi import APIRouter

router = APIRouter()


@router.get("/shadow/dup")
async def same_path_other_app():
    return {}
''',
    "migrations/001_init.sql": "CREATE TABLE IF NOT EXISTS known_table (id uuid primary key);\n",
    "web/src/lib/api.ts": '''
const BASE_URL = process.env.NEXT_PUBLIC_API_URL || "https://example.test/api/v1";

async function request(path: string, options?: RequestInit) {
  return fetch(`${BASE_URL}${path}`, options);
}

export const api = {
  ok: () => request("/alpha"),
  okParam: () => request(`/dupns/items/${"x"}`),
  drift: () => request("/shared/second"),
  missing: () => request("/does-not-exist"),
};
''',
    "web/src/app/page.tsx": '''
import { BASE_URL } from "@/lib/api";

export function Page() {
  fetch(`${BASE_URL}/alpha`);
  fetch(`${process.env.NEXT_PUBLIC_API_URL || ""}/alpha`);
  fetch(someRuntimeUrl);
}
''',
}


def build_fixture(root: Path) -> Path:
    for rel, body in FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body.lstrip("\n"), encoding="utf-8")
    rules = root / "rules.yml"
    rules.write_text(RULES_YML.lstrip("\n"), encoding="utf-8")
    return rules


def run_scan(root: Path, rules: Path) -> tuple[int, dict]:
    buf = io.StringIO()
    with redirect_stdout(buf), redirect_stderr(io.StringIO()):
        code = scan_aads.main([
            "--root", str(root), "--rules", str(rules), "--json",
        ])
    try:
        return code, json.loads(buf.getvalue())
    except json.JSONDecodeError:
        return code, {}


class Checker:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.passed = 0

    def check(self, label: str, actual, expected) -> None:
        if actual == expected:
            self.passed += 1
            print(f"  PASS  {label}")
        else:
            self.failures.append(f"{label}: 기대 {expected!r} / 실제 {actual!r}")
            print(f"  FAIL  {label}: 기대 {expected!r} / 실제 {actual!r}")

    def check_true(self, label: str, actual: bool) -> None:
        self.check(label, bool(actual), True)


def main() -> int:
    chk = Checker()
    tmp = Path(tempfile.mkdtemp(prefix="aag-selftest-"))
    try:
        root = tmp / "repo"
        root.mkdir()
        rules = build_fixture(root)

        print("[1] 합성 저장소 스캔")
        code, payload = run_scan(root, rules)
        counts = payload.get("findings_by_rule", {})
        chk.check("종료코드는 1 (결함 있음)", code, 1)
        chk.check("DUP_MODULE", counts.get("DUP_MODULE"), 1)
        chk.check("ORPHAN_ROUTER", counts.get("ORPHAN_ROUTER"), 1)
        chk.check("STALE_BACKUP", counts.get("STALE_BACKUP"), 1)
        chk.check_true("DOUBLE_MOUNT 1건 이상", counts.get("DOUBLE_MOUNT", 0) >= 1)
        chk.check_true("PATH_DRIFT 1건 이상", counts.get("PATH_DRIFT", 0) >= 1)
        chk.check_true("ROUTE_MISSING 1건 이상", counts.get("ROUTE_MISSING", 0) >= 1)
        chk.check_true("TABLE_NO_MODEL 1건 이상", counts.get("TABLE_NO_MODEL", 0) >= 1)
        chk.check_true("ROUTE_SHADOWED 1건 이상", counts.get("ROUTE_SHADOWED", 0) >= 1)

        graph = json.loads((root / "out" / "graph.json").read_text(encoding="utf-8"))
        findings = graph["findings"]

        def by_rule(rule: str) -> list[dict]:
            return [f for f in findings if f["rule"] == rule]

        print("[2] 함정 — 독스트링 include_router 를 마운트로 읽지 않는가")
        orphans = {f["module"] for f in by_rule("ORPHAN_ROUTER")}
        chk.check("독스트링 예시는 마운트가 아니다", orphans, {"app/api/orphan.py"})

        print("[3] 같은 라우터 반복 등록만 DOUBLE_MOUNT 로 잡는가")
        shared = [f for f in by_rule("DOUBLE_MOUNT") if f["namespace"] == "/api/v1/shared"]
        chk.check("/api/v1/shared 중복 마운트 1건", len(shared), 1)
        if shared:
            chk.check("중복 소유 모듈", shared[0]["owners"], ["app/api/alpha.py"])

        print("[4] 보조 엔트리포인트에 마운트된 라우터는 고아가 아니다")
        chk.check_true("sidecar 는 ORPHAN 아님", "app/api/sidecar.py" not in orphans)

        print("[5] 프런트 base 합성 — 없으면 전 호출이 ROUTE_MISSING 이 된다")
        missing = {f["path"] for f in by_rule("ROUTE_MISSING")}
        chk.check("없는 라우트만 ROUTE_MISSING", missing, {"/api/v1/does-not-exist"})
        drift = {(f["method"], f["path"]) for f in by_rule("PATH_DRIFT")}
        chk.check_true("메서드 드리프트 감지", ("GET", "/api/v1/shared/second") in drift)
        chk.check_true("접두 누락 드리프트 감지", ("GET", "/alpha") in drift)

        print("[6] SQL — 정의된 테이블은 빼고, 산문은 테이블로 보지 않는다")
        tables = {f["table"] for f in by_rule("TABLE_NO_MODEL")}
        chk.check("ghost_table 만 지적", tables, {"ghost_table"})
        kinds = {u["kind"] for u in graph["unresolved"]}
        chk.check_true("동적 테이블명은 UNRESOLVED", "SQL_TABLE" in kinds)
        chk.check_true("해석 불가 URL 은 UNRESOLVED", "FRONTEND_URL" in kinds)

        print("[7] ROUTE_SHADOWED — 같은 앱에 두 번 등록된 라우트만 잡는가")
        shadowed = [f for f in by_rule("ROUTE_SHADOWED")
                    if f["path"] == "/api/v1/shadow/dup"]
        chk.check("의도한 shadow 라우트 1건", len(shadowed), 1)
        if shadowed:
            f = shadowed[0]
            chk.check("METHOD+경로", (f["method"], f["path"]),
                      ("GET", "/api/v1/shadow/dup"))
            chk.check("엔트리포인트", f["entrypoint"], "app/main.py")
            chk.check("한 모듈 안의 중복", f["same_module"], True)
            # 소스에서 먼저 온 쪽이 이긴다. ast.walk 순서를 그대로 쓰면 여기서 뒤집힌다.
            chk.check("먼저 등록된 쪽이 살아남는다",
                      f["winner"]["lineno"] < f["shadowed"][0]["lineno"], True)
            chk.check("죽은 라우트 1개", len(f["shadowed"]), 1)
            chk.check("P0", f["severity"], "P0")
        paths = {(x["entrypoint"], x["method"], x["path"]) for x in shadowed}
        chk.check_true("다른 엔트리포인트의 같은 경로는 충돌 아님",
                       ("app/side_main.py", "GET", "/api/v1/shadow/dup") not in paths)
        chk.check_true("METHOD 가 다르면 충돌 아님",
                       ("app/main.py", "POST", "/api/v1/shadow/dup") not in paths)

        print("[8] 0건 가드 — 빈 디렉터리는 '위반 0건' 이 아니라 exit 2")
        empty = tmp / "empty"
        empty.mkdir()
        code_empty, _ = run_scan(empty, rules)
        chk.check("빈 저장소 종료코드", code_empty, 2)

        print("[9] 결함이 없는 저장소는 exit 0")
        clean = tmp / "clean"
        (clean / "app/api").mkdir(parents=True)
        (clean / "app/routers").mkdir(parents=True)
        (clean / "migrations").mkdir(parents=True)
        (clean / "web/src").mkdir(parents=True)
        (clean / "app/api/__init__.py").write_text("", encoding="utf-8")
        (clean / "app/api/alpha.py").write_text(FILES["app/api/alpha.py"].lstrip("\n"),
                                                encoding="utf-8")
        (clean / "app/main.py").write_text(
            "from fastapi import FastAPI\n"
            "from app.api import alpha\n\n"
            "app = FastAPI()\n"
            'app.include_router(alpha.router, prefix="/api/v1")\n',
            encoding="utf-8",
        )
        # 프런트 소스가 한 개는 있어야 "깨끗한 저장소" 다. 규칙이 frontend.roots 를
        # 선언했는데 파일이 0개면 계약 검사가 조용히 꺼진 상태이므로 가드가 잡는다
        # (2026-09-16 유령 미러 제거 직후 실제로 그 상태가 됐다).
        (clean / "web/src/api.ts").write_text(
            'const BASE_URL = process.env.NEXT_PUBLIC_API_URL || "/api/v1";\n'
            'export const listAlpha = () => fetch(`${BASE_URL}/alpha`);\n',
            encoding="utf-8",
        )
        clean_rules = clean / "rules.yml"
        clean_rules.write_text(RULES_YML.lstrip("\n"), encoding="utf-8")
        code_clean, payload_clean = run_scan(clean, clean_rules)
        chk.check("깨끗한 저장소 종료코드", code_clean, 0)
        chk.check("결함 0건", payload_clean.get("findings_total"), 0)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if chk.failures:
        print(f"selftest 실패 {len(chk.failures)}건 / 통과 {chk.passed}건")
        for f in chk.failures:
            print(f"  - {f}")
        return 1
    print(f"selftest 전부 통과 ({chk.passed}건)")
    print("ALL SELF-TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
