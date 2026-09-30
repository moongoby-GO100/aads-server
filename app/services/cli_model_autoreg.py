"""CLI 모델(codex CLI) 신규 모델 자동등록 (AADS-CLI-MODEL-AUTOREG-20260930).

발견과 실행가능 판정을 분리한다. codex CLI 카탈로그(models_cache.json)에 있다고
실행 가능한 것이 아니다 — 2026-09-30 gpt-6.1-sol 은 목록(visibility=list)에 있었지만
`codex exec -m gpt-6.1-sol` 은 "not supported when using Codex with a ChatGPT account"
400 으로 실패했다.

    발견(여기, 컨테이너 sync)       → llm_models codex 행 inactive/non-executable
                                     + llm_model_candidates status='discovered'
    실호출 검증(scripts/codex_model_probe.py, 호스트) → verified | blocked_account | probe_failed
    라우팅 후보(여기)               → model_routing_preferences(runner_llm) is_enabled=false 로만 추가

활성화는 CEO 설정 UI 토글로만 한다. 이 모듈은 기존 행을 바꾸지 않는다(INSERT … DO NOTHING).
asyncpg 에 의존하지 않는다 — conn 은 호출자가 넘긴다.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Iterable, Mapping, Sequence

logger = logging.getLogger(__name__)

CODEX_CACHE_ENV = "AADS_CODEX_MODELS_CACHE"
CODEX_CACHE_DEFAULT = "/root/.codex/models_cache.json"
# 컨테이너에는 /root/.codex 가 마운트돼 있지 않다(2026-09-30 docker inspect 실측).
# 호스트 probe 가 민감정보(identity/etag)를 뺀 사본을 app/data 아래(컨테이너 /app/app/data 로 마운트)에 둔다.
CODEX_CACHE_MIRROR_ENV = "AADS_CODEX_MODELS_CACHE_MIRROR"
CODEX_CACHE_MIRROR_DEFAULT = "/app/app/data/codex_cli/models_cache.json"

DISCOVERY_SOURCE = "codex_cli_cache"
RUNNER_ROUTE_KEY = "runner_llm"
AUTO_DISCOVERY_ACTOR = "auto_discovery"
# 러너가 실제로 태울 수 있는 CLI 계열만 라우팅 후보로 올린다(Claude CLI / codex CLI).
RUNNER_CANDIDATE_PROVIDERS = frozenset({"anthropic", "openai", "codex"})
RUNNER_CANDIDATE_DISPLAY_ORDER = 900
# 템플릿/수동 행은 "새로 발견" 이 아니다.
_NON_DISCOVERY_SOURCES = frozenset({"", "template", "accepted_alias", "manual_seed"})
_RAW_KEYS = ("slug", "display_name", "visibility", "supported_in_api", "context_window", "priority")
VERIFIED_ALERT_TITLE = "신규 CLI 모델 실행 확인 — 설정에서 활성화 가능"


def codex_cache_paths() -> list[str]:
    paths = [
        (os.getenv(CODEX_CACHE_ENV) or CODEX_CACHE_DEFAULT).strip(),
        (os.getenv(CODEX_CACHE_MIRROR_ENV) or CODEX_CACHE_MIRROR_DEFAULT).strip(),
    ]
    out: list[str] = []
    for path in paths:
        if path and path not in out:
            out.append(path)
    return out


def parse_codex_models_cache(payload: Any) -> tuple[list[dict[str, Any]], int]:
    """models[] 중 visibility=='list' 만 돌려준다. (rows, hidden_count)."""
    models = payload.get("models") if isinstance(payload, Mapping) else None
    rows: list[dict[str, Any]] = []
    hidden = 0
    seen: set[str] = set()
    for item in models or []:
        if not isinstance(item, Mapping):
            continue
        slug = str(item.get("slug") or "").strip()
        if not slug:
            continue
        if str(item.get("visibility") or "").strip().lower() != "list":
            hidden += 1
            continue
        if slug in seen:
            continue
        seen.add(slug)
        raw = {key: item.get(key) for key in _RAW_KEYS if key in item}
        rows.append({
            "model_id": slug,
            "display_name": str(item.get("display_name") or slug),
            "raw": raw,
        })
    return rows, hidden


def read_codex_models_cache(paths: Sequence[str] | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """캐시를 읽는다. 읽을 수 없으면 조용히 넘기지 않고 status=unavailable + 사유를 돌려준다."""
    reasons: list[str] = []
    for path in list(paths or codex_cache_paths()):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except FileNotFoundError:
            reasons.append(f"not_found:{path}")
            continue
        except PermissionError:
            reasons.append(f"permission_denied:{path}")
            continue
        except (OSError, ValueError) as exc:
            reasons.append(f"unreadable:{path}:{type(exc).__name__}")
            continue
        if not isinstance(payload, Mapping) or not isinstance(payload.get("models"), list):
            reasons.append(f"invalid_format:{path}")
            continue
        rows, hidden = parse_codex_models_cache(payload)
        return rows, {
            "status": "ok",
            "count": len(rows),
            "hidden_count": hidden,
            "source_path": path,
            "fetched_at": payload.get("fetched_at"),
            "client_version": payload.get("client_version"),
            "discovery_source": DISCOVERY_SOURCE,
            "runtime_executable": False,
            "auto_discovery_supported": True,
            "discovery_requirement": "codex CLI models_cache.json (host) 또는 probe 가 둔 사본",
            "model_source": "discovery",
        }
    return [], {
        "status": "unavailable",
        "count": 0,
        "error": "; ".join(reasons) or "no_path",
        "discovery_source": DISCOVERY_SOURCE,
        "runtime_executable": False,
        "auto_discovery_supported": False,
        "discovery_requirement": f"{CODEX_CACHE_ENV} 또는 {CODEX_CACHE_MIRROR_ENV} 경로의 파일",
        "model_source": "template",
    }


def is_codex_cli_row(row: Mapping[str, Any]) -> bool:
    return row.get("provider") == "codex" and row.get("discovery_source") == DISCOVERY_SOURCE


def apply_verified_codex(model_rows: Iterable[dict[str, Any]], verified_ids: set[str]) -> int:
    """probe 가 verified 로 판정한 codex 행은 sync 가 다시 non-executable 로 덮지 않게 한다."""
    changed = 0
    for row in model_rows:
        if not is_codex_cli_row(row) or row.get("model_id") not in verified_ids:
            continue
        row["is_executable"] = True
        row["is_selectable"] = True
        row["verification_status"] = "verified"
        changed += 1
    return changed


def select_new_codex_rows(
    model_rows: Iterable[Mapping[str, Any]],
    *,
    template_ids: Iterable[str],
    existing_keys: set[tuple[str, str]],
) -> list[Mapping[str, Any]]:
    """템플릿에도, sync 이전 llm_models codex 행에도 없는 codex_cli_cache 발견분."""
    templates = set(template_ids)
    return [
        row for row in model_rows
        if is_codex_cli_row(row)
        and row["model_id"] not in templates
        and ("codex", row["model_id"]) not in existing_keys
    ]


def select_runner_candidates(
    model_rows: Iterable[Mapping[str, Any]],
    *,
    existing_keys: set[tuple[str, str]],
    verified_codex_ids: Iterable[str] = (),
) -> list[tuple[str, str]]:
    """runner_llm 에 비활성 후보로 올릴 (provider, model_id).

    - API discovery(openai_api/anthropic_api)로 이번 sync 에 처음 llm_models 에 들어온 실행가능 모델
    - probe 로 verified 된 codex CLI 모델
    """
    picked: list[tuple[str, str]] = []
    for row in model_rows:
        provider = str(row.get("provider") or "")
        model_id = str(row.get("model_id") or "")
        key = (provider, model_id)
        if provider not in RUNNER_CANDIDATE_PROVIDERS or provider == "codex" or not model_id:
            continue
        if str(row.get("discovery_source") or "") in _NON_DISCOVERY_SOURCES:
            continue
        if key in existing_keys or not row.get("is_executable"):
            continue
        if key not in picked:
            picked.append(key)
    for model_id in sorted(set(verified_codex_ids)):
        key = ("codex", model_id)
        if key not in picked:
            picked.append(key)
    return picked


async def fetch_verified_codex_ids(conn: Any) -> set[str]:
    try:
        rows = await conn.fetch(
            "SELECT model_id FROM llm_model_candidates WHERE provider = 'codex' AND status = 'verified'"
        )
    except Exception as exc:  # 테이블/컬럼 미적용 환경에서도 sync 는 계속한다.
        logger.warning("cli_model_autoreg.verified_lookup_failed: %s", str(exc)[:160])
        return set()
    return {str(row["model_id"]) for row in rows or []}


async def register_codex_candidates(conn: Any, rows: Sequence[Mapping[str, Any]]) -> list[str]:
    """llm_model_candidates 에 status='discovered' 로 넣는다. 이미 있으면 건드리지 않는다."""
    inserted: list[str] = []
    for row in rows:
        raw = (row.get("metadata") or {}).get("raw") if isinstance(row.get("metadata"), Mapping) else {}
        result = await conn.fetch(
            """
            INSERT INTO llm_model_candidates (
                provider, model_id, product_name, surface_scope, pricing_kind,
                price_status, training_use, status, discovery_source, notes
            )
            VALUES ('codex', $1, $2, ARRAY['terminal_cli','runner']::TEXT[], 'subscription',
                    'estimated', 'unknown', 'discovered', $3, $4)
            ON CONFLICT (provider, model_id) DO NOTHING
            RETURNING model_id
            """,
            row["model_id"],
            f"{row.get('display_name') or row['model_id']} (codex CLI)",
            DISCOVERY_SOURCE,
            "codex CLI models_cache 자동 발견. 실호출 검증 전 — scripts/codex_model_probe.py 가 판정한다. "
            f"supported_in_api={(raw or {}).get('supported_in_api')}",
        )
        inserted.extend(str(r["model_id"]) for r in result or [])
    return inserted


async def register_runner_llm_candidates(conn: Any, candidates: Sequence[tuple[str, str]]) -> list[tuple[str, str]]:
    """model_routing_preferences(runner_llm) 에 is_enabled=false 로만 추가한다. 기존 행 불변."""
    inserted: list[tuple[str, str]] = []
    for provider, model_id in candidates:
        result = await conn.fetch(
            """
            INSERT INTO model_routing_preferences (
                route_key, provider, model_id, display_order, is_enabled, is_default, notes, updated_by
            )
            VALUES ($1, $2, $3, $4, FALSE, FALSE, $5, $6)
            ON CONFLICT (route_key, provider, model_id) DO NOTHING
            RETURNING provider, model_id
            """,
            RUNNER_ROUTE_KEY,
            provider,
            model_id,
            RUNNER_CANDIDATE_DISPLAY_ORDER,
            "자동 발견 후보 — 설정 UI 에서 활성화해야 러너가 사용",
            AUTO_DISCOVERY_ACTOR,
        )
        inserted.extend((str(r["provider"]), str(r["model_id"])) for r in result or [])
    return inserted


async def _notify_verified(models: Sequence[str]) -> None:
    if not models:
        return
    try:
        from app.services.ohvis_alert import INFO, notify

        await notify(
            VERIFIED_ALERT_TITLE,
            "codex CLI 실호출 검증 통과: " + ", ".join(models)
            + " — runner_llm 에 비활성 후보로 등록됨. 설정 > 모델 라우팅에서 켜면 러너가 사용한다.",
            severity=INFO,
            category="llm_model",
            project="AADS",
        )
    except Exception as exc:
        logger.warning("cli_model_autoreg.notify_failed: %s", str(exc)[:160])


async def autoregister_after_sync(
    conn: Any,
    *,
    model_rows: Sequence[Mapping[str, Any]],
    existing_keys: set[tuple[str, str]],
    template_codex_ids: Iterable[str],
    verified_codex_ids: set[str],
) -> dict[str, Any]:
    new_codex_rows = select_new_codex_rows(model_rows, template_ids=template_codex_ids, existing_keys=existing_keys)
    codex_inserted = await register_codex_candidates(conn, new_codex_rows)
    runner_candidates = select_runner_candidates(
        model_rows, existing_keys=existing_keys, verified_codex_ids=verified_codex_ids
    )
    runner_inserted = await register_runner_llm_candidates(conn, runner_candidates)
    newly_verified = [model_id for provider, model_id in runner_inserted if provider == "codex"]
    await _notify_verified(newly_verified)
    summary = {
        "codex_new": [row["model_id"] for row in new_codex_rows],
        "codex_candidates_inserted": codex_inserted,
        "runner_candidates_inserted": [f"{p}:{m}" for p, m in runner_inserted],
        "verified_notified": newly_verified,
    }
    if codex_inserted or runner_inserted:
        logger.info("cli_model_autoreg.registered %s", summary)
    return summary
