"""외부 노출 포트 판정기 단위 테스트. 입력은 전부 문서용 대역(192.0.2.x)의 가짜 값."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("exposure_verifier", _REPO / "scripts/security/exposure_verifier.py")
ev = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ev)
ALLOWLIST = _REPO / "tests/fixtures/exposure_verifier/allowlist.json"

SS_TCP = "tcp LISTEN 0 128 0.0.0.0:8080 0.0.0.0:*\n"
SS_UDP = "udp UNCONN 0 0 [::]:5353 [::]:*\n"


def _nft(*rules, forward_policy="accept", input_policy="drop"):
    items = [
        {"chain": {"family": "inet", "table": "f", "name": "in", "hook": "input", "policy": input_policy}},
        {"chain": {"family": "inet", "table": "f", "name": "fw", "hook": "forward", "policy": forward_policy}},
    ]
    return json.dumps({"nftables": items + [{"rule": r} for r in rules]})


def _drop_rule(chain, port, proto="tcp"):
    return {"family": "inet", "table": "f", "chain": chain, "expr": [
        {"match": {"op": "==", "left": {"payload": {"protocol": proto, "field": "dport"}}, "right": port}},
        {"drop": None}]}


def _dport(chain, port, verdict="drop", proto="tcp", fam_proto=None):
    return {"family": "inet", "table": "f", "chain": chain, "expr": [
        {"match": {"op": "==", "left": {"payload": {"protocol": proto, "field": "dport"}}, "right": port}},
        {verdict: None}]}


def _saddr(chain, net, op, verdict, *, family="inet", extra=()):
    addr, _, ln = net.partition("/")
    return {"family": family, "table": "f", "chain": chain, "expr": [
        {"match": {"op": op, "left": {"payload": {"protocol": "ip", "field": "saddr"}},
                   "right": {"prefix": {"addr": addr, "len": int(ln)}}}}, *extra, {verdict: None}]}


def _items(chains, *rules):
    return json.dumps({"nftables": [{"chain": c} for c in chains] + [{"rule": r} for r in rules]})


def _run(tmp_path, capsys, ss, nft=None, docker=None, allow=True):
    args = []
    for flag, name, text in (("--ss", "ss", ss), ("--nft", "nft", nft), ("--docker", "docker", docker)):
        if text is not None:
            (tmp_path / name).write_text(text)
            args += [flag, str(tmp_path / name)]
    if docker is None:
        args.append("--no-docker")
    if allow:
        args += ["--allowlist", str(ALLOWLIST)]
    code = ev.main(args)
    return code, json.loads(capsys.readouterr().out)


def test_tcp_exposure_is_violation(tmp_path, capsys):
    code, out = _run(tmp_path, capsys, SS_TCP)
    assert code == 1 and out["violations"] == 1
    assert out["ports"][0]["verdict"] == "위반" and out["ports"][0]["firewall"] == "열림"


def test_udp_exposure_is_violation(tmp_path, capsys):
    code, out = _run(tmp_path, capsys, SS_UDP)
    assert code == 1 and out["ports"][0]["proto"] == "udp"


def test_docker_input_blocked_but_forward_open_is_open(tmp_path, capsys):
    docker = json.dumps({"Ports": "0.0.0.0:9000->80/tcp, :::9000->80/tcp"})
    nft = _nft(_drop_rule("in", 9000))
    code, out = _run(tmp_path, capsys, "", nft, docker)
    row = out["ports"][0]
    assert row["firewall"] == "열림" and row["verdict"] == "위반" and code == 1


def test_docker_blocked_in_both_hooks(tmp_path, capsys):
    docker = json.dumps({"Ports": "0.0.0.0:9000->80/tcp"})
    nft = _nft(_drop_rule("in", 9000), _drop_rule("fw", 80))
    code, out = _run(tmp_path, capsys, "", nft, docker)
    assert out["ports"][0]["firewall"] == "차단됨" and code == 0


def test_loopback_binding_not_target(tmp_path, capsys):
    ss = "tcp LISTEN 0 128 127.0.0.1:5432 0.0.0.0:*\ntcp LISTEN 0 128 [::1]:631 [::]:*\n"
    docker = json.dumps({"Ports": "127.0.0.1:6379->6379/tcp"})
    code, out = _run(tmp_path, capsys, ss, None, docker)
    assert code == 0 and out["ports"] == []


def test_nft_failure_is_undetermined(tmp_path, capsys):
    code, out = _run(tmp_path, capsys, SS_TCP, "not json")
    assert code == 2 and out["undetermined"] >= 1 and out["violations"] == 0


def test_nft_file_unreadable_is_undetermined(tmp_path, capsys):
    (tmp_path / "ss").write_text(SS_TCP)
    code = ev.main(["--ss", str(tmp_path / "ss"), "--nft", str(tmp_path / "missing"), "--no-docker"])
    assert code == 2
    capsys.readouterr()


def test_docker_missing_is_undetermined(tmp_path, capsys):
    (tmp_path / "ss").write_text(SS_TCP)
    assert ev.main(["--ss", str(tmp_path / "ss")]) == 2
    capsys.readouterr()


def test_allowlisted_any_port_passes(tmp_path, capsys):
    code, out = _run(tmp_path, capsys, "tcp LISTEN 0 128 0.0.0.0:443 0.0.0.0:*\n")
    assert code == 0 and out["ports"][0]["verdict"] == "허용"


def test_source_restricted_allow_open_to_all_is_violation(tmp_path, capsys):
    # 지적1: 192.0.2.0/24 만 허용된 UDP 51820 이 전체 출발지에 열려 있으면 위반
    code, out = _run(tmp_path, capsys, "udp UNCONN 0 0 0.0.0.0:51820 0.0.0.0:*\n")
    assert code == 1 and out["ports"][0]["verdict"] == "위반"


def test_source_restricted_allow_blocked_outside_passes(tmp_path, capsys):
    nft = _nft(_saddr("in", "192.0.2.0/24", "!=", "drop", extra=[
        {"match": {"op": "==", "left": {"payload": {"protocol": "udp", "field": "dport"}}, "right": 51820}}]),
        input_policy="accept")
    code, out = _run(tmp_path, capsys, "udp UNCONN 0 0 0.0.0.0:51820 0.0.0.0:*\n", nft)
    assert code == 0 and out["ports"][0]["verdict"] == "허용(출발지 제한 확인)"


def test_source_conditional_accept_with_default_drop_is_undetermined(tmp_path, capsys):
    # 지적2: 출발지 조건부 accept + 기본 drop 을 '차단됨' 으로 세면 안 된다
    nft = _nft(_saddr("in", "198.51.100.0/24", "==", "accept"))
    code, out = _run(tmp_path, capsys, SS_TCP, nft)
    assert code == 2 and out["ports"][0]["verdict"] == "판정불가"


def test_unsupported_condition_unknown(tmp_path, capsys):
    rule = {"family": "inet", "table": "f", "chain": "in", "expr": [
        {"match": {"op": "==", "left": {"meta": {"key": "iifname"}}, "right": "eth0"}}, {"accept": None}]}
    code, _ = _run(tmp_path, capsys, SS_TCP, _nft(rule))
    assert code == 2


def test_ipv4_only_drop_does_not_block_ipv6_listener(tmp_path, capsys):
    # 지적3: ip 테이블 drop 은 [::] 리스너를 막지 않는다. 계열별 후보가 따로 나온다
    rule = {"family": "ip", "table": "f", "chain": "in", "expr": [
        {"match": {"op": "==", "left": {"payload": {"protocol": "tcp", "field": "dport"}}, "right": 8080}},
        {"drop": None}]}
    chains = [{"family": "ip", "table": "f", "name": "in", "hook": "input", "type": "filter", "policy": "accept"}]
    ss = "tcp LISTEN 0 128 *:8080 *:*\n"
    code, out = _run(tmp_path, capsys, ss, _items(chains, rule))
    fams = {r["family"]: r["firewall"] for r in out["ports"]}
    assert fams == {"ip": "차단됨", "ip6": "열림"} and code == 1


def test_distinct_bindings_not_overwritten(tmp_path, capsys):
    ss = "tcp LISTEN 0 128 0.0.0.0:8080 0.0.0.0:*\ntcp LISTEN 0 128 [::]:8080 [::]:*\n"
    _, out = _run(tmp_path, capsys, ss)
    assert len(out["ports"]) == 2


def test_docker_forward_checks_post_dnat_port(tmp_path, capsys):
    # 지적4: forward 가 게시 포트 9000 만 dport 로 막으면 DNAT 후 80 트래픽은 통과 → 열림
    docker = json.dumps({"Ports": "0.0.0.0:9000->80/tcp"})
    nft = _nft(_drop_rule("in", 9000), _drop_rule("fw", 9000))
    code, out = _run(tmp_path, capsys, "", nft, docker)
    assert out["ports"][0]["firewall"] == "열림" and code == 1


def test_docker_forward_ct_original_dport_blocks(tmp_path, capsys):
    docker = json.dumps({"Ports": "0.0.0.0:9000->80/tcp"})
    ct = {"family": "inet", "table": "f", "chain": "fw", "expr": [
        {"match": {"op": "==", "left": {"ct": {"key": "proto-dst", "dir": "original"}}, "right": 9000}},
        {"drop": None}]}
    nft = _nft(_drop_rule("in", 9000), ct)
    code, out = _run(tmp_path, capsys, "", nft, docker)
    assert out["ports"][0]["firewall"] == "차단됨" and code == 0


def test_docker_specific_external_address_is_candidate(tmp_path, capsys):
    # 지적5: 특정 외부 주소 게시도 검사, 루프백 게시는 제외
    docker = json.dumps({"Ports": "192.0.2.10:9000->80/tcp, 127.0.0.1:6379->6379/tcp"})
    code, out = _run(tmp_path, capsys, "", None, docker)
    assert [r["port"] for r in out["ports"]] == [9000] and out["ports"][0]["bind"] == "192.0.2.10" and code == 1


def test_docker_ports_null_is_undetermined(tmp_path, capsys):
    # 지적6
    code, out = _run(tmp_path, capsys, "", None, json.dumps({"Ports": None}))
    assert code == 2 and out["undetermined"] >= 1


def test_nft_schema_error_is_undetermined(tmp_path, capsys):
    code, out = _run(tmp_path, capsys, SS_TCP, json.dumps({"nftables": [{"rule": {}}]}))
    assert code == 2 and out["violations"] == 0


def test_nft_missing_key_is_undetermined(tmp_path, capsys):
    code, _ = _run(tmp_path, capsys, SS_TCP, "{}")
    assert code == 2


def test_allowlist_proto_mismatch_is_violation(tmp_path, capsys):
    code, out = _run(tmp_path, capsys, "tcp LISTEN 0 128 0.0.0.0:51820 0.0.0.0:*\n")
    assert code == 1


def test_input_blocked_host_port_not_violation(tmp_path, capsys):
    code, out = _run(tmp_path, capsys, SS_TCP, _nft())
    assert out["ports"][0]["firewall"] == "차단됨" and code == 0


def test_collect_uses_timeout(monkeypatch):
    seen = {}

    def fake(cmd, **kw):
        seen.update(kw)
        raise ev.subprocess.TimeoutExpired(cmd, kw["timeout"])

    monkeypatch.setattr(ev.subprocess, "run", fake)
    assert ev._run("nft") is None and seen["timeout"] == 15


# ---- 재작업 라운드 2 지적 1~3 ----
def _docker_line(ports):
    return json.dumps({"Names": "a", "Ports": ports}) + "\n"


def test_same_port_different_publish_addresses_both_kept(tmp_path, capsys):
    """지적 1: 같은 포트·계열의 서로 다른 게시 주소가 덮어써지지 않는다."""
    docker = _docker_line("192.0.2.10:9000->80/tcp, 192.0.2.11:9000->81/tcp")
    nft = _nft(_dport("in", 9000), _dport("fw", 81), forward_policy="accept")
    code, out = _run(tmp_path, capsys, "", nft, docker)
    rows = [r for r in out["ports"] if r["port"] == 9000]
    assert len(rows) == 2
    by_c = {r["container_port"]: r["firewall"] for r in rows}
    assert by_c == {80: "열림", 81: "차단됨"}
    assert code == 1


def test_goto_returns_to_callers_continuation(tmp_path, capsys):
    """지적 2: input(drop) 'jump a; accept', a 'goto b', b 빈 체인 → input 의 accept 로 복귀 = 열림."""
    chains = [
        {"family": "inet", "table": "f", "name": "in", "hook": "input", "policy": "drop"},
        {"family": "inet", "table": "f", "name": "a"},
        {"family": "inet", "table": "f", "name": "b"},
    ]
    def r(chain, expr):
        return {"family": "inet", "table": "f", "chain": chain, "expr": expr}
    nft = _items(chains, r("in", [{"jump": {"target": "a"}}]), r("in", [{"accept": None}]),
                 r("a", [{"goto": {"target": "b"}}]))
    code, out = _run(tmp_path, capsys, SS_TCP, nft)
    assert out["ports"][0]["firewall"] == "열림" and code == 1


def test_vmap_accept_with_default_drop_is_undetermined(tmp_path, capsys):
    """지적 3: vmap 처럼 지원하지 않는 식이 판정을 낼 수 있으면 차단됨이 아니라 판정불가."""
    chains = [{"family": "inet", "table": "f", "name": "in", "hook": "input", "policy": "drop"}]
    rule = {"family": "inet", "table": "f", "chain": "in", "expr": [
        {"vmap": {"key": {"payload": {"protocol": "tcp", "field": "dport"}}, "data": {"set": [[8080, {"accept": None}]]}}}]}
    code, out = _run(tmp_path, capsys, SS_TCP, _items(chains, rule))
    assert out["ports"][0]["firewall"] == "판정불가" and code == 2
