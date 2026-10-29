"""Versioned, secret-free login observations shared by registration and replay."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime, timezone
from urllib.parse import urlsplit

ACCESS = frozenset(
    {
        "server_verified",
        "server_restricted_pc_unverified",
        "pc_verified_after_server_restriction",
        "native_pc_required",
        "both_unavailable",
        "unknown",
    }
)
PRIMARY = frozenset(
    {
        "password",
        "social_oauth",
        "qr_app",
        "sso",
        "joint_certificate",
        "financial_certificate",
        "passkey",
        "manual",
    }
)
MFA = frozenset(
    {"none", "sms", "email", "totp", "app_push", "phone_identity", "hardware_key", "unknown"}
)
POPUPS = frozenset(
    {"notice_modal", "auth_new_tab", "js_dialog", "native_auth_window", "consent_or_security"}
)


def normalize_login_contract(raw):
    if not isinstance(raw, Mapping) or raw.get("version") != 1:
        raise ValueError("login_contract_version_invalid")
    allowed = {
        "submit_mode",
        "human_submit_required",
        "submit_step",
        "succeeds_when",
        "human_submit_timeout_seconds",
        "submit_observation_seconds",
        "version",
        "access",
        "observation",
        "primary",
        "mfa",
        "popups",
        "options",
        "challenge",
        "oauth",
        "business_ref",
        "account_ref",
    }
    if set(raw) - allowed:
        raise ValueError("login_contract_unknown_field")
    for key in ("business_ref", "account_ref"):
        if key in raw and (
            not isinstance(raw[key], str) or not raw[key].strip() or len(raw[key]) > 200
        ):
            raise ValueError("login_contract_reference_invalid")
    if "human_submit_required" in raw and type(raw["human_submit_required"]) is not bool:
        raise ValueError("human_submit_required_invalid")
    if raw.get("submit_mode", "browser") not in {"browser", "pc_native_click", "human"}:
        raise ValueError("login_submit_mode_invalid")
    if any(
        key in raw
        for key in (
            "submit_step",
            "succeeds_when",
            "human_submit_timeout_seconds",
            "submit_observation_seconds",
        )
    ) or raw.get("human_submit_required") or raw.get("submit_mode") in {"pc_native_click", "human"}:
        if type(raw.get("submit_step")) is not int or raw["submit_step"] < 1:
            raise ValueError("login_submit_step_required")
        checks = raw.get("succeeds_when")
        if not isinstance(checks, list) or not checks:
            raise ValueError("login_success_conditions_required")
        for check in checks:
            if (
                not isinstance(check, Mapping)
                or set(check) != {"kind", "value"}
                or check["kind"] not in {"url_equals", "text_contains"}
                or not isinstance(check["value"], str)
                or not check["value"]
            ):
                raise ValueError("login_success_condition_invalid")
            if check["kind"] == "url_equals":
                target = urlsplit(check["value"])
                if (
                    target.scheme not in {"https", "http"}
                    or not target.hostname
                    or target.username
                    or target.password
                    or target.query
                    or target.fragment
                ):
                    raise ValueError("login_success_url_invalid")
        for key in ("human_submit_timeout_seconds", "submit_observation_seconds"):
            if key in raw and (type(raw[key]) is not int or not 1 <= raw[key] <= 3600):
                raise ValueError("login_wait_timeout_invalid")
    result = deepcopy(dict(raw))
    result["submit_mode"] = "human" if raw.get("human_submit_required") else raw.get("submit_mode", "browser")
    result.setdefault("access", "unknown")
    if result["access"] not in ACCESS:
        raise ValueError("login_contract_access_invalid")
    for key, choices in (
        ("primary", PRIMARY),
        ("mfa", MFA),
        ("popups", POPUPS),
        ("options", PRIMARY),
    ):
        values = result.get(key, [])
        if not isinstance(values, list) or any(
            not isinstance(v, str) or v not in choices for v in values
        ):
            raise ValueError("login_contract_classification_invalid")
        result[key] = list(dict.fromkeys(values))
    observation = result.get("observation", {})
    if not isinstance(observation, Mapping) or set(observation) - {
        "origin",
        "lane",
        "artifact_id",
        "expires_at",
    }:
        raise ValueError("login_contract_observation_invalid")
    if observation:
        origin = urlsplit(str(observation.get("origin", "")))
        if (
            origin.scheme != "https"
            or not origin.hostname
            or origin.username
            or origin.password
            or origin.query
            or origin.fragment
        ):
            raise ValueError("login_contract_origin_invalid")
        if observation.get("lane") not in {"server", "pc"}:
            raise ValueError("login_contract_lane_invalid")
        expected_lane = {
            "server_verified": "server",
            "server_restricted_pc_unverified": "server",
            "pc_verified_after_server_restriction": "pc",
            "native_pc_required": "pc",
        }.get(result["access"])
        if expected_lane and observation["lane"] != expected_lane:
            raise ValueError("login_contract_access_lane_mismatch")
        if not observation.get("artifact_id"):
            raise ValueError("login_contract_artifact_required")
        try:
            expiry = datetime.fromisoformat(str(observation["expires_at"]).replace("Z", "+00:00"))
            if expiry.tzinfo is None:
                raise ValueError()
        except (KeyError, ValueError):
            raise ValueError("login_contract_expiry_invalid") from None
    elif result["access"] != "unknown":
        raise ValueError("login_contract_observation_required")
    challenge = result.get("challenge")
    if challenge is not None:
        if (
            not isinstance(challenge, Mapping)
            or set(challenge) != {"kind", "active"}
            or challenge["kind"] not in PRIMARY | MFA
            or type(challenge["active"]) is not bool
        ):
            raise ValueError("login_contract_challenge_invalid")
    oauth = result.get("oauth")
    if oauth is not None:
        if not isinstance(oauth, Mapping) or set(oauth) != {
            "provider",
            "callback",
            "opener",
            "target",
        }:
            raise ValueError("login_contract_oauth_invalid")
        for value in oauth.values():
            parts = urlsplit(str(value))
            if (
                parts.scheme != "https"
                or not parts.hostname
                or parts.username
                or parts.password
                or parts.query
                or parts.fragment
            ):
                raise ValueError("login_contract_oauth_scope_invalid")
    return result


def contract_route(raw, *, now=None):
    contract = normalize_login_contract(raw)
    observation = contract.get("observation", {})
    now = now or datetime.now(timezone.utc)
    if (
        observation
        and datetime.fromisoformat(observation["expires_at"].replace("Z", "+00:00")) <= now
    ):
        return {"runtime": "human_gateway", "reason": "login_observation_expired"}
    access = contract["access"]
    if access in {"native_pc_required", "pc_verified_after_server_restriction"}:
        return {"runtime": "pc_agent", "reason": access}
    if access in {"server_restricted_pc_unverified", "both_unavailable"}:
        return {"runtime": "human_gateway", "reason": access}
    return {"runtime": "browser_agent", "reason": access}


def adapt_legacy_login(metadata):
    """Legacy flags remain readable without inventing verified observations."""
    return (
        normalize_login_contract(metadata["login_contract"])
        if "login_contract" in metadata
        else {
            "version": 1,
            "access": "unknown",
            "primary": [],
            "mfa": [],
            "popups": [],
            "options": [],
        }
    )


def assert_oauth_scope(contract, *, current_url, opener_url, target_url):
    """Compare exact OAuth origins and paths; do not return authorization query data."""
    oauth = normalize_login_contract(contract).get("oauth")
    if not oauth:
        return

    def endpoint(value):
        parsed = urlsplit(str(value))
        if parsed.scheme != "https" or parsed.username or parsed.password:
            raise ValueError("oauth_scope_rejected")
        return parsed.scheme, parsed.netloc.lower(), parsed.path.rstrip("/")

    if endpoint(current_url) not in {
        endpoint(oauth["provider"]),
        endpoint(oauth["callback"]),
        endpoint(oauth["target"]),
    }:
        raise ValueError("oauth_provider_or_callback_rejected")
    if endpoint(opener_url) != endpoint(oauth["opener"]) or endpoint(target_url) != endpoint(
        oauth["target"]
    ):
        raise ValueError("oauth_opener_or_target_rejected")


def adapt_browser_recipe(raw, *, credential_scope=None):
    """Explicit BrowserRecipe import; unsupported operations fail instead of guessing.

    Import produces an unapproved WorkRecipe. Legacy runtime policy remains a
    legacy flag; it is never promoted into a verified access observation.
    """
    from app.services.work_recipe.schema import WorkRecipe
    from app.services.work_recipe.registration import normalize_verify_steps
    from app.services.work_recipe.vault_reference import parse_scope

    if not isinstance(raw, Mapping):
        raise ValueError("legacy_browser_recipe_invalid")
    origins = raw.get("allowed_origins") or []
    if len(origins) != 1:
        raise ValueError("legacy_browser_recipe_single_origin_required")
    origin = urlsplit(str(origins[0]))
    if origin.scheme != "https" or not origin.hostname or origin.username or origin.password:
        raise ValueError("legacy_browser_recipe_origin_invalid")
    version = str(raw.get("version") or "v1").removeprefix("v")
    if not version.isdigit():
        raise ValueError("legacy_browser_recipe_version_invalid")
    metadata = {
        "legacy_browser_recipe_id": raw.get("recipe_id"),
        "work_key": raw.get("work_key_template") or raw.get("recipe_id"),
    }
    runtime = (raw.get("resource_policy") or raw.get("runtime_policy") or {}).get("runtime")
    if runtime == "pc_agent":
        metadata["lane"] = "pc_agent"
        metadata["native_auth_required"] = True
    elif runtime == "self_hosted_playwright":
        metadata["lane"] = "server"
    if raw.get("login_contract") is not None:
        metadata["login_contract"] = normalize_login_contract(raw["login_contract"])
    if credential_scope is not None:
        metadata["credential_scope"] = deepcopy(credential_scope)
    steps = [*(raw.get("login_steps") or []), *(raw.get("navigation_steps") or [])]
    verifier = raw.get("verifier") or {}
    recipe = WorkRecipe.from_dict(
        {
            "name": raw.get("recipe_id"),
            "domain": origin.hostname,
            "version": int(version),
            "steps": steps,
            "verify": normalize_verify_steps(verifier.get("assertions") or []),
            "inputs": [
                {"name": key, "secret": True} for key in (credential_scope or {}).get("inputs", {})
            ],
            "metadata": metadata,
        }
    )
    if credential_scope is not None:
        parse_scope(recipe)
    return recipe
