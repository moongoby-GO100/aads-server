"""런타임 패치: auto-register 라우트를 기존 FastAPI 앱에 등록."""
import sys
import importlib

def patch():
    main_mod = sys.modules.get("app.main")
    if not main_mod:
        print("ERROR: app.main not loaded")
        return False

    app = getattr(main_mod, "app", None)
    if not app:
        print("ERROR: app object not found")
        return False

    # 1) auth whitelist 패치
    exempt = getattr(main_mod, "_AUTH_EXEMPT_PREFIXES", ())
    target_path = "/api/v1/devices/android/auto-register"
    if target_path not in exempt:
        main_mod._AUTH_EXEMPT_PREFIXES = exempt + (target_path,)
        print(f"OK: auth whitelist patched ({len(exempt)} -> {len(exempt)+1})")
    else:
        print("OK: auth whitelist already has auto-register")

    # 2) device 모듈 리로드
    device_mod = sys.modules.get("app.api.device")
    if device_mod:
        importlib.reload(device_mod)
        new_router = getattr(device_mod, "router", None)
        if new_router:
            # 기존 device 라우트 제거
            app.routes = [r for r in app.routes if not (hasattr(r, 'path') and '/devices/' in getattr(r, 'path', ''))]
            # 새 라우터 등록
            app.include_router(new_router, prefix="/api/v1", tags=["device"])
            print(f"OK: device router re-registered ({len(new_router.routes)} routes)")
            return True

    print("ERROR: app.api.device not loaded")
    return False

if __name__ == "__main__":
    patch()
