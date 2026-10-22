"""AAG S2 — 스크립트·systemd·crontab·nginx·compose 간선, 운영 관측 파서, 플래그 off 불변.

가짜 저장소를 tmp 에 만들어 실제 Scan 을 돌린다. 운영 서버·/root/aads 는 건드리지 않고,
관측 수집 명령은 가짜 runner 로만 호출한다(실제 crontab/systemctl 실행 없음).
"""

import ast
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


sc = _load("aag_scan_aads_ops", "tools/aag/scan_aads.py")
oa = sc.ops_assets
ob = _load("aag_ops_observation_t", "tools/aag/ops_observation.py")
collect_cli = _load("aag_ops_collect_t", "scripts/aag_ops_collect.py")


def _w(base: Path, rel: str, body: str = "") -> None:
    p = base / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(body), encoding="utf-8")


RULES = textwrap.dedent("""
    version: 1
    scan:
      router_dirs: [app/api]
      app_roots: [app]
      backup_roots: [app]
      schema_roots: [migrations]
      exclude_dir_names: [node_modules, .git, __pycache__]
    entrypoints:
      primary: app/main.py
      secondary: []
    frontend:
      roots: []
      extensions: [".js"]
      base_env_names: []
      fallback_base: ""
      call_names: [fetch]
    code_deps:
      python_roots: [app, scripts]
      python_import_roots: [""]
      test_roots: [tests]
    ops_assets:
      script_roots: [scripts, deploy.sh]
      asset_roots: [scripts, deploy]
      asset_root_globs: ["docker-compose*.yml", "nginx*.conf", "Dockerfile*", "*.service"]
      path_prefix_map: [/root/aads/aads-server/, /app/]
    sql:
      ignore_tables: []
      known_external_tables: []
""")


def _repo(base: Path) -> Path:
    _w(base, "app/__init__.py")
    _w(base, "app/main.py", """
        from fastapi import FastAPI
        from app.api import chat
        app = FastAPI()
        app.include_router(chat.router, prefix="/api/v1")
        """)
    _w(base, "app/api/__init__.py")
    _w(base, "app/api/chat.py", """
        from fastapi import APIRouter
        router = APIRouter(prefix="/chat")
        @router.get("/ping")
        def ping():
            return 1
        """)
    _w(base, "app/jobs/__init__.py")
    _w(base, "app/jobs/nightly.py", "def run():\n    return 1\n")
    _w(base, "scripts/run_nightly.sh", """
        #!/usr/bin/env bash
        set -e
        ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
        cd "$ROOT"
        python3 -m app.jobs.nightly --full
        bash scripts/helper.sh
        "$ROOT/scripts/report.py" --x
        python3 - <<'PY'
        from app.jobs import nightly
        PY
        ssh host "bash scripts/remote_only.sh"
        echo "see scripts/not_a_call.sh"
        cat scripts/also_not_a_call.sh
        if [ -f scripts/guard.sh ]; then :; fi
        bash scripts/missing_script.sh
        """)
    _w(base, "scripts/helper.sh", "#!/bin/sh\necho ok\n")
    _w(base, "scripts/remote_only.sh", "echo remote\n")
    _w(base, "scripts/not_a_call.sh", "echo x\n")
    _w(base, "scripts/also_not_a_call.sh", "echo x\n")
    _w(base, "scripts/guard.sh", "echo x\n")
    _w(base, "scripts/report.py", """
        import subprocess, sys, os, runpy
        subprocess.run(["bash", "scripts/helper.sh"], check=True)
        subprocess.run([sys.executable, "-m", "app.jobs.nightly"])
        os.system("bash /root/aads/aads-server/scripts/guard.sh")
        runpy.run_path("scripts/helper_py.py")
        """)
    _w(base, "scripts/helper_py.py", "x = 1\n")
    _w(base, "scripts/nightly.service", """
        [Unit]
        Description=nightly
        [Service]
        WorkingDirectory=/root/aads/aads-server
        ExecStart=/usr/bin/python3 -m app.jobs.nightly
        ExecStartPost=/root/aads/aads-server/scripts/helper.sh
        """)
    _w(base, "scripts/nightly.timer", """
        [Timer]
        OnCalendar=*-*-* 03:00:00
        [Install]
        WantedBy=timers.target
        """)
    _w(base, "scripts/backup.service", """
        [Service]
        ExecStart=/bin/bash -lc 'cd /root/aads/aads-server && bash scripts/guard.sh'
        ExecStopPost=/usr/local/bin/not-in-repo.sh
        """)
    _w(base, "scripts/backup.timer", """
        [Timer]
        OnCalendar=hourly
        Unit=backup.service
        """)
    _w(base, "scripts/orphan.timer", "[Timer]\nOnCalendar=daily\nUnit=gone.service\n")
    _w(base, "scripts/crontab.txt", """
        SHELL=/bin/bash
        */5 * * * * cd /root/aads/aads-server && bash scripts/helper.sh >> /var/log/h.log 2>&1
        0 4 * * * /usr/bin/python3 /root/aads/aads-server/scripts/report.py
        # 0 3 * * * /root/aads/aads-server/scripts/guard.sh
        @reboot /opt/elsewhere/start.sh
        """)
    _w(base, "nginx-test.conf", """
        upstream api_pool { server 127.0.0.1:8100; server 127.0.0.1:8101; }
        server {
            listen 80;
            server_name example.test;
            location /api/v1/chat/ { proxy_pass http://api_pool; }
            location = /api/v1/ghost { proxy_pass http://api_pool; }
            location ~ ^/api/v1/(chat|other)/stream$ { proxy_pass http://api_pool; }
            location /api/ { proxy_pass http://127.0.0.1:8100/api/; }
            location /static/ { root /var/www; }
            location / { proxy_pass http://127.0.0.1:3000; }
        }
        """)
    _w(base, "Dockerfile", "FROM python:3.11\n")
    _w(base, "docker-compose.yml", """
        services:
          api:
            build: .
            image: aads-server:local
            command: ["python", "-m", "app.jobs.nightly"]
          worker:
            image: aads-server:local
            working_dir: /app
            command: bash scripts/run_nightly.sh
          db:
            image: postgres:15
          dash:
            build:
              context: ../dashboard
              dockerfile: Dockerfile
        """)
    _w(base, "deploy.sh", "#!/bin/bash\nbash scripts/helper.sh\n")
    rules = base / "rules.yml"
    rules.write_text(RULES, encoding="utf-8")
    return rules


