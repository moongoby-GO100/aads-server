"""Behavioural tests for scripts/deploy_acct_app_cafe24.sh (ops target ACCT/app).

The script is sourced by a harness that replaces every cafe24 touchpoint (`ssh_c`, `pub_get_body`,
`pub_get_code`, `sleep`) with a small state machine kept in files. Nothing here reaches cafe24,
docker, a database or the public site; the harness records every "remote" call so the tests can
assert ordering (backup before migration), what was and was not mutated, and the exact build input.
"""

import importlib.util
import json
import os
import re
import shlex
import subprocess
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "scripts/deploy_acct_app_cafe24.sh"
MARKER = "downloadSignedContractPdf"
R8_IMAGE = "sha256:71e5e811c220efbe9bc527f36836e0cdc9638794ec01a6b1bbd5256ea5e8150e"
MIG_HRDOC = "20261001_obys_hrdoc_expiry_integrity_superseded.sql"
MIG_CLOBE = "20261003_obys_clobe_collection.sql"
MIG_CLOBE_STORE = "20261008_obys_clobe_mcp_store.sql"

HARNESS = r'''#!/bin/bash
set -euo pipefail
source "$SCRIPT_UNDER_TEST"
D="$FAKE_DIR"

sleep() { :; }

fake_op() {
  local op=$1; shift
  local sql
  case $op in
    prev_info)
      cat <<EOT
PREV_RUNNING=true
PREV_IMAGE_ID=${FAKE_PREV_IMAGE:-sha256:71e5e811c220efbe9bc527f36836e0cdc9638794ec01a6b1bbd5256ea5e8150e}
PREV_NETMODE=container:nsid123
WORKDIR=/app
REV_LABELS=org.opencontainers.image.revision=$(cat "$D/base_rev");
NS_ID=nsid123
NS_RUNNING=true
NS_IP=172.18.0.5
NS_PUBLISHED=0
PG_USER=obys
EOT
      ;;
    list_candidates) cat "$D/cands" ;;
    snapshot)
      cat "$D/unrelated"
      while IFS='|' read -r n _; do [[ -n $n ]] && echo "/$n id-$n true t0"; done < "$D/cands"
      ;;
    apache_upstream) echo "172.18.0.5 $(cat "$D/port")" ;;
    apache_switch)
      local site=$1 from=$2 to=$3
      if [[ -n ${FAKE_APACHE_FAIL:-} && $to != 8111 ]]; then echo "FAIL configtest"; return 1; fi
      if [[ $(cat "$D/port") != "$from" ]]; then echo "FAIL upstream mismatch"; return 1; fi
      echo "$to" > "$D/port"
      echo "APACHE_BACKUP=/root/fb-cutover-backups/vhost.bk.$to"
      echo "APPLIED upstream port $from -> $to"
      ;;
    apache_restore) echo 8111 > "$D/port" ;;
    cf_purge)
      printf '%s\n' "$@" > "$D/purge.args"
      case ${FAKE_PURGE:-ok} in
        ok) echo 'PURGE_RESULT {"purged": 16, "status": "ok", "urls": 16}' ;;
        skipped) echo 'PURGE_RESULT {"reason": "credentials_missing", "status": "skipped", "urls": 16}' ;;
        failed) echo 'PURGE_RESULT {"reason": "api_error", "status": "failed", "urls": 16}' ;;
        noresult) echo "python3: command not found" >&2; return 127 ;;
      esac
      ;;
    pg_backup)
      if [[ -n ${FAKE_BACKUP_FAIL:-} ]]; then echo "pg_dump failed" >&2; return 1; fi
      echo "BACKUP_PATH=/root/acct-release-backups/obys_pre_x.dump"
      echo "BACKUP_BYTES=1234"
      ;;
    start_candidate)
      printf '%s\n' "$@" > "$D/start_candidate.args"
      if [[ -n ${FAKE_START_FAIL:-} ]]; then echo "run failed" >&2; return 1; fi
      echo "$1|true|$5|sha256:newimg|$7" >> "$D/cands"
      if [[ -n ${FAKE_MUTATE_OTHER:-} ]]; then echo "/shortflow-x idz true t9" >> "$D/unrelated"; fi
      echo "cid"
      ;;
    *) echo "unknown op $op" >&2; return 99 ;;
  esac
}

fake_cmd() {
  local c="${1% }" sql key tag port
  case $c in
    "docker image inspect "*)
      tag="${c##* }"
      if [[ -f "$D/images/${tag//:/_}" ]]; then cat "$D/images/${tag//:/_}"; else return 1; fi ;;
    "docker build "*)
      cat > "$D/build.tar"
      tar -xOf "$D/build.tar" ./Dockerfile > "$D/Dockerfile.built"
      echo x >> "$D/build_count"
      if [[ -n ${FAKE_BUILD_FAIL:-} ]]; then return 1; fi
      tag="${c#*-t }"; tag="${tag%% *}"
      local rev rel
      rev=$(sed -n 's/.*org.opencontainers.image.revision="\([^"]*\)".*/\1/p' "$D/Dockerfile.built" | head -1)
      rel=$(sed -n 's/.*acct.release.sha="\([^"]*\)".*/\1/p' "$D/Dockerfile.built" | head -1)
      mkdir -p "$D/images"
      echo "sha256:builtimg|$rel|$rev" > "$D/images/${tag//:/_}" ;;
    "docker inspect "*RestartCount*) echo "true 0" ;;
    "docker inspect "*) echo "true" ;;
    "docker logs "*) echo "fake log" ;;
    "docker rm "*) name="${c##* }"; grep -v "^$name|" "$D/cands" > "$D/cands.new" || true; mv "$D/cands.new" "$D/cands" ;;
    "docker start "*) : ;;
    "docker tag "*) echo "TAG ${c#docker tag }" >> "$D/ssh.log" ;;
    "docker exec -i "*)
      sql="$(cat)"
      if [[ $sql == */\*identity\*/* ]]; then echo t
      elif [[ $sql == */\*probe\*/* ]]; then
        if [[ $sql == *clobe_mcp_connection* ]]; then key=clobe_store; elif [[ $sql == *obys_clobe_company_link* ]]; then key=clobe; else key=hrdoc; fi
        if [[ -f "$D/applied.$key" ]]; then echo t; else echo f; fi
      else
        if [[ $sql == *clobe_mcp_t* ]]; then key=clobe_store; elif [[ $sql == *obys_clobe* ]]; then key=clobe; else key=hrdoc; fi
        echo "MIGAPPLY $key" >> "$D/ssh.log"
        if [[ ${FAKE_MIG_FAIL:-} == "$key" ]]; then return 1; fi
        touch "$D/applied.$key"
      fi ;;
    "curl -s -m 3 -o /dev/null "*)
      port="${c##*:}"; port="${port%/}"
      if [[ " ${FAKE_BUSY_PORTS:-} " == *" $port "* ]]; then return 0; fi
      return 7 ;;
    curl*http_code*) printf 200 ;;
    curl*health/live*)
      if [[ -n ${FAKE_CAND_UNHEALTHY:-} ]]; then return 7; fi
      printf '{"status":"ok"}' ;;
    curl*index.html*)
      if [[ -n ${FAKE_CAND_NO_MARKER:-} ]]; then printf '<html>old</html>'; else printf '<html>downloadSignedContractPdf</html>'; fi ;;
    *) echo "unexpected remote command: $c" >&2; return 98 ;;
  esac
}

ssh_c() {
  local cmd="$*"
  if [[ $cmd == ACCT_REMOTE_OP=* ]]; then
    local op="${cmd%% *}"; op="${op#ACCT_REMOTE_OP=}"
    local -a w
    read -ra w <<<"$cmd"
    echo "OP $op" >> "$D/ssh.log"
    cat > "$D/stdin.$op"
    fake_op "$op" "${w[@]:4}"
    return $?
  fi
  echo "CMD ${cmd% }" >> "$D/ssh.log"
  fake_cmd "$cmd"
}

pub_state() { # pub_state -> echoes "fail" when this public call must fail
  local port; port=$(cat "$D/port")
  [[ $port == 8111 ]] && { echo ok; return; }
  if [[ -n ${FAKE_PUB_FAIL:-} ]]; then echo fail; return; fi
  if [[ -n ${FAKE_PUB_FAIL_AFTER:-} ]]; then
    echo x >> "$D/pubcount"
    if (( $(wc -l < "$D/pubcount") > FAKE_PUB_FAIL_AFTER )); then echo fail; return; fi
  fi
  echo ok
}
pub_get_body() {
  [[ $(pub_state) == ok ]] || return 1
  case $1 in
    */health/live) printf '{"status":"ok"}' ;;
    */index.html) if [[ $(cat "$D/port") != 8111 ]]; then printf 'downloadSignedContractPdf'; else printf 'old'; fi ;;
    *) printf 'x' ;;
  esac
}
pub_get_code() { [[ $(pub_state) == ok ]] || return 1; printf 200; }

if [[ ${1:-} == --call ]]; then shift; "$@"; else main "$@"; fi
'''


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


