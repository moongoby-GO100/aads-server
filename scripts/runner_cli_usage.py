#!/usr/bin/env python3
"""러너 CLI 사용량을 oauth_usage_log 에 job_id 와 함께 남긴다.

AADS-LLM-M9-COST-BASIS-20260930.

러너(pipeline-runner.sh)는 Claude/Codex CLI 를 호스트에서 직접 띄운다. 앱의
릴레이 경로를 거치지 않으므로 그동안 oauth_usage_log 에 한 줄도 남지 않았고,
러너 비용을 러너 작업(pipeline_jobs)에 귀속할 수 없었다.

동작 — 두 단계는 **따로 불리고 따로 실패한다** (rework 1, 2026-09-30):
  restore     claude_cli 를 `--output-format json` 으로 띄운 출력 파일이 결과
              객체면 원문을 <out>.usage.json 으로 보존하고, 출력 파일을 예전
              text 모드와 같은 결과 텍스트로 되돌린다(하류 판정 불변). 결과
              객체가 아니면(빈 파일·오류 텍스트) 건드리지 않는다. 실패하면 0 이
              아닌 종료코드를 낸다 — 러너가 출력 파일을 다시 검사해 판정한다.
  usage       <out>.usage.json(없으면 출력 파일)을 **읽기만** 하고 INSERT SQL 을
              낸다. 출력 파일을 쓰지 않으므로 이 단계가 실패해도 러너 결과는
              오염되지 않는다. 사용량 한 건을 잃을 뿐이다.
              claude_cli: modelUsage 의 모델별 costUSD 를 relay_reported 로 적는다.
                          결과 객체가 없으면 비용 미측정 행 하나를 적는다.
              codex_cli:  Codex exec 출력에는 토큰 분해·비용이 없다. 추측하지
                          않고 비용 미측정(unknown) 행 하나로 귀속만 남긴다.

usage 는 표준출력으로 INSERT SQL 을 낸다. 실행은 러너의 db_update 가 한다 — 여기서 DB 에
붙지 않는 이유는 러너가 이미 docker/원격 두 방식의 접속을 한 곳(_psql_cmd)에서
관리하기 때문이다. 정가 재계산(cost_usd_catalog)은 DB 함수가 한다.

이 스크립트는 러너 호스트의 python3 로 돈다. app 패키지를 import 하지 않는다.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional

CALL_SOURCE = {
    "claude_cli": "runner_claude_cli",
    "codex_cli": "runner_codex_cli",
}
_INT_MAX = 2_147_483_647


def _int(value: Any) -> int:
    try:
        n = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, min(n, _INT_MAX))


def _cost(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_claude_result(text: str) -> Optional[Dict[str, Any]]:
    """CLI `-p --output-format json` 결과 객체. 아니면 None.

    결과 객체는 한 줄이다. 앞에 경고 줄이 섞여도 찾도록 마지막 `{` 줄까지 본다.
    """
    stripped = (text or "").strip()
    if not stripped:
        return None
    candidates = [stripped]
    candidates.extend(
        line.strip() for line in reversed(stripped.splitlines())
        if line.strip().startswith("{")
    )
    for candidate in candidates:
        if not candidate.startswith("{"):
            continue
        try:
            payload = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(payload, dict) and payload.get("type") == "result":
            return payload
    return None


def usage_rows_from_claude_result(
    payload: Dict[str, Any],
    fallback_model: str = "",
) -> List[Dict[str, Any]]:
    """모델별 사용량 행. 비용은 CLI 가 보고한 값만 쓴다(relay_reported).

    modelUsage 가 있으면 모델별로, 없으면 최상위 usage/total_cost_usd 로 한 행.
    보고된 비용이 없으면 cost_usd=None, cost_source='unknown'.
    """
    rows: List[Dict[str, Any]] = []
    model_usage = payload.get("modelUsage")
    if isinstance(model_usage, dict) and model_usage:
        for name, mu in model_usage.items():
            if not isinstance(mu, dict):
                continue
            cost = _cost(mu.get("costUSD"))
            rows.append({
                "model": str(name).split("[")[0][:60],
                "input_tokens": _int(mu.get("inputTokens")),
                "output_tokens": _int(mu.get("outputTokens")),
                "cache_read_tokens": _int(mu.get("cacheReadInputTokens")),
                "cache_creation_tokens": _int(mu.get("cacheCreationInputTokens")),
                "cost_usd": cost,
                "cost_source": "relay_reported" if cost is not None else "unknown",
            })
    if not rows:
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        cost = _cost(payload.get("total_cost_usd"))
        rows.append({
            "model": (fallback_model or "unknown")[:60],
            "input_tokens": _int(usage.get("input_tokens")),
            "output_tokens": _int(usage.get("output_tokens")),
            "cache_read_tokens": _int(usage.get("cache_read_input_tokens")),
            "cache_creation_tokens": _int(usage.get("cache_creation_input_tokens")),
            "cost_usd": cost,
            "cost_source": "relay_reported" if cost is not None else "unknown",
        })
    return rows


def unmeasured_row(model: str) -> Dict[str, Any]:
    return {
        "model": (model or "unknown")[:60],
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "cost_usd": None,
        "cost_source": "unknown",
    }


def _lit(value: Any) -> str:
    if value is None:
        return "NULL"
    text = str(value).replace("\x00", "")
    return "'" + text.replace("'", "''") + "'"


def build_insert_sql(
    rows: List[Dict[str, Any]],
    *,
    job_id: str,
    call_source: str,
    account_slot: str,
    session_id: str = "",
    error_code: Optional[str] = None,
    duration_ms: int = 0,
) -> str:
    """oauth_usage_log INSERT. cost_usd_catalog 는 DB 함수로만 채운다."""
    if not rows or not job_id:
        return ""
    job = _lit(job_id[:100])
    values = []
    for r in rows:
        cost = r.get("cost_usd")
        cost_sql = "NULL" if cost is None else repr(float(cost))
        values.append(
            "(" + ", ".join([
                _lit((account_slot or "runner")[:20]),
                "''",
                _lit(r["model"]),
                str(_int(r["input_tokens"])),
                str(_int(r["output_tokens"])),
                str(_int(r["cache_creation_tokens"])),
                str(_int(r["cache_read_tokens"])),
                cost_sql,
                _lit(r["cost_source"]),
                "llm_catalog_cost_usd(%s, %d, %d)" % (
                    _lit(r["model"]), _int(r["input_tokens"]), _int(r["output_tokens"])
                ),
                _lit(call_source[:30]),
                _lit((session_id or "")[:100]),
                job,
                _lit(error_code[:20]) if error_code else "NULL",
                str(_int(duration_ms)),
                "COALESCE((SELECT tenant_id FROM pipeline_jobs WHERE job_id = %s LIMIT 1), "
                "aads_internal_tenant_id())" % job,
            ]) + ")"
        )
    return (
        "INSERT INTO oauth_usage_log (account_slot, token_prefix, model, input_tokens, "
        "output_tokens, cache_creation_tokens, cache_read_tokens, cost_usd, cost_source, "
        "cost_usd_catalog, call_source, session_id, job_id, error_code, duration_ms, tenant_id) "
        "VALUES " + ", ".join(values) + ";"
    )


def usage_raw_path(output_file: str) -> str:
    return output_file + ".usage.json"


def restore_text_output(output_file: str, payload: Dict[str, Any]) -> None:
    """원문 JSON 을 보존하고 출력 파일을 text 모드 결과와 같게 되돌린다."""
    with open(output_file, "r", encoding="utf-8", errors="replace") as f:
        raw = f.read()
    with open(usage_raw_path(output_file), "w", encoding="utf-8") as f:
        f.write(raw)
    result = payload.get("result")
    text = result if isinstance(result, str) else ""
    tmp = output_file + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        if text and not text.endswith("\n"):
            f.write("\n")
    os.replace(tmp, output_file)


def restore_output(output_file: str) -> str:
    """restore 단계. 'restored' 또는 'not_json'. 입출력 오류는 예외로 올린다.

    같은 출력 파일을 재시도가 다시 쓰므로, 지난 시도의 <out>.usage.json 이
    이번 시도의 사용량으로 읽히지 않게 먼저 지운다.
    """
    raw_path = usage_raw_path(output_file)
    if os.path.exists(raw_path):
        os.remove(raw_path)
    try:
        with open(output_file, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except FileNotFoundError:
        return "not_json"
    payload = parse_claude_result(text)
    if payload is None:
        return "not_json"
    restore_text_output(output_file, payload)
    return "restored"


def _read_usage_payload(output_file: str) -> Optional[Dict[str, Any]]:
    for path in (usage_raw_path(output_file), output_file):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                payload = parse_claude_result(f.read())
        except OSError:
            continue
        if payload is not None:
            return payload
    return None


def usage_sql(
    kind: str,
    output_file: str,
    *,
    job_id: str,
    account_slot: str,
    model: str,
    exit_code: int,
    duration_ms: int,
) -> str:
    """usage 단계. 파일을 읽기만 한다."""
    call_source = CALL_SOURCE.get(kind)
    if not call_source or not job_id:
        return ""
    error_code = None if exit_code == 0 else ("exit_%d" % exit_code)[:20]

    if kind == "claude_cli":
        payload = _read_usage_payload(output_file)
        if payload is not None:
            if payload.get("is_error") and not error_code:
                error_code = "cli_error"
            rows = usage_rows_from_claude_result(payload, fallback_model=model)
            return build_insert_sql(
                rows,
                job_id=job_id,
                call_source=call_source,
                account_slot=account_slot,
                session_id=str(payload.get("session_id") or ""),
                error_code=error_code,
                duration_ms=duration_ms,
            )
        return build_insert_sql(
            [unmeasured_row(model)],
            job_id=job_id,
            call_source=call_source,
            account_slot=account_slot,
            error_code=error_code or "no_usage_json",
            duration_ms=duration_ms,
        )

    return build_insert_sql(
        [unmeasured_row(model)],
        job_id=job_id,
        call_source=call_source,
        account_slot=account_slot,
        error_code=error_code,
        duration_ms=duration_ms,
    )


def process(kind: str, output_file: str, **kwargs: Any) -> str:
    """restore 후 usage. 단위시험·수동 점검용 — 러너는 두 단계를 따로 부른다."""
    if kind == "claude_cli":
        restore_output(output_file)
    return usage_sql(kind, output_file, **kwargs)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("mode", nargs="?", choices=("restore", "usage"), default="usage")
    ap.add_argument("--kind", default="claude_cli")
    ap.add_argument("--output-file", required=True)
    ap.add_argument("--job-id", default="")
    ap.add_argument("--slot", default="")
    ap.add_argument("--model", default="")
    ap.add_argument("--exit-code", type=int, default=0)
    ap.add_argument("--duration-ms", type=int, default=0)
    args = ap.parse_args(argv)
    if args.mode == "restore":
        try:
            status = restore_output(args.output_file)
        except OSError as e:
            print("runner_cli_usage: restore failed: %s" % e, file=sys.stderr)
            return 2
        sys.stdout.write(status)
        return 0
    sql = usage_sql(
        args.kind,
        args.output_file,
        job_id=args.job_id,
        account_slot=args.slot or ("codex" if args.kind == "codex_cli" else "runner"),
        model=args.model,
        exit_code=args.exit_code,
        duration_ms=args.duration_ms,
    )
    if sql:
        sys.stdout.write(sql)
    return 0


if __name__ == "__main__":
    sys.exit(main())
