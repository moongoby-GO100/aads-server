"""36개 메뉴 원천표를 서버 코드와 대조한다. 앱·DB·네트워크를 실행하지 않는다."""

import ast
from collections import Counter
from pathlib import Path
import re

import pytest


ROOT = Path(__file__).resolve().parents[2]
PRD = (ROOT / "docs/PRD-OBYS-MOCKUP-V4-1-FINAL.md").read_text()
SERVER = (ROOT / "app/api/obys_workspaces.py").read_text()
TREE = ast.parse(SERVER)
SECTION = PRD.split("### 8.2 ", 1)[1].split("### 8.3 ", 1)[0]


def constant(name):
    for node in TREE.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            target = node.targets[0] if isinstance(node, ast.Assign) else node.target
            if isinstance(target, ast.Name) and target.id == name:
                value = node.value
                if isinstance(value, ast.Call) and value.func.id == "frozenset":
                    value = value.args[0]
                if name == "LEGACY_PEOPLE_SQL":
                    return {ast.literal_eval(key) for key in value.keys}
                return ast.literal_eval(value)
    raise AssertionError("서버 상수 없음: " + name)


def function(name):
    return next(n for n in TREE.body if isinstance(n, ast.AsyncFunctionDef) and n.name == name)


def table_rows():
    rows = []
    for line in SECTION.splitlines():
        if line.startswith("| `"):
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            assert len(cells) == 5, line
            rows.append((cells[0].strip("`"), cells[1:]))
    return rows


ROWS = dict(table_rows())
ROUTES = sorted(constant("ROUTES"))


def compact(value):
    return re.sub(r"\s+", "", value.replace("`", ""))


def assert_documented(fragment, text=SECTION):
    assert compact(fragment) in compact(text), fragment


def branch_for(route):
    """_source_rows의 분기 순서를 읽어 상수 중복 소속의 우선순위를 반영한다."""
    for branch in function("_source_rows").body:
        if not isinstance(branch, ast.If):
            continue
        comparison = branch.test
        if isinstance(comparison, ast.BoolOp):
            comparison = comparison.values[0]  # attendance + own_email 조건
        if not isinstance(comparison, ast.Compare):
            continue
        if not isinstance(comparison.left, ast.Name) or comparison.left.id != "route":
            continue
        right = comparison.comparators[0]
        if isinstance(comparison.ops[0], ast.Eq) and isinstance(right, ast.Constant):
            if right.value == route:
                return "전용", branch
        if isinstance(comparison.ops[0], ast.In) and isinstance(right, ast.Name):
            if route in constant(right.id):
                if right.id == "GENERIC_ROUTE_SQL":
                    # payroll/attendance는 내부 전용 조회·병합이 추가된다.
                    dedicated = any(
                        isinstance(n, ast.Call)
                        and isinstance(n.func, ast.Name)
                        and n.func.id == "_legacy_people_rows"
                        for n in ast.walk(branch)
                    )
                    if dedicated and route in constant("LEGACY_PEOPLE_SQL"):
                        return "전용", branch
                    return "GENERIC", branch
                return right.id[:-len("_ROUTES")], branch
    raise AssertionError("처리 분기 없음: " + route)


def test_exact_route_set_and_unique_rows():
    entries = table_rows()
    assert len(entries) == len(ROWS) == len(ROUTES) == 36
    assert set(ROWS) == set(ROUTES)


@pytest.mark.parametrize("route", ROUTES)
def test_effective_server_path(route):
    expected, _ = branch_for(route)
    processing = ROWS[route][0]
    assert processing.split(" · ")[0] == expected
    assert "_source_rows" in processing or "list_attendance_records" in processing


@pytest.mark.parametrize("route", sorted(constant("GENERIC_ROUTE_SQL")))
def test_generic_sql_table_where_order_and_limit(route):
    if branch_for(route)[0] != "GENERIC":
        return  # 전용 급여·근태 조건은 별도 검사
    sql, table = constant("GENERIC_ROUTE_SQL")[route]
    assert table in ROWS[route][1]
    assert_documented(sql[sql.index("FROM "):], ROWS[route][1])


@pytest.mark.parametrize("route", sorted(constant("LEDGER_ROUTES")))
def test_ledger_category_and_precedence(route):
    category = constant("LEDGER_ROUTES")[route]
    assert_documented("category=" + category, ROWS[route][1])
    path, branch = branch_for(route)
    if path == "전용":
        # tax-gap은 상수의 purchase보다 앞선 sales/purchase 루프와 카드 조회다.
        categories = next(
            ast.literal_eval(n.iter) for n in ast.walk(branch)
            if isinstance(n, ast.For) and isinstance(n.target, ast.Name)
            and n.target.id == "category"
        )
        for value in categories:
            assert_documented("category=" + value, ROWS[route][1])
        assert "yeoljeong_card_transactions" in ROWS[route][1]


