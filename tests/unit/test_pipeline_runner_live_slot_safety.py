"""Read-only AADS live-slot contract with real Bash/Git and explicit probe fixtures."""
import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = (ROOT / "scripts/pipeline-runner.sh").read_text()
HELPERS = ("aads_live_route_snapshot", "aads_live_container_snapshot", "aads_live_slot_snapshot")


def function(name, source=SCRIPT):
    start = source.index(f"{name}() {{")
    return source[start:source.index("\n}\n", start) + 3]


def helper_source():
    return "".join(function(name) for name in HELPERS)


DOCKER = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
w = Path(os.environ["WORK"])
x = json.loads((w / "world.json").read_text())
a = sys.argv[1:]
if os.environ.get("ACTIVE_DIGEST"):
    for slot in x["slots"].values(): slot["digest"] = os.environ["ACTIVE_DIGEST"]
with (w / "probes.log").open("a") as f: f.write("docker " + " ".join(a) + "\n")
if x.get("inspect_fail"): sys.exit(1)
if a[:2] == ["image", "inspect"]:
    ref = a[2]
    for s in x["slots"].values():
        if s["image"] == ref:
            print(x.get("tag_digest", s["digest"])); sys.exit(0)
    sys.exit(1)
if a[0] != "inspect" or a[1] not in x["slots"]: sys.exit(1)
s = x["slots"][a[1]]
if not s["image"]: sys.exit(1)
fmt = a[a.index("--format") + 1]
if fmt == "{{.Config.Image}}": print(s["image"])
elif fmt == "{{.Image}}": print(s["digest"])
else:
    print("|".join(str(s[k]) for k in ("id", "running", "status", "paused", "restarting", "health", "digest", "image", "started", "restarts", "pid")))
'''
CURL = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
w=Path(os.environ["WORK"]); p=w/"world.json"; x=json.loads(p.read_text())
with (w/"probes.log").open("a") as f: f.write("curl " + " ".join(sys.argv[1:]) + "\n")
x["health_calls"]=x.get("health_calls",0)+1
if x["health_calls"] == 2:
    change=x.get("after_health",{})
    for k,v in change.get("slot",{}).items(): x["slots"][x["active_container"]][k]=v
    if change.get("route"):
        (w/"upstream.conf").write_text("upstream aads_api { server 127.0.0.1:8102; }\n")
p.write_text(json.dumps(x))
if x.get("health_fail") or os.environ.get("HEALTH_OK", "1") != "1": sys.exit(28)
print(x.get("health_body", '{"status":"ok","graph_ready":true}'))
'''


def install_case(work, *, blue="", green="", active=None, digest=None):
    """Probe schema follows Docker fields; helpers still parse and enforce every field."""
    work = Path(work)
    bindir = work / "bin"
    bindir.mkdir(exist_ok=True)
    state = work / "state"
    state.mkdir(exist_ok=True)
    active = active or ("aads-server-green" if green else "aads-server")
    port = "8102" if active.endswith("-green") else "8100"
    (state / ".active_port").write_text(port + "\n")
    (state / ".active_container").write_text(active + "\n")
    (work / "upstream.conf").write_text(f"upstream aads_api {{ server 127.0.0.1:{port}; }}\n")
    slots = {}
    for i, (name, image) in enumerate((("aads-server", blue), ("aads-server-green", green))):
        slots[name] = dict(id=str(i + 1) * 64, running="true", status="running", paused="false",
                           restarting="false", health="healthy", digest=digest or "sha256:" + "a" * 64,
                           image=image, started="2026-10-06T00:00:00Z", restarts="0", pid=str(100 + i))
    (work / "world.json").write_text(json.dumps(dict(slots=slots, active_container=active)))
    for name, body in (("docker", DOCKER), ("curl", CURL)):
        f = bindir / name
        f.write_text(body)
        f.chmod(0o755)
    return {"PATH": str(bindir) + ":" + os.environ.get("PATH", "/usr/bin:/bin"),
            "WORK": str(work), "AADS_DEPLOY_STATE_DIR": str(state),
            "AADS_UPSTREAM_CONF": str(work / "upstream.conf"), "AADS_API_URL": "http://aads.fixture"}


def edit_world(work, **changes):
    p = Path(work) / "world.json"
    x = json.loads(p.read_text())
    slot = changes.pop("slot", {})
    x["slots"][x["active_container"]].update(slot)
    x.update(changes)
    p.write_text(json.dumps(x))


@pytest.fixture
def case(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    def git(*args):
        return subprocess.check_output(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args], text=True).strip()
    git("init", "-q")
    shas = []
    for i in range(3):
        (repo / "f").write_text(str(i)); git("add", "f"); git("commit", "-qm", str(i)); shas.append(git("rev-parse", "HEAD"))
    work = tmp_path / "probe"
    work.mkdir()
    env = install_case(work, blue=f"aads-server:{shas[2][:12]}")
    script = work / "functions.sh"
    script.write_text("set -uo pipefail\ndb_exec() { echo \"${RUN_ROW:-}\"; }\n" + helper_source() + function("aads_release_live_verdict"))
    return work, repo, shas, env, script


