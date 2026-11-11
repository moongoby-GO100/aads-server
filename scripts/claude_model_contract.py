"""Version-preserving Claude execution contract, shared by API, relay and runners.

stdlib only; usable by the standalone host relay and remote shell runner.
Relay validation is syntax-only; API execution authorization lives in the DB cache.
"""
import json
import re
import sys

# Protocol version only; catalog changes must never bump this value.
CONTRACT_VERSION = 1
AADS_MODEL_IDS = {
    "claude-sonnet": "claude-sonnet-5-5",
    "claude-sonnet-5-5": "claude-sonnet-5-5",
    "claude-sonnet-5": "claude-sonnet-5",
    "claude-opus": "claude-opus-5-5",
    "claude-opus-5-5": "claude-opus-5-5",
    "claude-opus-5": "claude-opus-5",
    "claude-opus-46": "claude-opus-4-6",
    "claude-haiku": "claude-haiku-4-5-20251001",
    "claude-haiku-5-5": "claude-haiku-5-5",
    "claude-fable-5": "claude-fable-5",
    "claude-fable-5-1": "claude-fable-5-1",
    "claude-fable-5-2": "claude-fable-5-2",
}
ALIASES = dict(AADS_MODEL_IDS, **{
    "claude-fable-5.1": "claude-fable-5-1",
    "claude-fable-5.2": "claude-fable-5-2",
    "claude-fable-latest": "claude-fable-5-1",
    "claude-haiku-4-5": "claude-haiku-4-5-20251001",
    "claude-haiku-5.5": "claude-haiku-5-5",
    "opus": "claude-opus-5-5",
    "sonnet": "claude-sonnet-5-5",
    "haiku": "claude-haiku-4-5-20251001",
})
# Offline authorization fallback only when DB and last successful cache are unavailable.
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


_MODEL_PATTERN = re.compile(r"claude-[a-z]+-[0-9]+(?:-[0-9]+)*", re.ASCII)


def resolve_relay_model(model):
    value = str(model or "")
    resolved = ALIASES.get(value, value)
    if resolved not in EXACT_MODEL_IDS and not _MODEL_PATTERN.fullmatch(resolved):
        raise ValueError("unsupported_claude_model: %s" % value)
    return resolved


def resolve_authorized_model(model, rows):
    """Standalone runner authorization against a successful DB snapshot.

    None means DB unavailable with no last success -> offline code fallback.
    A successful snapshot is authoritative, EMPTY included (denies everything).
    Sync-side guards, not this function, prevent an accidental all-off catalog.
    """
    if rows is None:
        return resolve_model(model)  # Offline fallback only.
    exact = resolve_relay_model(model)
    for row in rows:
        model_id = str(row.get("model_id") or "")
        try:
            canonical = resolve_relay_model(model_id)
            execution = resolve_relay_model(row.get("execution_model_id") or model_id)
        except ValueError:
            continue
        if model == model_id or exact in (canonical, execution):
            return execution
    raise ValueError("unsupported_claude_model: %s" % model)


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
    return "%s@%s" % (key, resolve_relay_model(model)) if model else key


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
            if len(sys.argv) == 3 and sys.argv[1] == "--authorize":
                lines = [ln.strip() for ln in sys.stdin.read().splitlines() if ln.strip()]
                try:
                    # Last non-empty line: tolerate stray psql tags before the JSON.
                    snapshot = json.loads(lines[-1]) if lines else None
                except ValueError:
                    print("claude_snapshot_unparseable: using offline fallback", file=sys.stderr)
                    snapshot = None
                if snapshot is not None and not isinstance(snapshot, list):
                    print("claude_snapshot_not_array: using offline fallback", file=sys.stderr)
                    snapshot = None
                print(resolve_authorized_model(sys.argv[2], snapshot))
            else:
                print(resolve_relay_model(sys.argv[1] if len(sys.argv) == 2 else ""))
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            sys.exit(2)
