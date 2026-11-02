"""Optional local Chromium smoke; never an adapter comparison."""

import json
import threading
from pathlib import Path

from fixture_server import FixtureServer


def main():
    try:
        from playwright.sync_api import Error, sync_playwright

    except ImportError as exc:
        print(json.dumps({"status": "not_run", "reason": f"{type(exc).__name__}: {exc}"}))
        return 2

    with sync_playwright() as p:
        # Check only installation absence; launch/runtime failures must propagate.
        if not Path(p.chromium.executable_path).is_file():
            print(json.dumps({"status": "not_run", "reason": "chromium_not_installed"}))
            return 2
        try:
            browser = p.chromium.launch(headless=True)
        except Error as exc:
            if "Executable doesn't exist at " in str(exc):
                reason = "chromium_not_installed"
            elif "Host system is missing dependencies" in str(exc):
                reason = "chromium_dependencies_missing"
            else:
                raise
            print(json.dumps({"status": "not_run", "reason": reason}))
            return 2
        try:
            with FixtureServer() as server:
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    page = browser.new_page()
                    base = f"http://127.0.0.1:{server.server_port}"
                    page.goto(base + "/fixture/delay")
                    page.get_by_text("ready", exact=True).wait_for()
                    page.goto(base + "/fixture/popup")
                    page.get_by_role("button", name="Dismiss").click()
                    assert not page.locator("dialog").is_visible()
                    page.goto(base + "/fixture/dom-move")
                    page.get_by_role("button", name="Continue").click()
                    page.goto(base + "/fixture/navigation")
                    page.get_by_role("link", name="Detail").click()
                    assert page.get_by_role("heading").inner_text() == "Detail 17"
                finally:
                    server.shutdown()
                    thread.join()
        finally:
            browser.close()
    print(json.dumps({"status": "passed", "kind": "synthetic_browser_smoke"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
