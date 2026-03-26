import pathlib

d = pathlib.Path('/tmp/.claude-relay/.claude/projects/-root-aads-aads-server/memory')
d.mkdir(parents=True, exist_ok=True)

(d / 'feedback_zero_downtime.md').write_text(
    '---\n'
    'name: zero-downtime-deploy\n'
    'description: AADS 대시보드 배포 시 무중단 배포 필수 - CEO 직접 지시\n'
    'type: feedback\n'
    '---\n\n'
    'AADS 대시보드(aads-dashboard) 빌드/배포 시 반드시 무중단 시스템을 적용해야 한다.\n\n'
    '**Why:** CEO가 실시간으로 채팅을 사용 중이므로, 배포 중 서비스 중단이 발생하면 CEO 업무에 직접적 영향.\n\n'
    '**How to apply:** docker compose up -d --build 대신, 새 컨테이너를 먼저 기동하고 헬스체크 통과 후 기존 컨테이너를 교체하는 방식(blue-green 또는 rolling update) 사용. 절대 기존 컨테이너를 먼저 중지하지 않는다.\n'
)

(d / 'feedback_user_verification.md').write_text(
    '---\n'
    'name: user-perspective-verification\n'
    'description: 기능 배포 후 반드시 사용자(CEO) 관점에서 외부 접근 검증 필수\n'
    'type: feedback\n'
    '---\n\n'
    '기능을 배포하고 URL을 CEO에게 전달하기 전에, 반드시 사용자 관점에서 외부 접근이 되는지 검증해야 한다.\n\n'
    '**Why:** 서버 내부 브라우저 도구로는 접근 가능하지만 외부에서는 404/403이 나는 경우가 실제로 발생함 (design-preview.html 사건). CEO에게 거짓 보고가 됨.\n\n'
    '**How to apply:** 파일 배포 후 반드시 curl -sI https://... 로 외부 HTTP 상태코드(200 OK) 확인 완료 후에만 CEO에게 URL 전달. 403/404 등 오류 시 원인(SELinux, 권한, nginx 설정 등) 해결 후 재검증.\n'
)

(d / 'MEMORY.md').write_text(
    '# MEMORY.md — AADS Project Memory Index\n\n'
    '- [Zero-downtime Deploy](feedback_zero_downtime.md) — 대시보드 배포 시 무중단 필수, CEO 직접 지시\n'
    '- [User Perspective Verification](feedback_user_verification.md) — 기능 배포 후 외부 접근 검증 필수 (거짓보고 방지)\n'
)

print('All 3 files written OK')
