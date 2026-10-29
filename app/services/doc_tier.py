"""Server-owned document classification; settings are read at call time."""
import os


def settings() -> dict:
    return {
        "long_chars": int(os.getenv("AADS_DOC_TIER_LONG_CHARS", "15000")),
        "split_chars": int(os.getenv("AADS_DOC_TIER_SPLIT_CHARS", "50000")),
        "goal_link_enabled": os.getenv("AADS_DOC_TIER_GOAL_LINK_ENABLED", "false").lower() in ("1", "true", "yes"),
        "d3_daily_limit": int(os.getenv("AADS_DOC_TIER_D3_DAILY_LIMIT", "20")),
    }


def compute_doc_tier(kind: str, char_count: int, goal_linked: bool = False) -> str:
    config = settings()
    if char_count > config["split_chars"]:
        return "SPLIT"
    if kind in {"prd", "contract", "architecture"} or char_count > config["long_chars"] or (
        goal_linked and config["goal_link_enabled"]
    ):
        return "D3"
    if kind in {"plan", "design", "spec", "tasks"}:
        return "D2"
    return "D1"


def provider_of(model: str | None) -> str:
    if not isinstance(model, str):
        return "unknown"
    model = model.strip().lower()
    if not model or model == "unknown":
        return "unknown"
    if model.startswith(("claude-", "claude:")):
        return "anthropic"
    if model.startswith("codex:"):
        return "openai"
    return "other:" + model.split(":", 1)[0]


def revision_metadata(row, kind: str) -> dict:
    data = dict(row)
    result = {key: data.get(key) for key in ("doc_tier", "author_model", "author_provider")}
    if result["doc_tier"] is None:
        result["doc_tier_computed"] = compute_doc_tier(
            kind, data.get("char_count") if data.get("char_count") is not None else len(data.get("content") or ""),
            bool(data.get("goal_linked") or data.get("goal_id")),
        )
    return result
