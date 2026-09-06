"""HANDOVER.md 상단에 2026-09-07 07:32 KST 핸드오버 요약을 삽입 (1MB 초과 파일 패치 우회용, 1회성)."""
HANDOVER = "/root/aads/aads-server/HANDOVER.md"
NOTE = "/root/aads/aads-server/docs/handover/20260907_0732_ohvis_phase012_infra_recovery.md"

with open(HANDOVER, "r", encoding="utf-8") as f:
    content = f.read()

marker = "# AADS HANDOVER\n\n"
entry = (
    "## 2026-09-07 07:32 KST - OHVIS Phase 0/1/2 인프라 복구·품질게이트·목표원장 main 반영 (origin/main 6bcfe216)\n"
    "- 상세: `docs/handover/20260907_0732_ohvis_phase012_infra_recovery.md`\n"
    "- 핵심: pipeline_runner_service DELEGATED→awaiting_approval 에스컬레이션, quality_gate.py 정적 품질게이트, "
    "agent_orchestrator L3 역할 DB 동적 로드, memory_recall CEO 통합 교차 주입, goal_planner + goals/milestones 목표 원장.\n"
    "- 주의: write/patch_remote_file은 활성 컨테이너(/app)에만 쓰므로 호스트 반영은 docker cp + git 커밋 필요.\n"
    "- 배포: 6bcfe216 blue/green 반영은 타 세션 배포(PID 4173589) 종료 후 재실행 필요 (노트 시점 미완료).\n\n"
)

if entry.splitlines()[0] in content:
    print("already present")
elif content.startswith(marker):
    content = marker + entry + content[len(marker):]
    with open(HANDOVER, "w", encoding="utf-8") as f:
        f.write(content)
    print("HANDOVER.md prepended OK")
else:
    with open(HANDOVER, "a", encoding="utf-8") as f:
        f.write("\n\n" + entry)
    print("HANDOVER.md appended (marker missing)")