def _run(base: Path, rules: Path, monkeypatch, s1="1", s2="1"):
    monkeypatch.setenv("AAG_SCAN_CODE_DEPS", s1)
    monkeypatch.setenv(oa.FLAG_ENV, s2)
    loaded, warnings = sc.load_rules(rules)
    scan = sc.Scan(base.resolve(), loaded)
    scan.warnings.extend(warnings)
    scan.scan_backend()
    scan.scan_frontend()
    scan.scan_code_deps()
    scan.scan_ops_assets()
    scan.apply_rules()
    scan.build_graph()
    return scan, sc.build_baseline(scan)


def _pairs(scan, kind):
    return {(e["from"], e["to"]) for e in scan.edges if e["kind"] == kind}


@pytest.fixture()
def scanned(tmp_path, monkeypatch):
    rules = _repo(tmp_path)
    scan, baseline = _run(tmp_path, rules, monkeypatch)
    return scan, baseline


# ── invokes ────────────────────────────────────────────────────────────


def test_shell_script_invokes_edges(scanned):
    scan, _ = scanned
    inv = _pairs(scan, "invokes")
    src = "scripts/run_nightly.sh"
    assert (src, "app/jobs/nightly.py") in inv          # python3 -m app.jobs.nightly
    assert (src, "scripts/helper.sh") in inv            # bash scripts/helper.sh
    assert (src, "scripts/report.py") in inv            # "$ROOT/scripts/report.py" ($ROOT 확장)
    assert ("deploy.sh", "scripts/helper.sh") in inv
    # 호출이 아닌 줄(ssh 원격, echo/cat 인자, [ -f ] 검사)은 간선이 아니다
    for dst in ("scripts/remote_only.sh", "scripts/not_a_call.sh",
                "scripts/also_not_a_call.sh", "scripts/guard.sh"):
        assert (src, dst) not in inv
    # 저장소에 없는 스크립트는 간선 없이 해석 실패로만 센다
    assert not any(e["to"].endswith("missing_script.sh") for e in scan.edges)


def test_python_script_invokes_edges(scanned):
    scan, _ = scanned
    inv = _pairs(scan, "invokes")
    assert ("scripts/report.py", "scripts/helper.sh") in inv          # subprocess.run([...])
    assert ("scripts/report.py", "app/jobs/nightly.py") in inv        # sys.executable -m
    assert ("scripts/report.py", "scripts/guard.sh") in inv           # os.system + 배포 경로 접두
    assert ("scripts/report.py", "scripts/helper_py.py") in inv       # runpy.run_path


def test_heredoc_python_imports_become_invokes(scanned):
    scan, _ = scanned
    assert ("scripts/run_nightly.sh", "app/jobs/nightly.py") in _pairs(scan, "invokes")
    findings = oa.analyze_shell_script(
        "python3 - <<'PY'\nfrom app.jobs import nightly\nPY\ncat <<EOF\nbash scripts/helper.sh\nEOF\n",
        "scripts/x.sh",
        oa.Resolver(oa.RepoPaths(["scripts/helper.sh", "app/jobs/nightly.py", "app/jobs/__init__.py",
                                  "app/__init__.py"], []),
                    oa.PyIndex(["app/jobs/nightly.py", "app/jobs/__init__.py", "app/__init__.py"], [""])),
    )
    got = {f for f, _l, _v in findings.files}
    assert "app/jobs/nightly.py" in got
    assert "scripts/helper.sh" not in got        # 일반 heredoc 본문은 코드가 아니다


# ── runs ───────────────────────────────────────────────────────────────


def test_systemd_timer_service_script_chain(scanned):
    scan, baseline = scanned
    runs = _pairs(scan, "runs")
    assert ("scripts/nightly.timer", "scripts/nightly.service") in runs     # 암묵 Unit
    assert ("scripts/backup.timer", "scripts/backup.service") in runs       # Unit= 명시
    assert ("scripts/nightly.service", "app/jobs/nightly.py") in runs       # python -m
    assert ("scripts/nightly.service", "scripts/helper.sh") in runs         # ExecStartPost 절대경로
    assert ("scripts/backup.service", "scripts/guard.sh") in runs           # bash -lc 'cd … && bash …'
    # 존재하지 않는 서비스를 가리키는 timer 는 간선이 없고 집계에 남는다
    assert not any(f == "scripts/orphan.timer" for f, _ in runs)
    sysd = baseline["scope"]["ops_assets"]["systemd"]
    assert sysd["timers"] == 3 and sysd["timer_links"] == 2 and sysd["timer_unresolved"] == 1
    nodes = {n["id"]: n for n in scan.nodes}
    assert nodes["scripts/nightly.timer"]["kind"] == "systemd_unit"
    assert nodes["scripts/nightly.timer"]["schedule"] == "OnCalendar=*-*-* 03:00:00"
    # 저장소 밖 실행 파일은 추측하지 않는다
    assert not any("not-in-repo" in e["to"] for e in scan.edges)
    assert baseline["scope"]["ops_assets"]["external_refs"] >= 1


def test_ambiguous_timer_target_is_not_linked():
    units = {
        "a/x.timer": oa.unit_info("a/x.timer", "[Timer]\nOnCalendar=daily\n"),
        "b/x.service": oa.unit_info("b/x.service", "[Service]\nExecStart=/bin/true\n"),
        "c/x.service": oa.unit_info("c/x.service", "[Service]\nExecStart=/bin/true\n"),
    }
    edges = oa.EdgeSet()
    stats = oa.link_systemd(units, oa.Resolver(oa.RepoPaths(units, []), oa.PyIndex([], [""])), edges)
    assert edges.edges == []
    assert stats["timer_ambiguous"] == 1


