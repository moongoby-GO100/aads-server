"""Version-preserving Claude execution contract, shared by API, relay and runners.

stdlib only; usable by the standalone host relay and remote shell runner.
This allowlist describes accepted IDs, not account entitlement or availability.
"""
import json
import sys

CONTRACT_VERSION = 1
AADS_MODEL_IDS = {
    "claude-sonnet": "claude-sonnet-4-6",
    "claude-sonnet-5": "claude-sonnet-5",
    "claude-opus": "claude-opus-5",
    "claude-opus-5": "claude-opus-5",
    "claude-opus-46": "claude-opus-4-6",
    "claude-haiku": "claude-haiku-4-5-20251001",
    "claude-fable-5": "claude-fable-5",
    "claude-fable-5-1": "claude-fable-5-1",
}
ALIASES = dict(AADS_MODEL_IDS, **{
    "claude-fable-5.1": "claude-fable-5-1",
    "claude-fable-latest": "claude-fable-5-1",
    "claude-haiku-4-5": "claude-haiku-4-5-20251001",
    # Pin previously floating CLI aliases. Legacy AADS claude-sonnet remains 4.6.
    "opus": "claude-opus-5",
    "sonnet": "claude-sonnet-5",
    "haiku": "claude-haiku-4-5-20251001",
})
EXACT_MODEL_IDS = frozenset(AADS_MODEL_IDS.values()) | frozenset((
    "claude-opus-4-5", "claude-opus-4-6", "claude-opus-4-7", "claude-opus-4-8",
    "claude-sonnet-4-5", "claude-sonnet-4-5-20250929",
    "claude-opus-4-5-20251101", "claude-3-5-sonnet-20241022",
    "claude-3-sonnet-20240229", "claude-3-opus-20240229",
    "claude-3-5-haiku-20241022", "claude-3-haiku-20240307", "claude-2.1",
))


def resolve_model(model):
    value = str(model or "").strip()
    resolved = ALIASES.get(value, value)
    if resolved not in EXACT_MODEL_IDS:
        raise ValueError("unsupported_claude_model: %s" % value)
    return resolved


def runtime_alias(model):
    value = str(model or "").strip()
    try:
        resolved = resolve_model(value)
    except ValueError:
        return value  # Non-Claude routing is outside this contract.
    for alias, exact in AADS_MODEL_IDS.items():
        if resolved == exact:
            return alias
    return resolved


def session_key(session_id, slot=None, model=None):
    if not session_id:
        return ""
    slot = str(slot or "")
    key = "%s@%s" % (session_id, slot) if slot not in ("", "0", "none", "proxy") else session_id
    return "%s@%s" % (key, resolve_model(model)) if model else key


class ModelObservation:
    """Do not mistake a subagent's first modelUsage entry for the main model."""

    def __init__(self, requested_model="", cli_model=""):
        self.requested_model = requested_model
        self.cli_model = cli_model
        self.primary_models = set()
        self.used_models = set()

    def observe(self, event):
        if event.get("type") == "assistant" and not event.get("parent_tool_use_id"):
            model = (event.get("message") or {}).get("model")
            if model and str(model).startswith("claude-"):
                self.primary_models.add(str(model).split("[")[0])
        if event.get("type") == "result":
            self.used_models.update(str(m).split("[")[0] for m in (event.get("modelUsage") or {}))
        self.used_models.update(self.primary_models)
        # Usage is aggregate across subagents: even a single usage key alone is
        # not proof of which model produced the primary response.
        actual = next(iter(self.primary_models)) if len(self.primary_models) == 1 else "unverified"
        verified = actual != "unverified"
        return {
            "version": CONTRACT_VERSION,
            "requested_model": self.requested_model,
            "cli_model": self.cli_model,
            "actual_model": actual,
            "model_verified": verified,
            "verification_source": "assistant.message.model" if verified else "unverified",
            "model_mismatch": verified and bool(self.cli_model) and actual != self.cli_model,
            "used_models": sorted(self.used_models),
        }


if __name__ == "__main__":
    if sys.argv[1:] == ["--describe"]:
        print(json.dumps({"version": CONTRACT_VERSION, "models": sorted(EXACT_MODEL_IDS)}))
    else:
        try:
            print(resolve_model(sys.argv[1] if len(sys.argv) == 2 else ""))
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            sys.exit(2)
