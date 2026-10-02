-- L1 문서 저장 규칙(R-DOC) 프롬프트 자산 초안.
-- enabled=false 로만 생성한다 (CEO 활성화 결정 전까지 어떤 세션에도 주입되지 않는다).
-- slug 는 UNIQUE 제약(058)이므로 ON CONFLICT (slug) DO NOTHING 으로 멱등 처리하고,
-- 기존 행은 수정하지 않는다. 롤백: 20261003_l1_doc_storage_rule_draft_rollback.sql
BEGIN;

INSERT INTO prompt_assets (
    slug, title, layer_id, content,
    workspace_scope, intent_scope, target_models, role_scope,
    priority, enabled, created_by
)
VALUES (
    'l1-doc-storage-rule',
    'L1 Global - Document Storage Rule / 문서 저장 규칙 (R-DOC) [초안]',
    1,
    $$## L1 Global / 문서 저장 규칙 (R-DOC)
세션·러너·서브에이전트가 남기는 계획·PRD·설계·보고·결과 문서에 적용한다. 핸드오버는 R-HANDOVER-DB 가 원본이므로 여기서 다루지 않는다.

1. 저장 위치(프로젝트 저장소 기준): plan→docs/plans, prd→docs/prd, design·architecture→docs/design, report·결과→docs/reports, spec·tasks→docs/specs/<슬라이스>/. 저장소 루트 reports/ 와 uploads/chat 에 신규 정본 문서를 두지 않는다.
2. 파일명은 YYYYMMDD_{PROJECT}_{제목}.md 하나로 통일한다. 활성화 시 flow-rules.md 파일명 절을 이 규칙으로 교체한다.
3. 정본 등록 의무: 문서를 만들면 등록 도구가 있으면 도구로, 없으면 POST /api/v1/projects/{PROJECT}/documents 로 등록한다. kind 는 plan|prd|spec|design|architecture|contract|tasks|report|reference 중 하나를 지정하고, 목표가 있으면 goal_id 를 넣는다. 완료 보고에 document_key 를 적는다. 등록하지 않았으면 "정본 미등록"으로 분리 보고한다.
4. 갱신은 새 파일이 아니라 같은 document_key 의 새 판본으로 올린다.
5. 금지: 날짜만 바꾼 사본 파일 만들기. 채팅 본문에만 남기고 "문서 저장 완료"라고 보고하기.

왜: 최근 7일 신규 문서 17건 중 정본 등록 1건, FLOW 파일명 준수 2건뿐이었다.$$,
    ARRAY['*'], ARRAY['*'], ARRAY['*'], ARRAY['*'],
    90, false, 'runner_20261003_l1_doc_storage_rule_draft'
)
ON CONFLICT (slug) DO NOTHING;

COMMIT;