def test_crontab_file_runs_edges_with_schedule(scanned):
    scan, baseline = scanned
    runs = {(e["from"], e["to"]): e for e in scan.edges if e["kind"] == "runs"}
    assert runs[("scripts/crontab.txt", "scripts/helper.sh")]["schedule"] == "*/5 * * * *"
    assert ("scripts/crontab.txt", "scripts/report.py") in runs
    # 주석 처리된 일정은 간선이 아니다(세기만 한다)
    assert ("scripts/crontab.txt", "scripts/guard.sh") not in runs
    cron = baseline["scope"]["ops_assets"]["crontab"]
    assert cron == {"files": 1, "entries": 3, "resolved": 2, "commented_entries": 1}
    node = {n["id"]: n for n in scan.nodes}["scripts/crontab.txt"]
    assert node["kind"] == "crontab" and node["entries"] == 3


def test_parse_crontab_system_format_has_user_field():
    parsed = oa.parse_crontab("MAILTO=x\n30 2 * * 1 root /opt/a.sh run\n", user_field=True)
    assert parsed["entries"][0]["user"] == "root"
    assert parsed["entries"][0]["command"] == "/opt/a.sh run"
    assert parsed["env"] == {"MAILTO": "x"}


# ── proxies ────────────────────────────────────────────────────────────


def test_nginx_location_proxies_edges(scanned):
    scan, baseline = scanned
    ids = {n["id"]: n for n in scan.nodes}
    chat = "nginx:nginx-test.conf::example.test@80::/api/v1/chat/"
    assert ids[chat]["kind"] == "nginx_location" and ids[chat]["handler"] == "proxy"
    prox = _pairs(scan, "proxies")
    assert (chat, "ns:/api/v1/chat") in prox                  # API 네임스페이스
    assert (chat, "upstream:api_pool") in prox                # 이름 있는 upstream
    assert ids["upstream:api_pool"]["servers"] == ["127.0.0.1:8100", "127.0.0.1:8101"]
    # 정규식 location: 리터럴 접두(/api/v1/)만으로는 네임스페이스를 단정하지 않는다
    regex = "nginx:nginx-test.conf::example.test@80::~ ^/api/v1/(chat|other)/stream$"
    assert (regex, "upstream:api_pool") in prox
    assert not any(f == regex and t.startswith("ns:") for f, t in prox)
    # 그래프에 없는 네임스페이스는 만들어 잇지 않는다
    ghost = "nginx:nginx-test.conf::example.test@80::= /api/v1/ghost"
    assert not any(f == ghost and t.startswith("ns:") for f, t in prox)
    assert "ns:/api/v1/ghost" not in ids
    # static location 은 노드만, 간선 없음
    static = "nginx:nginx-test.conf::example.test@80::/static/"
    assert ids[static]["handler"] == "static"
    assert not any(f == static for f, _ in prox)
    ng = baseline["scope"]["ops_assets"]["nginx"]
    assert ng["locations"] == 6 and ng["proxied"] == 5
    assert ng["ns_linked"] == 1 and ng["ns_missing"] == 1 and ng["prefix_only"] == 1


def test_nginx_parser_handles_comments_quotes_and_nesting():
    text = textwrap.dedent("""
        # location /commented { proxy_pass http://nope; }
        http {
          server {
            server_name a.test; listen 443 ssl;
            location ^~ /api/v1/x/ { if ($a) { return 403; } proxy_pass "http://u/api/v1/x/"; }
          }
        }
    """)
    parsed = oa.nginx_locations("n.conf", text)
    assert [l["path"] for l in parsed["locations"]] == ["/api/v1/x/"]
    assert parsed["locations"][0]["proxy_pass"]["uri"] == "/api/v1/x/"


# ── compose ────────────────────────────────────────────────────────────


def test_compose_service_edges(scanned):
    scan, baseline = scanned
    ids = {n["id"]: n for n in scan.nodes}
    api = "compose:docker-compose.yml:api"
    assert ids[api]["kind"] == "compose_service"
    assert (api, "image:aads-server:local") in _pairs(scan, "uses_image")
    assert ("compose:docker-compose.yml:db", "image:postgres:15") in _pairs(scan, "uses_image")
    assert (api, "Dockerfile") in _pairs(scan, "builds")
    runs = _pairs(scan, "runs")
    assert (api, "app/jobs/nightly.py") in runs
    assert ("compose:docker-compose.yml:worker", "scripts/run_nightly.sh") in runs
    # 저장소 밖 컨텍스트의 Dockerfile 은 만들어 잇지 않는다
    assert not any(f.endswith(":dash") and e["kind"] == "builds"
                   for e in scan.edges for f in [e["from"]])
    cp = baseline["scope"]["ops_assets"]["compose"]
    assert cp["services"] == 4 and cp["images"] == 3 and cp["builds"] == 1
    assert ids["image:aads-server:local"]["kind"] == "image"
    # 같은 이미지를 쓰는 두 서비스가 노드 하나를 공유한다
    assert sum(1 for n in scan.nodes if n["id"] == "image:aads-server:local") == 1


def test_compose_invalid_yaml_is_recorded_not_fatal(tmp_path, monkeypatch):
    rules = _repo(tmp_path)
    _w(tmp_path, "docker-compose.broken.yml", "services:\n  a: [unclosed\n")
    scan, baseline = _run(tmp_path, rules, monkeypatch)
    assert baseline["scope"]["ops_assets"]["compose"]["errors"] == 1
    assert any("docker-compose.broken.yml" in e for e in baseline["scope"]["ops_assets"]["errors"])


# ── 통계·커버리지 ──────────────────────────────────────────────────────


