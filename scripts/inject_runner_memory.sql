INSERT INTO memory_facts (id, workspace_id, project, category, subject, detail, confidence, tags, created_at, updated_at)
SELECT gen_random_uuid(), w.id,
  CASE w.name
    WHEN '[AADS] 프로젝트 매니저' THEN 'AADS'
    WHEN '[KIS] 자동매매' THEN 'KIS'
    WHEN '[GO100] 빡억이' THEN 'GO100'
    WHEN '[SF] ShortFlow' THEN 'SF'
    WHEN '[NTV2] NewTalk V2' THEN 'NTV2'
    WHEN '[NAS] Image' THEN 'NAS'
    WHEN '[CEO] 통합지시' THEN 'AADS'
    WHEN '[COM] 마케팅팀' THEN 'AADS'
  END,
  'ceo_instruction',
  'Pipeline Runner v2.1 작업방식 (전서버 통일)',
  E'[2026-03-18 CEO 확정] Runner 작업방식 v2.1:\n1. Claude Code는 코드 수정만 수행 (빌드/배포 절대 금지)\n2. 금지 명령: docker compose, docker build, supervisorctl restart, npm run build, 서비스 재시작\n3. 작업 완료 후 승인 대기 (코드만 수정된 상태, 서버 무변화)\n4. CEO/AI 승인 후에만: git commit → push → 빌드 → 배포 (deploy_job이 자동 수행)\n5. 거부 시: git checkout으로 코드 원복 (서버 영향 없음)\n6. 핵심 원칙: 승인 전에는 서버에 아무 변화 없음\n7. instruction 작성 시 빌드/배포 문구 포함하지 말 것\n8. Runner 가드레일이 모든 instruction 앞에 금지 규칙을 자동 주입함\n9. 68서버(AADS)/211서버(KIS,GO100)/114서버(SF,NTV2) 전서버 동일 적용',
  0.95,
  ARRAY['runner','process','v2.1','deploy','guard','전서버'],
  NOW(), NOW()
FROM chat_workspaces w
WHERE w.name IN ('[AADS] 프로젝트 매니저','[CEO] 통합지시','[COM] 마케팅팀','[GO100] 빡억이','[KIS] 자동매매','[NAS] Image','[NTV2] NewTalk V2','[SF] ShortFlow');
