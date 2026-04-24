"""Governance v2.1: feature_flags.py + intent_router.py 패치"""

# Patch 1: feature_flags.py — governance_enabled() 추가
FF_PATH = "/root/aads/aads-server/app/core/feature_flags.py"
with open(FF_PATH, "r") as f:
    ff_code = f.read()

if "governance_enabled" not in ff_code:
    # get_flag 함수 뒤, set_flag 함수 앞에 삽입
    old_ff = "async def set_flag(flag_key: str, enabled: bool, changed_by: str = \"system\") -> dict[str, Any]:"
    new_ff = """async def governance_enabled(default: bool = True) -> bool:
    return await get_flag("governance_enabled", default=default)


async def set_flag(flag_key: str, enabled: bool, changed_by: str = "system") -> dict[str, Any]:"""
    if old_ff in ff_code:
        ff_code = ff_code.replace(old_ff, new_ff, 1)
        with open(FF_PATH, "w") as f:
            f.write(ff_code)
        print("Patch 1: feature_flags.py governance_enabled() added OK")
    else:
        print("Patch 1: SKIP — set_flag pattern not found")
else:
    print("Patch 1: SKIP — governance_enabled already exists")

# Patch 2: intent_router.py — resolve_intent_temperature 존재 확인
IR_PATH = "/root/aads/aads-server/app/services/intent_router.py"
with open(IR_PATH, "r") as f:
    ir_code = f.read()

if "resolve_intent_temperature" in ir_code:
    print("Patch 2: intent_router.py resolve_intent_temperature OK (already exists)")
else:
    print("Patch 2: WARNING — resolve_intent_temperature NOT found in intent_router.py")

# Syntax check
import py_compile
try:
    py_compile.compile(FF_PATH, doraise=True)
    print("Syntax check feature_flags.py: OK")
except Exception as e:
    print(f"Syntax check feature_flags.py: FAIL — {e}")