def test_stats_and_coverage(scanned):
    scan, baseline = scanned
    ops = baseline["scope"]["ops_assets"]
    assert set(ops["edges"]) == {"invokes", "runs", "proxies", "uses_image", "builds"}
    assert all(v > 0 for v in ops["edges"].values())
    assert ops["nodes"] == {
        "script": 7, "systemd_unit": 5, "crontab": 1, "nginx_location": 6,
        "compose_service": 4, "image": 2, "upstream": 3, "dockerfile": 1,
    }
    assert ops["schedule_nodes"] == 3 + 1                     # timer 3 + crontab 파일 1
    assert ops["scripts"] == {"total": 9, "sh": 7, "py": 2}
    cov = baseline["scope"]["coverage"]
    assert cov["scripts_total"] == 9 and cov["scripts_as_nodes"] == 9 and cov["script_node_ratio"] == 1.0
    assert cov["schedule_nodes"] == 4
    assert baseline["scope"]["edges_by_kind"]["proxies"] == ops["edges"]["proxies"]
    json.dumps(baseline)


def test_s2_does_not_change_existing_node_kinds_and_dedupes(scanned, tmp_path, monkeypatch):
    on, _ = scanned
    rules = tmp_path / "rules.yml"
    off, _ = _run(tmp_path, rules, monkeypatch, s2="0")
    on_nodes = {n["id"]: n for n in on.nodes}
    for node in off.nodes:
        assert on_nodes[node["id"]]["kind"] == node["kind"]
    assert len(on.edges) == len({(e["from"], e["to"], e["kind"]) for e in on.edges})
    assert on.findings == off.findings
    # .py 스크립트는 module 노드 + role=script (S1 스냅샷과 kind 가 같다)
    assert on_nodes["scripts/report.py"]["kind"] == "module"
    assert on_nodes["scripts/report.py"]["role"] == "script"


# ── 플래그 off = S1 스냅샷 ─────────────────────────────────────────────


def test_flag_off_equals_s1_snapshot(tmp_path, monkeypatch):
    rules = _repo(tmp_path)
    off, off_base = _run(tmp_path, rules, monkeypatch, s2="0")
    assert off.ops_assets_enabled is False

    # 섹션이 없는 규칙(= S2 이전 규칙 파일)과 결과가 바이트 단위로 같다
    legacy = tmp_path / "legacy_rules.yml"
    text = RULES
    legacy.write_text(text[:text.index("ops_assets:")] + text[text.index("sql:"):], encoding="utf-8")
    ref, ref_base = _run(tmp_path, legacy, monkeypatch, s2="1")
    assert ref.ops_assets_enabled is False

    def dump(scan, base):
        scope = dict(base["scope"])
        return json.dumps({"nodes": scan.nodes, "edges": scan.edges, "scope": scope,
                           "findings": scan.findings, "unresolved": scan.unresolved},
                          sort_keys=True, ensure_ascii=False)

    assert dump(off, off_base) == dump(ref, ref_base)
    assert "ops_assets" not in off_base["scope"]
    assert "scripts_total" not in off_base["scope"]["coverage"]
    assert not {e["kind"] for e in off.edges} & {"invokes", "runs", "proxies", "uses_image", "builds"}
    assert not {n["kind"] for n in off.nodes} & set(oa.NODE_KINDS)
    assert not any(n.get("role") == "script" for n in off.nodes)

    # 켜면 off 결과의 상위집합: 기존 노드·간선은 하나도 빠지거나 바뀌지 않는다
    on, _ = _run(tmp_path, rules, monkeypatch)
    on_nodes = {n["id"]: n for n in on.nodes}
    for node in off.nodes:
        got = on_nodes[node["id"]]
        assert all(got.get(k) == v for k, v in node.items())
    assert all(e in on.edges for e in off.edges)


def test_flag_off_with_s1_off_matches_pre_s1(tmp_path, monkeypatch):
    rules = _repo(tmp_path)
    scan, base = _run(tmp_path, rules, monkeypatch, s1="0", s2="0")
    assert {e["kind"] for e in scan.edges} <= {"queries", "calls", "owns", "mounts"}
    assert set(base["scope"]) == {
        "app_py_files", "router_dir_files", "router_modules", "include_router_calls",
        "mounted_routes", "namespaces", "frontend_files", "frontend_calls_resolved",
        "sql_tables_referenced", "graph_nodes", "graph_edges", "coverage",
    }


def test_s2_alone_still_reports_edges_by_kind(tmp_path, monkeypatch):
    rules = _repo(tmp_path)
    scan, base = _run(tmp_path, rules, monkeypatch, s1="0", s2="1")
    assert base["scope"]["edges_by_kind"]["runs"] > 0
    assert "code_deps" not in base["scope"]


def test_ops_assets_failure_does_not_abort_scan(tmp_path, monkeypatch):
    rules = _repo(tmp_path)

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(sc.ops_assets, "build", boom)
    scan, base = _run(tmp_path, rules, monkeypatch)
    assert scan.ops_assets_stats["error"].startswith("RuntimeError")
    assert any("ops_assets 추출 실패" in w for w in scan.warnings)
    assert not {e["kind"] for e in scan.edges} & {"invokes", "proxies", "uses_image", "builds"}
    json.dumps(base)


def test_bad_file_is_isolated(tmp_path, monkeypatch):
    rules = _repo(tmp_path)
    _w(tmp_path, "scripts/bad.service", "[Service\nExecStart=\x00\n")
    _w(tmp_path, "scripts/bad.py", "def (:\n")
    scan, base = _run(tmp_path, rules, monkeypatch)
    assert base["scope"]["ops_assets"]["edges"]["runs"] > 0


def test_section_missing_means_no_extraction(tmp_path, monkeypatch):
    rules = _repo(tmp_path)
    text = RULES
    rules.write_text(text[:text.index("ops_assets:")] + text[text.index("sql:"):], encoding="utf-8")
    scan, base = _run(tmp_path, rules, monkeypatch)
    assert scan.ops_assets_enabled is False and "ops_assets" not in base["scope"]


def test_flag_parsing():
    assert oa.enabled({}) is True
    assert oa.enabled({oa.FLAG_ENV: "1"}) is True
    for off in ("0", "false", "OFF", "no"):
        assert oa.enabled({oa.FLAG_ENV: off}) is False


