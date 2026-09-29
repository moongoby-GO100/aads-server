"""Browser recipe registry for API-less authenticated admin automation."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from typing import Any
from urllib.parse import urlparse

from app.core.db_pool import get_pool
from app.services.browser_permission_policy import classify_browser_action, mask_sensitive_value
from app.services.managed_browser import egress_for_target, normalize_egress_policy, normalize_origin, normalize_work_key

ALLOWED_RUNTIMES = {"pc_agent", "self_hosted_playwright", "external_sandbox", "auto"}
ALLOWED_QUEUE_STRATEGIES = {"fifo", "priority", "latest_only", "reject_on_conflict"}
DEFAULT_CONCURRENCY_POLICY = {
    "max_parallel_runs": 1,
    "queue_strategy": "fifo",
    "conflict_keys": ["work_key", "origin"],
}
DEFAULT_RESOURCE_POLICY = {
    "runtime": "auto",
    "max_browser_contexts": 1,
    "max_memory_mb": 1024,
    "max_runtime_seconds": 900,
    "artifact_budget_mb": 256,
}
ACTIVE_RECIPE_RUN_STATUSES = ("queued", "running", "approval_required")
COUPANGEATS_LOGIN_URL = "https://store.coupangeats.com/merchant/login"
# Draft instructions for a screen recording operator.  They are deliberately
# not browser_recipes rows and cannot be played or approved by this module.
COUPANGEATS_LOGIN_FRAGMENTS = (
    {
        "name": "coupangeats_01_open_login",
        "version": 2,
        "domain": "store.coupangeats.com",
        "lane": "pc_agent",
        "work_key": "coupangeats:owner:login",
        "starts_when": "authenticated_pc_session_available",
        "succeeds_when": "login_form_visible_without_access_denied_or_challenge",
        "predecessor": None,
        "recover_at": "open_login_start",
        "credential_scope": "yeoljeong-coupangeats",
        "step_requirements": ["navigate_exact_login_url"],
        "verify_requirements": ["visible_login_form", "no_access_denied_or_challenge"],
    },
    {
        "name": "coupangeats_02_vault_fill",
        "version": 2,
        "domain": "store.coupangeats.com",
        "lane": "pc_agent",
        "work_key": "coupangeats:owner:login",
        "starts_when": "open_login_v2_screen_verified_in_same_pc_session",
        "succeeds_when": "vault_identity_matches_and_password_field_filled",
        "predecessor": {"name": "coupangeats_01_open_login", "version": 2},
        "recover_at": "open_login_form",
        "credential_scope": "yeoljeong-coupangeats",
        "password_check": "filled_or_empty_only",
        "step_requirements": ["fill_username_from_vault", "fill_password_from_vault"],
        "verify_requirements": ["username_matches_vault", "password_field_filled_boolean_only"],
    },
    {
        "name": "coupangeats_03_login_confirm",
        "version": 1,
        "domain": "store.coupangeats.com",
        "lane": "pc_agent",
        "work_key": "coupangeats:owner:login",
        "starts_when": "approved_vault_fill_v2_screen_verified_in_same_pc_session",
        "succeeds_when": "authenticated_store_matches_vault_without_access_denied_captcha_or_login_error",
        "predecessor": {"name": "coupangeats_02_vault_fill", "version": 2},
        "recover_at": "open_login_form_without_resubmitting",
        "credential_scope": "yeoljeong-coupangeats",
        "step_requirements": ["click_observed_submit_control"],
        "verify_requirements": ["authenticated_username_matches_vault", "authenticated_store_matches_vault", "no_error_or_challenge"],
    },
)


def matches_coupangeats_vault_identity(
    *,
    vault_scope: str,
    vault_username: str,
    vault_store: str,
    screen_username: str,
    screen_store: str,
) -> bool:
    """Compare transient Vault identity with the screen; persist neither value."""
    return (
        vault_scope == "yeoljeong-coupangeats"
        and hmac.compare_digest(vault_username.encode("utf-8"), b"mimi77")
        and bool(vault_username and vault_store and screen_username and screen_store)
        and hmac.compare_digest(vault_username.encode("utf-8"), screen_username.encode("utf-8"))
        and hmac.compare_digest(vault_store.encode("utf-8"), screen_store.encode("utf-8"))
    )


def prepare_coupangeats_login_drafts(
    saved_open_login: dict[str, Any] | None, *, trusted_evidence: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Check the saved WorkRecipe v2 before using it as a split-login reference.

    A missing/unverified row never becomes an approved reference.  A matching
    row only proves the open-login definition, not login or replay success.
    """
    row = saved_open_login or {}
    spec = _json_dict(row.get("spec"))
    steps = _json_list(spec.get("steps"))
    verify = _json_list(spec.get("verify"))
    metadata = _json_dict(spec.get("metadata"))
    evidence = _json_dict(metadata.get("screen_e2e"))
    artifact = evidence.get("screenshot_url") or evidence.get("snapshot_ref")
    login_url = urlparse(COUPANGEATS_LOGIN_URL)
    def is_login_url(value: Any) -> bool:
        parsed = urlparse(str(value or ""))
        return (parsed.scheme, parsed.hostname, parsed.path.rstrip("/")) == (
            login_url.scheme, login_url.hostname, login_url.path.rstrip("/"))

    valid = (
        row.get("name") == "coupangeats_01_open_login"
        and row.get("domain") == "store.coupangeats.com"
        and row.get("version") == 2
        and row.get("enabled") is True
        and row.get("max_risk") == "READ"
        and row.get("approval_status") == "approved"
        and spec.get("name") == row.get("name")
        and spec.get("version") == 2
        and spec.get("domain") == row.get("domain")
        and any(isinstance(step, dict) and step.get("action") == "navigate"
                and is_login_url(step.get("url")) for step in steps)
        and any(isinstance(step, dict) and step.get("action") == "snapshot"
                and step.get("assertion") == "login_form_visible"
                for step in verify)
        and evidence.get("screen_verified") is True
        and is_login_url(evidence.get("url"))
        and isinstance(artifact, str) and bool(artifact.strip())
        and evidence.get("outcome") == COUPANGEATS_LOGIN_FRAGMENTS[0]["succeeds_when"]
        and evidence.get("blocked") is False
        and evidence_matches_run(evidence, trusted_evidence, fragment_name=row.get("name"))
        and metadata.get("source_chat_session_id") == trusted_evidence.get("chat_session_id")
        and metadata.get("starts_when") == COUPANGEATS_LOGIN_FRAGMENTS[0]["starts_when"]
        and metadata.get("succeeds_when") == COUPANGEATS_LOGIN_FRAGMENTS[0]["succeeds_when"]
        and metadata.get("lane") == "pc_agent"
        and metadata.get("work_key") == COUPANGEATS_LOGIN_FRAGMENTS[0]["work_key"]
    )
    fragments = [dict(item) for item in COUPANGEATS_LOGIN_FRAGMENTS]
    fragments[0]["registration_state"] = "approved" if valid else "registration_pending"
    fragments[0]["registration_ready"] = valid
    for item in fragments[1:]:
        item["registration_state"] = "registration_pending"
        item["registration_ready"] = False
    return {
        "reference_verified": valid,
        "reference_reason": "saved_v2_definition_verified"
        if valid
        else "saved_v2_definition_unverified",
        "fragments": fragments,
        "replay_state": "not_verified",
        "registration_path": "work_recipe.registration.request_registration",
        "requires_observed_selectors": True,
    }


