# Bridge 규칙
<!-- paths: **/bridge*, **/genspark* -->
- 메시지 중복방지 3단계: SKIP_PATTERNS → SHA256 해시 → seen_tasks
- [BRIDGE-SENT] 태그 필수 삽입
- 컨텍스트 압축 감지 시 session_restore_prompt 자동 재주입 (AADS-115)
- GenSpark 웹훅 미지원 → 폴링+ACK 패턴 (L-005)
