#!/usr/bin/env python3
"""Worker-side mockup approval gate (stdlib only; runs on the runner host).

    verify_mockup_approval.py --job-id runner-ab12cd34 [--phase pre_execution|checkpoint] [--instruction-file F|-]
    verify_mockup_approval.py --phase pre_push --task-id push-<sha> --changed-files F --instruction-file MSG_FILE
    verify_mockup_approval.py --classify-only [--instruction-file F|-]
    verify_mockup_approval.py --offline [--changed-files F] [--instruction-file F|-]

Re-verifies, right before the worker starts, the same approval bundle that was checked at submit time. Unrelated
backend tasks (no MOCKUP_* lines, not a UI task) exit 0 without any network call. This script only asks the API
and prints the answer: it never kills, restarts or modifies a job.

Exit codes: 0 allowed (or gate not applicable), 10 denied by the approval state, 20 gate unavailable (API
unreachable / unreadable answer) for a task that needs the gate. Both 10 and 20 mean "do not start the worker".

pre_push (git hook) judges one commit: --changed-files lists its files, --instruction-file is its message. A denial
or an unreachable API only returns 10/20 under MOCKUP_GATE_MODE=enforce; under shadow the answer carries
"blocking": false and the exit code is 0, so the hook warns without stopping the push.

--classify-only runs the server rules (mockup_gate_rules.classify) and nothing else: exit 0 = gate not applicable,
11 = applicable. The runner uses it to decide what a crashed or timed-out gate run means.

--offline is for CI, where the API is out of reach. --changed-files is classified like pre_push (classify_paths) and
the text (commit messages) carries the MOCKUP_* declaration; it reports "UI change without a bundle declaration". It
never claims a bundle is approved -- a declared bundle is reported as approval_verified=false. Exit 10 only under
MOCKUP_GATE_MODE=enforce (or for a malformed declaration); the default shadow mode only warns.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ALLOWED, DENIED, UNAVAILABLE = 0, 10, 20
APPLICABLE = 11
_RULES = Path(__file__).resolve().parent.parent / "app" / "services" / "mockup_gate_rules.py"


def _load_rules():
    spec = importlib.util.spec_from_file_location("mockup_gate_rules", _RULES)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _post(api: str, path: str, payload: dict, timeout: float) -> dict:
    request = urllib.request.Request(
        f"{api.rstrip('/')}{path}", method="POST", data=json.dumps(payload).encode(),
        headers={"x-monitor-key": "internal-pipeline-call", "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed internal URL
        return json.loads(response.read().decode())


def _ask(api: str, job_id: str, phase: str, timeout: float, **extra) -> dict:
    if phase == "pre_push":
        return _post(api, "/api/v1/pipeline/mockup-gate/push",
                     {"phase": phase, "task_id": job_id, "project": extra.get("project", "AADS"),
                      "paths": extra.get("paths", []), "message": extra.get("message", "")}, timeout)
    return _post(api, f"/api/v1/pipeline/jobs/{job_id}/mockup-gate", {"phase": phase}, timeout)


def _record(ask, args, phase: str, **extra) -> None:
    """Let the API write its verdict row for a decision the CLI already made locally. Never changes the exit code."""
    try:
        ask(args.api_url, args.job_id, phase, min(args.timeout, 5.0), **extra)
    except Exception:  # noqa: BLE001 - evidence is best effort
        pass


def _offline(rules, info: dict) -> tuple[int, dict]:
    out = {"offline": True, "approval_verified": False, "mode": info["mode"], "applicable": info["applicable"],
           "ui_task": info["ui_task"], "ui_reasons": info["ui_reasons"], "declared": info["declared"]}
    if not info["applicable"]:
        return ALLOWED, {**out, "allowed": True, "reasons": []}
    if info["bundle"]["errors"]:
        return DENIED, {**out, "allowed": False, "reasons": [f"bundle_{e}" for e in info["bundle"]["errors"]]}
    if info["declared"]:
        # Only the server can say whether the bundle is approved; offline can just confirm it is well formed.
        return ALLOWED, {**out, "allowed": True, "reasons": ["bundle_declared_not_verified_offline"]}
    decision = rules.no_bundle_decision(info["mode"])
    return (ALLOWED if decision["allowed"] else DENIED), {**out, **decision}


def run(argv: list[str], stdin_text: str | None = None, environ: dict[str, str] | None = None, ask=_ask) -> tuple[int, dict]:
    env = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--job-id", "--task-id", dest="job_id")
    parser.add_argument("--phase", default="pre_execution", choices=("pre_execution", "checkpoint", "pre_push"))
    parser.add_argument("--instruction-file", default="-", help="path, or - for stdin (the commit message for pre_push)")
    parser.add_argument("--changed-files", default="", help="pre_push / offline: file with one changed path per line")
    parser.add_argument("--project", default="AADS")
    parser.add_argument("--api-url", default=env.get("AADS_API_URL", "http://127.0.0.1"))
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--classify-only", action="store_true", help="server rules only; exit 0 or 11")
    parser.add_argument("--offline", action="store_true", help="CI mode: no API call, never claims approval")
    args = parser.parse_args(argv)
    if not (args.job_id or args.classify_only or args.offline):
        parser.error("--job-id is required unless --classify-only or --offline")

    rules = _load_rules()
    if stdin_text is not None:
        instruction = stdin_text
    elif args.instruction_file == "-":
        instruction = sys.stdin.read()
    else:
        instruction = Path(args.instruction_file).read_text(encoding="utf-8", errors="replace")

    extra: dict = {}
    if args.phase == "pre_push" or args.offline:
        paths = []
        if args.changed_files:
            paths = [p for p in Path(args.changed_files).read_text(encoding="utf-8", errors="replace").splitlines() if p.strip()]
        info = rules.classify_paths(paths, instruction, env)
        extra = {"project": args.project, "paths": paths, "message": instruction}
    else:
        info = rules.classify(instruction, env)
    if args.classify_only:
        return (APPLICABLE if info["applicable"] else ALLOWED), {
            k: info[k] for k in ("mode", "applicable", "declared", "ui_task", "ui_reasons")}
    if args.offline:
        return _offline(rules, info)
    if not info["applicable"]:
        return ALLOWED, {"applicable": False, "allowed": True, "mode": info["mode"], "reasons": []}

    def finish(code: int, answer: dict) -> tuple[int, dict]:
        # chat_write / pre_push guard work that never went through the runner: they only stop it under enforce.
        if code != ALLOWED and not rules.stops_action(args.phase, info["mode"]):
            return ALLOWED, {**answer, "blocking": False, "would_exit": code}
        return code, answer

    local = None
    if not info["declared"]:
        decision = rules.no_bundle_decision(info["mode"])
        local = (ALLOWED if decision["allowed"] else DENIED), {"applicable": True, "mode": info["mode"], **decision}
    elif info["bundle"]["errors"]:
        reasons = [f"bundle_{e}" for e in info["bundle"]["errors"]]
        local = DENIED, {"applicable": True, "allowed": False, "mode": info["mode"], "reasons": reasons}
    if local is not None:
        _record(ask, args, args.phase, **extra)
        return finish(*local)
    try:
        answer = ask(args.api_url, args.job_id, args.phase, args.timeout, **extra)
        allowed = answer["allowed"] is True
    except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError) as exc:
        decision = rules.unavailable_decision(info)
        return finish(ALLOWED if decision["allowed"] else UNAVAILABLE, {
            "applicable": True, "mode": info["mode"], "error": type(exc).__name__, **decision})
    return finish(ALLOWED if allowed else DENIED, answer)


def main() -> int:
    code, result = run(sys.argv[1:])
    print(json.dumps(result, ensure_ascii=False))
    return code


if __name__ == "__main__":
    sys.exit(main())