def build_coupangeats_registration_candidate(
    *, fragment_name: str, observed_steps: list[dict[str, Any]],
    observed_verify: list[dict[str, Any]], screen_evidence: dict[str, Any],
    trusted_evidence: dict[str, Any] | None = None,
) -> Any:
    """Build a pending WorkRecipe only from observed controls and screen evidence.

    This does not request approval or execute a browser. The reviewer must inspect
    the referenced screen; a caller's screen_verified flag is not independent proof.
    """
    from app.services.work_recipe.schema import WorkRecipe

    fragment = next((item for item in COUPANGEATS_LOGIN_FRAGMENTS
                     if item["name"] == fragment_name), None)
    if fragment is None:
        raise ValueError("unknown_fragment")
    evidence = _json_dict(screen_evidence)
    artifact = evidence.get("screenshot_url") or evidence.get("snapshot_ref")
    parsed = urlparse(str(evidence.get("url") or ""))
    if (evidence.get("screen_verified") is not True
            or not isinstance(artifact, str) or not artifact.strip()
            or parsed.scheme != "https" or parsed.hostname != fragment["domain"]
            or evidence.get("outcome") != fragment["succeeds_when"]
            or evidence.get("blocked") is not False
            or not evidence_matches_run(evidence, trusted_evidence, fragment_name=fragment_name)):
        raise ValueError("verified_screen_evidence_required")
    expected = (["navigate"] if fragment_name == "coupangeats_01_open_login" else
                ["fill", "fill"] if fragment_name == "coupangeats_02_vault_fill" else ["click"])
    required_assertions = {
        "coupangeats_01_open_login": ["login_form_visible"],
        "coupangeats_02_vault_fill": ["username_matches_vault", "password_filled"],
        "coupangeats_03_login_confirm": ["authenticated_username_matches_vault", "authenticated_store_matches_vault"],
    }
    if ([step.get("action") for step in observed_steps] != expected
            or [step.get("assertion") for step in observed_verify] != required_assertions[fragment_name]
            or any(step.get("action") != "snapshot" for step in observed_verify)
            or any(set(step) - {"action", "selector", "url", "value", "risk", "timeout"}
                   for step in observed_steps)
            or any(set(step) - {"action", "selector", "assertion", "risk", "timeout"}
                   for step in observed_verify)
            or any((not str(step.get("selector") or "").strip()
                    or "<" in str(step.get("selector")))
                   for step in [*observed_steps, *observed_verify]
                   if step.get("action") != "navigate")):
        raise ValueError("observed_steps_and_verify_required")
    if fragment_name == "coupangeats_02_vault_fill" and [
        step.get("value") for step in observed_steps
    ] != ["{{vault_username}}", "{{vault_password}}"]:
        raise ValueError("vault_placeholders_required")
    if fragment_name == "coupangeats_01_open_login" and observed_steps[0].get("url") != COUPANGEATS_LOGIN_URL:
        raise ValueError("exact_login_url_required")
    if fragment_name == "coupangeats_01_open_login" and observed_steps[0].get("risk", "READ") != "READ":
        raise ValueError("open_login_read_risk_required")
    if fragment_name != "coupangeats_03_login_confirm" and evidence.get("url") != COUPANGEATS_LOGIN_URL:
        raise ValueError("exact_login_url_required")
    if fragment_name == "coupangeats_03_login_confirm" and evidence.get("url") == COUPANGEATS_LOGIN_URL:
        raise ValueError("authenticated_screen_url_required")
    if any(step.get("value") is not None for step in observed_verify):
        raise ValueError("verify_must_not_contain_secret_value")
    safe_evidence = {key: evidence[key] for key in
                     ("screenshot_url", "snapshot_ref", "url", "captured_at", "outcome", "run_id", "artifact_id")
                     if key in evidence}
    safe_evidence["screen_verified"] = True
    safe_evidence["blocked"] = False
    recipe = {
        "name": fragment["name"], "domain": fragment["domain"],
        "version": fragment["version"],
        "inputs": ([{"name": "vault_username", "secret": True},
                    {"name": "vault_password", "secret": True}]
                   if fragment_name == "coupangeats_02_vault_fill" else
                   [{"name": "vault_username", "secret": True},
                    {"name": "vault_store", "secret": True}]
                   if fragment_name == "coupangeats_03_login_confirm" else []),
        "steps": [{**step, **({"risk": "WRITE_EXTERNAL"} if fragment_name == "coupangeats_03_login_confirm" else {})}
                  for step in observed_steps], "verify": observed_verify,
        "metadata": {
            "lane": fragment["lane"], "work_key": fragment["work_key"],
            "starts_when": fragment["starts_when"],
            "succeeds_when": fragment["succeeds_when"],
            "predecessor": fragment["predecessor"],
            "recover_at": fragment["recover_at"],
            "credential_scope": fragment["credential_scope"],
            "source_chat_session_id": trusted_evidence["chat_session_id"],
            "screen_e2e": safe_evidence,
        },
    }
    return WorkRecipe.from_dict(recipe)


