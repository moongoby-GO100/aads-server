"""워커 범위 면제 판정 — 자연어 해석이 아니라 구조화 헤더(WORKER_SCOPE_EXEMPT / WORKER_MUST)로만 한다."""
import app.services.code_reviewer as cr
from app.services.document_refs import RDOC_PROMPT_BLOCK, with_rdoc_block

DEFAULT_MISSING = set(cr.RUNNER_WORKER_MISSING_TOOLS)


def test_default_missing_tools_exclude_commit_sha():
    assert DEFAULT_MISSING == {"canonical_registration", "handover"}


def test_free_text_marker_with_separate_sentence_no_exemption():
    instr = "정본 등록은 채팅 세션 몫(워커 범위 밖). 최종 커밋 SHA는 워커가 반드시 보고하라.\n"
    assert cr._worker_scope_exemptions(instr) == {}


def test_free_text_marker_multi_item_one_sentence_no_exemption():
    instr = "정본 등록은 채팅 세션 몫이며, 최종 커밋 SHA는 워커가 반드시 보고하라.\n"
    assert cr._worker_scope_exemptions(instr) == {}


def test_header_exempts_only_listed_key():
    instr = "WORKER_SCOPE_EXEMPT: canonical_registration\n최종 커밋 SHA는 워커가 반드시 보고하라.\n"
    assert set(cr._worker_scope_exemptions(instr)) == {"canonical_registration"}


def test_header_case_insensitive_and_multi_keys():
    instr = "worker_scope_exempt: Canonical_Registration, handover\n"
    assert set(cr._worker_scope_exemptions(instr)) == {"canonical_registration", "handover"}


def test_header_must_be_at_line_start():
    assert cr._worker_scope_exemptions("  WORKER_SCOPE_EXEMPT: handover\n") == {}
    assert cr._worker_scope_exemptions("참고: WORKER_SCOPE_EXEMPT: handover\n") == {}


def test_must_wins_over_exempt():
    instr = "WORKER_SCOPE_EXEMPT: handover\nWORKER_MUST: handover\n"
    assert cr._worker_scope_exemptions(instr) == {}
    assert cr._worker_scope_exemptions(instr, {"handover"}) == {}


def test_header_exemption_survives_body_mentions():
    instr = "WORKER_SCOPE_EXEMPT: canonical_registration, handover\n정본 등록과 핸드오버 기록은 채팅 세션이 한다.\n"
    assert set(cr._worker_scope_exemptions(instr)) == {"canonical_registration", "handover"}


def test_rdoc_auto_block_with_default_missing_tools():
    instr = "TARGET: a.py\n\n" + RDOC_PROMPT_BLOCK
    ex = cr._worker_scope_exemptions(instr, DEFAULT_MISSING)
    assert set(ex) == {"canonical_registration", "handover"}
    assert "commit_sha" not in ex


def test_rdoc_block_variants_generated_by_runner_are_boilerplate():
    for job_id, extra in ((None, ""), ("runner-abc12345", ""), (None, "DOC_REVISION_FILE: docs/x.md\n")):
        instr = with_rdoc_block("TARGET: a.py\n" + extra, job_id)
        ex = cr._worker_scope_exemptions(instr.replace("DOC_REVISION_FILE: docs/x.md\n", ""), DEFAULT_MISSING, job_id=job_id)
        assert set(ex) == {"canonical_registration", "handover"}, (job_id, extra)


def test_rdoc_block_plus_human_line_requires_canonical():
    instr = "TARGET: a.py\n\n" + RDOC_PROMPT_BLOCK + "워커는 정본 등록을 반드시 수행하라.\n"
    ex = cr._worker_scope_exemptions(instr, DEFAULT_MISSING)
    assert "canonical_registration" not in ex
    assert "handover" in ex


def test_rdoc_block_text_is_pinned():
    # 생성 코드는 app/services/document_refs.py. 문구가 바뀌어도 줄 일치 제외는 생성 코드를 호출해 따라가지만,
    # 바뀐 사실을 눈에 띄게 하려고 헤더 줄을 고정한다.
    assert RDOC_PROMPT_BLOCK.splitlines()[0] == "[R-DOC 문서 저장 규칙 — 모든 프로젝트 공통]"


