"""
AADS-195 Phase 3: Windows 서비스 등록 스크립트.
PC 부팅 시 AADS PC Agent 자동 시작.

사용법 (관리자 권한 필요):
    python install_service.py install   — 서비스 설치
    python install_service.py remove    — 서비스 제거
    python install_service.py start     — 서비스 시작
    python install_service.py stop      — 서비스 중지
"""
from __future__ import annotations

import os
import subprocess
import sys


SERVICE_NAME = "AADSPCAgent"
SERVICE_DISPLAY = "AADS PC Agent"
SERVICE_DESCRIPTION = "AADS 자율 AI 개발 시스템 — PC 제어 에이전트"


def _get_exe_path() -> str:
    """빌드된 EXE 경로 반환."""
    # 같은 디렉토리의 dist/AADSAgent.exe
    base = os.path.dirname(os.path.abspath(__file__))
    exe = os.path.join(base, "dist", "AADSAgent.exe")
    if not os.path.exists(exe):
        # 현재 디렉토리에서도 확인
        exe = os.path.join(base, "AADSAgent.exe")
    return exe


def install() -> None:
    """Windows 서비스 등록 (sc create 사용)."""
    exe_path = _get_exe_path()
    if not os.path.exists(exe_path):
        print(f"EXE 파일을 찾을 수 없습니다: {exe_path}")
        print("먼저 python build.py로 빌드하세요.")
        sys.exit(1)

    # NSSM (Non-Sucking Service Manager) 방식 — 일반 EXE를 서비스로 등록
    # NSSM이 없으면 sc create + 작업 스케줄러 폴백
    nssm = _find_nssm()

    if nssm:
        _install_with_nssm(nssm, exe_path)
    else:
        _install_with_task_scheduler(exe_path)


def _find_nssm() -> str | None:
    """NSSM 실행 파일 찾기."""
    for path in ["nssm.exe", os.path.join(os.path.dirname(__file__), "nssm.exe")]:
        try:
            result = subprocess.run([path, "version"], capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                return path
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
    return None


def _install_with_nssm(nssm: str, exe_path: str) -> None:
    """NSSM으로 서비스 등록."""
    cmds = [
        [nssm, "install", SERVICE_NAME, exe_path],
        [nssm, "set", SERVICE_NAME, "DisplayName", SERVICE_DISPLAY],
        [nssm, "set", SERVICE_NAME, "Description", SERVICE_DESCRIPTION],
        [nssm, "set", SERVICE_NAME, "Start", "SERVICE_AUTO_START"],
        [nssm, "set", SERVICE_NAME, "AppStdout", os.path.join(os.path.dirname(exe_path), "agent.log")],
        [nssm, "set", SERVICE_NAME, "AppStderr", os.path.join(os.path.dirname(exe_path), "agent_error.log")],
    ]
    for cmd in cmds:
        subprocess.run(cmd, check=True)
    print(f"서비스 '{SERVICE_NAME}' 등록 완료 (NSSM)")
    print(f"시작: python install_service.py start")


def _install_with_task_scheduler(exe_path: str) -> None:
    """작업 스케줄러로 부팅 시 자동 시작 등록 (NSSM 없을 때 폴백)."""
    cmd = [
        "schtasks", "/create",
        "/tn", SERVICE_NAME,
        "/tr", f'"{exe_path}"',
        "/sc", "onlogon",
        "/rl", "highest",
        "/f",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        print(f"작업 스케줄러에 '{SERVICE_NAME}' 등록 완료")
        print("로그온 시 자동 시작됩니다.")
    else:
        print(f"등록 실패: {result.stderr}")
        sys.exit(1)


def remove() -> None:
    """서비스 제거."""
    nssm = _find_nssm()
    if nssm:
        subprocess.run([nssm, "stop", SERVICE_NAME], capture_output=True)
        subprocess.run([nssm, "remove", SERVICE_NAME, "confirm"], check=True)
        print(f"서비스 '{SERVICE_NAME}' 제거 완료")
    else:
        subprocess.run(["schtasks", "/delete", "/tn", SERVICE_NAME, "/f"], check=True)
        print(f"작업 스케줄러 '{SERVICE_NAME}' 제거 완료")


def start() -> None:
    """서비스 시작."""
    nssm = _find_nssm()
    if nssm:
        subprocess.run([nssm, "start", SERVICE_NAME], check=True)
    else:
        subprocess.run(["schtasks", "/run", "/tn", SERVICE_NAME], check=True)
    print(f"'{SERVICE_NAME}' 시작됨")


def stop() -> None:
    """서비스 중지."""
    nssm = _find_nssm()
    if nssm:
        subprocess.run([nssm, "stop", SERVICE_NAME], check=True)
    else:
        # 프로세스 종료
        subprocess.run(["taskkill", "/im", "AADSAgent.exe", "/f"], capture_output=True)
    print(f"'{SERVICE_NAME}' 중지됨")


def main() -> None:
    """CLI 엔트리포인트."""
    if len(sys.argv) < 2:
        print("사용법: python install_service.py [install|remove|start|stop]")
        sys.exit(1)

    action = sys.argv[1].lower()
    actions = {"install": install, "remove": remove, "start": start, "stop": stop}

    fn = actions.get(action)
    if fn is None:
        print(f"알 수 없는 명령: {action}")
        print("사용 가능: install, remove, start, stop")
        sys.exit(1)

    fn()


if __name__ == "__main__":
    main()