async def read_coupangeats_login_drafts(*, tenant_id: str) -> dict[str, Any]:
    """Read the tenant's approved v2 and return a secret-free readiness plan."""
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT r.name, r.domain, r.version, r.enabled, r.max_risk, r.spec,
                   a.status AS approval_status
              FROM work_recipes r
              JOIN work_recipe_registration_requests a
                ON a.recipe_id = r.id AND a.tenant_id = r.tenant_id
             WHERE r.tenant_id = $1 AND r.name = $2
               AND r.domain = $3 AND r.version = 2
             LIMIT 1
            """,
            uuid.UUID(str(tenant_id)),
            "coupangeats_01_open_login",
            "store.coupangeats.com",
        )
    saved = dict(row) if row else None
    evidence = _json_dict(_json_dict(_json_dict(saved.get("spec")).get("metadata")).get("screen_e2e")) if saved else {}
    trusted = await lookup_coupangeats_run_evidence(
        tenant_id=tenant_id, chat_session_id=None, run_id=evidence.get("run_id"),
        artifact_id=evidence.get("artifact_id"),
    ) if evidence.get("run_id") or evidence.get("artifact_id") else None
    return prepare_coupangeats_login_drafts(saved, trusted_evidence=trusted)


def evidence_matches_run(
    claimed: dict[str, Any], trusted: dict[str, Any] | None, *, fragment_name: str
) -> bool:
    """Only a completed, session-bound audit artifact can support a draft."""
    if (not trusted or trusted.get("status") != "success"
            or trusted.get("route") != "pc_agent" or trusted.get("verified") is not True
            or trusted.get("recipe_name") != fragment_name):
        return False
    ref = claimed.get("screenshot_url") or claimed.get("snapshot_ref")
    return bool(ref and (
                    (claimed.get("run_id") and claimed.get("run_id") == trusted.get("run_id"))
                    or (claimed.get("artifact_id") and claimed.get("artifact_id") == trusted.get("artifact_id")))
                and ref == trusted.get("screenshot_url")
                and claimed.get("url") == trusted.get("url")
                and trusted.get("chat_session_id"))


async def lookup_coupangeats_run_evidence(
    *, tenant_id: str, chat_session_id: str | None, run_id: Any = None,
    artifact_id: Any = None,
) -> dict[str, Any] | None:
    """Read the server's append-only successful verify record; never trust a supplied flag."""
    if artifact_id:
        try:
            artifact_uuid = uuid.UUID(str(artifact_id))
        except (TypeError, ValueError):
            return None
        async with get_pool().acquire() as conn:
            row = await conn.fetchrow(
                """SELECT id, source_url, storage_uri, metadata FROM browser_artifacts
                    WHERE id=$1 AND tenant_id=$2 AND artifact_type='coupangeats_screen_e2e'""",
                artifact_uuid, uuid.UUID(str(tenant_id)),
            )
        if not row:
            return None
        metadata = _json_dict(row["metadata"])
        if chat_session_id and metadata.get("chat_session_id") != chat_session_id:
            return None
        return {
            "artifact_id": str(row["id"]), "status": "success",
            "verified": metadata.get("verified") is True,
            "recipe_name": metadata.get("fragment_name"),
            "chat_session_id": metadata.get("chat_session_id"),
            "route": metadata.get("route"), "url": row["source_url"],
            "screenshot_url": row["storage_uri"],
        }
    try:
        run_uuid = uuid.UUID(str(run_id))
    except (TypeError, ValueError):
        return None
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            """SELECT r.id, r.status, r.recipe_name, r.triggered_by, s.output, s.screenshot_path
                 FROM recipe_runs r JOIN recipe_run_steps s ON s.run_id = r.id
                WHERE r.id=$1 AND r.tenant_id=$2 AND r.domain=$3
                  AND r.status='success' AND s.phase='verify' AND s.status='success'
                  AND s.output->'output'->>'verified'='true'
                  AND ($4::text IS NULL OR r.triggered_by=$4)
                ORDER BY s.seq DESC LIMIT 1""",
            run_uuid, uuid.UUID(str(tenant_id)), "store.coupangeats.com",
            f"chat:{chat_session_id}" if chat_session_id else None,
        )
    if not row:
        return None
    output = _json_dict(row["output"])
    audit = _json_dict(output.get("evidence"))
    return {
        "run_id": str(row["id"]), "status": str(row["status"]),
        "recipe_name": str(row["recipe_name"]),
        "verified": _json_dict(output.get("output")).get("verified") is True,
        "chat_session_id": str(row["triggered_by"] or "").removeprefix("chat:"),
        "route": output.get("route"), "url": audit.get("url"),
        "screenshot_url": row["screenshot_path"],
    }