# ── 경로 해석 ──────────────────────────────────────────────────────────


def test_repo_paths_prefix_mapping_and_escape():
    paths = oa.RepoPaths(["scripts/a.sh", "app/m.py"], ["/root/aads/aads-server/", "/app"])
    assert paths.resolve_file("/root/aads/aads-server/scripts/a.sh", [""]) == ("scripts/a.sh", "repo")
    assert paths.resolve_file("/app/app/m.py", [""]) == ("app/m.py", "repo")
    assert paths.resolve_file("/usr/bin/true", [""])[1] == "external"
    assert paths.resolve_file("../../etc/passwd", ["scripts"])[0] is None   # 저장소 밖으로 못 나간다
    assert paths.resolve_file("$X/a.sh", [""])[1] == "dynamic"
    assert paths.resolve_file("a.sh", ["scripts", ""]) == ("scripts/a.sh", "repo")


def test_expand_vars_defaults_only():
    env = {"A": "x"}
    assert oa.expand_vars("${A}/y", env) == "x/y"
    assert oa.expand_vars("${B:-/opt}/y", env) == "/opt/y"
    assert "$" in oa.expand_vars("${B}/y", env)


def test_no_catastrophic_backtracking_on_large_script():
    body = ("echo " + "(a|b)" * 50 + "\n") * 2000 + "python3 -m app.x " + "\\\n" * 500 + "\n"
    res = oa.analyze_shell_script(body, "scripts/big.sh",
                                  oa.Resolver(oa.RepoPaths([], []), oa.PyIndex([], [""])))
    assert res.files == []


# ── 운영 관측 ──────────────────────────────────────────────────────────

TIMERS_TEXT = textwrap.dedent("""\
    NEXT                        LEFT          LAST                        PASSED       UNIT                          ACTIVATES
    Thu 2026-10-22 10:00:00 KST 4min 3s left  Thu 2026-10-22 09:00:01 KST 55min ago    aads-review-hold-sweeper.timer aads-review-hold-sweeper.service
    n/a                         n/a           n/a                         n/a          legacy.timer                  legacy.service

    2 timers listed.
    Pass --all to see loaded but inactive timers, too.
""")
CRON_TEXT = "MAILTO=ops\n*/5 * * * * cd /root/aads/aads-server && bash scripts/helper.sh\n# 0 1 * * * x.sh\n"


def _doc(**kw):
    return ob.build_observation("AADS", "h1", "2026-10-22T10:00:00+00:00",
                                {"crontab": CRON_TEXT, "timers": TIMERS_TEXT}, **kw)


def test_observation_parsers():
    timers = ob.parse_list_timers(TIMERS_TEXT)
    assert [t["unit"] for t in timers] == ["aads-review-hold-sweeper.timer", "legacy.timer"]
    assert timers[0]["activates"] == "aads-review-hold-sweeper.service"
    assert timers[0]["when"].startswith("Thu 2026-10-22 10:00:00 KST")
    cron = ob.parse_crontab_output(CRON_TEXT)
    assert cron["entries"][0]["schedule"] == "*/5 * * * *"
    assert cron["env"] == {"MAILTO": "ops"} and cron["commented_entries"] == 1


def test_observation_document_and_digest():
    doc = _doc()
    assert ob.validate_observation(doc) == []
    assert doc["schema"] == ob.SCHEMA_VERSION
    assert {k: v["ok"] for k, v in doc["command_status"].items()} == {"crontab": True, "timers": True}
    # 수집 시각·호스트가 달라도 내용이 같으면 digest 가 같다(내용 비교용)
    other = ob.build_observation("AADS", "h2", "2027-01-01T00:00:00+00:00",
                                 {"crontab": CRON_TEXT, "timers": TIMERS_TEXT})
    assert other["content_digest"] == doc["content_digest"]
    changed = ob.build_observation("AADS", "h1", "2026-10-22T10:00:00+00:00",
                                   {"crontab": CRON_TEXT + "1 1 * * * a.sh\n", "timers": TIMERS_TEXT})
    assert changed["content_digest"] != doc["content_digest"]
    tampered = json.loads(json.dumps(doc))
    tampered["timers"].append({"unit": "x.timer", "activates": "x.service", "when": "", "raw": ""})
    assert any("digest" in p for p in ob.validate_observation(tampered))


def test_observation_no_crontab_is_empty_not_failure():
    doc = ob.build_observation("AADS", "h", "t", {"crontab": "no crontab for root\n", "timers": TIMERS_TEXT})
    assert doc["command_status"]["crontab"]["ok"] is True and doc["crontab"]["entries"] == []


def test_observation_failed_command_is_unobserved_not_empty():
    doc = ob.build_observation("AADS", "h", "t", {"crontab": CRON_TEXT, "timers": None},
                               {"timers": "FileNotFoundError: systemctl"})
    assert doc["command_status"]["timers"]["ok"] is False
    assert doc["command_status"]["timers"]["error"].startswith("FileNotFoundError")
    graph = {"nodes": [{"id": "u/a.timer", "kind": "systemd_unit", "unit_type": "timer", "unit": "a.timer"}]}
    cmp = ob.compare_with_repo(doc, graph)
    assert cmp["timers_compared"] is False and cmp["repo_not_observed"] == []   # 관측 못 함 ≠ 없음


def test_observation_compare_with_repo_graph():
    doc = _doc()
    graph = {"nodes": [
        {"id": "scripts/legacy.timer", "kind": "systemd_unit", "unit_type": "timer", "unit": "legacy.timer"},
        {"id": "scripts/inactive.timer", "kind": "systemd_unit", "unit_type": "timer", "unit": "inactive.timer"},
        {"id": "scripts/x.service", "kind": "systemd_unit", "unit_type": "service", "unit": "x.service"},
    ]}
    cmp = ob.compare_with_repo(doc, graph)
    assert cmp["observed_not_in_repo"] == ["aads-review-hold-sweeper.timer"]
    assert cmp["repo_not_observed"] == ["scripts/inactive.timer"]


