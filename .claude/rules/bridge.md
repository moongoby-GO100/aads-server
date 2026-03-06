# Bridge 규칙
- 중복 방지 3중: SKIP_PATTERNS + [BRIDGE-SENT] + SHA256 해시
- 외부 서비스 연동 시 ACK+Retry 패턴 적용 (L-005, L-008)
- seen_tasks.json에 실패 작업 잔류 주의 → 교차검증 체크 8이 자동 해제
- 컨텍스트 압축 감지 시 session_restore_prompt 자동 재주입 (AADS-115)
