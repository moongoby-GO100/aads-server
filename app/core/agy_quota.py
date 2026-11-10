"""Account-attributed AGY quota adaptation, independent of API/DB imports."""
from datetime import datetime, timezone
from typing import Literal, TypedDict


class QuotaWindow(TypedDict):
    period: Literal["weekly", "five-hour"]
    remaining_percent: float | None
    used_percent: float | None
    reset_at: str | None
    collected_at: str
    account: str
    source: Literal["agy_cli_usage"]
    stale: bool


class QuotaGroup(TypedDict):
    name: str
    models: list[str]
    windows: list[QuotaWindow]


def _antigravity_groups(binding, email, state, *, now=None) -> list[QuotaGroup]:
    """Validate account-attributed observations without aggregating quota pools."""
    now = now or datetime.now(timezone.utc)
    if (not email or not isinstance(binding, dict)
            or binding.get("needs_login") or binding.get("binding_mismatch")
            or binding.get("runtime_auth_status") == "login_required"
            or (state != "ok" and not (state == "unknown" and binding.get("usage_status") == "collection_failed"))
            or binding.get("usage_source") != "agy_cli_usage"
            or str(binding.get("quota_account", "")).casefold() != email.casefold()):
        return []
    groups = []
    try:
        collected = datetime.fromisoformat(binding["usage_collected_at"].replace("Z", "+00:00"))
        age = (now - collected).total_seconds()
        if age < 0 or collected.tzinfo is None:
            return []
        stale = (age > 180 or binding.get("usage_status") != "available"
                 or binding.get("runtime_auth_status") == "unknown" or state != "ok")
        seen = set()
        for group in binding.get("groups", []):
            name = group["name"]
            models = group["models"]
            if (not isinstance(name, str) or name not in {"Gemini", "Claude and GPT"} or name in seen
                    or not isinstance(models, list) or not models
                    or any(not isinstance(model, str) or not model for model in models)):
                raise ValueError("invalid group")
            seen.add(name)
            windows, periods = [], set()
            for window in group["windows"]:
                period = window["period"]
                remaining = window.get("remaining_percent")
                disabled = (window.get("disabled") is True and period == "five-hour"
                            and any(w.get("period") == "weekly" and w.get("remaining_percent") == 0
                                    for w in group["windows"]))
                if (period not in {"weekly", "five-hour"} or period in periods
                        or (remaining is None and not stale and not disabled)
                        or (remaining is not None and (isinstance(remaining, bool)
                            or not isinstance(remaining, (int, float)) or not 0 <= remaining <= 100))):
                    raise ValueError("invalid window")
                periods.add(period)
                reset = window.get("reset_at")
                if reset is not None:
                    reset_dt = datetime.fromisoformat(reset.replace("Z", "+00:00"))
                    if reset_dt.tzinfo is None:
                        raise ValueError("missing timezone")
                    reset = reset_dt.isoformat()
                windows.append({"period": period, "remaining_percent": None if stale else remaining,
                                "used_percent": None if stale or disabled else 100 - remaining,
                                "reset_at": None if stale else reset, "collected_at": collected.isoformat(),
                                "disabled": disabled,
                                "reset_description": None if stale else window.get("reset_description"),
                                "account": email, "source": "agy_cli_usage", "stale": stale})
            if periods != {"weekly", "five-hour"}:
                raise ValueError("incomplete windows")
            groups.append({"name": name, "models": list(dict.fromkeys(models)), "windows": windows})
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        return []
    return groups


def _antigravity_usage(binding, email, state, *, relay_available):
    """Only accept measured provider quota explicitly attributed to this account.

    Group windows remain independent; legacy scalar windows stay compatible.
    A collector must supply actual observations before quota can be available.
    """
    result = {"windows": [], "used_percent": None, "resets_at": None,
              "usage_status": "not_measured", "usage_note": "Quota has not been measured",
              "usage_collected_at": None, "usage_summary": None,
              "usage_source": "agy_cli_usage",
              "quota_account": ((binding or {}).get("quota_account")
                  if email and str((binding or {}).get("quota_account") or "").casefold() == email.casefold()
                  and (binding or {}).get("runtime_auth_status") != "login_required" else None),
              "groups": _antigravity_groups(
                  dict(binding, usage_status="collection_failed") if binding and not relay_available else binding,
                  email, state)}
    if result["groups"]:
        result["usage_collected_at"] = result["groups"][0]["windows"][0]["collected_at"]
    if not relay_available:
        result.update(usage_status="collection_failed", usage_note="Relay unavailable")
        return result
    if not binding:
        return result
    status = binding.get("usage_status", "unsupported")
    if status != "available":
        result.update(usage_status=status if status in {"unsupported", "collection_failed", "not_measured", "unknown", "login_required"} else "not_measured",
                      usage_note={"unsupported": "Installed relay has no supported provider quota collector",
                                  "collection_failed": "Provider quota collection failed"}.get(status, "Quota has not been measured"))
        return result
    try:
        if state != "ok" or not email or str(binding.get("quota_account", "")).casefold() != email.casefold():
            raise ValueError("account mismatch")
        collected = datetime.fromisoformat(binding["usage_collected_at"].replace("Z", "+00:00"))
        if collected.tzinfo is None:
            raise ValueError("missing timezone")
        age = (datetime.now(timezone.utc) - collected).total_seconds()
        if result["groups"]:
            if any(w["stale"] for g in result["groups"] for w in g["windows"]):
                result.update(usage_status="stale", usage_note="Quota observation expired",
                              usage_collected_at=collected.isoformat())
                return result
            result.update(usage_status="available", usage_note="Independent provider quota groups",
                          usage_collected_at=collected.isoformat())
            return result
        if "groups" in binding:
            raise ValueError("invalid quota groups")
        if binding.get("runtime_auth_status") in {"unknown", "login_required"}:
            raise ValueError("runtime authentication unknown")
        windows = []
        for win in binding["windows"]:
            percent = win["used_percent"]
            minutes = win["window_minutes"]
            reset = datetime.fromisoformat(win["resets_at"].replace("Z", "+00:00"))
            if (isinstance(percent, bool) or not isinstance(percent, (int, float))
                    or not 0 <= percent <= 100 or type(minutes) is not int or minutes <= 0 or reset.tzinfo is None):
                raise ValueError("invalid quota")
            windows.append({"window_minutes": minutes, "used_percent": percent, "resets_at": reset.isoformat()})
        if not windows:
            raise ValueError("empty quota")
        if not 0 <= age <= 180:
            result.update(usage_status="stale", usage_note="Quota observation expired",
                          usage_collected_at=collected.isoformat())
            return result
    except (KeyError, TypeError, ValueError, AttributeError):
        result.update(usage_status="collection_failed", usage_note="Quota missing valid measurement or account attribution")
        return result
    result.update(windows=windows, used_percent=windows[0]["used_percent"], resets_at=windows[0]["resets_at"],
                  usage_status="available", usage_note="Account-attributed provider quota",
                  usage_collected_at=collected.isoformat())
    return result