def test_observation_cron_refs_resolve_against_repo_files():
    refs = ob.resolve_cron_refs(_doc(), ["scripts/helper.sh"], [], ["/root/aads/aads-server/"])
    assert refs[0]["repo_files"] == ["scripts/helper.sh"]


def test_observation_insert_is_idempotent_and_injection_safe():
    doc = _doc()
    sql = ob.insert_statement(doc)
    assert "ON CONFLICT ON CONSTRAINT aag_ops_observations_event_uniq DO NOTHING" in sql
    evil = ob.build_observation("AADS", "h'; DROP TABLE x; --$aagops0$", "2026-10-22T10:00:00+00:00",
                                {"crontab": CRON_TEXT, "timers": TIMERS_TEXT})
    stmt = ob.insert_statement(evil)
    assert "$aagops0$" in stmt and "$aagops1$" in stmt        # 본문에 나오는 태그를 피해 인용한다
    with pytest.raises(ValueError):
        ob.insert_statement({"schema": "x"})


def test_collect_uses_only_allowlisted_readonly_commands():
    seen = []

    def fake(argv, timeout):
        seen.append(list(argv))
        if argv[0] == "crontab":
            return 1, "", "no crontab for root"
        return 0, TIMERS_TEXT, ""

    outputs, errors = ob.collect(fake)
    assert seen == [["crontab", "-l"], ["systemctl", "list-timers", "--all", "--no-pager"]]
    assert errors == {} and outputs["timers"] == TIMERS_TEXT
    doc = ob.build_observation("AADS", "h", "t", outputs, errors)
    assert doc["command_status"]["crontab"]["ok"] is True and doc["crontab"]["entries"] == []
    for argv in ob.COLLECT_COMMANDS.values():
        assert argv[0] in ("crontab", "systemctl")
        assert not {"restart", "start", "stop", "enable", "disable", "-e", "-r", "reload"} & set(argv)


def test_collect_records_failures():
    def fake(argv, timeout):
        if argv[0] == "systemctl":
            raise FileNotFoundError("systemctl")
        return 3, "", "boom"

    outputs, errors = ob.collect(fake)
    assert outputs == {"crontab": None, "timers": None}
    assert set(errors) == {"crontab", "timers"}


def test_collector_cli_default_is_plan_only(monkeypatch, capsys):
    def forbidden(*a, **k):
        raise AssertionError("계획 모드에서는 아무것도 실행하면 안 된다")

    monkeypatch.setattr(collect_cli.subprocess, "run", forbidden)
    monkeypatch.setattr(ob.subprocess, "run", forbidden)
    assert collect_cli.main([]) == 0
    out = capsys.readouterr().out
    assert "crontab -l" in out and "systemctl list-timers --all --no-pager" in out
    assert "aag_ops_observations" in out


def test_collector_cli_builds_document_from_files(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(collect_cli.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError))
    (tmp_path / "c.txt").write_text(CRON_TEXT, encoding="utf-8")
    (tmp_path / "t.txt").write_text(TIMERS_TEXT, encoding="utf-8")
    out = tmp_path / "obs.json"
    rc = collect_cli.main(["--project", "AADS", "--host", "h1", "--crontab-file", str(tmp_path / "c.txt"),
                           "--timers-file", str(tmp_path / "t.txt"), "--out", str(out), "--sql"])
    assert rc == 0
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert ob.validate_observation(doc) == []
    printed = capsys.readouterr().out
    assert "INSERT INTO aag_ops_observations" in printed
    assert collect_cli.main(["--project", "AADS", "--crontab-file", str(tmp_path / "c.txt")]) == 2  # --host 없음


def test_migration_is_additive_and_matches_insert_columns():
    sql = (ROOT / "migrations/20261022_aag_ops_observations.sql").read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS aag_ops_observations" in sql
    for forbidden in ("DROP ", "ALTER TABLE aag_graph", "DELETE ", "TRUNCATE"):
        assert forbidden not in sql
    stmt = ob.insert_statement(_doc())
    for col in ("project", "host", "collected_at", "schema_version", "content_digest",
                "crontab_entries", "active_timers", "command_status", "observation"):
        assert col in sql and col in stmt
    assert "aag_ops_observations_event_uniq" in sql


# ── 원격 Python 3.8 호환 ───────────────────────────────────────────────


@pytest.mark.parametrize("rel", ["tools/aag/ops_assets.py", "tools/aag/ops_observation.py"])
def test_modules_are_python38_compatible(rel):
    src = (ROOT / rel).read_text(encoding="utf-8")
    ast.parse(src, feature_version=(3, 8))
    tree = ast.parse(src)
    banned_attrs = {"removeprefix", "removesuffix", "unparse", "cache"}
    assert not any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                   and node.func.attr in banned_attrs for node in ast.walk(tree))
    assert "ANTHROPIC_API_KEY" not in src


def test_remote_sync_lists_ship_ops_assets():
    for rel, minimum in (("scripts/aag_all_projects_refresh.sh", 2), ("scripts/aag_go100_refresh.sh", 1)):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert text.count("tools/aag/ops_assets.py") >= minimum, rel
    assert "AAG_SCAN_OPS_ASSETS" in (ROOT / "scripts/aag_go100_refresh.sh").read_text(encoding="utf-8")