async def attest_coupangeats_live_screen(
    *, tenant_id: str, chat_session_id: str, fragment_name: str,
    browser_session_id: str, browser_work_key: str,
    observed_verify: list[dict[str, Any]], values: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Verify the designated PC page and persist a server-owned screen artifact.

    This reads the already completed screen. It never submits a login or stores
    a password, cookie, token, or field value.
    """
    from app.browser_bridge.service import get_browser_bridge_service
    from app.services.work_recipe.executor import BrowserRecipeExecutor
    from app.services.work_recipe.orchestrator import CoupangVerificationExecutor
    from app.services.work_recipe.schema import WorkRecipe

    fragment = next((item for item in COUPANGEATS_LOGIN_FRAGMENTS
                     if item["name"] == fragment_name), None)
    session = get_browser_bridge_service().sessions.get(browser_session_id)
    if (fragment is None or session is None or session.work_key != fragment["work_key"]
            or browser_work_key != fragment["work_key"]
            or str(session.endpoint.kind.value) != "local_agent"):
        raise ValueError("coupangeats_pc_session_required")
    recipe = WorkRecipe.from_dict({
        "name": fragment_name, "domain": fragment["domain"],
        "metadata": {"credential_scope": fragment["credential_scope"]},
        "steps": [{"action": "snapshot", "selector": "body"}],
        "verify": observed_verify,
    })
    executor = BrowserRecipeExecutor(
        browser_session_id=browser_session_id, browser_work_key=browser_work_key
    )
    page = await executor._get_page({
        "url": COUPANGEATS_LOGIN_URL,
        "context": {"smart_browser": {"server_access_blocked": True}},
    })
    # PC Agent click() does not refresh the facade's cached URL.
    current_url = str(await page.evaluate("window.location.href") or "")
    page.url = current_url
    parsed = urlparse(current_url)
    if (parsed.scheme != "https" or parsed.hostname != fragment["domain"]
            or (fragment_name != "coupangeats_03_login_confirm"
                and current_url != COUPANGEATS_LOGIN_URL)
            or (fragment_name == "coupangeats_03_login_confirm"
                and current_url == COUPANGEATS_LOGIN_URL)):
        raise ValueError("coupangeats_screen_url_mismatch")
    verifier = CoupangVerificationExecutor(executor, recipe, values)
    last: dict[str, Any] = {}
    for step in recipe.verify:
        last = await verifier({
            **step.to_dict(), "phase": "verify",
            "context": {"smart_browser": {"server_access_blocked": True}},
        })
        if last.get("ok") is not True:
            raise ValueError("coupangeats_live_verify_failed")
    screenshot = _json_dict(_json_dict(last.get("evidence")).get("screenshot"))
    screenshot_url = str(screenshot.get("url") or "")
    digest = str(screenshot.get("sha256") or "")
    if not screenshot_url or not digest:
        raise ValueError("coupangeats_screen_artifact_required")
    metadata = {
        "fragment_name": fragment_name, "chat_session_id": chat_session_id,
        "browser_session_id": browser_session_id, "work_key": browser_work_key,
        "route": "pc_agent", "verified": True,
    }
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            """INSERT INTO browser_artifacts
                (tenant_id, artifact_type, source_url, content_hash, storage_uri, metadata)
                VALUES ($1, 'coupangeats_screen_e2e', $2, $3, $4, $5::jsonb)
                ON CONFLICT (tenant_id, content_hash) DO NOTHING
                RETURNING id""",
            uuid.UUID(str(tenant_id)), current_url, digest, screenshot_url,
            json.dumps(metadata),
        )
        if row is None:
            previous = await conn.fetchrow(
                """SELECT id, source_url, storage_uri, metadata FROM browser_artifacts
                    WHERE tenant_id=$1 AND content_hash=$2
                      AND artifact_type='coupangeats_screen_e2e'""",
                uuid.UUID(str(tenant_id)), digest,
            )
            if (previous is None or _json_dict(previous["metadata"]) != metadata
                    or previous["source_url"] != current_url):
                raise ValueError("coupangeats_screen_artifact_already_recorded")
            row = previous
            screenshot_url = str(previous["storage_uri"])
    evidence = {
        "screen_verified": True, "artifact_id": str(row["id"]),
        "screenshot_url": screenshot_url, "url": current_url,
        "outcome": fragment["succeeds_when"], "blocked": False,
    }
    return evidence, {**metadata, "artifact_id": str(row["id"]),
                      "status": "success", "recipe_name": fragment_name,
                      "chat_session_id": chat_session_id, "url": current_url,
                      "screenshot_url": screenshot_url}


RECIPE_HASH_FIELDS = (
    "recipe_id",
    "version",
    "service",
    "allowed_origins",
    "work_key_template",
    "runtime_policy",
    "concurrency_policy",
    "resource_policy",
    "login_steps",
    "challenge_policy",
    "navigation_steps",
    "capture_rules",
    "parser_id",
    "upload_rules",
    "risk_actions",
    "verifier",
    "fallbacks",
)


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _int_between(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def normalize_concurrency_policy(policy: dict[str, Any] | None) -> dict[str, Any]:
    item = {**DEFAULT_CONCURRENCY_POLICY, **_json_dict(policy)}
    queue_strategy = str(item.get("queue_strategy") or "fifo").strip().lower()
    if queue_strategy not in ALLOWED_QUEUE_STRATEGIES:
        queue_strategy = "fifo"
    conflict_keys = [
        str(key).strip() for key in _json_list(item.get("conflict_keys")) if str(key).strip()
    ]
    return {
        "max_parallel_runs": _int_between(
            item.get("max_parallel_runs"), default=1, minimum=1, maximum=20
        ),
        "queue_strategy": queue_strategy,
        "conflict_keys": conflict_keys[:20] or ["work_key", "origin"],
    }


def normalize_resource_policy(policy: dict[str, Any] | None) -> dict[str, Any]:
    item = {**DEFAULT_RESOURCE_POLICY, **_json_dict(policy)}
    runtime = str(item.get("runtime") or "auto").strip().lower()
    if runtime not in ALLOWED_RUNTIMES:
        runtime = "auto"
    return {
        "runtime": runtime,
        "max_browser_contexts": _int_between(
            item.get("max_browser_contexts"), default=1, minimum=1, maximum=50
        ),
        "max_memory_mb": _int_between(
            item.get("max_memory_mb"), default=1024, minimum=256, maximum=32768
        ),
        "max_runtime_seconds": _int_between(
            item.get("max_runtime_seconds"), default=900, minimum=30, maximum=86400
        ),
        "artifact_budget_mb": _int_between(
            item.get("artifact_budget_mb"), default=256, minimum=1, maximum=10240
        ),
    }


def normalize_allowed_origins(origins: list[Any]) -> list[str]:
    normalized: list[str] = []
    for origin in origins:
        try:
            value = normalize_origin(str(origin))
        except ValueError:
            value = ""
        if value and value not in normalized:
            normalized.append(value)
    return normalized


def normalize_recipe_payload(payload: dict[str, Any]) -> dict[str, Any]:
    recipe_id = str(payload.get("recipe_id") or "").strip()
    if not recipe_id:
        raise ValueError("recipe_id_required")
    version = str(payload.get("version") or "v1").strip()
    if not version:
        raise ValueError("version_required")
    work_key_template = normalize_work_key(str(payload.get("work_key_template") or recipe_id))
    allowed_origins = normalize_allowed_origins(_json_list(payload.get("allowed_origins")))
    if not allowed_origins:
        raise ValueError("allowed_origin_required")
    service = str(payload.get("service") or recipe_id.split(".")[0]).strip()
    runtime_policy = _json_dict(payload.get("runtime_policy"))
    resource_policy = normalize_resource_policy(payload.get("resource_policy") or runtime_policy)
    runtime_policy = {
        **runtime_policy,
        "runtime": resource_policy["runtime"],
        "egress_policy": normalize_egress_policy(runtime_policy.get("egress_policy") or "direct"),
    }
    return {
        "recipe_id": recipe_id,
        "version": version,
        "title": str(payload.get("title") or recipe_id).strip()[:300],
        "service": service[:120],
        "allowed_origins": allowed_origins,
        "work_key_template": work_key_template,
        "runtime_policy": runtime_policy,
        "concurrency_policy": normalize_concurrency_policy(payload.get("concurrency_policy")),
        "resource_policy": resource_policy,
        "login_steps": _json_list(payload.get("login_steps")),
        "challenge_policy": _json_dict(payload.get("challenge_policy")),
        "navigation_steps": _json_list(payload.get("navigation_steps")),
        "capture_rules": _json_dict(payload.get("capture_rules")),
        "parser_id": str(payload.get("parser_id") or "").strip()[:200],
        "upload_rules": _json_dict(payload.get("upload_rules")),
        "risk_actions": _json_list(payload.get("risk_actions")),
        "verifier": _json_dict(payload.get("verifier")),
        "fallbacks": _json_dict(payload.get("fallbacks")),
        "enabled": bool(payload.get("enabled", True)),
    }


def compute_recipe_hash(recipe: dict[str, Any]) -> str:
    canonical = {key: recipe.get(key) for key in RECIPE_HASH_FIELDS}
    raw = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_recipe_dry_run_plan(recipe: dict[str, Any], *, target_url: str = "") -> dict[str, Any]:
    normalized = normalize_recipe_payload(recipe)
    version_hash = compute_recipe_hash(normalized)
    risk_actions = []
    for item in normalized["risk_actions"]:
        if not isinstance(item, dict):
            continue
        action_type = str(item.get("action_type") or item.get("type") or "").strip()
        summary = str(item.get("summary") or item.get("action_summary") or "")
        policy = classify_browser_action(action_type, summary, _json_dict(item.get("payload")))
        risk_actions.append(
            {
                "action_type": action_type,
                "summary": summary,
                "policy": policy.to_dict(),
                "approval_required": policy.decision == "ask",
            }
        )
    return {
        "recipe_id": normalized["recipe_id"],
        "version": normalized["version"],
        "version_hash": version_hash,
        "target_origin": normalize_origin(target_url)
        if target_url
        else normalized["allowed_origins"][0],
        "runtime": normalized["resource_policy"]["runtime"],
        "runtime_plan": build_runtime_execution_plan(normalized, target_url=target_url),
        "concurrency_policy": normalized["concurrency_policy"],
        "resource_policy": normalized["resource_policy"],
        "required_approvals": [item for item in risk_actions if item["approval_required"]],
        "blocked_actions": [item for item in risk_actions if item["policy"]["decision"] == "deny"],
        "artifact_capture": bool(normalized["capture_rules"]),
        "upload_enabled": bool(normalized["upload_rules"]),
    }


def build_recipe_concurrency_key(
    recipe: dict[str, Any], *, target_url: str = "", work_key: str = ""
) -> str:
    normalized = normalize_recipe_payload(recipe)
    policy = normalized["concurrency_policy"]
    origin = normalize_origin(target_url) if target_url else normalized["allowed_origins"][0]
    values = {
        "recipe_id": normalized["recipe_id"],
        "version": normalized["version"],
        "service": normalized["service"],
        "work_key": normalize_work_key(work_key or normalized["work_key_template"]),
        "origin": origin,
        "runtime": normalized["resource_policy"]["runtime"],
    }
    parts = [f"{key}:{values[key]}" for key in policy["conflict_keys"] if values.get(key)]
    if not parts:
        parts = [f"work_key:{values['work_key']}", f"origin:{values['origin']}"]
    return "|".join(parts)


def build_resource_claim(recipe: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_recipe_payload(recipe)
    resource_policy = normalized["resource_policy"]
    return {
        "runtime": resource_policy["runtime"],
        "browser_contexts": resource_policy["max_browser_contexts"],
        "memory_mb": resource_policy["max_memory_mb"],
        "runtime_seconds": resource_policy["max_runtime_seconds"],
        "artifact_budget_mb": resource_policy["artifact_budget_mb"],
    }


def _http_target_url(value: str) -> bool:
    parsed = urlparse(str(value or "").strip())
    return parsed.scheme.lower() in {"http", "https"} and bool(parsed.netloc)


def _recipe_requires_pc_agent(recipe: dict[str, Any]) -> tuple[bool, list[str]]:
    challenge_policy = _json_dict(recipe.get("challenge_policy"))
    runtime_policy = _json_dict(recipe.get("runtime_policy"))
    fallbacks = _json_dict(recipe.get("fallbacks"))
    reasons: list[str] = []
    if runtime_policy.get("requires_pc_agent") is True:
        reasons.append("runtime_policy_requires_pc_agent")
    for key in ("certificate", "local_certificate", "passkey", "desktop_only", "local_file_picker"):
        if challenge_policy.get(key) or runtime_policy.get(key) or fallbacks.get(key):
            reasons.append(f"{key}_requires_user_environment")
    return bool(reasons), reasons


def build_runtime_execution_plan(recipe: dict[str, Any], *, target_url: str = "") -> dict[str, Any]:
    normalized = normalize_recipe_payload(recipe)
    configured_runtime = normalized["resource_policy"]["runtime"]
    candidate_target = target_url or (
        normalized["allowed_origins"][0] if normalized["allowed_origins"] else ""
    )
    self_hosted_eligible = _http_target_url(candidate_target)
    pc_agent_required, pc_agent_reasons = _recipe_requires_pc_agent(normalized)
    fallback_config = _json_dict(normalized.get("fallbacks"))
    configured_fallbacks = [
        str(value).strip()
        for value in fallback_config.values()
        if str(value).strip() in ALLOWED_RUNTIMES
    ]

    if configured_runtime != "auto":
        primary_runtime = configured_runtime
    elif pc_agent_required:
        primary_runtime = "pc_agent"
    elif self_hosted_eligible:
        primary_runtime = "self_hosted_playwright"
    else:
        primary_runtime = "pc_agent"

    fallback_runtimes: list[str] = []
    for runtime in [
        *configured_fallbacks,
        "pc_agent",
        "self_hosted_playwright",
        "external_sandbox",
    ]:
        if runtime != primary_runtime and runtime not in fallback_runtimes:
            fallback_runtimes.append(runtime)

    return {
        "configured_runtime": configured_runtime,
        "primary_runtime": primary_runtime,
        **{key: value for key, value in egress_for_target(
            normalized["runtime_policy"]["egress_policy"], candidate_target
        ).items() if key != "proxy"},
        "fallback_runtimes": fallback_runtimes[:4],
        "self_hosted_eligible": self_hosted_eligible,
        "pc_agent_required": pc_agent_required,
        "pc_agent_reasons": pc_agent_reasons,
        "access_probe_supported": primary_runtime in {"self_hosted_playwright", "auto"}
        or self_hosted_eligible,
        "notes": [
            "서버 Playwright 접근 실패는 access-check/live-frame diagnosis로 분류합니다.",
            "OTP/CAPTCHA/인증서는 승인 토큰 범위 안에서만 자동 입력 또는 모델 판독을 허용합니다.",
        ],
    }


def evaluate_recipe_run_admission(
    recipe: dict[str, Any],
    *,
    active_runs: int,
    target_url: str = "",
    work_key: str = "",
) -> dict[str, Any]:
    normalized = normalize_recipe_payload(recipe)
    policy = normalized["concurrency_policy"]
    max_parallel_runs = policy["max_parallel_runs"]
    queue_strategy = policy["queue_strategy"]
    concurrency_key = build_recipe_concurrency_key(
        normalized, target_url=target_url, work_key=work_key
    )
    can_start = active_runs < max_parallel_runs
    if can_start:
        decision = "start"
        reason = "capacity_available"
        status = "running"
    elif queue_strategy == "reject_on_conflict":
        decision = "reject"
        reason = "concurrency_conflict"
        status = "rejected"
    elif queue_strategy == "latest_only":
        decision = "queue_latest_only"
        reason = "supersede_queued_conflict"
        status = "queued"
    else:
        decision = "queue"
        reason = "capacity_exhausted"
        status = "queued"
    return {
        "decision": decision,
        "reason": reason,
        "status": status,
        "concurrency_key": concurrency_key,
        "active_runs": int(active_runs),
        "max_parallel_runs": max_parallel_runs,
        "queue_strategy": queue_strategy,
        "resource_claim": build_resource_claim(normalized),
        "runtime_plan": build_runtime_execution_plan(normalized, target_url=target_url),
    }


def _row_to_recipe(row: Any) -> dict[str, Any]:
    item = dict(row)
    for key in ("id", "tenant_id"):
        item[key] = str(item[key])
    for key in ("created_at", "updated_at"):
        if item.get(key):
            item[key] = item[key].isoformat()
    for key in (
        "allowed_origins",
        "runtime_policy",
        "concurrency_policy",
        "resource_policy",
        "login_steps",
        "challenge_policy",
        "navigation_steps",
        "capture_rules",
        "upload_rules",
        "risk_actions",
        "verifier",
        "fallbacks",
    ):
        if key in item:
            item[key] = (
                _json_dict(item[key])
                if key.endswith("_policy")
                or key
                in {"challenge_policy", "capture_rules", "upload_rules", "verifier", "fallbacks"}
                else _json_list(item[key])
            )
    return mask_sensitive_value(item)


async def upsert_browser_recipe(
    *, tenant_id: str, user_id: str, payload: dict[str, Any]
) -> dict[str, Any]:
    recipe = normalize_recipe_payload(payload)
    recipe["version_hash"] = compute_recipe_hash(recipe)
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO browser_recipes (
                tenant_id, recipe_id, version, title, service, allowed_origins, work_key_template,
                runtime_policy, concurrency_policy, resource_policy, login_steps, challenge_policy,
                navigation_steps, capture_rules, parser_id, upload_rules, risk_actions, verifier,
                fallbacks, enabled, version_hash, created_by, updated_at
            )
            VALUES (
                $1, $2, $3, $4, $5, $6::jsonb, $7,
                $8::jsonb, $9::jsonb, $10::jsonb, $11::jsonb, $12::jsonb,
                $13::jsonb, $14::jsonb, $15, $16::jsonb, $17::jsonb, $18::jsonb,
                $19::jsonb, $20, $21, $22, NOW()
            )
            ON CONFLICT (tenant_id, recipe_id, version) DO UPDATE
               SET title = EXCLUDED.title,
                   service = EXCLUDED.service,
                   allowed_origins = EXCLUDED.allowed_origins,
                   work_key_template = EXCLUDED.work_key_template,
                   runtime_policy = EXCLUDED.runtime_policy,
                   concurrency_policy = EXCLUDED.concurrency_policy,
                   resource_policy = EXCLUDED.resource_policy,
                   login_steps = EXCLUDED.login_steps,
                   challenge_policy = EXCLUDED.challenge_policy,
                   navigation_steps = EXCLUDED.navigation_steps,
                   capture_rules = EXCLUDED.capture_rules,
                   parser_id = EXCLUDED.parser_id,
                   upload_rules = EXCLUDED.upload_rules,
                   risk_actions = EXCLUDED.risk_actions,
                   verifier = EXCLUDED.verifier,
                   fallbacks = EXCLUDED.fallbacks,
                   enabled = EXCLUDED.enabled,
                   version_hash = EXCLUDED.version_hash,
                   updated_at = NOW()
            RETURNING *
            """,
            uuid.UUID(str(tenant_id)),
            recipe["recipe_id"],
            recipe["version"],
            recipe["title"],
            recipe["service"],
            json.dumps(recipe["allowed_origins"], ensure_ascii=False),
            recipe["work_key_template"],
            json.dumps(recipe["runtime_policy"], ensure_ascii=False),
            json.dumps(recipe["concurrency_policy"], ensure_ascii=False),
            json.dumps(recipe["resource_policy"], ensure_ascii=False),
            json.dumps(recipe["login_steps"], ensure_ascii=False),
            json.dumps(recipe["challenge_policy"], ensure_ascii=False),
            json.dumps(recipe["navigation_steps"], ensure_ascii=False),
            json.dumps(recipe["capture_rules"], ensure_ascii=False),
            recipe["parser_id"],
            json.dumps(recipe["upload_rules"], ensure_ascii=False),
            json.dumps(recipe["risk_actions"], ensure_ascii=False),
            json.dumps(recipe["verifier"], ensure_ascii=False),
            json.dumps(recipe["fallbacks"], ensure_ascii=False),
            recipe["enabled"],
            recipe["version_hash"],
            user_id,
        )
    result = _row_to_recipe(row)
    # Legacy BrowserRecipe remains the API adapter during rollout.  The shared
    # OVISRecipe reference is synchronized after its committed legacy write so
    # pre-migration deployments retain their existing behavior.
    from app.services.ovis_recipe import sync_legacy_reference

    result["ovis_recipe_ref"] = await sync_legacy_reference(
        tenant_id=tenant_id,
        canonical_key=f"browser:{recipe['recipe_id']}",
        # A legacy caller may overwrite `v1`; the canonical contract converts
        # that mutable label into an immutable content-addressed version while
        # retaining the caller's label inside the lossless definition.
        version=f"{recipe['version']}@{recipe['version_hash'][:16]}",
        status=str(result.get("version_status") or ("active" if recipe["enabled"] else "archived")),
        source_type="browser_recipe",
        source_id=result["id"],
        definition=result,
        approval_scope={
            "legacy": "browser_recipe",
            "tenant_id": str(tenant_id),
            "version": recipe["version"],
        },
    )
    return result


