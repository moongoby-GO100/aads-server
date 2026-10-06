"""browser_snapshot 비밀번호 칸 값 마스킹 (AADS-BROWSER-SNAPSHOT-MASK-PASSWORD)."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

from app.api import ceo_chat_tools as cct
from app.api.ceo_chat_tools import (
    _DOM_FALLBACK_JS,
    _format_dom_fallback,
    _mask_password_fields_in_aria,
)

SECRET = "S3cr3t!값#99"


def test_korean_password_value_filled():
    snap = f'- textbox "비밀번호": {SECRET}'
    out = _mask_password_fields_in_aria(snap, [])
    assert out == '- textbox "비밀번호": [filled]'
    assert SECRET not in out


def test_english_password_label_masked():
    out = _mask_password_fields_in_aria(f'- textbox "Password": {SECRET}', [])
    assert SECRET not in out and "[filled]" in out


def test_generic_name_masked_when_identified_by_type():
    snap = f'- textbox "Your secret": {SECRET}\n- textbox "Memo": hello'
    out = _mask_password_fields_in_aria(snap, ["Your secret"])
    assert SECRET not in out
    assert '- textbox "Your secret": [filled]' in out
    assert "- textbox \"Memo\": hello" in out


def test_generic_name_not_masked_without_identification():
    snap = '- textbox "Your secret": abc'
    assert _mask_password_fields_in_aria(snap, []) == snap


def test_empty_password_field():
    assert _mask_password_fields_in_aria('- textbox "비밀번호"', []) == '- textbox "비밀번호": [empty]'
    assert _mask_password_fields_in_aria('- textbox "비밀번호":', []) == '- textbox "비밀번호": [empty]'


def test_filled_flag_from_page_overrides_missing_value():
    out = _mask_password_fields_in_aria('- textbox "Secret"', {"Secret": True})
    assert out == '- textbox "Secret": [filled]'


def test_username_value_kept():
    snap = f'- textbox "아이디": admin01\n- textbox "비밀번호": {SECRET}'
    out = _mask_password_fields_in_aria(snap, [])
    assert "- textbox \"아이디\": admin01" in out
    assert SECRET not in out


def test_placeholder_child_line_preserved():
    snap = (
        '- form:\n'
        f'  - textbox "비밀번호": {SECRET}\n'
        '    - /placeholder: 비밀번호 입력\n'
        '  - button "로그인"'
    )
    out = _mask_password_fields_in_aria(snap, [])
    assert out.split("\n") == [
        '- form:',
        '  - textbox "비밀번호": [filled]',
        '    - /placeholder: 비밀번호 입력',
        '  - button "로그인"',
    ]


def test_attrs_kept_and_unnamed_password_field():
    out = _mask_password_fields_in_aria(
        f'- textbox "Pwd" [disabled]: {SECRET}\n- textbox: {SECRET}', {"": True}
    )
    assert SECRET not in out
    assert '- textbox "Pwd" [disabled]: [filled]' in out


def test_name_pattern_does_not_hit_unrelated_words():
    snap = '- textbox "Pwa name": x\n- textbox "Display": y'
    assert _mask_password_fields_in_aria(snap, []) == snap


def test_escaped_quote_in_name():
    out = _mask_password_fields_in_aria(f'- textbox "비밀번호 \\"확인\\"": {SECRET}', [])
    assert SECRET not in out


def test_dom_fallback_drops_password_text():
    els = [
        {"tag": "input", "type": "password", "text": SECRET, "placeholder": "pw", "role": "", "href": ""},
        {"tag": "input", "type": "text", "text": "", "placeholder": "id", "role": "", "href": "", "secret": True},
        {"tag": "button", "type": "", "text": "로그인", "placeholder": "", "role": "", "href": ""},
    ]
    out = _format_dom_fallback(els, "https://x.newtalk.kr", "t")
    assert SECRET not in out
    assert "로그인" in out


def test_dom_fallback_js_never_reads_password_value():
    assert ".value" not in _DOM_FALLBACK_JS
    assert "password" in _DOM_FALLBACK_JS


def _run_snapshot(aria, evaluate_side_effect):
    page = MagicMock()
    page.url = "https://aads.newtalk.kr/login"
    page.title = AsyncMock(return_value="t")
    page.evaluate = AsyncMock(side_effect=evaluate_side_effect)
    page.locator.return_value.aria_snapshot = (
        AsyncMock(return_value=aria) if not isinstance(aria, Exception) else AsyncMock(side_effect=aria)
    )
    orig = (cct._acquire_pw_context, cct._current_page)
    cct._acquire_pw_context = AsyncMock(return_value=(object(), None))
    cct._current_page = AsyncMock(return_value=page)
    try:
        return asyncio.run(cct.tool_browser_snapshot())
    finally:
        cct._acquire_pw_context, cct._current_page = orig


def test_tool_masks_and_keeps_header():
    out = _run_snapshot(
        f'- textbox "Login pw": {SECRET}\n- textbox "ID": u1',
        [[{"name": "Login pw", "filled": True}]],
    )
    assert out.startswith("[ARIA 스냅샷 — https://aads.newtalk.kr/login]")
    assert SECRET not in out and "u1" in out


def test_tool_fail_closed_when_identification_fails():
    def effect(js):
        raise RuntimeError("boom")

    out = _run_snapshot(f'- textbox "비밀번호": {SECRET}', effect)
    assert SECRET not in out and "[filled]" in out


def test_tool_dom_fallback_when_aria_fails():
    calls = []

    def effect(js):
        calls.append(js)
        if len(calls) == 1:
            return []
        return [{"tag": "input", "type": "password", "text": SECRET, "placeholder": "", "role": "", "href": "", "secret": True},
                {"tag": "input", "type": "password", "text": "", "placeholder": "pw", "role": "", "href": ""}]

    out = _run_snapshot(RuntimeError("no aria"), effect)
    assert SECRET not in out
    assert out.startswith("[UI 요소 추출")
