"""AADS-REVIEWER-PRESERVATION-PUBLIC-SIGNATURE — 공개 선언 1:1 재작성 면제 고정.

보존 하드 게이트는 "같은 파일 diff 에서 선언이 정확히 한 번 사라지고 한 번 다시
생겼으면 그것은 삭제가 아니라 시그니처 재작성" 이라는 면제를 private 함수(`_foo`)
에만 적용했다. 그래서 공개 함수에 인자를 하나 더하는 흔한 변경이 통째로
"public 함수 삭제" 로 잡혔다.

2026-09-18 실측 — runner-1f09e9e5(오비서 O3 매입·재고, 553추가/81삭제)가

    -async def get_item(item_id: str) -> Optional[dict[str, Any]]:
    +async def get_item(company_id: str, item_id: str) -> Optional[dict[str, Any]]:

이 꼴의 시그니처 변경 9건 때문에 FLAG 0.3(PRESERVATION_HARD_GATE)으로 전량
폐기됐다. 같은 오판이 runner-3eeda0e6 / runner-2872d735 에서도 났다.

이 파일은 두 갈래를 고정한다.
① 공개 함수·클래스 선언의 1:1 재작성은 삭제로 보지 않는다.
② 진짜 삭제(+ 쪽 없음), 리네임(+ 쪽 이름 다름), 이름만으로 동일성을 판정할 수
   없는 `@router.*`, 같은 이름이 여러 번 사라지는 경우는 계속 게이트에 남는다.
"""

import app.services.code_reviewer as cr


def _file_diff(path: str, body: str) -> str:
    """한 파일짜리 가짜 diff. 게이트는 `diff --git` 단위로 쪼개 본다."""
    return (
        f"diff --git a/{path} b/{path}\n"
        f"index 0000001..0000002 100644\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        f"@@ -1,8 +1,8 @@\n"
        f"{body}"
    )


_SERVICE = "app/services/yeoljeong_inventory_service.py"
_API = "app/api/obys_inventory.py"


def test_public_async_signature_rewrite_is_not_a_removal():
    """공개 async 함수에 인자를 더한 것은 삭제가 아니다 (runner-1f09e9e5 회귀)."""
    diff = _file_diff(
        _SERVICE,
        "-async def get_item(item_id: str) -> dict:\n"
        "+async def get_item(company_id: str, item_id: str) -> dict:\n",
    )

    assert cr._removed_preservation_symbols(diff) == []


def test_runner_1f09e9e5_nine_signatures_pass_the_gate():
    """실제로 차단됐던 9개 선언을 그대로 넣어도 게이트에 남는 것이 없다."""
    names = [
        "get_item", "create_item", "update_item", "delete_item",
        "low_stock_items", "get_order", "create_order", "update_order",
        "delete_order",
    ]
    body = "".join(
        f"-async def {name}(item_id: str) -> dict:\n"
        f"+async def {name}(company_id: str, item_id: str) -> dict:\n"
        for name in names
    )

    assert cr._removed_preservation_symbols(_file_diff(_SERVICE, body)) == []


def test_public_class_signature_rewrite_is_not_a_removal():
    """클래스 선언의 1:1 재작성도 같은 면제를 받는다."""
    diff = _file_diff(
        _SERVICE,
        "-class StockBalance(BaseModel):\n"
        "+class StockBalance(TenantScopedModel):\n",
    )

    assert cr._removed_preservation_symbols(diff) == []


def test_true_public_deletion_is_still_gated():
    """+ 쪽 짝이 없는 진짜 삭제는 그대로 차단한다."""
    diff = _file_diff(
        _API,
        "-async def delete_order(order_id: str) -> bool:\n"
        "-    return await service.delete_order(order_id)\n"
        "+# 발주 삭제는 더 쓰지 않는다\n",
    )

    assert "async def delete_order" in cr._removed_preservation_symbols(diff)


def test_public_rename_is_still_gated():
    """이름이 바뀐 리네임은 호출부를 깨는 계약 변경이므로 남긴다."""
    diff = _file_diff(
        _SERVICE,
        "-async def create_order(data: dict) -> dict:\n"
        "+async def create_purchase_order(data: dict) -> dict:\n",
    )

    assert "async def create_order" in cr._removed_preservation_symbols(diff)


def test_router_decorator_rewrite_is_still_gated():
    """`@router.get` 은 경로가 심볼에 담기지 않아 이름만으로 동일성을 못 본다."""
    diff = _file_diff(
        _API,
        '-@router.get("/items")\n'
        '+@router.get("/stock-balances")\n',
    )

    assert "@router.get" in cr._removed_preservation_symbols(diff)


def test_duplicate_same_name_removal_is_still_gated():
    """같은 이름이 두 번 사라지고 한 번만 생기면 개수가 맞지 않아 남는다."""
    diff = _file_diff(
        _SERVICE,
        "-async def get_item(item_id: str) -> dict:\n"
        "-async def get_item(item_id: int) -> dict:\n"
        "+async def get_item(company_id: str, item_id: str) -> dict:\n",
    )

    assert "async def get_item" in cr._removed_preservation_symbols(diff)


def test_private_function_exemption_still_holds():
    """종전 면제 대상(private 함수)이 회귀로 깨지지 않는다."""
    diff = _file_diff(
        _SERVICE,
        "-def _company_scope(user: dict) -> str:\n"
        "+def _company_scope(user: dict, strict: bool = True) -> str:\n",
    )

    assert cr._removed_preservation_symbols(diff) == []
