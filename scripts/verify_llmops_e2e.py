#!/usr/bin/env python3
"""OHVIS 내부 LLMOps E2E 검증 — 정본 API만 사용하는 재현 가능한 점검기.

이 스크립트는 저장소 **밖**에 있던 `/tmp/e2e_eval.py`를 대체한다. 옛 스크립트는
검증이라는 이름으로 DB에 직접 붙어

    DELETE FROM llmops_scores WHERE experiment_id IN (SELECT id FROM llmops_experiments WHERE status='running')
    DELETE FROM llmops_experiments WHERE status = 'running'

로 **실행 중인 모든 실험과 점수를 지우고**, 하드코딩된 UUID를 쓰고, rule
evaluator를 거치지 않은 점수를 INSERT로 지어냈다. 그건 검증이 아니라 데이터
조작이다. 그 파일은 실행/커밋/재사용하지 않는다 (docs/reports 기록 참조).

여기서 지키는 원칙:

1. **기본은 읽기 전용.** 인자 없이 돌리면 GET만 한다.
2. **쓰기는 `--write`로만**, 그리고 오직 라벨이 붙은 합성 AADS 격리 픽스처
   (dataset slug = ``aads-llmops-e2e-verify``)에만 추가된다. 운영 dataset
   ``aads-failed-traces``는 건드리지 않는다.
3. **점수는 정본 rule evaluator가 낸다.** SQL INSERT로 점수를 지어내지 않는다.
4. **삭제/일괄 정리 없음.** UPDATE로 기존 실험을 되돌리지도 않는다. 추가만 한다.
5. **UUID를 하드코딩하지 않는다.** 대상 trace는 후보 API가 돌려준 실제 id다.
6. **시크릿을 출력하지 않는다.** 인증은 기존 승인된 env만 읽는다.

인증 (기존 메커니즘 그대로, 둘 중 하나):
    AADS_MONITOR_KEY   → ``X-Monitor-Key`` 헤더
    AADS_API_TOKEN     → ``Authorization: Bearer`` 헤더 (JWT)

사용:
    python3 scripts/verify_llmops_e2e.py                    # 읽기 전용
    python3 scripts/verify_llmops_e2e.py --write            # 합성 픽스처 E2E
    python3 scripts/verify_llmops_e2e.py --json             # 기계 판독용 출력

종료 코드: 0 = 전부 통과, 1 = 검증 실패, 2 = 설정/인증 문제.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

DEFAULT_BASE_URL = "https://aads.newtalk.kr"
API_PREFIX = "/api/v1/ohvis/llmops"
DEFAULT_PROJECT = "AADS"

# 합성 픽스처는 운영 dataset과 슬러그부터 분리한다. 이 슬러그 밖으로는
# 아무것도 쓰지 않는다.
FIXTURE_DATASET_SLUG = "aads-llmops-e2e-verify"
FIXTURE_LABEL = "e2e-verify"

# 옛 스크립트를 되살리지 못하게 이름을 남겨둔다 (docs/report와 교차 참조).
RETIRED_SCRIPT = "/tmp/e2e_eval.py"

MONITOR_KEY_ENV = "AADS_MONITOR_KEY"
BEARER_ENV = "AADS_API_TOKEN"
USER_AGENT = "aads-llmops-e2e-verify/1.0"

# 스코프 집계가 전역 집계를 넘어설 수 없다 — 넘으면 프로젝트 격리가 깨진 것이다.
SCOPED_METRICS = ("datasets", "examples", "experiments", "scores", "feedback")


class VerifyError(RuntimeError):
    """검증 자체를 진행할 수 없는 설정/인증 문제."""


# ── HTTP ────────────────────────────────────────────────────────────────────


def build_auth_headers() -> dict[str, str]:
    """승인된 기존 env만 읽어 인증 헤더를 만든다. 값은 절대 출력하지 않는다."""
    monitor_key = (os.getenv(MONITOR_KEY_ENV) or "").strip()
    if monitor_key:
        return {"X-Monitor-Key": monitor_key}
    bearer = (os.getenv(BEARER_ENV) or "").strip()
    if bearer:
        return {"Authorization": f"Bearer {bearer}"}
    raise VerifyError(
        f"인증 정보가 없다. {MONITOR_KEY_ENV} 또는 {BEARER_ENV}를 환경변수로 넣어라 "
        "(값은 로그에 남기지 않는다)."
    )


def request_json(
    base_url: str,
    path: str,
    *,
    headers: dict[str, str],
    params: Optional[dict[str, Any]] = None,
    body: Optional[dict[str, Any]] = None,
    timeout: int = 30,
) -> Any:
    url = base_url.rstrip("/") + path
    if params:
        clean = {key: value for key, value in params.items() if value is not None}
        if clean:
            url = f"{url}?{urllib.parse.urlencode(clean)}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(  # noqa: S310 — 고정된 내부 https 호스트
        url,
        data=data,
        method="POST" if body is not None else "GET",
        # 기본 User-Agent(Python-urllib)는 앞단에서 403으로 걷어차인다.
        headers={"Accept": "application/json", "User-Agent": USER_AGENT, **headers,
                 **({"Content-Type": "application/json"} if data else {})},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return json.loads(response.read().decode("utf-8") or "null")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        if exc.code in (401, 403):
            raise VerifyError(f"{path} 인증 실패 ({exc.code}) — env 인증 정보를 확인하라") from exc
        raise VerifyError(f"{path} HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise VerifyError(f"{path} 연결 실패: {exc.reason}") from exc


# ── 검사 ────────────────────────────────────────────────────────────────────


class Report:
    def __init__(self) -> None:
        self.checks: list[dict[str, Any]] = []
        self.facts: dict[str, Any] = {}

    def record(self, name: str, ok: bool, detail: str) -> bool:
        self.checks.append({"check": name, "ok": bool(ok), "detail": detail})
        return bool(ok)

    @property
    def failed(self) -> list[dict[str, Any]]:
        return [check for check in self.checks if not check["ok"]]


def _counts(db: dict[str, Any]) -> dict[str, Any]:
    """상태 응답에서 지표만 뽑는다. 없는 키는 없는 채로 둔다 (0으로 꾸미지 않는다)."""
    counts = {key: db[key] for key in SCOPED_METRICS if key in db}
    traces = db.get("traces")
    if isinstance(traces, dict):
        counts.update({f"traces.{field}": value for field, value in traces.items()})
    return counts


def check_status_scope(base_url: str, headers: dict[str, str], project: str, report: Report) -> None:
    """스코프 집계가 실제로 좁혀지는지 — 이게 이번 수정의 핵심 계약이다."""
    scoped = request_json(base_url, f"{API_PREFIX}/status", headers=headers,
                          params={"project": project})
    global_status = request_json(base_url, f"{API_PREFIX}/status", headers=headers)

    scoped_db = scoped.get("db") or {}
    global_db = global_status.get("db") or {}
    report.facts["status_scoped"] = _counts(scoped_db)
    report.facts["status_global"] = _counts(global_db)
    report.facts["foundation_ready"] = scoped_db.get("foundation_ready")

    report.record(
        "status.available",
        scoped_db.get("available") is True,
        f"db.available={scoped_db.get('available')} error={scoped_db.get('error')}",
    )
    report.record(
        "status.project_echoed",
        scoped.get("project") == project,
        f"project={scoped.get('project')!r}",
    )
    report.record(
        "status.project_scoped_flag",
        scoped_db.get("project_scoped") is True and global_db.get("project_scoped") is False,
        f"scoped={scoped_db.get('project_scoped')} global={global_db.get('project_scoped')}",
    )
    report.record(
        "status.reports_scores_and_feedback",
        "scores" in scoped_db and "feedback" in scoped_db,
        f"scores={scoped_db.get('scores', 'missing')} feedback={scoped_db.get('feedback', 'missing')}",
    )

    # 격리 불변식: 프로젝트 총계는 전역 총계를 절대 넘을 수 없다.
    leaks = [
        f"{metric}: {scoped_db[metric]} > {global_db[metric]}"
        for metric in SCOPED_METRICS
        if metric in scoped_db and metric in global_db and scoped_db[metric] > global_db[metric]
    ]
    report.record("status.no_scope_leak", not leaks, "; ".join(leaks) or "스코프 ≤ 전역")

    # 이 DB에 다른 프로젝트가 실재하면 스코프가 실제로 좁혀졌는지도 확인한다.
    narrowed = [
        metric
        for metric in SCOPED_METRICS
        if metric in scoped_db and metric in global_db and scoped_db[metric] < global_db[metric]
    ]
    report.record(
        "status.scope_actually_narrows",
        bool(narrowed),
        f"좁혀진 지표: {narrowed or '없음 (다른 프로젝트 데이터가 없으면 정상)'}",
    )


def check_traces_and_candidates(
    base_url: str, headers: dict[str, str], project: str, report: Report
) -> Optional[dict[str, Any]]:
    traces = request_json(base_url, f"{API_PREFIX}/traces", headers=headers,
                          params={"project": project, "hours": 720, "limit": 50})
    listed = traces.get("traces") or []
    foreign = sorted({item.get("project") for item in listed} - {project})
    report.record(
        "traces.scoped_to_project",
        traces.get("available") is True and not foreign,
        f"{len(listed)}건, 다른 project 값: {foreign or '없음'}",
    )

    candidates = request_json(base_url, f"{API_PREFIX}/candidates", headers=headers,
                              params={"project": project, "hours": 720, "limit": 20})
    items = candidates.get("candidates") or []
    report.facts["candidate_count"] = len(items)
    report.record("candidates.reachable", isinstance(items, list), f"{len(items)}건")

    # 정본 API가 돌려준 실제 trace만 쓴다 — UUID를 지어내지 않는다.
    v2 = [item for item in items if item.get("source_table") == "llmops_traces"]
    return v2[0] if v2 else None


def run_write_verification(
    base_url: str,
    headers: dict[str, str],
    project: str,
    trace: dict[str, Any],
    before: dict[str, Any],
    report: Report,
) -> None:
    """정본 승격 → 평가 → 결과 조회. 추가만 하고 아무것도 지우지 않는다."""
    trace_id = trace["id"]
    report.facts["fixture_trace_id"] = trace_id
    report.facts["fixture_dataset_slug"] = FIXTURE_DATASET_SLUG

    promote_body = {
        "trace_id": trace_id,
        "dataset_slug": FIXTURE_DATASET_SLUG,
        "project": project,
        "force": True,
    }
    first = request_json(base_url, f"{API_PREFIX}/datasets/from-trace",
                         headers=headers, body=promote_body)
    report.record(
        "promote.creates_fixture_example",
        first.get("promoted") is True and first.get("example_id"),
        f"example_id={first.get('example_id')} reason={first.get('reason')}",
    )
    report.facts["fixture_dataset_id"] = first.get("dataset_id")
    report.facts["fixture_example_id"] = first.get("example_id")

    # 같은 trace를 다시 승격해도 example은 하나여야 한다 (멱등성).
    second = request_json(base_url, f"{API_PREFIX}/datasets/from-trace",
                          headers=headers, body=promote_body)
    report.record(
        "promote.is_idempotent",
        second.get("example_id") == first.get("example_id")
        and second.get("dataset_id") == first.get("dataset_id"),
        f"1회차={first.get('example_id')} 2회차={second.get('example_id')}",
    )

    # 점수는 정본 rule evaluator가 낸다. SQL INSERT로 지어내지 않는다.
    run = request_json(
        base_url,
        f"{API_PREFIX}/evals/run",
        headers=headers,
        body={
            "dataset_slug": FIXTURE_DATASET_SLUG,
            "name": f"{FIXTURE_LABEL} {project}",
            "created_by": FIXTURE_LABEL,
            "limit": 50,
        },
    )
    experiment_id = run.get("experiment_id")
    report.facts["fixture_experiment_id"] = experiment_id
    report.record(
        "eval.rule_evaluator_ran",
        run.get("ok") is True and bool(experiment_id) and run.get("evaluator"),
        f"experiment_id={experiment_id} evaluator={run.get('evaluator')} "
        f"summary={run.get('summary')}",
    )

    if experiment_id:
        result = request_json(base_url, f"{API_PREFIX}/evals/{experiment_id}", headers=headers)
        scores = result.get("scores") or []
        report.facts["fixture_score_count"] = len(scores)
        report.record(
            "eval.result_is_readable",
            result.get("status") == "completed" and bool(scores),
            f"status={result.get('status')} scores={len(scores)} "
            f"dataset={result.get('dataset_slug')}",
        )
        report.record(
            "eval.scores_come_from_the_rule_evaluator",
            all(score.get("criterion") for score in scores)
            and result.get("evaluator") == run.get("evaluator"),
            f"evaluator={result.get('evaluator')} "
            f"criteria={sorted({s.get('criterion') for s in scores})}",
        )

    after = request_json(base_url, f"{API_PREFIX}/status", headers=headers,
                         params={"project": project})
    after_counts = _counts(after.get("db") or {})
    deltas = {
        metric: after_counts[metric] - before[metric]
        for metric in SCOPED_METRICS
        if metric in before and metric in after_counts
    }
    report.facts["status_delta_scoped"] = deltas
    unreported = [metric for metric in ("experiments", "scores") if metric not in deltas]
    report.record(
        "status.reflects_the_new_fixture",
        not unreported and deltas["experiments"] >= 1 and deltas["scores"] >= 1,
        f"delta={deltas}"
        + (f" — 상태 응답이 {unreported}를 아예 내지 않는다" if unreported else ""),
    )
    report.record(
        "status.is_additive_only",
        all(value >= 0 for value in deltas.values()),
        f"음수 delta 없음: {deltas}",
    )


# ── 진입점 ──────────────────────────────────────────────────────────────────


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default=os.getenv("AADS_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--project", default=DEFAULT_PROJECT)
    parser.add_argument(
        "--write",
        action="store_true",
        help="라벨이 붙은 합성 AADS 격리 픽스처에 한해 승격/평가까지 실행한다 (추가만).",
    )
    parser.add_argument("--json", action="store_true", help="결과를 JSON으로 출력한다")
    args = parser.parse_args(argv)

    report = Report()
    try:
        headers = build_auth_headers()
        check_status_scope(args.base_url, headers, args.project, report)
        candidate = check_traces_and_candidates(args.base_url, headers, args.project, report)

        if args.write:
            before = _counts(
                (
                    request_json(
                        args.base_url, f"{API_PREFIX}/status", headers=headers,
                        params={"project": args.project},
                    ).get("db")
                    or {}
                )
            )
            if candidate is None:
                report.record(
                    "promote.creates_fixture_example",
                    False,
                    f"{args.project}에 승격 가능한 v2 trace가 없다 — 대상을 지어내지 않고 중단한다",
                )
            else:
                run_write_verification(
                    args.base_url, headers, args.project, candidate, before, report
                )
    except VerifyError as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        else:
            print(f"[설정/인증 오류] {exc}", file=sys.stderr)
        return 2

    ok = not report.failed
    payload = {
        "ok": ok,
        "base_url": args.base_url,
        "project": args.project,
        "mode": "write" if args.write else "read-only",
        "retired_script_not_used": RETIRED_SCRIPT,
        "facts": report.facts,
        "checks": report.checks,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        print(f"LLMOps E2E — {args.base_url} project={args.project} "
              f"mode={payload['mode']}")
        for check in report.checks:
            print(f"  [{'PASS' if check['ok'] else 'FAIL'}] {check['check']}: {check['detail']}")
        print(f"\nfacts: {json.dumps(report.facts, ensure_ascii=False, default=str)}")
        print(f"결과: {'통과' if ok else f'{len(report.failed)}건 실패'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
