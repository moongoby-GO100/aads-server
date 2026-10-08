"""deploy/acct-egress/acct_egress_proxy.sh: allow-list parsing and rendered tinyproxy config (no docker involved)."""

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "deploy/acct-egress/acct_egress_proxy.sh"


def render(tmp_path, allow=None, extra_env=None):
    out = tmp_path / "out"
    env = {k: v for k, v in os.environ.items() if not k.startswith("ACCT_EGRESS_")}
    if allow is not None:
        env["ACCT_EGRESS_ALLOW"] = allow
    env.update(extra_env or {})
    proc = subprocess.run(["bash", str(SCRIPT), "render", str(out)], capture_output=True, text=True, env=env)
    return proc, out


def test_script_has_valid_bash_syntax():
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0


def test_default_allows_only_api_clobe_ai_443(tmp_path):
    proc, out = render(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert (out / "allow.list").read_text() == "api.clobe.ai\n"
    assert (out / "allow.filter").read_text() == "^api\\.clobe\\.ai$\n"
    conf = (out / "tinyproxy.conf").read_text()
    assert "ConnectPort 443" in conf and "FilterDefaultDeny Yes" in conf
    assert "FilterURLs Off" in conf and "FilterType ere" in conf
    assert conf.count("ConnectPort") == 1 and "Allow " not in conf


def test_list_is_extendable_normalised_and_deduplicated(tmp_path):
    proc, out = render(tmp_path, "API.clobe.ai:443, mcp.clobe.ai  api.clobe.ai")
    assert proc.returncode == 0, proc.stderr
    assert (out / "allow.list").read_text().splitlines() == ["api.clobe.ai", "mcp.clobe.ai"]
    assert (out / "allow.filter").read_text().splitlines() == ["^api\\.clobe\\.ai$", "^mcp\\.clobe\\.ai$"]


@pytest.mark.parametrize(
    "allow",
    [
        "   ",
        "api.clobe.ai:80",
        "api.clobe.ai:8443",
        "*.clobe.ai",
        "clobe",
        "1.2.3.4",
        "1.2.3.4:443",
        "api.clobe.ai/path",
        "https://api.clobe.ai",
        "api.clobe.ai;rm",
        "-bad.example.com",
        "exa mple.com:443:1",
    ],
)
def test_bad_entries_fail_closed_and_write_nothing(tmp_path, allow):
    proc, out = render(tmp_path, allow)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert not (out / "tinyproxy.conf").exists()


def test_one_bad_entry_rejects_the_whole_list(tmp_path):
    proc, out = render(tmp_path, "api.clobe.ai:443,evil.example.com:22")
    assert proc.returncode == 2 and not out.exists()


@pytest.mark.parametrize(
    "env",
    [
        {"ACCT_EGRESS_OUT_NET": "acct_egress"},
        {"ACCT_EGRESS_OUT_NET": "acct_net"},
        {"ACCT_EGRESS_NAME": "some-other-container"},
        {"ACCT_EGRESS_CONF_DIR": "relative/dir"},
    ],
)
def test_names_that_could_touch_other_things_are_refused(tmp_path, env):
    proc, _ = render(tmp_path, None, env)
    assert proc.returncode == 2


def test_up_uses_a_fake_docker_with_isolation_flags_and_never_the_temporary_network(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "docker.log"
    docker = bindir / "docker"
    docker.write_text(
        "#!/bin/bash\n"
        'echo "$*" >> "$DOCKER_LOG"\n'
        'case "$1 $2" in\n'
        '  "network inspect") if [[ $* == *acct_egress_out* ]]; then exit 1; fi; echo true ;;\n'
        '  "image inspect") exit 0 ;;\n'
        "esac\n"
    )
    docker.chmod(0o755)
    conf = tmp_path / "conf"
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith("ACCT_EGRESS_")},
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "DOCKER_LOG": str(log),
        "ACCT_EGRESS_CONF_DIR": str(conf),
    }
    proc = subprocess.run(["bash", str(SCRIPT), "up"], capture_output=True, text=True, env=env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    lines = log.read_text().splitlines()
    run = next(x for x in lines if x.startswith("run "))
    assert "--network acct_egress_out" in run and "--cap-drop ALL" in run and "--read-only" in run
    assert "-p " not in run and "--publish" not in run
    assert any(x.startswith("network create") and x.endswith("acct_egress_out") for x in lines)
    assert "network connect acct_net acct-egress-proxy" in lines
    assert not any("acct_egress " in x + " " and "acct_egress_out" not in x for x in lines)
    assert (conf / "tinyproxy.conf").exists()
    assert "http://acct-egress-proxy:8888" in proc.stdout


def test_up_refuses_a_non_internal_internal_network(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    docker = bindir / "docker"
    docker.write_text('#!/bin/bash\n[[ "$1 $2" == "network inspect" ]] && echo false\nexit 0\n')
    docker.chmod(0o755)
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith("ACCT_EGRESS_")},
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "ACCT_EGRESS_CONF_DIR": str(tmp_path / "conf"),
    }
    proc = subprocess.run(["bash", str(SCRIPT), "up"], capture_output=True, text=True, env=env)
    assert proc.returncode == 12