MIG_BODY = "BEGIN;\nCREATE TABLE IF NOT EXISTS {t} (id int);\nCOMMIT;\n"


class Box:
    def __init__(self, tmp):
        self.tmp = tmp
        self.repo = tmp / "repo"
        self.fake = tmp / "fake"
        self.fake.mkdir()
        (self.fake / "images").mkdir()
        self._make_repo()
        (self.fake / "port").write_text("8111\n")
        (self.fake / "cands").write_text(f"acct-app-candidate-r8|true|8111|{R8_IMAGE}|\n")
        (self.fake / "unrelated").write_text("/newtalk-a id1 true t1\n/acct-pg nsid123 true t2\n/jinah-pg id3 true t3\n")
        (self.fake / "base_rev").write_text(self.base[:8])
        self.harness = tmp / "harness.sh"
        self.harness.write_text(HARNESS)
        self.env = {
            **os.environ,
            "SCRIPT_UNDER_TEST": str(SCRIPT),
            "FAKE_DIR": str(self.fake),
            "AADS_DEPLOY_REPO_DIR": str(self.repo),
            "ACCT_APP_LOCK_FILE": str(tmp / "lock"),
            "ACCT_APP_RESULT_DIR": str(tmp / "results"),
            "MONITOR_SECONDS": "30",
            "MONITOR_INTERVAL": "10",
            "ACCT_HEALTH_WAIT_SECONDS": "6",
        }
        self.env.pop("ACCT_EXPECTED_PREV_IMAGE_ID", None)

    def _make_repo(self):
        r = self.repo
        for rel, text in {
            "app/main.py": "print('base')\n",
            "app/static/apps/obys/index.html": "<html>old</html>\n",
            f"migrations/{MIG_HRDOC}": MIG_BODY.format(t="hrdoc_t"),
            f"migrations/{MIG_CLOBE}": MIG_BODY.format(t="obys_clobe_t"),
            f"migrations/{MIG_CLOBE_STORE}": MIG_BODY.format(t="clobe_mcp_t"),
            "migrations/20260901_unrelated_aads.sql": "DROP TABLE aads_thing;\n",
            "deploy/obys/requirements.obys.lock": "fastapi==0.115\n",
        }.items():
            p = r / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
        subprocess.run(["git", "init", "-q", "-b", "main", str(r)], check=True)
        git(r, "add", "-A")
        git(r, "commit", "-q", "-m", "base")
        self.base = git(r, "rev-parse", "HEAD")
        (r / "app/static/apps/obys/index.html").write_text(f"<html>{MARKER}</html>\n")
        (r / "app/feature.py").write_text("print('release')\n")
        git(r, "add", "-A")
        git(r, "commit", "-q", "-m", "release")
        self.sha = git(r, "rev-parse", "HEAD")
        origin = self.tmp / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
        git(r, "remote", "add", "origin", str(origin))
        git(r, "push", "-q", "origin", "main")

    def commit_release(self, files):
        for rel, text in files.items():
            p = self.repo / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "release2")
        self.sha = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "push", "-q", "origin", "main")

    def run(self, *args, sha=None, run_id="77", env=None, timeout=60):
        argv = list(args) if args else [sha or self.sha, run_id]
        return subprocess.run(
            ["bash", str(self.harness), *argv], env={**self.env, **(env or {})},
            capture_output=True, text=True, timeout=timeout,
        )

    def log(self):
        p = self.fake / "ssh.log"
        return p.read_text().splitlines() if p.exists() else []

    def mutations(self):
        verbs = r"docker (rm|stop|kill|restart|start|run|network|volume|image rm|system|compose)\b"
        return [x for x in self.log() if re.match(r"CMD " + verbs, x)]

    def port(self):
        return (self.fake / "port").read_text().strip()

    def cands(self):
        return [x.split("|")[0] for x in (self.fake / "cands").read_text().splitlines() if x]

    def builds(self):
        p = self.fake / "build_count"
        return len(p.read_text().splitlines()) if p.exists() else 0

    def result(self, proc):
        lines = [x for x in proc.stdout.splitlines() if x.startswith("ACCT_APP_RELEASE_RESULT ")]
        assert lines, proc.stdout + proc.stderr
        return json.loads(lines[-1].split(" ", 1)[1])


