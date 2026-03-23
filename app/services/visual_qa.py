"""
Visual QA Service — Playwright 기반 스크린샷 자동 촬영 및 Visual Regression.

playwright 또는 Docker 기반 fallback으로 headless 스크린샷 촬영.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import structlog

logger = structlog.get_logger()

WORKSPACE_ROOT = Path("/tmp/aads_workspace")
SCREENSHOTS_DIR = WORKSPACE_ROOT / "screenshots"
BASELINES_DIR = WORKSPACE_ROOT / "baselines"

# 호스트에서 playwright 직접 실행 가능 여부 캐시
_playwright_available: Optional[bool] = None


def _check_playwright_available() -> bool:
    """playwright CLI 혹은 Python playwright 직접 실행 가능 여부 확인."""
    global _playwright_available
    if _playwright_available is not None:
        return _playwright_available
    try:
        result = subprocess.run(
            ["python3", "-c", "from playwright.sync_api import sync_playwright; p = sync_playwright().start(); p.stop()"],
            capture_output=True,
            timeout=15,
        )
        _playwright_available = result.returncode == 0
    except Exception:
        _playwright_available = False
    logger.info("playwright_availability_check", available=_playwright_available)
    return _playwright_available


# ---------------------------------------------------------------------------
# Playwright 스크립트 (Docker 또는 직접 실행)
# ---------------------------------------------------------------------------

PLAYWRIGHT_CAPTURE_SCRIPT = """
import sys, asyncio, json
from pathlib import Path
from playwright.async_api import async_playwright

async def main(base_url, pages, out_dir, project_id):
    out = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1920, "height": 1080})
        page = await context.new_page()
        for path in pages:
            url = base_url.rstrip("/") + path
            page_name = path.strip("/").replace("/", "_") or "home"
            ts = __import__("time").strftime("%Y%m%d_%H%M%S")
            fname = f"{page_name}_{ts}.png"
            out_path = Path(out_dir) / project_id / fname
            out_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                await page.goto(url, wait_until="networkidle", timeout=30000)
                await page.screenshot(path=str(out_path), full_page=False)
                out.append({"page": path, "page_name": page_name, "path": str(out_path), "success": True})
            except Exception as e:
                out.append({"page": path, "page_name": page_name, "path": None, "success": False, "error": str(e)})
        await browser.close()
    print(json.dumps(out))

data = json.loads(sys.argv[1])
asyncio.run(main(data["base_url"], data["pages"], data["out_dir"], data["project_id"]))
"""

PLAYWRIGHT_COMPARE_SCRIPT = """
import sys, json
from pathlib import Path
from PIL import Image, ImageChops, ImageFilter
import math

def main(current_path, baseline_path):
    c = Path(current_path)
    b = Path(baseline_path)
    if not c.exists():
        print(json.dumps({"match": False, "diff_percent": 100.0, "diff_image_path": None, "error": "current not found"}))
        return
    if not b.exists():
        print(json.dumps({"match": False, "diff_percent": 100.0, "diff_image_path": None, "error": "baseline not found"}))
        return
    img_c = Image.open(c).convert("RGB")
    img_b = Image.open(b).convert("RGB")
    # 크기 맞추기
    if img_c.size != img_b.size:
        img_b = img_b.resize(img_c.size, Image.LANCZOS)
    diff = ImageChops.difference(img_c, img_b)
    pixels = list(diff.getdata())
    total = len(pixels)
    nonzero = sum(1 for px in pixels if any(v > 5 for v in px))
    diff_percent = round((nonzero / total) * 100, 4) if total else 0.0
    match = diff_percent < 1.0
    diff_path = None
    if not match:
        diff_path = str(c.parent / ("diff_" + c.name))
        enhanced = diff.point(lambda x: min(255, x * 10))
        enhanced.save(diff_path)
    print(json.dumps({"match": match, "diff_percent": diff_percent, "diff_image_path": diff_path}))

data = json.loads(sys.argv[1])
main(data["current_path"], data["baseline_path"])
"""


@dataclass
class ScreenshotResult:
    page: str
    page_name: str
    path: Optional[str]
    success: bool
    error: Optional[str] = None


@dataclass
class CompareResult:
    match: bool
    diff_percent: float
    diff_image_path: Optional[str]
    error: Optional[str] = None


async def _run_script_direct(script: str, arg: dict) -> str:
    """호스트에서 직접 Python playwright 실행."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(script)
        script_path = f.name
    try:
        proc = await asyncio.create_subprocess_exec(
            "python3", script_path, json.dumps(arg),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        if proc.returncode != 0:
            raise RuntimeError(f"Script failed: {stderr.decode()}")
        return stdout.decode().strip()
    finally:
        os.unlink(script_path)


async def _run_script_docker(script: str, arg: dict) -> str:
    """Docker 컨테이너에서 playwright 실행 (호스트 glibc 구버전 fallback)."""
    screenshots_dir = WORKSPACE_ROOT / "screenshots"
    baselines_dir = WORKSPACE_ROOT / "baselines"
    screenshots_dir.mkdir(parents=True, exist_ok=True)
    baselines_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, dir="/tmp") as f:
        f.write(script)
        script_path = f.name

    try:
        cmd = [
            "docker", "run", "--rm",
            "-v", f"/tmp/aads_workspace:/tmp/aads_workspace",
            "-v", f"{script_path}:{script_path}",
            "--network", "host",
            "mcr.microsoft.com/playwright:v1.48.0-jammy",
            "python3", script_path, json.dumps(arg),
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=180)
        if proc.returncode != 0:
            raise RuntimeError(f"Docker playwright failed: {stderr.decode()}")
        return stdout.decode().strip()
    finally:
        os.unlink(script_path)


