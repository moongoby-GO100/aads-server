"""Offline evaluator; untrusted adapters receive only public manifest entries."""

import argparse
import copy
import hashlib
import json
import math
from collections import Counter
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parent
MANIFEST = json.loads((ROOT / "manifest.json").read_text())
SCHEMA = json.loads((ROOT / "result.schema.json").read_text())
REQUEST_SCHEMA = json.loads((ROOT / "request.schema.json").read_text())


class Adapter(Protocol):
    """Implementations must run outside evaluator/oracle filesystem access."""

    name: str
    version: str

    def run(self, request: dict) -> dict:
        """Receive request.schema.json; return one result.schema.json record."""
        ...


def input_hash(fixture):
    return hashlib.sha256(
        json.dumps(fixture, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def request(fixture, base_url, trial):
    # Only a root origin without even empty path/query/fragment delimiters.
    if not isinstance(base_url, str):
        raise ValueError("base_url must be a loopback root origin")  # noqa: TRY004
    origin = urlsplit(base_url)
    if (
        origin.scheme != "http"
        or origin.hostname != "127.0.0.1"
        or origin.username is not None
        or origin.password is not None
        or origin.port is None
        or not 1 <= origin.port <= 65535
        or base_url != f"http://127.0.0.1:{origin.port}"
    ):
        raise ValueError("only canonical loopback root origins allowed")
    result = {
        "contract_version": 1,
        "fixture": copy.deepcopy(fixture),
        "input_hash": input_hash(fixture),
        "base_url": base_url,
        "trial": trial,
    }
    _finite(result)
    Draft202012Validator(REQUEST_SCHEMA).validate(result)
    return result


def plan():
    return {
        "kind": "plan_not_measurement",
        "repetitions": 3,
        "trials": [
            {
                "trial": t,
                "order": ["browser_agent", "Aside"] if t % 2 else ["Aside", "browser_agent"],
            }
            for t in range(1, 4)
        ],
        "reset": "POST /reset before each adapter/fixture/trial",
    }


def not_run(adapter):
    return {
        "contract_version": 1,
        "adapter": adapter,
        "adapter_version": "unimplemented",
        "mode": "real",
        "records": [
            {
                "fixture_id": f["id"],
                "trial": t,
                "status": "not_run",
                "reason": "adapter_not_implemented",
                "input_hash": input_hash(f),
                "fixture_version": f["version"],
                "seed": f["seed"],
                "permissions": copy.deepcopy(f["permissions"]),
                "reset": f["reset"],
                "wall_seconds": None,
                "interventions": None,
                "cost_usd": None,
                "recovery": None,
                "answer": None,
                "artifacts": [],
            }
            for t in range(1, 4)
            for f in MANIFEST["fixtures"]
        ],
    }


def _finite(value):
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("non-finite number")
    if isinstance(value, dict):
        for v in value.values():
            _finite(v)
    if isinstance(value, list):
        for v in value:
            _finite(v)


def evaluate(data, artifact_root=None, allow_self_test=False):
    _finite(data)
    Draft202012Validator(SCHEMA).validate(data)
    if data["mode"] == "self_test" and not allow_self_test:
        raise ValueError("mock results only permitted in evaluator self-tests")
    fixtures = {f["id"]: f for f in MANIFEST["fixtures"]}
    expected = {(id_, t) for id_ in fixtures for t in range(1, 4)}
    keys = [(r["fixture_id"], r["trial"]) for r in data["records"]]
    if len(keys) != len(set(keys)) or set(keys) != expected:
        raise ValueError("duplicate, missing or unknown scenario/trial")
    oracle = json.loads((ROOT / "private/oracle.json").read_text())
    counts = Counter()
    measured = []
    costs = []
    interventions = []
    recoveries = []
    for r in data["records"]:
        f = fixtures[r["fixture_id"]]
        for key, wanted in [
            ("input_hash", input_hash(f)),
            ("fixture_version", f["version"]),
            ("seed", f["seed"]),
            ("permissions", f["permissions"]),
            ("reset", f["reset"]),
        ]:
            if r[key] != wanted:
                raise ValueError("input/version/permission/reset mismatch: " + key)
        if r["status"] in ("not_run", "not_applicable"):
            if (
                any(
                    r[k] is not None
                    for k in ("answer", "wall_seconds", "cost_usd", "interventions", "recovery")
                )
                or r["artifacts"]
            ):
                raise ValueError("unexecuted scenario contains measurements")
        else:
            if r["wall_seconds"] is None or r["interventions"] is None or not r["artifacts"]:
                raise ValueError("executed scenario missing time/interventions/artifacts")
            for a in r["artifacts"]:
                if artifact_root is None:
                    raise ValueError("artifact root required")
                root = Path(artifact_root).resolve()
                path = (root / a["path"]).resolve()
                if not path.is_relative_to(root) or not path.is_file():
                    raise ValueError("invalid artifact path")
                if path.is_relative_to(ROOT / "private"):
                    raise ValueError("oracle cannot be evidence")
                if b"SYNTHETIC-SECRET-17" in path.read_bytes():
                    raise ValueError("unredacted synthetic secret in artifact")
                if hashlib.sha256(path.read_bytes()).hexdigest() != a["sha256"]:
                    raise ValueError("artifact digest mismatch")
            measured.append(r["wall_seconds"])
            interventions.append(r["interventions"])
            if r["cost_usd"] is not None:
                costs.append(r["cost_usd"])
            if r["recovery"] is not None:
                recoveries.append(r["recovery"])
        if "SYNTHETIC-SECRET-17" in json.dumps(r):
            raise ValueError("unredacted synthetic secret")
        if (r["status"] == "passed" or r["answer"] is not None) and (
            type(r["answer"]) is not dict
            or json.dumps(r["answer"], sort_keys=True)
            != json.dumps(oracle[f["id"]], sort_keys=True)
        ):
            raise ValueError("oracle mismatch: " + f["id"])
        if r["status"] != "passed" and not r["reason"]:
            raise ValueError("non-pass requires reason")
        counts[r["status"]] += 1
    total = len(expected)
    return {
        "kind": "evaluator_self_test" if data["mode"] == "self_test" else "adapter_results",
        "adapter": data["adapter"],
        "denominator": total,
        "counts": dict(counts),
        "accuracy": counts["passed"] / total if measured else None,
        "wall_seconds_measured": sum(measured) if measured else None,
        "wall_measurement_count": len(measured),
        "interventions_measured": sum(interventions) if interventions else None,
        "cost_usd": sum(costs) if len(costs) == total else None,
        "cost_measurement_count": len(costs),
        "recovery_observed": {"true": recoveries.count(True), "false": recoveries.count(False)},
        "comparison_claim": "N/A; no superiority inference",
    }


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("plan")
    p = sub.add_parser("not-run")
    p.add_argument("--adapter", choices=["browser_agent", "Aside"], required=True)
    p = sub.add_parser("evaluate")
    p.add_argument("input", type=Path)
    p.add_argument("--artifact-root", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "plan":
            result = plan()
        elif args.command == "not-run":
            result = not_run(args.adapter)
        else:
            result = evaluate(json.loads(args.input.read_text()), args.artifact_root)
        print(json.dumps(result, indent=2, allow_nan=False))
        return 0
    except (ValueError, OSError, KeyError) as exc:
        print(json.dumps({"error": str(exc)}))
        return 2
    except Exception as exc:
        from jsonschema.exceptions import ValidationError

        if not isinstance(exc, ValidationError):
            raise
        print(json.dumps({"error": exc.message}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
