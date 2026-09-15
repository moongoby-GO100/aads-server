"""승인 버튼이 사유 없이 눌려도 통과해야 한다.

2026-09-15, 대표님이 `/approvals` 에서 승인이 안 먹는다고 알려 오셨다.
원인은 한 줄이었다.

    SET decision = $2, reason = NULLIF($3, ''), ...

`reason` 은 `NOT NULL` 컬럼이다. 사유를 비우고 누르면 NULLIF 가 NULL 을
만들고, 제약에 걸려 UPDATE 가 통째로 실패한다 — 그리고 API 는 그것을
503 "승인 처리 실패" 로 돌려준다. 사유를 적고 누르는 사람은 드물기 때문에
사실상 **승인이 언제나 실패**했다.
"""
import inspect
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
SOURCE = (REPO / "app" / "api" / "project_docs.py").read_text()


def test_decide_does_not_null_out_a_not_null_column():
    assert "reason = NULLIF($3, '')" not in SOURCE
    assert "reason = CASE WHEN $3 <> '' THEN $3 ELSE reason END" in SOURCE


def test_no_bare_nullif_assignment_into_reason():
    """같은 모양이 다시 들어오는 것을 막는다."""
    bad = re.findall(r"reason\s*=\s*NULLIF\(\s*\$\d", SOURCE)
    assert bad == [], bad


def test_decide_still_records_a_reason_when_given():
    """사유를 적었으면 그대로 남아야 한다 — 빈 사유만 예외다."""
    block = SOURCE.split("approvals_decide", 1)[1]
    assert "$3 <> ''" in block