def test_explicit_plan_never_executes(monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        raise AssertionError("plan must not collect or load")
    monkeypatch.setattr(collect_cli.obs, "collect", forbidden)
    monkeypatch.setattr(collect_cli, "run_psql", forbidden)
    assert collect_cli.main(["--plan", "--execute", "--load"]) == 0
    assert "수집 계획" in capsys.readouterr().out


def test_python_optimization_flag_keeps_script_target():
    resolver = oa.Resolver(oa.RepoPaths(["scripts/job.py"]), oa.PyIndex(["scripts/job.py"], [""]))
    found = oa.Findings()
    resolver.tokens(["python3", "-O", "scripts/job.py"], [""], 1, "scripts/run.sh", found)
    assert [entry[0] for entry in found.files] == ["scripts/job.py"]


@pytest.mark.parametrize("command", [
    "python3 scripts/a.py --input scripts/b.py",
    "python3 -m scripts.a --input scripts/b.py",
    "python3 -c 'import scripts.a' --input scripts/b.py",
])
def test_python_arguments_are_not_invocations(command):
    files = ["scripts/a.py", "scripts/b.py"]
    resolver = oa.Resolver(oa.RepoPaths(files), oa.PyIndex(files, [""]))
    out = oa.Findings()
    resolver.shell_text(command, [""], 1, "scripts/run.sh", out)
    assert {f for f, _, _ in out.files} == {"scripts/a.py"}


@pytest.mark.parametrize("directory,expected", [
    ("/app/jobs", {"jobs/worker.sh"}),
    ("/outside", set()),
    ("$UNKNOWN", set()),
])
def test_multiline_cd_tracks_cwd(directory, expected):
    files = ["scripts/worker.sh", "jobs/worker.sh", "worker.sh"]
    resolver = oa.Resolver(oa.RepoPaths(files, ["/app/"]), oa.PyIndex([], [""]))
    out = oa.analyze_shell_script("cd " + directory + "\nbash worker.sh\n", "scripts/run.sh", resolver)
    assert {f for f, _, _ in out.files} == expected


def test_observation_return_to_previous_state_preserves_latest():
    # Exercise the generated INSERT and its migration uniqueness in a local SQL engine.
    import sqlite3
    import re
    db = sqlite3.connect(":memory:")
    migration = (ROOT / "migrations/20261022_aag_ops_observations.sql").read_text()
    unique = re.search(r"event_uniq UNIQUE \(([^)]+)\)", migration).group(1)
    db.execute("CREATE TABLE observations (project, host, collected_at, schema_version, "
               "content_digest, crontab_entries, active_timers, command_status, observation, "
               "UNIQUE (" + unique + "))")
    docs = [ob.build_observation("AADS", "host", "2026-10-22T%02d:00:00Z" % hour,
            {"crontab": cron, "timers": ""})
            for hour, cron in [(10, "0 * * * * bash a.sh"), (11, "0 * * * * bash b.sh"),
                               (12, "0 * * * * bash a.sh")]]
    for doc in docs + [docs[-1], docs[0]]:
        sql = ob.insert_statement(doc)
        values = re.findall(r"(\$aagops\d+\$)(.*?)\1", sql, re.S)
        texts = [v for _, v in values]
        db.execute("INSERT OR IGNORE INTO observations VALUES (?,?,?,?,?,?,?,?,?)",
                   texts[:5] + [len(doc["crontab"]["entries"]), len(doc["timers"])] + texts[5:])
    assert db.execute("SELECT count(*) FROM observations").fetchone()[0] == 3
    latest = db.execute("SELECT observation FROM observations ORDER BY collected_at DESC LIMIT 1").fetchone()[0]
    assert json.loads(latest) == docs[-1]
    assert docs[0]["content_digest"] == docs[-1]["content_digest"]


# ── 리뷰 반려 교정: 인자·미확정 경로·작업 디렉터리·entrypoint+command ──────────────

_REPO_FILES = ["scripts/real.sh", "scripts/data.sh", "scripts/job.py", "app/jobs/nightly.py",
               "app/jobs/__init__.py", "app/__init__.py"]


def _resolver(prefixes=("/app/",)):
    paths = oa.RepoPaths(_REPO_FILES, list(prefixes))
    return oa.Resolver(paths, oa.PyIndex([f for f in _REPO_FILES if f.endswith(".py")], [""]))


def _targets(found):
    return {f for f, _l, _v in found.files}


@pytest.mark.parametrize("command,expected", [
    ("bash scripts/real.sh scripts/data.sh", {"scripts/real.sh"}),
    ("sh -e scripts/real.sh --input scripts/data.sh", {"scripts/real.sh"}),
    ("bash -o pipefail scripts/real.sh scripts/data.sh", {"scripts/real.sh"}),
    ("./scripts/real.sh scripts/data.sh", {"scripts/real.sh"}),
    ("source scripts/real.sh scripts/data.sh", {"scripts/real.sh"}),
    ("flock -n /tmp/x.lock bash scripts/real.sh scripts/data.sh", {"scripts/real.sh"}),
    ("sudo -u bob timeout 5 scripts/real.sh scripts/data.sh", {"scripts/real.sh"}),
    ("docker exec ctr bash scripts/real.sh scripts/data.sh", {"scripts/real.sh"}),
    ("myprog scripts/data.sh", set()),
    ("timeout 5 myprog scripts/data.sh", set()),
    ("docker cp scripts/data.sh ctr:/tmp/data.sh", set()),
    ("sudo cat scripts/data.sh", set()),
])
def test_only_the_executed_target_gets_an_edge(command, expected):
    out = oa.Findings()
    _resolver().shell_text(command, [""], 1, "scripts/run.sh", out)
    assert _targets(out) == expected


def test_shell_script_arguments_are_not_invocations_in_files():
    out = oa.analyze_shell_script("bash scripts/real.sh scripts/data.sh\n", "scripts/run.sh", _resolver())
    assert _targets(out) == {"scripts/real.sh"}


def test_unknown_dynamic_prefix_never_becomes_a_repo_path():
    src = textwrap.dedent("""
        import subprocess
        subprocess.run(['bash', f'{unknown_root}/scripts/real.sh'])
        subprocess.run(['bash', unknown_root + '/scripts/real.sh'])
        subprocess.run(['bash', os.path.join(unknown_root, 'scripts/real.sh')])
        subprocess.run(f'bash {unknown_root}/scripts/real.sh', shell=True)
        os.system('bash %s/scripts/real.sh' % unknown_root)
        runpy.run_path(f'{unknown_root}/scripts/job.py')
    """)
    out = oa.analyze_python_script(src, "tools/x.py", _resolver())
    assert out.files == []
    assert [u["reason"] for u in out.unresolved].count("dynamic_path") >= 4


def test_file_anchored_paths_are_resolved_exactly():
    src = textwrap.dedent("""
        import os, subprocess, sys
        from pathlib import Path
        ROOT = Path(__file__).resolve().parents[1]
        HERE = os.path.dirname(os.path.abspath(__file__))
        subprocess.run(['bash', str(ROOT / 'scripts/real.sh')])
        subprocess.run(['bash', os.path.join(HERE, 'data.sh')])
        subprocess.run([sys.executable, Path(__file__).parent / 'job.py'])
    """)
    out = oa.analyze_python_script(src, "scripts/x.py", _resolver())
    assert _targets(out) == {"scripts/real.sh", "scripts/data.sh", "scripts/job.py"}


def test_ambiguous_name_assignment_is_not_trusted():
    src = textwrap.dedent("""
        import subprocess
        ROOT = '/__repo__'
        if x:
            ROOT = other()
        subprocess.run(['bash', ROOT + '/scripts/real.sh'])
    """)
    assert oa.analyze_python_script(src, "tools/x.py", _resolver()).files == []


@pytest.mark.parametrize("cwd,expected", [
    ("'/outside'", set()),
    ("unknown_dir", set()),
    ("os.getcwd()", set()),
    ("'/app'", {"scripts/real.sh"}),
])
def test_python_subprocess_cwd_is_honoured(cwd, expected):
    src = "import subprocess, os\nsubprocess.run(['bash', 'scripts/real.sh'], cwd=%s)\n" % cwd
    out = oa.analyze_python_script(src, "tools/x.py", _resolver())
    assert _targets(out) == expected


@pytest.mark.parametrize("workdir,expected", [
    ("/outside", set()),
    ("-/outside", set()),
    ("~", set()),
    ("%h", set()),
    ("/app", {"scripts/real.sh"}),
    ("/app/scripts", set()),
])
def test_systemd_working_directory_outside_repo_does_not_link(workdir, expected):
    unit = "[Service]\nWorkingDirectory=%s\nExecStart=/bin/bash scripts/real.sh\n" % workdir
    units = {"deploy/a.service": oa.unit_info("deploy/a.service", unit)}
    edges = oa.EdgeSet()
    oa.link_systemd(units, _resolver(), edges)
    assert {e["to"] for e in edges.edges} == expected


@pytest.mark.parametrize("workdir,expected", [
    ("/outside", set()),
    ("relative/dir", set()),
    ("${DIR}", set()),
    ("/app", {"scripts/real.sh"}),
])
def test_compose_working_dir_outside_repo_does_not_link(workdir, expected):
    text = "services:\n  s:\n    image: x\n    working_dir: %s\n    command: bash scripts/real.sh\n" % workdir
    nodes, edges = {}, oa.EdgeSet()
    oa.link_compose("docker-compose.yml", text, _resolver(), lambda r: False,
                    lambda i, k, **x: nodes.setdefault(i, k), edges)
    assert {e["to"] for e in edges.edges if e["kind"] == "runs"} == expected


def test_docker_workdir_flag_changes_relative_base():
    out = oa.Findings()
    _resolver().shell_text("docker exec -w /outside ctr bash scripts/real.sh", [""], 1, "s.sh", out)
    assert out.files == []
    out = oa.Findings()
    _resolver().shell_text("docker exec -w /app ctr bash scripts/real.sh", [""], 1, "s.sh", out)
    assert _targets(out) == {"scripts/real.sh"}


@pytest.mark.parametrize("entrypoint,command,expected_field", [
    ("[python3]", "['-m', 'app.jobs.nightly']", "entrypoint+command"),
    ("python3 -m app.jobs.nightly", "[]", "entrypoint"),
    ("[]", "['python3', '-m', 'app.jobs.nightly']", "command"),
    ("['/bin/sh', '-c']", "['python3 -m app.jobs.nightly']", "entrypoint+command"),
])
def test_compose_entrypoint_and_command_form_one_argv(entrypoint, command, expected_field):
    text = "services:\n  s:\n    image: x\n    entrypoint: %s\n    command: %s\n" % (entrypoint, command)
    nodes, edges = {}, oa.EdgeSet()
    stats = oa.link_compose("docker-compose.yml", text, _resolver(), lambda r: False,
                            lambda i, k, **x: nodes.setdefault(i, k), edges)
    runs = [e for e in edges.edges if e["kind"] == "runs"]
    assert [(e["from"], e["to"]) for e in runs] == [("compose:docker-compose.yml:s", "app/jobs/nightly.py")]
    assert runs[0]["field"] == expected_field and stats["runs"] == 1


def test_compose_script_entrypoint_makes_command_plain_arguments():
    text = ("services:\n  s:\n    image: x\n    entrypoint: ['/app/scripts/real.sh']\n"
            "    command: ['bash', 'scripts/data.sh']\n")
    nodes, edges = {}, oa.EdgeSet()
    oa.link_compose("docker-compose.yml", text, _resolver(), lambda r: False,
                    lambda i, k, **x: nodes.setdefault(i, k), edges)
    assert {e["to"] for e in edges.edges if e["kind"] == "runs"} == {"scripts/real.sh"}


def test_compose_command_without_interpreter_is_recorded_as_unresolved():
    text = "services:\n  s:\n    image: x\n    command: ['-m', 'app.jobs.nightly']\n"
    edges = oa.EdgeSet()
    stats = oa.link_compose("docker-compose.yml", text, _resolver(), lambda r: False,
                            lambda i, k, **x: None, edges)
    assert [e for e in edges.edges if e["kind"] == "runs"] == []
    assert [u["reason"] for u in stats["unresolved"]] == ["image_entrypoint_unknown"]


def test_uvicorn_app_target_is_still_found_after_python_m():
    for command in ("uvicorn app.jobs.nightly:app --port 1", "python3 -m uvicorn app.jobs.nightly:app"):
        out = oa.Findings()
        _resolver().shell_text(command, [""], 1, "s.sh", out)
        assert _targets(out) == {"app/jobs/nightly.py"}, command
