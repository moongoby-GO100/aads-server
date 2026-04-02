INSERT INTO ceo_agenda (project, title, summary, status, priority, tags, created_by)
VALUES (
  'AADS',
  '시스템 프롬프트 최신기술 최적화',
  '최신 프롬프트 엔지니어링 기법(CoT, ReAct, Tool-use 최적화 등) 적용하여 AADS 시스템 프롬프트 전면 개선. 분석 보고서: docs/SYSTEM_PROMPT_OPTIMIZATION_REPORT.md 작성 완료(GitHub). CEO 검토 후 우선순위 및 적용 일정 확정 필요.',
  '보류',
  'P2',
  ARRAY[''시스템프롬프트'',''최적화'',''AI기술''],
  'CEO'
)
RETURNING id, title, status, priority, created_at;