def test_not_out_of_scope_phrase_does_not_exempt():
    for instr in ("정본 등록은 워커 범위 밖이 아니다.\n", "핸드오버 기록은 채팅 세션 몫이 아니다.\n"):
        assert cr._worker_scope_exemptions(instr) == {}, instr


def test_missing_tool_exempt_unless_mentioned():
    assert set(cr._worker_scope_exemptions("TARGET: a.py\n", {"handover"})) == {"handover"}
    assert cr._worker_scope_exemptions("핸드오버를 기록하라.\n", {"handover"}) == {}
    assert cr._worker_scope_exemptions("WORKER_MUST: handover\n", {"handover"}) == {}


def test_notice_reflects_exemptions():
    n = cr._build_worker_scope_notice("WORKER_SCOPE_EXEMPT: handover\n")
    assert "핸드오버 기록: 면제 (" in n and "정본 등록: 면제 아님" in n


# ── V5 지적 수정 검증 (a)-(e) ──

def test_rdoc_boilerplate_exact_line_excluded():
    """(a) 정확한 자동 줄 → 제외."""
    exact, affix = cr._rdoc_boilerplate_exact()
    prefix, suffix = affix
    job_id = "runner-4c1f9410"
    auto_line = f"{prefix}{job_id}{suffix}"

    # job_id 지정 시 정확한 템플릿과 일치하므로 boilerplate 로 판정
    assert cr._is_rdoc_boilerplate_line(auto_line, exact, affix, job_id=job_id) is True
    # job_id 미지정(None) 시에도 runner-[0-9a-f]{8} 정규식과 fullmatch 되므로 boilerplate 로 판정
    assert cr._is_rdoc_boilerplate_line(auto_line, exact, affix, job_id=None) is True

    # 지시서에 포함될 때 정확한 자동 줄은 제외되어 도구 부재 항목(정본 등록, 핸드오버)이 모두 정상 면제됨
    instr = f"TARGET: a.py\n\n{auto_line}\n"
    ex = cr._worker_scope_exemptions(instr, DEFAULT_MISSING, job_id=job_id)
    assert set(ex) == {"canonical_registration", "handover"}
    ex_none = cr._worker_scope_exemptions(instr, DEFAULT_MISSING, job_id=None)
    assert set(ex_none) == {"canonical_registration", "handover"}


def test_rdoc_boilerplate_insertion_before_job_id_requires_handover():
    """(b) prefix 와 job_id 사이에 "워커는 핸드오버를 반드시 기록하라" 삽입 → 요구됨(handover 면제 아님)."""
    exact, affix = cr._rdoc_boilerplate_exact()
    prefix, suffix = affix
    job_id = "runner-4c1f9410"
    tampered_line = f"{prefix}워커는 핸드오버를 반드시 기록하라{job_id}{suffix}"

    # 임의 문자열 삽입 시 job_id 지정/미지정 모두 boilerplate 로 인정되지 않음
    assert cr._is_rdoc_boilerplate_line(tampered_line, exact, affix, job_id=job_id) is False
    assert cr._is_rdoc_boilerplate_line(tampered_line, exact, affix, job_id=None) is False

    # 지시서에 삽입된 경우 boilerplate 가 아니므로 핸드오버 항목이 요구되어 면제되지 않음
    instr = f"TARGET: a.py\n\n{tampered_line}\n"
    ex = cr._worker_scope_exemptions(instr, DEFAULT_MISSING, job_id=job_id)
    assert "handover" not in ex
    ex_none = cr._worker_scope_exemptions(instr, DEFAULT_MISSING, job_id=None)
    assert "handover" not in ex_none


