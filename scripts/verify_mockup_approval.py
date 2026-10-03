#!/usr/bin/env python3
"""Worker-side mockup approval gate (stdlib only; runs on the runner host).

    verify_mockup_approval.py --job-id runner-ab12cd34 [--phase pre_execution|checkpoint] [--instruction-file F|-]

Re-verifies, right before the worker starts, the same approval bundle that was checked at submit time. Unrelated
backend tasks (no MOCKUP_* lines, not a UI task) exit 0 without any network call. This script only asks the API
and prints the answer: it never kills, restarts or modifies a job.

Exit codes: 0 allowed (or gate not applicable), 10 denied by the approval state, 20 gate unavailable (API
unreachable / unreadable answer) for a task that needs the gate. Both 10 and 20 mean "do not start the worker".
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
_RULES = Path(__file__).resolve().parent.parent / "app" / "services" / "mockup_gate_rules.py"


def _load_rules():
    spec = importlib.util.spec_from_file_location("mockup_gate_rules", _RULES)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _ask(api: str, job_id: str, phase: str, timeout: float) -> dict:
    request = urllib.request.Request(
        f"{api.rstrip('/')}/api/v1/pipeline/jobs/{job_id}/mockup-gate", method="POST",
        data=json.dumps({"phase": phase}).encode(),
        headers={"x-monitor-key": "internal-pipeline-call", "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed internal URL
        return json.loads(response.read().decode())


def run(argv: list[str], stdin_text: str | None = None, environ: dict[str, str] | None = None, ask=_ask) -> tuple[int, dict]:
    env = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--phase", default="pre_execution", choices=("pre_execution", "checkpoint"))
    parser.add_argument("--instruction-file", default="-", help="path, or - for stdin")
    parser.add_argument("--api-url", default=env.get("AADS_API_URL", "http://127.0.0.1"))
    parser.add_argument("--timeout", type=float, default=15.0)
    args = parser.parse_args(argv)

    rules = _load_rules()
    if stdin_text is not None:
        instruction = stdin_text
    elif args.instruction_file == "-":
        instruction = sys.stdin.read()
    else:
        instruction = Path(args.instruction_file).read_text(encoding="utf-8", errors="replace")

    info = rules.classify(instruction, env)
    if not info["applicable"]:
        return ALLOWED, {"applicable": False, "allowed": True, "mode": info["mode"], "reasons": []}
    if not info["declared"]:
        decision = rules.no_bundle_decision(info["mode"])
        return (ALLOWED if decision["allowed"] else DENIED), {"applicable": True, "mode": info["mode"], **decision}
    if info["bundle"]["errors"]:
        reasons = [f"bundle_{e}" for e in info["bundle"]["errors"]]
        return DENIED, {"applicable": True, "allowed": False, "mode": info["mode"], "reasons": reasons}
    try:
        answer = ask(args.api_url, args.job_id, args.phase, args.timeout)
        allowed = answer["allowed"] is True
    except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError) as exc:
        decision = rules.unavailable_decision(info)
        return (ALLOWED if decision["allowed"] else UNAVAILABLE), {
            "applicable": True, "mode": info["mode"], "error": type(exc).__name__, **decision}
    return (ALLOWED if allowed else DENIED), answer


def main() -> int:
    code, result = run(sys.argv[1:])
    print(json.dumps(result, ensure_ascii=False))
    return code


if __name__ == "__main__":
    sys.exit(main())