def verdict(case, row=""):
    work, repo, shas, env, script = case
    return subprocess.run(["bash", "-c", 'source "$1"; aads_release_live_verdict "$2" "$3"', "bash", str(script), shas[1], str(repo)],
                          env={**os.environ, **env, "RUN_ROW": row}, text=True, capture_output=True, timeout=15)


def test_healthy_routed_descendant_is_live(case):
    r = verdict(case)
    assert r.returncode == 0 and r.stdout.strip() == "live:aads-server"
    log = (case[0] / "probes.log").read_text()
    assert log.count("curl ") == 2 and log.count("docker image inspect") == 2
    assert "--connect-timeout 2 --max-time 4" in log


@pytest.mark.parametrize("slot", [
    {"running": "false", "status": "created", "pid": "0"},
    {"running": "false", "status": "exited", "pid": "0"},
    {"status": "restarting"}, {"paused": "true"}, {"restarting": "true"},
    {"health": "unhealthy"}, {"health": "starting"}, {"health": "missing"},
    {"id": ""}, {"digest": ""}, {"image": "aads-server:latest"}, {"pid": "0"},
])
def test_unusable_container_cannot_be_live(case, slot):
    edit_world(case[0], slot=slot)
    r = verdict(case)
    assert r.returncode == 1 and r.stdout.strip() == "not_live:none:none"
    assert "AADS_LIVE_UNKNOWN:container" in r.stderr


@pytest.mark.parametrize("changes", [
    {"inspect_fail": True}, {"tag_digest": "sha256:" + "b" * 64},
    {"health_fail": True}, {"health_body": "not-json"},
    {"health_body": '{"status":"initializing","graph_ready":false}'},
    {"health_body": '{"status":"ok","graph_ready":false}'},
    {"health_body": "null"},
    {"after_health": {"slot": {"id": "3" * 64}}},
    {"after_health": {"slot": {"started": "2026-10-06T00:01:00Z", "pid": "222"}}},
    {"after_health": {"slot": {"health": "unhealthy"}}},
    {"after_health": {"route": True}},
])
def test_unknown_or_changing_evidence_fails_closed(case, changes):
    edit_world(case[0], **changes)
    r = verdict(case)
    assert r.returncode == 1 and r.stdout.strip() == "not_live:none:none"


@pytest.mark.parametrize("route", [
    "", "upstream other { server 127.0.0.1:8100; }",
    "upstream aads_api { server 127.0.0.1:8100; server 127.0.0.1:8102; }",
    "upstream aads_api { server 127.0.0.1:8102; }",
    "upstream aads_api { server 127.0.0.1:8100 down; }",
    "upstream aads_api { server 10.0.0.1:8100; }",
])
def test_unsupported_route_is_not_live(case, route):
    (case[0] / "upstream.conf").write_text(route)
    r = verdict(case)
    assert r.returncode == 1 and r.stdout.strip() == "not_live:none:none"


def test_only_standby_contains_sha_does_not_pass(case):
    work, _, shas, _, _ = case
    p = work / "world.json"; x = json.loads(p.read_text())
    x["slots"]["aads-server"]["image"] = f"aads-server:{shas[0][:12]}"
    x["slots"]["aads-server-green"]["image"] = f"aads-server-green:{shas[2][:12]}"
    p.write_text(json.dumps(x))
    assert verdict(case).returncode == 1


def test_missing_marker_is_unknown(case):
    (case[0] / "state/.active_container").unlink()
    assert verdict(case).returncode == 1


@pytest.mark.parametrize("status", ["queued", "running", "verifying", "syncing_standby"])
def test_unhealthy_slot_preserves_queued_contract(case, status):
    edit_world(case[0], slot={"health": "unhealthy"})
    r = verdict(case, "42|" + status)
    assert r.returncode == 2 and r.stdout.strip() == "queued:42:" + status


def test_blocked_fallback_stays_not_live(case):
    edit_world(case[0], slot={"running": "false"})
    r = verdict(case, "42|blocked")
    assert r.returncode == 1 and r.stdout.strip() == "not_live:42:blocked"


@pytest.mark.parametrize("target", ["upstream.conf", "state/.active_port", "state/.active_container"])
def test_fifo_route_files_fail_without_waiting(case, target):
    import time
    path = case[0] / target
    path.unlink()
    os.mkfifo(path)
    before = time.monotonic()
    r = verdict(case)
    assert r.returncode == 1 and "AADS_LIVE_UNKNOWN:route" in r.stderr
    assert time.monotonic() - before < 3


def test_oversized_route_file_fails_closed(case):
    (case[0] / "upstream.conf").write_text(" " * 1048577)
    assert verdict(case).returncode == 1
