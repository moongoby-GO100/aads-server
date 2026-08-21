from app.services.auth_challenge_orchestrator import (
    classify_portal_state,
    make_resume_token,
    validate_provider_output,
)


def test_classifies_challenges_without_solving_them():
    assert classify_portal_state("https://portal.test", "보안문자 입력").state == "captcha_required"
    assert classify_portal_state("https://portal.test", "인증번호를 입력").state == "otp_required"
    assert classify_portal_state("https://portal.test/login", "로그인").state == "login_required"


def test_provider_state_is_allowlisted_and_invalid_output_is_unknown():
    assert validate_provider_output({"state": "collectable_page", "evidence": ["table"]}).state == "collectable_page"
    assert validate_provider_output({"state": "solve_captcha"}).state == "unknown"


def test_resume_token_is_opaque_and_stable():
    token = make_resume_token("work", "session", "run")
    assert token == make_resume_token("work", "session", "run")
    assert len(token) == 32
    assert "work" not in token and "session" not in token