async def _run_playwright_script(script: str, arg: dict) -> str:
    """playwright 스크립트 실행 (직접 → Docker fallback)."""
    if _check_playwright_available():
        return await _run_script_direct(script, arg)
    else:
        return await _run_script_docker(script, arg)


async def _run_pillow_compare(current_path: str, baseline_path: str) -> str:
    """Pillow 비교 스크립트 실행 (호스트에서 직접; Pillow는 glibc 무관)."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(PLAYWRIGHT_COMPARE_SCRIPT)
        script_path = f.name
    try:
        proc = await asyncio.create_subprocess_exec(
            "python3", script_path,
            json.dumps({"current_path": current_path, "baseline_path": baseline_path}),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        if proc.returncode != 0:
            raise RuntimeError(f"Compare script failed: {stderr.decode()}")
        return stdout.decode().strip()
    finally:
        os.unlink(script_path)


class VisualQAService:
    """Visual QA — Playwright 스크린샷 + Pillow pixelmatch 비교."""

    async def capture_screenshots(
        self,
        base_url: str,
        pages: List[str],
        project_id: str,
    ) -> List[ScreenshotResult]:
        """
        headless Playwright으로 각 페이지 스크린샷 촬영.

        Returns: ScreenshotResult 목록 (path, success, error)
        """
        SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)

        arg = {
            "base_url": base_url,
            "pages": pages,
            "out_dir": str(SCREENSHOTS_DIR),
            "project_id": project_id,
        }
        logger.info("capture_screenshots_start", base_url=base_url, pages=pages, project_id=project_id)

        raw = await _run_playwright_script(PLAYWRIGHT_CAPTURE_SCRIPT, arg)
        data = json.loads(raw)

        results = []
        for item in data:
            results.append(ScreenshotResult(
                page=item["page"],
                page_name=item["page_name"],
                path=item.get("path"),
                success=item["success"],
                error=item.get("error"),
            ))
        logger.info("capture_screenshots_done", count=len(results), project_id=project_id)
        return results

    async def compare_with_baseline(
        self,
        current_path: str,
        baseline_path: str,
    ) -> CompareResult:
        """
        Pillow pixelmatch으로 current vs baseline 비교.

        Returns: CompareResult {match, diff_percent, diff_image_path}
        """
        logger.info("compare_start", current=current_path, baseline=baseline_path)
        raw = await _run_pillow_compare(current_path, baseline_path)
        data = json.loads(raw)
        result = CompareResult(
            match=data["match"],
            diff_percent=data["diff_percent"],
            diff_image_path=data.get("diff_image_path"),
            error=data.get("error"),
        )
        logger.info("compare_done", match=result.match, diff_percent=result.diff_percent)
        return result

    async def save_as_baseline(
        self,
        screenshot_path: str,
        project_id: str,
        page_name: str,
    ) -> str:
        """현재 스크린샷을 baseline으로 저장. 저장된 경로 반환."""
        baseline_dir = BASELINES_DIR / project_id
        baseline_dir.mkdir(parents=True, exist_ok=True)
        dest = baseline_dir / f"{page_name}_baseline.png"
        shutil.copy2(screenshot_path, dest)
        logger.info("baseline_saved", src=screenshot_path, dest=str(dest))
        return str(dest)

    async def list_baselines(self, project_id: str) -> List[dict]:
        """등록된 baseline 목록 반환."""
        baseline_dir = BASELINES_DIR / project_id
        if not baseline_dir.exists():
            return []
        results = []
        for f in sorted(baseline_dir.glob("*.png")):
            stat = f.stat()
            results.append({
                "page_name": f.stem.replace("_baseline", ""),
                "path": str(f),
                "size_bytes": stat.st_size,
                "created_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            })
        return results

    def get_latest_screenshot(self, project_id: str, page_name: str) -> Optional[str]:
        """project_id / page_name에 해당하는 최신 스크린샷 경로 반환."""
        shot_dir = SCREENSHOTS_DIR / project_id
        if not shot_dir.exists():
            return None
        matches = sorted(shot_dir.glob(f"{page_name}_*.png"), reverse=True)
        return str(matches[0]) if matches else None


visual_qa_service = VisualQAService()
