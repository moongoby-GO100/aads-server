"""배달앱 4사(배민·쿠팡이츠·요기요·땡겨요) 스마트브라우저 조각 레시피 초안 카탈로그.

L1 '복잡한 웹페이지 레시피' 규칙: 한 번에 끝까지 가는 레시피 하나가 아니라, 화면에서 성공을
확인할 수 있는 조각으로 나눈다. 앱마다 같은 5조각이다.

  01 open_login          로그인 화면 진입           READ
  02 vault_fill          Vault 아이디·비밀번호 입력  WRITE_EXTERNAL (credential_scope 참조만)
  03 login_confirm       로그인 제출·성공 확인       WRITE_EXTERNAL
  04 sales_settlement    매출·정산 조회              READ
  05 review_ads          리뷰·광고 조회              READ

이 카탈로그는 browser_recipes 행이 아니다. 실행도, 승인도 하지 않는다. 운영자가 실제 화면에서
조각을 녹화한 뒤 smart_browser register_e2e 로 등록하면 승인 대기 초안이 되고, 승인은 사람이 한다
(auto_approve 금지). 선택자는 여기에 적지 않는다 — 관측한 화면에서만 얻는다. 비밀번호·쿠키·토큰
값도 여기에 없다. 자격증명은 credential_scope 이름으로만 참조한다.

쿠팡이츠 01~03 은 이미 승인 대기 중인 browser_recipe_registry.COUPANGEATS_LOGIN_FRAGMENTS 를
그대로 쓴다(이름·버전 유지, 복제 금지). 04·05 만 새로 추가한다.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from app.services.browser_recipe_registry import COUPANGEATS_LOGIN_FRAGMENTS
from app.services.delivery_collection_gate import USER_ACTION_KIND_BY_CODE
from app.services.yeoljeong_delivery_collectors import PORTAL_CONFIG

DELIVERY_RECIPE_SERVICES = ("baemin", "coupangeats", "yogiyo", "ddangyo")
LANE = "pc_agent"
REGISTRATION_STATE = "draft_pending_approval"

# 조각 번호 → (이름 접미, 위험도). 로그인 제출은 외부 상태를 바꾸므로 WRITE_EXTERNAL.
_PURPOSES = (
    ("01", "open_login", "READ"),
    ("02", "vault_fill", "WRITE_EXTERNAL"),
    ("03", "login_confirm", "WRITE_EXTERNAL"),
    ("04", "sales_settlement_query", "READ"),
    ("05", "review_ads_query", "READ"),
)

# 앱별로 다른 부분만 적는다. 나머지는 모든 앱 공통 규칙이다.
_SERVICE_NOTES: dict[str, dict[str, Any]] = {
    "baemin": {
        "work_domain": "self.baemin.com",
        "notes": "로그인은 biz-member.baemin.com, 조회는 self.baemin.com. 보안 위배 접근 제한 화면이면 즉시 중단.",
    },
    "coupangeats": {
        "work_domain": "store.coupangeats.com",
        "notes": "포털 차단(PORTAL_BLOCKED) 시 재시도하지 않고 정산 CSV 업로드로 대체한다.",
    },
    "yogiyo": {
        "work_domain": "ceo.yogiyo.co.kr",
        "notes": "",
    },
    "ddangyo": {
        "work_domain": "boss.ddangyo.com",
        "notes": "로그인 후 숫자 캡차가 뜨면 사용자 조치 상태로 전환. 안내 팝업 닫기는 01 이후 읽기 단계에서만.",
    },
}

CSV_FALLBACK = {
    "kind": "settlement_csv_upload",
    "api": "POST /api/v1/yeoljeong-finance/settlements/import",
    "screen": "데이터 가져오기 > 배달 정산 구분 > 서버 자동정리",
}


def _login_domain(service: str) -> str:
    return urlparse(str(PORTAL_CONFIG[service]["login_url"])).hostname or ""


def _user_action_codes(*kinds: str) -> list[str]:
    return sorted(code for code, kind in USER_ACTION_KIND_BY_CODE.items() if kind in kinds)


def _generic_fragment(service: str, number: str, purpose: str, risk: str, previous: dict[str, Any] | None) -> dict[str, Any]:
    name = f"{service}_{number}_{purpose}"
    login_domain = _login_domain(service)
    work_domain = _SERVICE_NOTES[service]["work_domain"]
    scope = f"yeoljeong-{service}"
    sections = PORTAL_CONFIG[service]["sections"]
    base: dict[str, Any] = {
        "name": name,
        "version": 1,
        "service": service,
        "domain": login_domain if number in {"01", "02", "03"} else work_domain,
        "lane": LANE,
        "work_key": f"{service}:owner:login",
        "credential_scope": scope,
        "predecessor": {"name": previous["name"], "version": previous["version"]} if previous else None,
    }
    if number == "01":
        base.update({
            "starts_when": "authenticated_pc_session_available",
            "succeeds_when": "login_form_visible_without_access_denied_or_challenge",
            "recover_at": "open_login_start",
            "step_requirements": ["navigate_exact_login_url"],
            "verify_requirements": ["visible_login_form", "no_access_denied_or_challenge"],
        })
    elif number == "02":
        base.update({
            "starts_when": "open_login_screen_verified_in_same_pc_session",
            "succeeds_when": "vault_identity_matches_and_password_field_filled",
            "recover_at": "open_login_form",
            "password_check": "filled_or_empty_only",
            "step_requirements": ["fill_username_from_vault", "fill_password_from_vault"],
            "verify_requirements": ["username_matches_vault", "password_field_filled_boolean_only"],
        })
    elif number == "03":
        base.update({
            "starts_when": "approved_vault_fill_screen_verified_in_same_pc_session",
            "succeeds_when": "authenticated_store_matches_vault_without_access_denied_captcha_or_login_error",
            "recover_at": "open_login_form_without_resubmitting",
            "step_requirements": ["click_observed_submit_control"],
            "verify_requirements": ["authenticated_username_matches_vault", "authenticated_store_matches_vault", "no_error_or_challenge"],
        })
    elif number == "04":
        base.update({
            "starts_when": "login_confirmed_in_same_pc_session",
            "succeeds_when": "sales_and_settlement_rows_or_explicit_empty_state_for_requested_period",
            "recover_at": "dashboard_home_keep_session",
            "step_requirements": ["enter_sales_section", "enter_settlement_section", "select_requested_period", "query_or_download"],
            "verify_requirements": ["period_matches_request", "rows_or_explicit_empty_state"],
            "sections": {"sales": list(sections["sales"]), "settlements": list(sections["settlements"])},
            "record_types": ["sales", "settlements"],
        })
    else:
        base.update({
            "starts_when": "login_confirmed_in_same_pc_session",
            "succeeds_when": "review_and_ads_rows_or_explicit_empty_state_for_requested_period",
            "recover_at": "dashboard_home_keep_session",
            "step_requirements": ["enter_review_section", "enter_ads_section", "select_requested_period", "read_rows"],
            "verify_requirements": ["period_matches_request", "rows_or_explicit_empty_state"],
            "sections": {"reviews": list(sections["reviews"]), "ads": list(sections["ads"])},
            "record_types": ["reviews", "ads"],
        })
    return base


def _finish(fragment: dict[str, Any], service: str, number: str, risk: str) -> dict[str, Any]:
    """모든 조각에 같은 승인·재시도 정책을 붙인다. 로그인 조각(01~03)은 사람 조치 코드에서 멈춘다."""
    login_piece = number in {"01", "02", "03"}
    fragment = dict(fragment)
    fragment.update({
        "fragment_no": number,
        "max_risk": risk,
        "registration_state": REGISTRATION_STATE,
        "auto_approve": False,
        "requires_observed_selectors": True,
        "secret_policy": "vault_reference_only",
        "on_failure": {
            "return_to": fragment["recover_at"],
            "user_action_codes": _user_action_codes("login_required", "captcha", "portal_blocked") if login_piece else _user_action_codes("portal_blocked"),
            "scheduler_retry": False if login_piece else "after_cooldown",
        },
        "notes": _SERVICE_NOTES[service]["notes"],
    })
    if service == "coupangeats":
        fragment["csv_fallback"] = dict(CSV_FALLBACK)
    return fragment


def _coupangeats_existing(number: str) -> dict[str, Any] | None:
    purpose = {"01": "open_login", "02": "vault_fill", "03": "login_confirm"}.get(number)
    if not purpose:
        return None
    wanted = f"coupangeats_{number}_{purpose}"
    return next((dict(item) for item in COUPANGEATS_LOGIN_FRAGMENTS if item["name"] == wanted), None)


def build_catalog() -> dict[str, list[dict[str, Any]]]:
    catalog: dict[str, list[dict[str, Any]]] = {}
    for service in DELIVERY_RECIPE_SERVICES:
        pieces: list[dict[str, Any]] = []
        previous: dict[str, Any] | None = None
        for number, purpose, risk in _PURPOSES:
            existing = _coupangeats_existing(number) if service == "coupangeats" else None
            if existing is not None:
                fragment = {**existing, "service": service}
            else:
                fragment = _generic_fragment(service, number, purpose, risk, previous)
                if service == "coupangeats" and previous is not None:
                    fragment["predecessor"] = {"name": previous["name"], "version": previous["version"]}
            fragment = _finish(fragment, service, number, risk)
            pieces.append(fragment)
            previous = fragment
        catalog[service] = pieces
    return catalog


def fragment_names(service: str | None = None) -> list[str]:
    catalog = build_catalog()
    services = [service] if service else list(DELIVERY_RECIPE_SERVICES)
    return [item["name"] for key in services for item in catalog.get(key, [])]


def register_e2e_inputs(name: str) -> dict[str, Any]:
    """smart_browser register_e2e 에 넘길 조각 메타데이터. 화면 증거·선택자는 운영자가 채운다."""
    for pieces in build_catalog().values():
        for item in pieces:
            if item["name"] == name:
                return {
                    "recipe_name": item["name"],
                    "domain": item["domain"],
                    "starts_when": item["starts_when"],
                    "succeeds_when": item["succeeds_when"],
                    "lane": item["lane"],
                    "browser_work_key": item["work_key"],
                }
    raise KeyError(name)
