import importlib.util
import sys
from pathlib import Path


_ORCHESTRATOR_PATH = Path(__file__).resolve().parents[2] / "app" / "services" / "auth_challenge_orchestrator.py"
_SPEC = importlib.util.spec_from_file_location("auth_challenge_orchestrator", _ORCHESTRATOR_PATH)
orchestrator = importlib.util.module_from_spec(_SPEC)
assert _SPEC and _SPEC.loader
sys.modules[_SPEC.name] = orchestrator
_SPEC.loader.exec_module(orchestrator)

classify_portal_state = orchestrator.classify_portal_state
make_resume_token = orchestrator.make_resume_token
validate_provider_output = orchestrator.validate_provider_output


def test_classifies_challenges_without_solving_them():
    assert classify_portal_state("https://portal.test", "보안문자 입력").state == "captcha_required"
    assert classify_portal_state("https://portal.test", "인증번호를 입력").state == "otp_required"
    assert classify_portal_state("https://portal.test/login", "로그인").state == "login_required"
    assert classify_portal_state("https://portal.test", "인증서 비밀번호").state == "certificate_password_required"
    assert classify_portal_state("https://portal.test", "본인인증").state == "identity_check_required"
    assert classify_portal_state("https://portal.test", "조회된 거래내역이 없습니다").state == "no_records"
    assert classify_portal_state("https://portal.test", "거래일자 입금금액 출금금액").state == "transaction_table"


def test_provider_state_is_allowlisted_and_invalid_output_is_unknown():
    assert validate_provider_output({"state": "collectable_page", "evidence": ["table"]}).state == "collectable_page"
    assert validate_provider_output({"state": "solve_captcha"}).state == "unknown"
    assert validate_provider_output({"state": "login_required", "confidence": 0.2}).state == "unknown"
    assert validate_provider_output(
        {"state": "login_required", "confidence": 0.9, "suggested_action": "solve_captcha"}
    ).state == "unknown"


def test_resume_token_is_opaque_and_stable():
    token = make_resume_token("work", "session", "run")
    assert token == make_resume_token("work", "session", "run")
    assert len(token) == 32
    assert "work" not in token and "session" not in token