async def list_browser_recipes(
    *, tenant_id: str, service: str | None = None, enabled: bool | None = None
) -> list[dict[str, Any]]:
    args: list[Any] = [uuid.UUID(str(tenant_id))]
    where = ["tenant_id = $1"]
    if service:
        args.append(service)
        where.append(f"service = ${len(args)}")
    if enabled is not None:
        args.append(enabled)
        where.append(f"enabled = ${len(args)}")
    async with get_pool().acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT *
              FROM browser_recipes
             WHERE {" AND ".join(where)}
             ORDER BY service, recipe_id, version DESC
            """,
            *args,
        )
    return [_row_to_recipe(row) for row in rows]


async def get_browser_recipe(
    *, tenant_id: str, recipe_id: str, version: str = "v1"
) -> dict[str, Any] | None:
    async with get_pool().acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT *
              FROM browser_recipes
             WHERE tenant_id = $1
               AND recipe_id = $2
               AND version = $3
            """,
            uuid.UUID(str(tenant_id)),
            recipe_id,
            version,
        )
    return _row_to_recipe(row) if row else None


async def plan_browser_recipe_run(
    *,
    tenant_id: str,
    recipe_id: str,
    version: str = "v1",
    target_url: str = "",
    work_key: str = "",
) -> dict[str, Any] | None:
    recipe = await get_browser_recipe(tenant_id=tenant_id, recipe_id=recipe_id, version=version)
    if not recipe:
        return None
    concurrency_key = build_recipe_concurrency_key(recipe, target_url=target_url, work_key=work_key)
    async with get_pool().acquire() as conn:
        active_runs = await conn.fetchval(
            """
            SELECT count(*)
              FROM browser_recipe_runs
             WHERE tenant_id = $1
               AND concurrency_key = $2
               AND status = ANY($3::text[])
            """,
            uuid.UUID(str(tenant_id)),
            concurrency_key,
            list(ACTIVE_RECIPE_RUN_STATUSES),
        )
    dry_run = build_recipe_dry_run_plan(recipe, target_url=target_url)
    admission = evaluate_recipe_run_admission(
        recipe,
        active_runs=int(active_runs or 0),
        target_url=target_url,
        work_key=work_key,
    )
    return {**dry_run, "admission": admission}