@pytest.fixture
def box(tmp_path):
    return Box(tmp_path)


def test_script_has_valid_bash_syntax():
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)


def test_script_never_references_anthropic_api_key_or_hardcoded_secrets():
    text = SCRIPT.read_text()
    assert not re.search(r"sk-ant-[A-Za-z0-9]|AKIA[0-9A-Z]{12}|password\s*=", text)
    code = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#") and "say " not in ln]
    assert not [ln for ln in code if re.search(r"/etc/nginx|nginx -[st]|systemctl|docker compose|supervisorctl", ln)]


# ---- happy path ---------------------------------------------------------------------------------
def test_happy_path_builds_once_migrates_with_backup_first_and_switches_port(box):
    proc = box.run()
    assert proc.returncode == 0, proc.stdout + proc.stderr
    res = box.result(proc)
    assert res["status"] == "switched"
    assert res["previous"]["container"] == "acct-app-candidate-r8"
    assert res["previous"]["image_id"] == R8_IMAGE
    assert res["new"]["container"] == "acct-app-candidate-r9"
    assert res["new"]["port"] == "8112" and box.port() == "8112"
    assert res["new"]["image"] == f"acct-candidate:{box.sha[:8]}"
    assert res["new"]["image_reused"] is False
    assert res["backups"]["pg_dump"] == "/root/acct-release-backups/obys_pre_x.dump"
    assert res["backups"]["apache_vhost"].startswith("/root/fb-cutover-backups/")
    assert res["migrations"]["applied"] == [MIG_HRDOC, MIG_CLOBE, MIG_CLOBE_STORE]
    assert res["cutover_kst"].endswith("KST")
    assert res["rollback_command"].endswith("rollback acct-app-candidate-r8 77")
    assert "/root/acct-release-backups/obys_pre_x.dump" in proc.stdout
    assert box.builds() == 1

    log = box.log()
    assert log.index("OP pg_backup") < log.index("MIGAPPLY hrdoc") < log.index("MIGAPPLY clobe")
    assert log.index("MIGAPPLY clobe") < log.index("MIGAPPLY clobe_store")
    assert log.index("MIGAPPLY clobe_store") < log.index("OP start_candidate") < log.index("OP apache_switch")
    assert res["unrelated_containers_changed"] == []
    assert (box.tmp / "results" / "acct-app-77.json").exists()


def test_previous_and_unrelated_containers_are_never_mutated(box):
    proc = box.run()
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert box.mutations() == []
    assert "acct-app-candidate-r8" in box.cands()
    for name in ("newtalk", "shortflow", "jinah-pg", "acct-pg"):
        assert not any(name in line for line in box.log() if line.startswith("CMD docker rm"))


def test_build_is_an_overlay_of_the_running_image_with_revision_label_and_no_secrets(box):
    assert box.run().returncode == 0
    dockerfile = (box.fake / "Dockerfile.built").read_text()
    base_ref = ("acct-candidate-base:" + R8_IMAGE.removeprefix("sha256:"))[:33]
    # BuildKit treats a bare "FROM sha256:<id>" as a registry name; the base is pinned under a local tag.
    assert dockerfile.startswith(f"FROM {base_ref}\n")
    assert any(line.startswith(f"TAG {R8_IMAGE} {base_ref}") for line in (box.fake / "ssh.log").read_text().splitlines())
    assert f'org.opencontainers.image.revision="{box.sha[:8]}"' in dockerfile
    assert f'acct.release.sha="{box.sha}"' in dockerfile
    assert re.findall(r"^COPY .*", dockerfile, re.M) == ["COPY app ./app", "COPY migrations ./migrations", "COPY deploy ./deploy"]
    assert not re.search(r"^(ENV|ARG|RUN|ADD) ", dockerfile, re.M)
    with tarfile.open(box.fake / "build.tar") as tf:
        names = tf.getnames()
    assert "./Dockerfile" in names and "./app/feature.py" in names
    assert not [n for n in names if re.search(r"(^|/)\.env|\.pem$|\.key$|\.git/", n)]


def test_start_candidate_script_copies_env_via_tmpfs_envfile_and_shares_netns(box):
    assert box.run().returncode == 0
    body = (box.fake / "stdin.start_candidate").read_text()
    assert '--network "container:$NS"' in body
    assert "--env-file" in body and "/dev/shm" in body and "trap 'rm -f \"$envf\"' EXIT" in body
    assert "grep -v -e '^APP_PORT='" in body
    assert "-p " not in body and "--publish" not in body
    assert "docker stop" not in body and "docker rm" not in body


