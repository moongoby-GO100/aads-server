import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "scripts/aads_active_slot_state.sh"
DEPLOY = ROOT / "deploy.sh"
WATCHDOG = ROOT / "scripts/aads_api_watchdog.sh"
MANUAL_SWITCH = ROOT / "scripts/bluegreen_switch.sh"


def _env(tmp_path: Path) -> dict[str, str]:
    state = tmp_path / "state"
    state.mkdir()
    upstream = tmp_path / "aads-upstream.conf"
    upstream.write_text(
        "upstream aads_api {\n"
        "    server 127.0.0.1:8100 max_fails=1 fail_timeout=10s;\n"
        "    server 127.0.0.1:8102 max_fails=1 fail_timeout=10s backup;\n"
        "}\n"
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    docker.write_text("#!/usr/bin/env bash\necho true\n")
    docker.chmod(0o755)
    return {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "AADS_DEPLOY_STATE_DIR": str(state),
        "AADS_UPSTREAM_CONF": str(upstream),
        "AADS_NGINX_LOCK": str(tmp_path / "nginx.lock"),
        "AADS_CONTROL_AUDIT_LOG": str(tmp_path / "audit.jsonl"),
    }


def _run(env: dict[str, str], *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        env=env,
        check=check,
        text=True,
        capture_output=True,
    )


def test_writer_authorizes_marker_pair(tmp_path: Path):
    env = _env(tmp_path)
    _run(env, "write", "8100", "aads-server", "pytest", "initial")

    state = Path(env["AADS_DEPLOY_STATE_DIR"])
    assert (state / ".active_port").read_text().strip() == "8100"
    assert (state / ".active_container").read_text().strip() == "aads-server"
    authorization = (state / ".active_slot_authorization").read_text()
    assert "fingerprint=" in authorization
    assert "actor=pytest" in authorization
    assert _run(env, "check", "pytest-guard").returncode == 0


def test_cutover_preserves_pinned_inodes_and_rollback_visibility(tmp_path: Path):
    env = _env(tmp_path)
    _run(env, "write", "8100", "aads-server", "pytest")
    state = Path(env["AADS_DEPLOY_STATE_DIR"])
    # Hard links model the inode pinned by a Docker single-file bind mount.
    aliases = {}
    for name in (".active_port", ".active_container"):
        alias = tmp_path / (name + ".mounted")
        os.link(state / name, alias)
        aliases[name] = alias
    upstream = Path(env["AADS_UPSTREAM_CONF"])
    original = upstream.read_text()
    upstream.write_text(original.replace("8100", "SWAP").replace("8102", "8100").replace("SWAP", "8102"))
    _run(env, "write", "8102", "aads-server-green", "pytest")
    assert aliases[".active_port"].read_text().strip() == "8102"
    assert aliases[".active_container"].read_text().strip() == "aads-server-green"
    for name, alias in aliases.items():
        assert alias.stat().st_ino == (state / name).stat().st_ino
    upstream.write_text(original)
    _run(env, "write", "8100", "aads-server", "pytest")
    assert aliases[".active_port"].read_text().strip() == "8100"
    assert aliases[".active_container"].read_text().strip() == "aads-server"
    assert _run(env, "check", "pytest-guard").returncode == 0


def test_container_marker_gate_rejects_stale_mount(tmp_path: Path):
    script = DEPLOY.read_text()
    function = "verify_container_slot_markers() {" + script.split("verify_container_slot_markers() {", 1)[1].split("\n}\n", 1)[0] + "\n}\n"
    for output, expected_rc in (("8102\\naads-server-green\\n", 0), ("8100\\naads-server\\n", 1)):
        completed = subprocess.run(
            ["bash", "-c", "docker() { printf '" + output + "'; };\n" + function + "verify_container_slot_markers candidate 8102 aads-server-green"],
            capture_output=True, text=True,
        )
        assert completed.returncode == expected_rc, completed.stdout + completed.stderr


def test_guard_detects_and_repairs_untracked_rewrite(tmp_path: Path):
    env = _env(tmp_path)
    _run(env, "write", "8100", "aads-server", "pytest", "initial")
    state = Path(env["AADS_DEPLOY_STATE_DIR"])

    # Even a same-value direct rewrite changes marker metadata and invalidates
    # the writer fingerprint.
    (state / ".active_port").write_text("8100\n")
    failed = _run(env, "check", "pytest-guard", check=False)
    assert failed.returncode == 1

    _run(env, "check", "pytest-guard", "--repair")
    assert _run(env, "check", "pytest-guard").returncode == 0
    audit = Path(env["AADS_CONTROL_AUDIT_LOG"]).read_text()
    assert "unauthorized_or_untracked_write" in audit
    assert "repaired_unauthorized_mutation" in audit


def test_writer_refuses_marker_not_matching_nginx_route(tmp_path: Path):
    env = _env(tmp_path)
    failed = _run(
        env,
        "write",
        "8102",
        "aads-server-green",
        "pytest",
        "wrong route",
        check=False,
    )
    assert failed.returncode == 1
    state = Path(env["AADS_DEPLOY_STATE_DIR"])
    assert not (state / ".active_port").exists()
    assert "nginx=8100; requested=8102" in Path(env["AADS_CONTROL_AUDIT_LOG"]).read_text()


def test_all_api_slot_mutators_use_the_audited_writer():
    deploy = DEPLOY.read_text()
    watchdog = WATCHDOG.read_text()
    manual = MANUAL_SWITCH.read_text()

    assert "write_active_slot_state \"$NEW_PORT\" \"$NEW_CONTAINER\"" in deploy
    assert "echo \"$NEW_PORT\" > \"$ACTIVE_PORT_FILE\"" not in deploy
    assert "printf '%s\\n' \"$active_port\" > \"$ACTIVE_PORT_FILE\"" not in watchdog
    assert "AADS_SLOT_STATE_LOCK_HELD=true" in watchdog
    assert 'STATE_WRITER="${COMPOSE_DIR}/scripts/aads_active_slot_state.sh"' in manual
    assert "routed health failed — rollback" in manual
