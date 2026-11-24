#!/usr/bin/env python3
"""외부 노출 포트 판정기 (읽기 전용, 표준 라이브러리만).

0.0.0.0 / :: 에 바인딩된 TCP·UDP 포트와 Docker 게시 포트(루프백 제외, 특정 외부 주소 게시 포함)를
'외부 노출 후보'로 보고, 허용 목록과 nft 규칙(input / forward 훅)에 비춰 판정한다.
방화벽을 적용·변경하지 않는다.

입력(파일 경로)
  --ss FILE         `ss -H -lntu` 출력
  --nft FILE        `nft -j list ruleset` JSON (생략하면 빈 규칙셋 = 아무것도 안 막음)
  --docker FILE     `docker ps --format '{{json .}}'` 줄들 (Docker 없음을 선언하려면 --no-docker)
  --allowlist FILE  {"allow":[{"port":443,"proto":"tcp","sources":["any"]}]}
  --collect         위 명령을 직접 실행(각 timeout=15). 운영 서버에서 실행하지 말 것.

판정 모델
  후보는 (proto, port, IP 계열) 단위다. `*` 바인딩은 IPv4·IPv6 두 후보로 나눈다.
  패킷은 '새 연결의 외부 출발지'로 모델링한다. 각 nft 조건은 참/거짓/모름 3값으로 평가한다.
  - 일반 포트: input 훅. Docker 게시 포트: input 과 forward 훅 둘 다.
  - forward 는 DNAT 이후 상태다: 현재 dport = 컨테이너 포트, ct original proto-dst = 게시 포트.
  - inet 체인은 두 계열에 적용, ip 체인은 IPv4, ip6 체인은 IPv6 후보에만 적용한다.
  - 지원하지 않는 조건(예: 인터페이스·daddr·집합 참조)이 결과를 바꿀 수 있으면 판정불가다.
  방화벽 값: 차단됨 / 열림 / 판정불가.

허용 목록 sources
  ["any"] 는 포트·프로토콜만 본다. CIDR 이 있으면 '허용 출발지 밖' 트래픽이 방화벽에서
  차단됨이어야 통과한다(열림 = 위반, 확인 불가 = 판정불가).

종료코드: 0=위반 0·판정불가 0 / 1=위반 있음 / 2=판정불가 있음(위반이 함께 있어도 2).
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import re
import subprocess
import sys
from pathlib import Path

BLOCKED, OPEN, UNKNOWN = "차단됨", "열림", "판정불가"
_WILDCARDS = {"0.0.0.0", "*", "::", "[::]"}
_NEUTRAL = {"counter", "log", "comment", "notrack"}
_TERMINAL = {"accept", "drop", "reject"}
_JUMPS = {"jump", "goto"}
_TIMEOUT = 15
_STEP_BUDGET = 20000
_SCHEMA_ERRORS = (KeyError, TypeError, AttributeError, ValueError, IndexError)
_COLLECT_CMDS = {
    "ss": ["ss", "-H", "-lntu"],
    "nft": ["nft", "-j", "list", "ruleset"],
    "docker": ["docker", "ps", "--format", "{{json .}}"],
}


class InputError(Exception):
    pass


# ---------- 입력 파싱 ----------
def _families(addr: str) -> list[str]:
    if addr in ("0.0.0.0",):
        return ["ip"]
    if addr in ("::", "[::]"):
        return ["ip6"]
    return ["ip", "ip6"]  # "*"


def parse_ss(text: str) -> tuple[list[dict], list[str]]:
    """ss 출력 → 와일드카드 바인딩 후보(계열별), 파싱 실패 사유."""
    found, problems = [], []
    for raw in text.splitlines():
        if not raw.strip():
            continue
        cols = raw.split()
        netid = cols[0].lower()
        if len(cols) < 5 or netid not in ("tcp", "udp"):
            problems.append(f"ss 줄 해석 불가: {raw[:60]}")
            continue
        addr, _, port = cols[4].rpartition(":")
        addr = addr.split("%", 1)[0]
        if not port.isdigit():
            problems.append(f"ss 포트 해석 불가: {cols[4][:40]}")
            continue
        if addr in _WILDCARDS:
            for fam in _families(addr):
                found.append({"proto": netid, "port": int(port), "family": fam, "bind": addr, "source": "ss"})
    return found, problems


def _expand_ports(spec: str) -> list[int]:
    lo, _, hi = spec.partition("-")
    a, b = int(lo), int(hi or lo)
    if not (0 < a <= b <= 65535) or b - a > 4096:
        raise ValueError(spec)
    return list(range(a, b + 1))


_PORT_RE = re.compile(r"^(?P<host>.*):(?P<hp>\d+(?:-\d+)?)->(?P<cp>\d+(?:-\d+)?)/(?P<proto>tcp|udp)$")


def _docker_host(host: str):
    """→ (families, bind) | None(루프백 = 비대상). 해석 불가는 ValueError."""
    host = host.split("%", 1)[0]
    if host in _WILDCARDS:
        return _families(host), host
    ip = ipaddress.ip_address(host.strip("[]"))
    if ip.is_loopback:
        return None
    return ["ip" if ip.version == 4 else "ip6"], host


def parse_docker(text: str) -> tuple[list[dict], list[str]]:
    found, problems = [], []
    for raw in text.splitlines():
        if not raw.strip():
            continue
        try:
            obj = json.loads(raw)
        except ValueError:
            problems.append("docker ps 줄 JSON 해석 불가")
            continue
        ports = obj.get("Ports") if isinstance(obj, dict) else None
        if not isinstance(ports, str):
            problems.append("docker ps 줄의 Ports 필드 형식 오류")
            continue
        for item in [p.strip() for p in ports.split(",") if p.strip()]:
            if "->" not in item:
                continue  # 게시되지 않은 포트
            m = _PORT_RE.match(item)
            if not m:
                problems.append(f"docker 포트 표기 해석 불가: {item[:50]}")
                continue
            try:
                target = _docker_host(m["host"])
                hps, cps = _expand_ports(m["hp"]), _expand_ports(m["cp"])
            except ValueError:
                problems.append(f"docker 게시 주소·포트 해석 불가: {item[:50]}")
                continue
            if target is None:
                continue  # 루프백 게시
            fams, bind = target
            if len(hps) != len(cps):
                cps = cps[:1] * len(hps)
            for hp, cp in zip(hps, cps):
                for fam in fams:
                    found.append({"proto": m["proto"], "port": hp, "cport": cp, "family": fam,
                                  "bind": bind, "source": "docker"})
    return found, problems


def _cand_key(c: dict) -> tuple:
    """후보 식별: 게시 주소가 다르면 별개 후보. 와일드카드 표기(0.0.0.0/*/::/[::])는 하나로 본다."""
    bind = "*" if c["bind"] in _WILDCARDS else c["bind"].strip("[]")
    return (c["proto"], c["port"], c["family"], bind, c.get("cport"))


def parse_allowlist(text: str) -> list[dict]:
    try:
        entries = json.loads(text)["allow"]
        out = []
        for e in entries:
            proto = e.get("proto")
            if proto not in (None, "tcp", "udp"):
                raise ValueError("proto")
            sources = e.get("sources", ["any"])
            if not isinstance(sources, list) or not sources:
                raise ValueError("sources")
            nets = []
            for s in sources:
                if s == "any":
                    nets = None
                    break
                nets.append(ipaddress.ip_network(s, strict=False))
            out.append({"port": int(e["port"]), "proto": proto,
                        "sources": ["any"] if nets is None else [str(s) for s in sources], "nets": nets})
        return out
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise InputError(f"허용 목록 해석 실패: {type(exc).__name__}") from exc


def _allow_sources(allow: list[dict], proto: str, port: int):
    """→ None(허용 목록에 없음) | ([문자열], None=any) | ([문자열], [네트워크])"""
    hits = [e for e in allow if e["port"] == port and e["proto"] in (None, proto)]
    if not hits:
        return None
    if any(e["nets"] is None for e in hits):
        return ["any"], None
    return [s for e in hits for s in e["sources"]], [n for e in hits for n in e["nets"]]


# ---------- nft 평가 ----------
class _Pkt:
    """검사 대상 패킷: 새 연결, 출발지는 '허용 출발지(nets) 밖의 임의 주소'."""

    def __init__(self, family: str, proto: str, dport: int, odport: int, nets: list):
        self.family, self.proto, self.dport, self.odport = family, proto, dport, odport
        ver = 4 if family == "ip" else 6
        self.nets = [n for n in nets if n.version == ver]


def _vals(right) -> list:
    if isinstance(right, dict):
        if "set" in right:
            return [v for r in right["set"] for v in _vals(r)]
        if "range" in right:
            lo, hi = right["range"]
            return [(lo, hi)]
        if "prefix" in right:
            return [right]
        return [None]
    if isinstance(right, list):
        return [v for r in right for v in _vals(r)]
    return [right]


def _eq(op, hit):
    if hit is None:
        return None
    if op in ("==", "in"):
        return hit
    if op == "!=":
        return not hit
    return None


def _port_hit(right, port: int):
    vals = _vals(right)
    for v in vals:
        if isinstance(v, tuple) and v[0] <= port <= v[1]:
            return True
        if isinstance(v, int) and not isinstance(v, bool) and v == port:
            return True
    if any(not (isinstance(v, tuple) or (isinstance(v, int) and not isinstance(v, bool))) for v in vals):
        return None  # 서비스 이름·집합 참조 등
    return False


def _port_test(op, right, port: int):
    if op in ("<", ">", "<=", ">="):
        if not isinstance(right, int) or isinstance(right, bool):
            return None
        return {"<": port < right, ">": port > right, "<=": port <= right, ">=": port >= right}[op]
    return _eq(op, _port_hit(right, port))


def _nets_of(right, version: int):
    """right 값 → 해당 IP 버전 네트워크 목록. 해석 불가면 None."""
    out = []
    for v in _vals(right):
        try:
            if isinstance(v, str) and not v.startswith("@"):
                n = ipaddress.ip_network(v, strict=False)
            elif isinstance(v, dict) and "prefix" in v:
                n = ipaddress.ip_network(f'{v["prefix"]["addr"]}/{v["prefix"]["len"]}', strict=False)
            else:
                return None
        except ValueError:
            return None
        if n.version == version:
            out.append(n)
    return out


def _saddr_hit(right, pkt: _Pkt):
    """출발지가 right 집합에 속하는가. 허용 출발지(pkt.nets) 밖 주소 전체에 대해 True/False/None."""
    ver = 4 if pkt.family == "ip" else 6
    s = _nets_of(right, ver)
    if s is None:
        return None
    if any(n.prefixlen == 0 for n in s):
        return True  # 모든 출발지
    if all(any(n.subnet_of(a) for a in pkt.nets) for n in s):
        return False  # 집합이 전부 허용 출발지 안 → 밖 주소와 겹치지 않음
    return None  # 일부 출발지만 해당 → 결과가 출발지에 달림


_L4_NUM = {"tcp": 6, "udp": 17}


def _match(body, pkt: _Pkt):
    """match 식 하나 → True/False/None(모름)."""
    op, left, right = body.get("op"), body.get("left"), body.get("right")
    if not isinstance(left, dict):
        return None
    if "payload" in left:
        pl = left["payload"]
        proto_, field = pl.get("protocol"), pl.get("field")
        if proto_ in ("icmp", "icmpv6"):
            return False  # tcp/udp 패킷이 아님
        if proto_ in ("tcp", "udp") and proto_ != pkt.proto:
            return False
        if field == "dport" and proto_ in ("tcp", "udp", "th"):
            return _port_test(op, right, pkt.dport)
        if proto_ in ("ip", "ip6") and (proto_ == "ip") != (pkt.family == "ip"):
            return False  # 다른 IP 계열 패킷
        if field == "saddr" and proto_ in ("ip", "ip6"):
            return _eq(op, _saddr_hit(right, pkt))
        if field in ("protocol", "nexthdr") and proto_ in ("ip", "ip6"):
            vals = _vals(right)
            return _eq(op, (pkt.proto in vals or _L4_NUM[pkt.proto] in vals)
                       if all(isinstance(v, (str, int)) for v in vals) else None)
        return None
    if "ct" in left:
        ct = left["ct"]
        if ct.get("key") == "proto-dst" and ct.get("dir") == "original":
            return _port_test(op, right, pkt.odport)
        if ct.get("key") == "state":
            vals = _vals(right)
            return _eq(op, ("new" in vals) if all(isinstance(v, str) for v in vals) else None)
        return None
    if "meta" in left:
        key = left["meta"].get("key")
        vals = _vals(right)
        if not all(isinstance(v, (str, int)) for v in vals):
            return None
        if key == "l4proto":
            return _eq(op, pkt.proto in vals or _L4_NUM[pkt.proto] in vals)
        if key == "nfproto":
            return _eq(op, ("ipv4" if pkt.family == "ip" else "ipv6") in vals)
        if key in ("iifname", "iif") and all(v == "lo" for v in vals):
            return _eq(op, False)  # 외부 트래픽은 loopback 으로 오지 않는다
        return None
    return None


def _analyze_rule(expr: list, pkt: _Pkt):
    """→ (match, verdict). match: True / False(해당 없음) / None(조건을 모름)."""
    state, verdict = True, None
    for e in expr:
        (kind, body), = e.items()
        if kind in _NEUTRAL:
            continue
        if kind in _TERMINAL or kind in _JUMPS or kind == "return":
            if verdict is None:  # 먼저 나온 판정이 규칙을 끝낸다
                verdict = (kind, body)
            continue
        if kind != "match":
            # vmap·map·queue 등 지원하지 않는 식은 스스로 판정을 낼 수 있다 → 판정불가 후보
            if verdict is None:
                verdict = ("unknown", None)
            state = None
            continue
        r = _match(body, pkt) if isinstance(body, dict) else None
        if r is False:
            return False, verdict
        if r is None:
            state = None
    return state, verdict


class _Walk:
    def __init__(self, rules: dict, pkt: _Pkt):
        self.rules, self.pkt, self.steps = rules, pkt, 0

    def run(self, key: tuple, i: int, k, depth: int = 0) -> str:
        """→ 'drop' | 'accept' | 'unknown'. k = 체인 끝에서 이어질 평가."""
        self.steps += 1
        if self.steps > _STEP_BUDGET or depth > 16:
            return "unknown"
        rules = self.rules.get(key, [])
        while i < len(rules):
            match, verdict = _analyze_rule(rules[i], self.pkt)
            if match is not False and verdict is not None:
                break
            i += 1
        else:
            return k()
        kind, body = verdict
        nxt = i + 1

        def rest():
            return self.run(key, nxt, k, depth)

        if kind in ("drop", "reject"):
            taken = "drop"
        elif kind == "accept":
            taken = "accept"
        elif kind == "return":
            taken = k()
        elif kind == "unknown":
            taken = "unknown"
        else:
            target = (key[0], key[1], (body or {}).get("target"))
            if not isinstance(target[2], str):
                return "unknown"
            # goto 는 대상 체인이 끝나면 현재 체인의 호출자로 복귀한다(= k). jump 는 이 체인의 다음 규칙으로.
            taken = self.run(target, 0, rest if kind == "jump" else k, depth + 1)
        if match is True:
            return taken
        other = rest()  # 조건을 모르는 규칙: 걸릴 때와 안 걸릴 때 결과가 같아야 확정
        return taken if taken == other else "unknown"


def _index_nft(nft: dict) -> tuple[list[dict], dict]:
    """nft JSON 구조 검증·색인. 구조가 어긋나면 ValueError."""
    items = nft["nftables"]
    if not isinstance(items, list):
        raise ValueError("nftables")
    chains, rules = [], {}
    for i in items:
        if not isinstance(i, dict):
            raise ValueError("item")
        if "chain" in i:
            c = i["chain"]
            if not all(isinstance(c.get(f), str) for f in ("family", "table", "name")):
                raise ValueError("chain")
            chains.append(c)
        elif "rule" in i:
            r = i["rule"]
            if not all(isinstance(r.get(f), str) for f in ("family", "table", "chain")):
                raise ValueError("rule")
            expr = r.get("expr", [])
            if not isinstance(expr, list) or not all(isinstance(e, dict) and len(e) == 1 for e in expr):
                raise ValueError("expr")
            rules.setdefault((r["family"], r["table"], r["chain"]), []).append(expr)
    return chains, rules


def _applies(chain_family: str, pkt_family: str) -> bool:
    return chain_family == "inet" or chain_family == pkt_family


def eval_hook(index: tuple, hook: str, pkt: _Pkt) -> str:
    chains, rules = index
    results = []
    for ch in chains:
        if ch.get("hook") != hook or ch.get("type", "filter") != "filter" or not _applies(ch["family"], pkt.family):
            continue
        policy = "drop" if ch.get("policy") == "drop" else "accept"

        def base(p=policy):
            return p

        try:
            res = _Walk(rules, pkt).run((ch["family"], ch["table"], ch["name"]), 0, base)
        except RecursionError:
            res = "unknown"
        results.append(res)
    if "drop" in results:
        return BLOCKED
    if "unknown" in results:
        return UNKNOWN
    return OPEN  # 훅 체인이 없거나 모두 accept


# ---------- 판정 ----------
def _firewall(index: tuple, c: dict, nets: list) -> str:
    port, proto, fam = c["port"], c["proto"], c["family"]
    parts = [eval_hook(index, "input", _Pkt(fam, proto, port, port, nets))]
    if c["source"] == "docker":
        parts.append(eval_hook(index, "forward", _Pkt(fam, proto, c["cport"], port, nets)))
    return OPEN if OPEN in parts else UNKNOWN if UNKNOWN in parts else BLOCKED


def verify(ss_text: str | None, nft_text: str | None, docker_text: str | None, allow_text: str | None,
           *, docker_declared_absent: bool = False, input_errors: list[str] | None = None) -> tuple[dict, int]:
    undetermined = [{"reason": r} for r in (input_errors or [])]
    allow = parse_allowlist(allow_text) if allow_text is not None else []

    cands: dict[tuple, dict] = {}
    if ss_text is None:
        undetermined.append({"reason": "ss 입력 없음"})
    else:
        found, probs = parse_ss(ss_text)
        undetermined += [{"reason": p} for p in probs]
        for c in found:
            cands[_cand_key(c)] = c
    if docker_declared_absent:
        pass
    elif docker_text is None:
        undetermined.append({"reason": "docker 조회 불가(미설치 또는 입력 없음)"})
    else:
        found, probs = parse_docker(docker_text)
        undetermined += [{"reason": p} for p in probs]
        for c in found:
            # 같은 주소·포트의 ss 항목(docker-proxy)은 docker 게시 후보로 대체한다
            cands.pop(_cand_key({**c, "cport": None}), None)
            cands[_cand_key(c)] = c

    if ss_text is not None and not ss_text.strip() and not cands:
        undetermined.append({"reason": "ss 출력이 비어 있음"})

    index = None
    if nft_text is None:
        undetermined.append({"reason": "nft 조회 실패"})
    else:
        try:
            nft = json.loads(nft_text)
            if not isinstance(nft, dict):
                raise ValueError("root")
            index = _index_nft(nft)
        except _SCHEMA_ERRORS:
            undetermined.append({"reason": "nft JSON 해석 또는 구조 검증 실패"})

    ports_out, violations = [], 0
    for key in sorted(cands, key=lambda k: (k[1], k[0], k[2], k[3], k[4] or 0)):
        c = cands[key]
        proto, port = c["proto"], c["port"]
        row = {"proto": proto, "port": port, "family": c["family"], "bind": c["bind"], "source": c["source"]}
        if c["source"] == "docker":
            row["container_port"] = c["cport"]
        allowed = _allow_sources(allow, proto, port)
        restricted = allowed is not None and allowed[1] is not None
        nets = allowed[1] if restricted else []
        fw, why = UNKNOWN, None
        if index is not None:
            try:
                fw = _firewall(index, c, nets)
            except _SCHEMA_ERRORS as exc:
                why = f"{proto}/{port} nft 규칙 구조 해석 실패({type(exc).__name__})"
        row["firewall"] = fw
        if allowed is not None:
            row["allowed_sources"] = allowed[0]
        if allowed is not None and not restricted:
            row["verdict"] = "허용"
        elif fw == BLOCKED:
            row["verdict"] = "허용(출발지 제한 확인)" if restricted else "차단됨(비허용)"
        elif fw == UNKNOWN:
            row["verdict"] = UNKNOWN
            if index is not None:
                undetermined.append({"reason": why or f"{proto}/{port}({c['family']}) 방화벽 판정불가"})
        else:
            row["verdict"] = "위반"
            if restricted:
                row["reason"] = "허용 출발지 밖 접근이 열려 있음"
            violations += 1
        ports_out.append(row)

    code = 2 if undetermined else (1 if violations else 0)
    summary = {"violations": violations, "undetermined": len(undetermined),
               "undetermined_reasons": undetermined, "exit_code": code, "ports": ports_out}
    return summary, code


# ---------- 입출력 ----------
def _read(path: str | None) -> str | None:
    if path is None:
        return None
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError:
        return None


def _run(name: str) -> str | None:
    try:
        r = subprocess.run(_COLLECT_CMDS[name], capture_output=True, text=True, timeout=_TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--ss")
    ap.add_argument("--nft")
    ap.add_argument("--docker")
    ap.add_argument("--no-docker", action="store_true", help="이 호스트에 Docker 가 없음을 선언")
    ap.add_argument("--allowlist")
    ap.add_argument("--collect", action="store_true", help="명령 직접 실행(운영 서버 금지)")
    a = ap.parse_args(argv)

    errors: list[str] = []
    if a.collect:
        ss, nft, docker = _run("ss"), _run("nft"), (None if a.no_docker else _run("docker"))
    else:
        ss, docker = _read(a.ss), (None if a.no_docker else _read(a.docker))
        if a.nft is None:
            nft = '{"nftables": []}'  # 생략 = 빈 규칙셋
        else:
            nft = _read(a.nft)
    allow = _read(a.allowlist) if a.allowlist else '{"allow": []}'
    if allow is None:
        errors.append("허용 목록 파일을 읽을 수 없음")
        allow = '{"allow": []}'
    try:
        summary, code = verify(ss, nft, docker, allow, docker_declared_absent=a.no_docker, input_errors=errors)
    except (InputError, *_SCHEMA_ERRORS) as exc:
        reason = str(exc) if isinstance(exc, InputError) else f"입력 구조 해석 실패: {type(exc).__name__}"
        summary, code = {"violations": 0, "undetermined": 1, "undetermined_reasons": [{"reason": reason}],
                         "exit_code": 2, "ports": []}, 2
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())