def _extra_env_snippet(box):
    body = (box.fake / "stdin.start_candidate").read_text()
    start = body.index("extra=/root/acct-app-extra.env")
    end = body.index("args=(run -d")
    return body[start:end]


def _run_extra_env(box, tmp_path, extra_lines, mode=0o600):
    snippet = _extra_env_snippet(box)
    extra = tmp_path / "extra.env"
    envf = tmp_path / "envf"
    envf.write_text("OBYS_PUBLIC_BASE_URL=https://old.example\nKEEP=1\n")
    if extra_lines is not None:
        extra.write_text(extra_lines)
        extra.chmod(mode)
    snippet = snippet.replace("extra=/root/acct-app-extra.env", f"extra={extra}")
    snippet = snippet.replace('"600 root"', '"600 $(id -un)"')
    proc = subprocess.run(
        ["bash", "-c", f'set -euo pipefail\nenvf={envf}\n{snippet}'],
        capture_output=True, text=True,
    )
    return proc, envf.read_text()


def test_start_candidate_extra_env_file_is_optional_and_overrides_only_allowed_names(box, tmp_path):
    assert box.run().returncode == 0
    proc, env = _run_extra_env(box, tmp_path, None)
    assert proc.returncode == 0 and env == "OBYS_PUBLIC_BASE_URL=https://old.example\nKEEP=1\n"
    # fixture key 는 형식만 흉내 낸 가짜 값이다.
    proc, env = _run_extra_env(box, tmp_path, "# comment\n\nOBYS_VAULT_KEY=fake-fixture-value\nOBYS_PUBLIC_BASE_URL=https://fb.newtalk.kr\n")
    assert proc.returncode == 0, proc.stderr
    lines = env.splitlines()
    assert "OBYS_VAULT_KEY=fake-fixture-value" in lines and "KEEP=1" in lines
    assert lines.count("OBYS_PUBLIC_BASE_URL=https://fb.newtalk.kr") == 1
    assert "OBYS_PUBLIC_BASE_URL=https://old.example" not in lines
    assert "fake-fixture-value" not in proc.stdout + proc.stderr


@pytest.mark.parametrize(
    "content,mode",
    [
        ("DATABASE_URL=postgres://x\n", 0o600),
        ("OBYS_VAULT_KEY=\n", 0o600),
        ("OBYS_VAULT_KEY=v\n", 0o644),
    ],
)
def test_start_candidate_extra_env_file_refuses_unknown_names_empty_values_and_loose_modes(box, tmp_path, content, mode):
    assert box.run().returncode == 0
    proc, env = _run_extra_env(box, tmp_path, content, mode)
    assert proc.returncode == 93
    assert "DATABASE_URL=postgres" not in proc.stdout + proc.stderr


# ---- egress proxy (opt-in) ---------------------------------------------------------------------
PROXY_URL = "http://acct-egress-proxy:8888"


def _start_candidate_args(box):
    return (box.fake / "start_candidate.args").read_text().replace("\\", "").splitlines()