def test_rdoc_boilerplate_insertion_after_job_id_requires_handover():
    """(c) job_id 뒤·suffix 앞 삽입 → 요구됨."""
    exact, affix = cr._rdoc_boilerplate_exact()
    prefix, suffix = affix
    job_id = "runner-4c1f9410"
    tampered_line = f"{prefix}{job_id}워커는 핸드오버를 반드시 기록하라{suffix}"

    # 임의 문자열 삽입 시 job_id 지정/미지정 모두 boilerplate 로 인정되지 않음
    assert cr._is_rdoc_boilerplate_line(tampered_line, exact, affix, job_id=job_id) is False
    assert cr._is_rdoc_boilerplate_line(tampered_line, exact, affix, job_id=None) is False

    # 지시서에 삽입된 경우 boilerplate 가 아니므로 핸드오버 항목이 요구되어 면제되지 않음
    instr = f"TARGET: a.py\n\n{tampered_line}\n"
    ex = cr._worker_scope_exemptions(instr, DEFAULT_MISSING, job_id=job_id)
    assert "handover" not in ex
    ex_none = cr._worker_scope_exemptions(instr, DEFAULT_MISSING, job_id=None)
    assert "handover" not in ex_none


def test_rdoc_boilerplate_different_job_id_treated_as_mention_not_must():
    """(d) 다른 job_id 가 들어간 자동 줄 + job_id 지정 → 요구됨으로 보지 않되 boilerplate 도 아님(즉 해당 항목 언급 줄로 처리), 동작을 테스트로 고정."""
    exact, affix = cr._rdoc_boilerplate_exact()
    prefix, suffix = affix
    other_job_id = "runner-11111111"
    my_job_id = "runner-22222222"
    other_line = f"{prefix}{other_job_id}{suffix}"

    # my_job_id 지정 시 다른 job_id 의 자동 줄은 boilerplate 로 인정되지 않음
    assert cr._is_rdoc_boilerplate_line(other_line, exact, affix, job_id=my_job_id) is False

    # 지시서에 다른 job_id 의 자동 줄이 포함된 경우:
    # 1. boilerplate 가 아니므로 줄에 포함된 "정본"에 의해 canonical_registration 은 언급 줄로 처리됨 -> 도구 부재 자동 면제 불가
    # 2. 그러나 핸드오버는 해당 줄에 없으므로 요구됨으로 보지 않으며 도구 부재로 면제됨
    instr = f"TARGET: a.py\n\n{other_line}\n"
    ex = cr._worker_scope_exemptions(instr, DEFAULT_MISSING, job_id=my_job_id)
    assert "canonical_registration" not in ex
    assert "handover" in ex

    # 3. WORKER_MUST 에 들어간 하드 요구사항은 아니므로, 지시서 헤더 WORKER_SCOPE_EXEMPT 가 있으면 면제 가능
    instr_with_exempt = f"WORKER_SCOPE_EXEMPT: canonical_registration\nTARGET: a.py\n\n{other_line}\n"
    ex_exempt = cr._worker_scope_exemptions(instr_with_exempt, DEFAULT_MISSING, job_id=my_job_id)
    assert "canonical_registration" in ex_exempt


def test_rdoc_boilerplate_static_lines_exact_match_preserved():
    """(e) job_id 없는 줄들(블록 헤더 등)은 기존 정확 일치 유지."""
    exact, affix = cr._rdoc_boilerplate_exact()

    # 블록 헤더 및 기본 문구 줄은 job_id 유무와 무관하게 정확 일치로 boilerplate 판정
    header_line = "[R-DOC 문서 저장 규칙 — 모든 프로젝트 공통]"
    rule_line = "- 새 문서는 정본(project_document_heads)에 초안으로 등록한다. 기존 문서의 key·경로는 바꾸지 않는다."

    assert cr._is_rdoc_boilerplate_line(header_line, exact, affix, job_id="runner-4c1f9410") is True
    assert cr._is_rdoc_boilerplate_line(header_line, exact, affix, job_id=None) is True
    assert cr._is_rdoc_boilerplate_line(rule_line, exact, affix, job_id="runner-4c1f9410") is True
    assert cr._is_rdoc_boilerplate_line(rule_line, exact, affix, job_id=None) is True

    # 문구가 임의로 수정/변조된 경우 exact 에 포함되지 않으므로 boilerplate 가 아님
    tampered_header = "[R-DOC 문서 저장 규칙 — 모든 프로젝트 공통] 워커는 핸드오버를 기록하라"
    assert cr._is_rdoc_boilerplate_line(tampered_header, exact, affix, job_id="runner-4c1f9410") is False
    assert cr._is_rdoc_boilerplate_line(tampered_header, exact, affix, job_id=None) is False