async def create_browser_recipe_run(
    *,
    tenant_id: str,
    recipe_id: str,
    version: str = "v1",
    target_url: str = "",
    work_key: str = "",
) -> dict[str, Any] | None:
    recipe = await get_browser_recipe(tenant_id=tenant_id, recipe_id=recipe_id, version=version)
    if not recipe:
        return None
    plan = await plan_browser_recipe_run(
        tenant_id=tenant_id,
        recipe_id=recipe_id,
        version=version,
        target_url=target_url,
        work_key=work_key,
    )
    if not plan:
        return None
    admission = plan["admission"]
    if admission["decision"] == "reject":
        return {"status": "rejected", "plan": plan}
    origin = normalize_origin(target_url) if target_url else recipe["allowed_origins"][0]
    run_work_key = normalize_work_key(work_key or recipe["work_key_template"])
    async with get_pool().acquire() as conn:
        if admission["decision"] == "queue_latest_only":
            await conn.execute(
                """
                UPDATE browser_recipe_runs
                   SET status = 'superseded',
                       updated_at = NOW(),
                       completed_at = COALESCE(completed_at, NOW())
                 WHERE tenant_id = $1
                   AND concurrency_key = $2
                   AND status = 'queued'
                """,
                uuid.UUID(str(tenant_id)),
                admission["concurrency_key"],
            )
        row = await conn.fetchrow(
            """
            INSERT INTO browser_recipe_runs (
                tenant_id, recipe_id, recipe_version, recipe_hash, work_key, origin, runtime,
                status, concurrency_key, resource_claim, started_at, updated_at
            )
            VALUES (
                $1, $2, $3, $4, $5, $6, $7,
                $8, $9, $10::jsonb,
                CASE WHEN $8 = 'running' THEN NOW() ELSE NULL END,
                NOW()
            )
            RETURNING *
            """,
            uuid.UUID(str(tenant_id)),
            recipe["recipe_id"],
            recipe["version"],
            recipe["version_hash"],
            run_work_key,
            origin,
            admission["resource_claim"]["runtime"],
            admission["status"],
            admission["concurrency_key"],
            json.dumps(admission["resource_claim"], ensure_ascii=False),
        )
    item = dict(row)
    for key in ("id", "tenant_id", "task_id"):
        if item.get(key):
            item[key] = str(item[key])
    for key in ("started_at", "completed_at", "created_at", "updated_at"):
        if item.get(key):
            item[key] = item[key].isoformat()
    item["resource_claim"] = _json_dict(item.get("resource_claim"))
    item["result"] = _json_dict(item.get("result"))
    return {"status": "created", "run": mask_sensitive_value(item), "plan": plan}