def _run_proxy_snippet(box, tmp_path, proxy, noproxy, running="true", envf_text="KEEP=1\nHTTPS_PROXY=http://old:1\nno_proxy=old\n"):
    body = (box.fake / "stdin.start_candidate").read_text()
    start = body.index("egress_proxy=${9:-}")
    end = body.index("# 클로브 수집용")
    envf = tmp_path / "envf"
    envf.write_text(envf_text)
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    docker = bindir / "docker"
    docker.write_text(f"#!/bin/bash\necho {running}\n")
    docker.chmod(0o755)
    script = f'set -euo pipefail\nenvf={envf}\nset -- a b c d e f g h {shlex.quote(proxy)} {shlex.quote(noproxy)}\n' if proxy else f'set -euo pipefail\nenvf={envf}\nset -- a b c d e f g h\n'
    proc = subprocess.run(
        ["bash", "-c", script + body[start:end]],
        capture_output=True, text=True, env={**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"},
    )
    return proc, envf.read_text()


def test_proxy_disabled_by_default_keeps_candidate_arguments_unchanged(box):
    proc = box.run()
    assert proc.returncode == 0 and "egress proxy" not in proc.stdout
    assert len(_start_candidate_args(box)) == 8


def test_proxy_enabled_passes_url_and_default_no_proxy_after_the_eight_original_arguments(box, tmp_path):
    other_dir = tmp_path / "plain"
    other_dir.mkdir()
    plain_box = Box(other_dir)
    assert plain_box.run().returncode == 0
    plain = _start_candidate_args(plain_box)
    proc = box.run(env={"ACCT_APP_EGRESS_PROXY_URL": PROXY_URL})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    args = _start_candidate_args(box)
    assert len(plain) == 8 and len(args) == 10
    assert args[:3] == plain[:3] and args[4:6] == plain[4:6]
    assert args[8] == PROXY_URL
    assert args[9] == "localhost,127.0.0.1,acct-pg"
    assert "egress proxy enabled" in proc.stdout


def test_proxy_no_proxy_is_overridable(box):
    proc = box.run(env={"ACCT_APP_EGRESS_PROXY_URL": PROXY_URL, "ACCT_APP_NO_PROXY": "localhost,127.0.0.1,acct-pg,10.0.0.5"})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _start_candidate_args(box)[9] == "localhost,127.0.0.1,acct-pg,10.0.0.5"


def test_proxy_snippet_is_a_noop_when_disabled(box, tmp_path):
    assert box.run().returncode == 0
    proc, env = _run_proxy_snippet(box, tmp_path, None, None)
    assert proc.returncode == 0, proc.stderr
    assert env == "KEEP=1\nHTTPS_PROXY=http://old:1\nno_proxy=old\n"


def test_proxy_snippet_replaces_proxy_env_and_keeps_the_rest(box, tmp_path):
    assert box.run().returncode == 0
    proc, env = _run_proxy_snippet(box, tmp_path, PROXY_URL, "localhost,127.0.0.1,acct-pg")
    assert proc.returncode == 0, proc.stderr
    lines = env.splitlines()
    assert "KEEP=1" in lines
    for k in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
        assert lines.count(f"{k}={PROXY_URL}") == 1
    for k in ("NO_PROXY", "no_proxy"):
        assert lines.count(f"{k}=localhost,127.0.0.1,acct-pg") == 1
    assert "HTTPS_PROXY=http://old:1" not in lines and "no_proxy=old" not in lines


@pytest.mark.parametrize(
    "proxy,noproxy,running",
    [
        ("https://acct-egress-proxy:8888", "localhost", "true"),
        ("http://acct-egress-proxy", "localhost", "true"),
        ("http://user:pw@acct-egress-proxy:8888", "localhost", "true"),
        (PROXY_URL, "local host;rm", "true"),
        (PROXY_URL, "localhost", "false"),
    ],
)
def test_proxy_snippet_refuses_bad_url_bad_no_proxy_and_a_stopped_proxy(box, tmp_path, proxy, noproxy, running):
    assert box.run().returncode == 0
    proc, env = _run_proxy_snippet(box, tmp_path, proxy, noproxy, running)
    assert proc.returncode == 94
    assert env == "KEEP=1\nHTTPS_PROXY=http://old:1\nno_proxy=old\n"


def test_network_mode_checks_are_untouched_by_the_proxy_option(box):
    proc = box.run(env={"ACCT_APP_EGRESS_PROXY_URL": PROXY_URL})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    body = (box.fake / "stdin.start_candidate").read_text()
    assert '--network "container:$NS"' in body
    text = SCRIPT.read_text()
    assert '${INFO[PREV_NETMODE]:-} == "container:${INFO[NS_ID]}"' in text


def test_clobe_client_follows_environment_proxies():
    src = (ROOT / "app/services/clobe_mcp_client.py").read_text()
    assert "trust_env=False" not in src and "proxies=" not in src and "proxy=" not in src


# ---- image once per SHA -------------------------------------------------------------------------
def test_existing_image_for_the_sha_is_reused_without_rebuild(box):
    tag = f"acct-candidate_{box.sha[:8]}"
    (box.fake / "images" / tag).write_text(f"sha256:already|{box.sha}|{box.sha[:8]}\n")
    proc = box.run()
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert box.builds() == 0
    res = box.result(proc)
    assert res["new"]["image_reused"] is True and res["new"]["image_id"] == "sha256:already"


def test_existing_image_with_wrong_revision_label_is_refused_not_rebuilt(box):
    (box.fake / "images" / f"acct-candidate_{box.sha[:8]}").write_text(f"sha256:bad|{box.sha}|deadbeef\n")
    proc = box.run()
    assert proc.returncode == 6
    assert box.builds() == 0 and box.port() == "8111"
    assert "OP start_candidate" not in box.log()


def test_build_failure_exits_6_with_production_untouched(box):
    proc = box.run(env={"FAKE_BUILD_FAIL": "1"})
    assert proc.returncode == 6
    assert box.port() == "8111" and "OP pg_backup" not in box.log() and box.mutations() == []
    assert box.result(proc)["status"] == "failed"


def test_dependency_file_change_since_running_revision_blocks_overlay_build(box):
    box.commit_release({"deploy/obys/requirements.obys.lock": "fastapi==0.116\n"})
    proc = box.run()
    assert proc.returncode == 6, proc.stdout + proc.stderr
    assert "full image build" in proc.stderr
    assert box.builds() == 0 and "OP start_candidate" not in box.log()


def test_time_budget_exhaustion_exits_124_before_any_change(box):
    proc = box.run(env={"DEPLOY_BUDGET_SECONDS": "100"})
    assert proc.returncode == 124, proc.stdout + proc.stderr
    assert box.builds() == 0 and box.port() == "8111" and box.mutations() == []


# ---- migrations ---------------------------------------------------------------------------------
def test_already_applied_migrations_are_skipped_without_backup(box):
    (box.fake / "applied.hrdoc").touch()
    (box.fake / "applied.clobe").touch()
    (box.fake / "applied.clobe_store").touch()
    proc = box.run()
    assert proc.returncode == 0, proc.stdout + proc.stderr
    res = box.result(proc)
    assert res["migrations"]["applied"] == [] and res["migrations"]["already_applied"] == [MIG_HRDOC, MIG_CLOBE, MIG_CLOBE_STORE]
    assert "OP pg_backup" not in box.log() and "MIGAPPLY hrdoc" not in box.log()
    assert res["backups"]["pg_dump"] == ""


def test_only_the_pending_migration_is_applied(box):
    (box.fake / "applied.hrdoc").touch()
    proc = box.run()
    assert proc.returncode == 0
    assert "MIGAPPLY hrdoc" not in box.log() and "MIGAPPLY clobe" in box.log()


def test_backup_failure_exits_7_and_applies_nothing(box):
    proc = box.run(env={"FAKE_BACKUP_FAIL": "1"})
    assert proc.returncode == 7
    assert not [x for x in box.log() if x.startswith("MIGAPPLY")]
    assert box.port() == "8111" and "OP start_candidate" not in box.log()


def test_migration_failure_exits_7_without_candidate_or_cutover(box):
    proc = box.run(env={"FAKE_MIG_FAIL": "clobe"})
    assert proc.returncode == 7, proc.stdout + proc.stderr
    assert "OP start_candidate" not in box.log() and "OP apache_switch" not in box.log()
    assert box.port() == "8111" and box.mutations() == []
    res = box.result(proc)
    assert res["status"] == "failed" and res["backups"]["pg_dump"]


@pytest.mark.parametrize(
    "body",
    [
        "BEGIN;\nDROP TABLE x;\nCOMMIT;\n",
        "BEGIN;\nTRUNCATE x;\nCOMMIT;\n",
        "BEGIN;\nDELETE FROM x;\nCOMMIT;\n",
        "CREATE TABLE x (id int);\n",
        "BEGIN;\nCREATE TABLE x (id int);\n",
    ],
)
def test_destructive_or_non_transactional_allowlisted_migration_fails_closed_before_any_remote_call(box, body):
    box.commit_release({f"migrations/{MIG_CLOBE}": body})
    proc = box.run()
    assert proc.returncode == 5, proc.stdout + proc.stderr
    assert box.log() == [] and box.builds() == 0


def test_drop_inside_comment_is_not_a_false_positive(box):
    box.commit_release({f"migrations/{MIG_CLOBE}": "-- never DROP anything\nBEGIN;\nCREATE TABLE IF NOT EXISTS obys_clobe_t (id int); /* TRUNCATE no */\nCOMMIT;\n"})
    proc = box.run()
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_migration_outside_the_allowlist_is_refused(box):
    proc = box.run("--call", "apply_migration", "20260901_unrelated_aads.sql")
    assert proc.returncode == 7
    assert "not in the allowlist" in proc.stdout
    assert box.log() == []
    assert box.run("--call", "migration_allowed", MIG_HRDOC).returncode == 0
    assert box.run("--call", "migration_allowed", "20260901_unrelated_aads.sql").returncode != 0


# ---- candidate gates ----------------------------------------------------------------------------
def test_unhealthy_candidate_is_removed_and_production_untouched(box):
    proc = box.run(env={"FAKE_CAND_UNHEALTHY": "1"})
    assert proc.returncode == 8, proc.stdout + proc.stderr
    assert "OP apache_switch" not in box.log() and box.port() == "8111"
    assert box.cands() == ["acct-app-candidate-r8"]
    assert box.mutations() == ["CMD docker rm -f acct-app-candidate-r9"]


def test_candidate_without_marker_is_rejected_and_removed(box):
    proc = box.run(env={"FAKE_CAND_NO_MARKER": "1"})
    assert proc.returncode == 8
    assert "OP apache_switch" not in box.log() and box.cands() == ["acct-app-candidate-r8"]


def test_candidate_start_failure_exits_8_without_touching_the_previous_container(box):
    proc = box.run(env={"FAKE_START_FAIL": "1"})
    assert proc.returncode == 8
    assert box.port() == "8111" and "acct-app-candidate-r8" in box.cands()
    assert all("acct-app-candidate-r8" not in m for m in box.mutations())


def test_candidate_port_skips_busy_ports(box):
    proc = box.run(env={"FAKE_BUSY_PORTS": "8112 8113"})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert box.result(proc)["new"]["port"] == "8114" and box.port() == "8114"


def test_marker_argument_is_checked_against_the_release_file(box):
    proc = box.run(box.sha, "77", "someMissingMarker")
    assert proc.returncode == 5
    assert box.log() == []


def test_unrelated_container_change_blocks_cutover(box):
    proc = box.run(env={"FAKE_MUTATE_OTHER": "1"})
    assert proc.returncode == 11, proc.stdout + proc.stderr
    assert "OP apache_switch" not in box.log() and box.port() == "8111"
    assert box.cands() == ["acct-app-candidate-r8"]


# ---- cutover recovery ---------------------------------------------------------------------------
def test_apache_switch_failure_exits_9_and_keeps_previous_port(box):
    proc = box.run(env={"FAKE_APACHE_FAIL": "1"})
    assert proc.returncode == 9, proc.stdout + proc.stderr
    assert box.port() == "8111"
    assert box.cands() == ["acct-app-candidate-r8"]


def test_public_verification_failure_restores_previous_port(box):
    proc = box.run(env={"FAKE_PUB_FAIL": "1"})
    assert proc.returncode == 9, proc.stdout + proc.stderr
    assert box.port() == "8111"
    res = box.result(proc)
    assert res["status"] == "failed" and res["recovered_previous_port"] is True
    assert box.cands() == ["acct-app-candidate-r8"]
    assert all("acct-app-candidate-r8" not in m for m in box.mutations())


def test_monitor_failure_restores_previous_port_and_exits_10(box):
    proc = box.run(env={"FAKE_PUB_FAIL_AFTER": "3"})
    assert proc.returncode == 10, proc.stdout + proc.stderr
    assert box.port() == "8111"
    assert box.result(proc)["recovered_previous_port"] is True
    assert "watch miss 2/2" in proc.stdout
    assert all("acct-app-candidate-r8" not in m for m in box.mutations())


def test_monitor_runs_the_configured_number_of_ticks(box):
    proc = box.run(env={"MONITOR_SECONDS": "300", "MONITOR_INTERVAL": "15"})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    running_checks = [
        x for x in box.log()
        if x.startswith("CMD docker inspect -f") and "RestartCount" not in x and x.endswith("acct-app-candidate-r9")
    ]
    assert len(running_checks) == 20


SHELL_HTML = (
    f"<html>{MARKER}\n"
    '<link rel="stylesheet" href="/static/apps/obys/modules/store-assistant-v2.css">\n'
    '<link rel="stylesheet" href="/static/apps/obys/modules/ledger-details.css?v=20260921-r3">\n'
    '<script src="/static/apps/obys/modules/app-config.js"></script>\n'
    '<script src="https://cdn.example.com/static/apps/obys/modules/x.js"></script>\n'
    '<script>fetch("/api/v1/obys/x?v=1")</script></html>\n'
)
SW_JS = 'const CACHE_VERSION = "obys-clock-shell-20261008-r2";\nconst CACHE_PREFIX = "obys-clock-shell-";\n'


def _built_file(box, name):
    with tarfile.open(box.fake / "build.tar") as tf:
        return tf.extractfile(name).read().decode()


def test_build_stamps_release_sha_into_shell_html_and_sw_but_not_the_checkout(box):
    box.commit_release({
        "app/static/apps/obys/index.html": SHELL_HTML,
        "app/static/apps/obys/sw.js": SW_JS,
    })
    proc = box.run()
    assert proc.returncode == 0, proc.stdout + proc.stderr
    sha8 = box.sha[:8]
    html = _built_file(box, "./app/static/apps/obys/index.html")
    assert f"modules/store-assistant-v2.css?v={sha8}" in html
    assert f"modules/ledger-details.css?v={sha8}" in html and "20260921-r3" not in html
    assert f"modules/app-config.js?v={sha8}" in html
    assert 'cdn.example.com/static/apps/obys/modules/x.js"' in html and "/api/v1/obys/x?v=1" in html
    assert f'const CACHE_VERSION = "obys-clock-shell-{sha8}";' in _built_file(box, "./app/static/apps/obys/sw.js")
    assert 'obys-clock-shell-20261008-r2' not in _built_file(box, "./app/static/apps/obys/sw.js")
    assert (box.repo / "app/static/apps/obys/index.html").read_text() == SHELL_HTML
    res = box.result(proc)
    assert res["assets"] == {"version": sha8, "stamp": "stamped"}


def test_same_release_sha_gives_byte_identical_build_context_files(box):
    box.commit_release({"app/static/apps/obys/index.html": SHELL_HTML, "app/static/apps/obys/sw.js": SW_JS})
    assert box.run().returncode == 0
    first = _built_file(box, "./app/static/apps/obys/index.html"), _built_file(box, "./app/static/apps/obys/sw.js")
    (box.fake / "images").rename(box.fake / "images.old")
    (box.fake / "images").mkdir()
    (box.fake / "cands").write_text(f"acct-app-candidate-r8|true|8111|{R8_IMAGE}|\n")
    (box.fake / "port").write_text("8111\n")
    assert box.run().returncode == 0
    assert (_built_file(box, "./app/static/apps/obys/index.html"), _built_file(box, "./app/static/apps/obys/sw.js")) == first


def test_sw_without_cache_version_line_fails_the_build_closed(box):
    box.commit_release({"app/static/apps/obys/index.html": SHELL_HTML, "app/static/apps/obys/sw.js": "self.x = 1;\n"})
    proc = box.run()
    assert proc.returncode == 6, proc.stdout + proc.stderr
    assert box.mutations() == [] and box.port() == "8111"


def test_edge_purge_runs_right_after_cutover_with_files_only_and_result_records_it(box):
    box.commit_release({"app/static/apps/obys/index.html": SHELL_HTML, "app/static/apps/obys/sw.js": SW_JS})
    proc = box.run()
    assert proc.returncode == 0, proc.stdout + proc.stderr
    res = box.result(proc)
    assert res["status"] == "switched" and res["edge_purge"]["status"] == "ok"
    log = box.log()
    assert log.index("OP apache_switch") < log.index("OP cf_purge")
    args = (box.fake / "purge.args").read_text().split()
    assert args[0] == "/root/.cloudflare_env"
    urls = [u.replace("\\", "") for u in args[1:]]  # the harness sees the shell-quoted form
    base = "https://fb.newtalk.kr/static/apps/obys"
    assert f"{base}/index.html" in urls and f"{base}/sw.js" in urls
    assert f"{base}/modules/store-assistant-v2.js" not in urls
    for ref in ("store-assistant-v2.css", "ledger-details.css", "app-config.js"):
        assert f"{base}/modules/{ref}" in urls and f"{base}/modules/{ref}?v={box.sha[:8]}" in urls
    assert all(u.startswith("https://fb.newtalk.kr/") for u in urls)
    script = (box.fake / "stdin.cf_purge").read_text()
    assert "purge_everything" not in script
    assert "purge --env-file" in script and not re.search(r"CF_API_KEY\s*=\s*\S", script)
    subprocess.run(["bash", "-n"], input=script, text=True, check=True)


@pytest.mark.parametrize("mode,expected", [("skipped", "skipped"), ("failed", "failed"), ("noresult", "failed")])
def test_purge_problems_are_warnings_and_the_release_still_succeeds(box, mode, expected):
    box.commit_release({"app/static/apps/obys/index.html": SHELL_HTML, "app/static/apps/obys/sw.js": SW_JS})
    proc = box.run(env={"FAKE_PURGE": mode})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    res = box.result(proc)
    assert res["status"] == "switched" and box.port() == "8112"
    assert res["edge_purge"]["status"] == expected
    assert "WARNING: edge purge" in proc.stdout


def test_no_purge_when_the_switch_fails(box):
    proc = box.run(env={"FAKE_APACHE_FAIL": "1"})
    assert proc.returncode == 9
    assert "OP cf_purge" not in box.log()


def test_failed_watch_restores_the_port_and_purges_the_edge_again(box):
    proc = box.run(env={"FAKE_PUB_FAIL_AFTER": "3", "MONITOR_SECONDS": "60"})
    assert proc.returncode == 10, proc.stdout + proc.stderr
    assert box.port() == "8111"
    assert box.log().count("OP cf_purge") == 2


def test_reused_image_is_reported_as_not_stamped_by_this_run(box):
    (box.fake / "images" / f"acct-candidate_{box.sha[:8]}").write_text(f"sha256:builtimg|{box.sha}|{box.sha[:8]}\n")
    proc = box.run()
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert box.result(proc)["assets"]["stamp"] == "image_reused"


def test_release_already_active_is_a_noop(box):
    (box.fake / "cands").write_text(
        f"acct-app-candidate-r8|true|8111|{R8_IMAGE}|{box.sha}\n"
    )
    proc = box.run()
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert box.result(proc)["status"] == "already_active"
    assert box.builds() == 0 and box.mutations() == []


def test_unexpected_running_image_is_refused(box):
    proc = box.run(env={"FAKE_PREV_IMAGE": "sha256:" + "0" * 64})
    assert proc.returncode == 12
    assert box.builds() == 0 and box.mutations() == []


# ---- rollback -----------------------------------------------------------------------------------
def test_rollback_points_apache_back_at_the_named_container(box):
    (box.fake / "port").write_text("8112\n")
    (box.fake / "cands").write_text(
        f"acct-app-candidate-r8|true|8111|{R8_IMAGE}|\nacct-app-candidate-r9|true|8112|sha256:new|abc\n"
    )
    proc = box.run("rollback", "acct-app-candidate-r8", "88")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert box.port() == "8111"
    assert box.result(proc)["status"] == "rolled_back"
    assert box.mutations() == []


def test_rollback_refuses_unhealthy_target_and_leaves_apache_alone(box):
    (box.fake / "port").write_text("8112\n")
    (box.fake / "cands").write_text(
        f"acct-app-candidate-r8|true|8111|{R8_IMAGE}|\nacct-app-candidate-r9|true|8112|sha256:new|abc\n"
    )
    proc = box.run("rollback", "acct-app-candidate-r8", "88", env={"FAKE_CAND_UNHEALTHY": "1"})
    assert proc.returncode == 12
    assert box.port() == "8112" and "OP apache_switch" not in box.log()


def test_rollback_only_accepts_candidate_container_names(box):
    proc = box.run("rollback", "newtalk-a", "88")
    assert proc.returncode == 2
    assert box.log() == []


def test_rollback_starts_a_stopped_target_through_the_guard(box):
    (box.fake / "port").write_text("8112\n")
    (box.fake / "cands").write_text(
        f"acct-app-candidate-r8|false|8111|{R8_IMAGE}|\nacct-app-candidate-r9|true|8112|sha256:new|abc\n"
    )
    proc = box.run("rollback", "acct-app-candidate-r8", "88")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert box.mutations() == ["CMD docker start acct-app-candidate-r8"]
    assert box.port() == "8111"


# ---- guards -------------------------------------------------------------------------------------
@pytest.mark.parametrize("verb,name,rc", [
    ("rm", "acct-app-candidate-r8", 11),
    ("stop", "acct-app-candidate-r8", 11),
    ("rm", "newtalk-a", 11),
    ("rm", "acct-pg", 11),
    ("rm", "shortflow-x", 11),
    ("rm", "acct-app-candidate-r5x", 11),
    ("kill", "acct-app-candidate-r9", 11),
    ("rm", "acct-app-candidate-r9", 0),
])
def test_docker_mutation_guard(box, verb, name, rc):
    args = ["--call", "docker_mut", verb] + (["-f"] if verb == "rm" else []) + [name]
    proc = box.run(*args)
    assert proc.returncode == rc, proc.stdout + proc.stderr
    assert (len(box.mutations()) == 1) == (rc == 0)


@pytest.mark.parametrize("args", [
    ["abc1234", "1"],
    ["z" * 40, "1"],
    ["a" * 40, "bad id;rm"],
])
def test_bad_arguments_exit_2_without_remote_calls(box, args):
    proc = box.run(*args)
    assert proc.returncode == 2
    assert box.log() == []


def test_concurrent_run_is_rejected_by_the_lock(box):
    holder = subprocess.Popen(["timeout", "30", "flock", str(box.tmp / "lock"), "sleep", "20"])
    try:
        for _ in range(50):
            if (box.tmp / "lock").exists():
                break
            subprocess.run(["sleep", "0.1"])
        subprocess.run(["sleep", "0.3"])
        proc = box.run()
        assert proc.returncode == 3, proc.stdout + proc.stderr
        assert box.log() == []
    finally:
        holder.kill()
        holder.wait()


# ---- release worktree ---------------------------------------------------------------------------
def test_unpushed_sha_is_rejected(box):
    (box.repo / "app/local_only.py").write_text("x = 1\n")
    git(box.repo, "add", "-A")
    git(box.repo, "commit", "-q", "-m", "local only")
    local = git(box.repo, "rev-parse", "HEAD")
    env = {"AADS_DEPLOY_REPO_DIR": "", "AADS_DEPLOY_SOURCE_REPO": str(box.repo), "ACCT_APP_RELEASE_ROOT": str(box.tmp / "wt")}
    proc = box.run(sha=local, env=env)
    assert proc.returncode == 5, proc.stdout + proc.stderr
    assert "not pushed" in proc.stderr
    assert box.log() == []


def test_release_runs_in_an_isolated_worktree_that_is_removed_afterwards(box):
    wt_root = box.tmp / "wt"
    (box.repo / "app/dirty.py").write_text("dirty = 1\n")  # untracked file in the source checkout stays untouched
    env = {"AADS_DEPLOY_REPO_DIR": "", "AADS_DEPLOY_SOURCE_REPO": str(box.repo), "ACCT_APP_RELEASE_ROOT": str(wt_root)}
    proc = box.run(env=env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "isolated release worktree" in proc.stdout
    assert list(wt_root.iterdir()) == []
    assert "worktree" not in git(box.repo, "worktree", "list").split("\n")[-1] or len(git(box.repo, "worktree", "list").splitlines()) == 1
    assert (box.repo / "app/dirty.py").read_text() == "dirty = 1\n"


# ---- registry -----------------------------------------------------------------------------------
def test_execution_target_registers_acct_app():
    spec = importlib.util.spec_from_file_location("acct_targets", ROOT / "app/services/deploy_adapters/targets.py")
    mod = importlib.util.module_from_spec(spec)
    import sys

    sys.modules["acct_targets"] = mod
    spec.loader.exec_module(mod)
    target = mod.get_execution_target("ACCT", "app")
    assert target is not None
    assert target.deploy_type == "container_replace" and target.executor == "local"
    assert target.supports_rollback is True and target.timeout_seconds == 1500
    assert target.command == ("bash", "/root/aads/aads-server/scripts/deploy_acct_app_cafe24.sh")
    assert target.route_url == "https://fb.newtalk.kr/health/live"
    assert mod.get_execution_target("ACCT", "fb-cutover").supports_rollback is False