@pytest.mark.parametrize("route", sorted(constant("ACCT_SOURCE_ROUTES")))
def test_acct_category_local_and_remote_conditions(route):
    category = constant("ACCT_SOURCE_ROUTES")[route]
    source_tree = ast.parse((ROOT / "app/api/acct_source_ledger.py").read_text())
    entry_types = next(
        ast.literal_eval(n.value) for n in source_tree.body
        if isinstance(n, ast.AnnAssign) and n.target.id == "_ENTRY_TYPE"
    )
    pivot = next(n for n in source_tree.body if isinstance(n, ast.FunctionDef) and n.name == "_pivot_sql")
    condition_map = next(
        ast.literal_eval(n.value.func.value) for n in ast.walk(pivot)
        if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "category_condition" for t in n.targets)
    )
    condition = condition_map.get(category)
    if condition is None:
        condition = "entry_type = '" + entry_types[category] + "'"
    storage = ROWS[route][1]
    assert_documented(condition, storage)
    for fragment in ("source_file", "atom_record", "sf.is_current IS TRUE", "sf.domain='wehago'"):
        assert_documented(fragment, storage)
    local_categories = {
        "sales": ("sales",), "purchase": ("purchase",),
        "evidence": ("sales", "purchase"), "card": ("card",), "bank": ("transaction",),
    }
    for local_category in local_categories[category]:
        assert_documented("category=" + local_category, storage)


def test_people_special_queries_and_employee_scope():
    for route, table in (("payroll", "yeoljeong_payroll_statements"), ("attendance", "yeoljeong_attendance_records")):
        assert table in ROWS[route][1]
        assert_documented("business_id=$1 AND deleted_at IS NULL", ROWS[route][1])
        assert "_legacy_people_rows" in ROWS[route][0]
    payroll_sql = next(
        n.value for n in ast.walk(function("_source_rows"))
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
        and n.value.startswith("SELECT *, (tax_withholding")
    )
    assert_documented(payroll_sql.split("WHERE ")[1].split(" ORDER BY")[0], ROWS["payroll"][1])
    for fragment in ("work_date>=$2", "work_date<=$3", "lower(employee_email)=$4", "lower(employee_email)=$2"):
        assert_documented(fragment, ROWS["attendance"][1])
        assert fragment in SERVER
    employees_sql = constant("GENERIC_ROUTE_SQL")["employees"][0]
    assert "status <> 'archived'" in employees_sql
    assert "status='approved'" not in ROWS["employees"][1]
    assert "approved 필터 없음" in ROWS["employees"][1]
    assert "list_approved_employees" not in SECTION


def test_journals_acct_first_and_bank_categories():
    _, branch = branch_for("journals")
    calls = [n.func.id for n in ast.walk(branch) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
    assert "_acct_journal_records" in calls
    storage = ROWS["journals"][1]
    for fragment in ("ACCT 우선", "journal_entry", "journal_line", "e.company_id", "yeoljeong_journal_vouchers", "v.tenant_id=$1 AND v.business_id=$2", "ACCT 0건은 폴백하지 않음"):
        assert_documented(fragment, storage)
    for route in constant("BANK_ROUTES") - constant("ACCT_SOURCE_ROUTES").keys():
        assert "yeoljeong_manual_bank_transactions" in ROWS[route][1]
        assert_documented("category=transaction", ROWS[route][1])
    for fragment in ("tenant_id=$1 AND business_id=$2 AND category=$3", "(occurred_at AT TIME ZONE 'Asia/Seoul')::date", "DISTINCT ON (occurred_on, voucher_key)"):
        assert_documented(fragment)


def test_retired_scope_excluded_from_current_source_and_acceptance():
    assert "폐기(라일론 제외 — 2026-11-05)" in PRD
    current = re.sub(r"### 12\.1 .*?(?=### 12\.2 )", "", PRD, flags=re.S)
    assert not re.search(r"진아|라일론|lylon", current, re.I)
    assert "### 12.2 포함 사업자 검증 기준" in current
    for name in ("중화", "성신", "미아", "언니냉면"):
        assert name in SECTION


def test_no_invented_measurements_when_query_failed():
    assert Counter(cells[3] for cells in ROWS.values()) == {"조회 실패": 36}
    assert all(cells[2] == "서버 동일조건 재현 불가 — 코드 판정" for cells in ROWS.values())
    assert "operation not permitted" in SECTION
    assert re.search(r"실측 시도 시각: \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} KST", SECTION)
