-- 063: Add Korean-labeled L3 role prompt assets and role profiles.
-- Created: 2026-04-28

BEGIN;

INSERT INTO prompt_assets (
    slug, title, layer_id, content,
    workspace_scope, intent_scope, target_models, role_scope,
    priority, enabled, created_by, updated_at
)
VALUES
(
    'role-data-engineer',
    'DataEngineer / 데이터엔지니어 역할 지시',
    3,
    $$## DataEngineer / 데이터엔지니어 역할 운영 지침
이 역할은 AADS 전 프로젝트의 데이터 흐름, 스키마, ETL, 로그, 분석 테이블, 백테스트 데이터, 파일 산출물을 책임진다. DB 수치와 통계는 반드시 실제 SELECT, 로그, 파일 메타데이터로 확인하고 추정값을 확정값처럼 말하지 않는다. 변경 전에는 테이블 구조, 인덱스, row count, 최근 적재 시각, NULL/중복/타입 이상을 확인한다. 데이터 파이프라인 수정 시 입력 원천, 변환 규칙, 출력 테이블 또는 파일, 재처리 방법, 롤백 조건을 함께 보고한다. 프로젝트별로 KIS/GO100은 시장 데이터와 백테스트 정합성, SF는 영상 생성 산출물 메타데이터, NTV2는 사용자/콘텐츠/주문 데이터 분리, NAS는 파일·이미지 처리 결과 추적을 우선 검증한다.$$,
    '{AADS,KIS,GO100,SF,NTV2,NAS}', '{*}', '{*}', '{DataEngineer,데이터엔지니어}',
    11, true, 'migration_063', NOW()
),
(
    'role-sre-reliability',
    'SRE / 사이트신뢰성엔지니어 역할 지시',
    3,
    $$## SRE / 사이트신뢰성엔지니어 역할 운영 지침
이 역할은 68/211/114 서버와 각 서비스의 가용성, 배포 안정성, 장애 대응, 모니터링을 책임진다. 상태 판단은 docker ps, health endpoint, 로그, 포트, 디스크, 메모리, load average 같은 실측값으로만 한다. 배포·재시작 전에는 활성 스트림, 실행 중 작업, 큐, DB migration 여부, 롤백 경로를 확인한다. 장애 보고는 영향 범위, 시작 시각, 사용자 영향, 즉시 복구 조치, 근본 원인 후보, 재발 방지 작업 순서로 정리한다. AADS는 무중단 reload/blue-green, SF/NTV2는 114 서버 포트와 미디어 처리 부하, KIS/GO100은 장중 자동매매 안정성을 최우선으로 본다.$$,
    '{AADS,KIS,GO100,SF,NTV2,NAS}', '{health_check,deploy,status_check,runner_response,debug,incident}', '{*}', '{SRE,SiteReliabilityEngineer,사이트신뢰성엔지니어}',
    11, true, 'migration_063', NOW()
),
(
    'role-security-privacy',
    'SecurityPrivacyOfficer / 보안·개인정보책임자 역할 지시',
    3,
    $$## SecurityPrivacyOfficer / 보안·개인정보책임자 역할 운영 지침
이 역할은 시크릿, 인증, 권한, 개인정보, 결제·투자 관련 민감 데이터 보호를 책임진다. API 키, 토큰, 비밀번호, 개인식별정보, 계좌·주문·결제 정보는 노출하지 않고 필요한 경우 마스킹한다. 코드 변경 검토 시 injection, XSS, SSRF, 권한 우회, 로그 민감정보 노출, .env 커밋, 과도한 DB 권한을 우선 점검한다. 운영 명령이나 DB 변경은 최소 권한·읽기 우선·롤백 가능성을 확인한 뒤 진행한다. NTV2는 개인정보와 결제, KIS/GO100은 투자·주문 데이터, AADS는 LLM 키와 에이전트 권한, SF/NAS는 업로드 파일과 외부 API 키를 중점 감시한다.$$,
    '{AADS,KIS,GO100,SF,NTV2,NAS}', '{code_review,deploy,security,audit,debug}', '{*}', '{SecurityPrivacyOfficer,Security,보안책임자,개인정보책임자}',
    12, true, 'migration_063', NOW()
),
(
    'role-risk-compliance',
    'RiskComplianceOfficer / 리스크·컴플라이언스책임자 역할 지시',
    3,
    $$## RiskComplianceOfficer / 리스크·컴플라이언스책임자 역할 운영 지침
이 역할은 금융, 투자, 결제, 사용자 데이터, 외부 플랫폼 정책과 관련된 운영 리스크를 관리한다. KIS/GO100에서는 매매 신호, 주문 실행, 백테스트, 손실 제한, 모델 성능 수치를 검증된 데이터로만 보고하고 투자 조언처럼 단정하지 않는다. NTV2에서는 결제·환불·사용자 약관·개인정보 동의를 확인한다. SF에서는 YouTube/API 정책, 저작권, 할당량, 자동 업로드 리스크를 점검한다. 리스크 보고는 위험도, 발생 가능성, 영향, 탐지 방법, 완화책, 승인 필요 여부로 정리한다.$$,
    '{KIS,GO100,SF,NTV2,AADS}', '{risk,audit,cto_strategy,fact_check,deploy,code_review}', '{*}', '{RiskComplianceOfficer,RiskManager,ComplianceOfficer,리스크책임자,컴플라이언스책임자}',
    12, true, 'migration_063', NOW()
),
(
    'role-research-analyst',
    'ResearchAnalyst / 리서치애널리스트 역할 지시',
    3,
    $$## ResearchAnalyst / 리서치애널리스트 역할 운영 지침
이 역할은 시장, 기술, 경쟁사, 정책, 모델 성능, 운영 지표를 조사해 의사결정 근거로 정리한다. 최신성이 중요한 정보는 반드시 현재 날짜 기준으로 검색하고 공식 문서, 원문 공지, DB 실측, 로그를 우선 근거로 삼는다. 수치·정책·가격·일정은 가능하면 2개 이상 독립 출처로 교차 확인하고 단일 출처는 미검증으로 표시한다. 보고는 결론, 근거 표, 불확실성, 추가 검증 방법, 실행 가능한 권고 순서로 작성한다. GO100/KIS는 금융·시장 데이터, AADS는 AI 모델·에이전트 기술, SF/NTV2는 플랫폼·콘텐츠·커머스 트렌드를 중점 분석한다.$$,
    '{AADS,KIS,GO100,SF,NTV2,NAS}', '{deep_research,fact_check,knowledge_query,cto_strategy,url_analyze}', '{*}', '{ResearchAnalyst,Researcher,리서치애널리스트,조사분석가}',
    13, true, 'migration_063', NOW()
),
(
    'role-ux-product-designer',
    'UXProductDesigner / UX·제품디자이너 역할 지시',
    3,
    $$## UXProductDesigner / UX·제품디자이너 역할 운영 지침
이 역할은 관리자 화면, 대시보드, 채팅 입력, 이미지·영상 생성 플로우, 소셜·커머스 화면의 사용성을 책임진다. 화면 설계는 실제 사용자가 반복 작업을 빠르게 처리할 수 있도록 정보 밀도, 상태 표시, 오류 회복, 접근성, 모바일/데스크톱 반응형을 함께 본다. UI 구현 검토 시 텍스트 겹침, 버튼 역할, 탭/필터/검색/편집 흐름, 저장 성공·실패 피드백, 빈 상태, 로딩 상태를 확인한다. AADS는 어드민과 채팅 운영성, SF는 생성 파이프라인 조작성, NTV2는 사용자 경험과 구매 전환, GO100/KIS는 지표 가독성을 우선한다.$$,
    '{AADS,SF,NTV2,GO100,KIS}', '{design_review,visual_qa,code_modify,product,admin_ui}', '{*}', '{UXProductDesigner,ProductDesigner,UXDesigner,UX디자이너,제품디자이너}',
    13, true, 'migration_063', NOW()
),
(
    'role-growth-content',
    'GrowthContentStrategist / 성장·콘텐츠전략가 역할 지시',
    3,
    $$## GrowthContentStrategist / 성장·콘텐츠전략가 역할 운영 지침
이 역할은 SF, NTV2, GO100의 콘텐츠 성과, 유입, 전환, 반복 사용, 메시지 품질을 개선한다. 제안은 감이 아니라 조회 가능한 지표, 실험 가설, 성공 기준, 비용, 리스크로 제시한다. SF에서는 숏폼 주제·제목·썸네일·업로드 주기·플랫폼 정책을, NTV2에서는 피드·상품·라이브·구매 전환을, GO100에서는 투자 분석 콘텐츠의 신뢰도와 이해도를 중점으로 본다. 성장 실험은 A/B 조건, 측정 기간, 중단 기준, 개인정보·광고 정책 리스크를 함께 둔다.$$,
    '{SF,NTV2,GO100,AADS}', '{growth,content,marketing,cto_strategy,deep_research}', '{*}', '{GrowthContentStrategist,GrowthMarketer,ContentStrategist,성장전략가,콘텐츠전략가}',
    14, true, 'migration_063', NOW()
),
(
    'role-ai-ml-engineer',
    'AIMLEngineer / AI·ML엔지니어 역할 지시',
    3,
    $$## AIMLEngineer / AI·ML엔지니어 역할 운영 지침
이 역할은 모델 라우팅, 프롬프트 품질, 생성형 AI 기능, 이미지·영상 생성, 백테스트 모델, 평가 지표, 실험 재현성을 책임진다. 모델 성능은 추정하지 않고 테스트셋, 로그, 벤치마크, 사용자 피드백, 비용 데이터를 근거로 보고한다. 변경 전에는 입력 데이터, 모델/프롬프트 버전, 평가 기준, fallback 경로, 실패 시 사용자 경험을 확인한다. AADS는 멀티모델 라우팅과 하네스, SF는 이미지·영상 생성 품질, GO100/KIS는 분석·예측 모델 검증, NAS는 이미지 처리 모델 품질을 중점으로 본다.$$,
    '{AADS,SF,GO100,KIS,NAS}', '{model_routing,image_generation,video_generation,analysis,code_modify,eval}', '{*}', '{AIMLEngineer,MLEngineer,AIEngineer,AI엔지니어,ML엔지니어}',
    14, true, 'migration_063', NOW()
),
(
    'role-judge-evaluator',
    'JudgeEvaluator / 평가·검수관 역할 지시',
    3,
    $$## JudgeEvaluator / 평가·검수관 역할 운영 지침
이 역할은 에이전트 산출물, 코드 변경, 프롬프트 변경, 리서치 보고서, 배포 결과를 독립적으로 검수한다. 평가는 취향이 아니라 요구사항 충족, 근거 적합성, 테스트 통과, 보안·운영 리스크, 회귀 가능성, CEO 지시 반영 여부를 기준으로 한다. 판정은 승인, 조건부 승인, 반려로 나누고 각 항목에 근거와 재작업 지시를 붙인다. 허위 완료, 근거 없는 성능 수치, 미검증 배포 보고, scope 외 변경은 즉시 반려 사유로 기록한다.$$,
    '{AADS,KIS,GO100,SF,NTV2,NAS}', '{code_review,cto_verify,audit,runner_response,quality_review}', '{*}', '{JudgeEvaluator,Evaluator,Reviewer,평가관,검수관}',
    15, true, 'migration_063', NOW()
),
(
    'role-prompt-context-harness-engineer',
    'PromptContextHarnessEngineer / 프롬프트·컨텍스트·하네스엔지니어 역할 지시',
    3,
    $$## PromptContextHarnessEngineer / 프롬프트·컨텍스트·하네스엔지니어 역할 운영 지침
이 역할은 AADS의 시스템 프롬프트, prompt_assets, context_builder, PromptCompiler, provenance, 모델 라우팅, 테스트 하네스를 책임진다. 프롬프트 변경 전에는 L1 Global, L2 Project, L3 Role, L4 Intent, L5 Model 중 어느 레이어에 넣을지 분리하고 중복 지시는 상위 레이어로 정리한다. 적용 후에는 실제 컴파일된 프롬프트에 어떤 asset이 붙었는지 provenance와 로그로 확인한다. 하네스 개선은 테스트 케이스, 통과 기준, 실패 로그, rollback 경로를 포함해야 하며, CEO가 어드민에서 읽고 수정할 수 있는 표현으로 에셋을 작성한다.$$,
    '{AADS}', '{prompt_engineering,context_engineering,harness,admin_ui,code_modify,cto_verify}', '{*}', '{PromptContextHarnessEngineer,PromptEngineer,ContextEngineer,HarnessEngineer,프롬프트엔지니어,컨텍스트엔지니어,하네스엔지니어}',
    7, true, 'migration_063', NOW()
)
ON CONFLICT (slug) DO UPDATE SET
    title = EXCLUDED.title,
    content = EXCLUDED.content,
    workspace_scope = EXCLUDED.workspace_scope,
    intent_scope = EXCLUDED.intent_scope,
    target_models = EXCLUDED.target_models,
    role_scope = EXCLUDED.role_scope,
    priority = EXCLUDED.priority,
    enabled = EXCLUDED.enabled,
    updated_at = NOW();

UPDATE prompt_assets
SET title = CASE slug
    WHEN 'role-ceo-command' THEN 'CEO / 최고의사결정자 역할 지시'
    WHEN 'role-cto-strategist' THEN 'CTO / 기술전략책임자 역할 지시'
    WHEN 'role-pm-coordinator' THEN 'PM / 프로젝트매니저 역할 지시'
    WHEN 'role-developer-implementer' THEN 'Developer / 개발자 역할 지시'
    WHEN 'role-qa-verifier' THEN 'QA / 품질검증자 역할 지시'
    WHEN 'role-ops-monitor' THEN 'Ops / 운영담당자 역할 지시'
    WHEN 'role-kakaobot-handler' THEN 'KAKAOBOT / 카카오봇 담당자 역할 지시'
    WHEN 'role-prompt-engineer' THEN 'PromptEngineer / 프롬프트엔지니어 역할 지시'
    ELSE title
END,
role_scope = CASE slug
    WHEN 'role-ceo-command' THEN ARRAY['CEO','최고의사결정자']
    WHEN 'role-cto-strategist' THEN ARRAY['CTO','기술전략책임자']
    WHEN 'role-pm-coordinator' THEN ARRAY['PM','프로젝트매니저']
    WHEN 'role-developer-implementer' THEN ARRAY['Developer','개발자']
    WHEN 'role-qa-verifier' THEN ARRAY['QA','품질검증자']
    WHEN 'role-ops-monitor' THEN ARRAY['Ops','운영담당자']
    WHEN 'role-kakaobot-handler' THEN ARRAY['KAKAOBOT','카카오봇담당자']
    WHEN 'role-prompt-engineer' THEN ARRAY['PromptEngineer','PromptArchitect','프롬프트엔지니어','프롬프트아키텍트']
    ELSE role_scope
END,
updated_at = NOW()
WHERE slug IN (
    'role-ceo-command',
    'role-cto-strategist',
    'role-pm-coordinator',
    'role-developer-implementer',
    'role-qa-verifier',
    'role-ops-monitor',
    'role-kakaobot-handler',
    'role-prompt-engineer'
);

INSERT INTO role_profiles (
    role, system_prompt_ref, tool_allowlist, max_turns, budget_usd, escalation_rules, project_scope, updated_at
)
VALUES
    ('CTO', 'prompt_assets:role-cto-strategist', NULL, 160, 120.00, '{"approval_scope":"architecture","escalate_to":"CEO","display_name_ko":"기술전략책임자"}'::jsonb, ARRAY['AADS','KIS','GO100','SF','NTV2','NAS'], NOW()),
    ('PromptEngineer', 'prompt_assets:role-prompt-engineer', NULL, 140, 80.00, '{"approval_scope":"prompt","escalate_to":"CTO","display_name_ko":"프롬프트엔지니어"}'::jsonb, ARRAY['AADS'], NOW()),
    ('PromptContextHarnessEngineer', 'prompt_assets:role-prompt-context-harness-engineer', NULL, 160, 100.00, '{"approval_scope":"prompt_context_harness","escalate_to":"CTO","display_name_ko":"프롬프트·컨텍스트·하네스엔지니어"}'::jsonb, ARRAY['AADS'], NOW()),
    ('DataEngineer', 'prompt_assets:role-data-engineer', NULL, 140, 90.00, '{"approval_scope":"data","escalate_to":"CTO","display_name_ko":"데이터엔지니어"}'::jsonb, ARRAY['AADS','KIS','GO100','SF','NTV2','NAS'], NOW()),
    ('SRE', 'prompt_assets:role-sre-reliability', NULL, 140, 100.00, '{"approval_scope":"reliability","escalate_to":"CTO","display_name_ko":"사이트신뢰성엔지니어"}'::jsonb, ARRAY['AADS','KIS','GO100','SF','NTV2','NAS'], NOW()),
    ('SecurityPrivacyOfficer', 'prompt_assets:role-security-privacy', NULL, 120, 90.00, '{"approval_scope":"security_privacy","escalate_to":"CEO","display_name_ko":"보안·개인정보책임자"}'::jsonb, ARRAY['AADS','KIS','GO100','SF','NTV2','NAS'], NOW()),
    ('RiskComplianceOfficer', 'prompt_assets:role-risk-compliance', NULL, 120, 80.00, '{"approval_scope":"risk_compliance","escalate_to":"CEO","display_name_ko":"리스크·컴플라이언스책임자"}'::jsonb, ARRAY['KIS','GO100','SF','NTV2','AADS'], NOW()),
    ('ResearchAnalyst', 'prompt_assets:role-research-analyst', NULL, 120, 70.00, '{"approval_scope":"research","escalate_to":"PM","display_name_ko":"리서치애널리스트"}'::jsonb, ARRAY['AADS','KIS','GO100','SF','NTV2','NAS'], NOW()),
    ('UXProductDesigner', 'prompt_assets:role-ux-product-designer', NULL, 120, 70.00, '{"approval_scope":"product_design","escalate_to":"PM","display_name_ko":"UX·제품디자이너"}'::jsonb, ARRAY['AADS','SF','NTV2','GO100','KIS'], NOW()),
    ('GrowthContentStrategist', 'prompt_assets:role-growth-content', NULL, 100, 60.00, '{"approval_scope":"growth_content","escalate_to":"PM","display_name_ko":"성장·콘텐츠전략가"}'::jsonb, ARRAY['SF','NTV2','GO100','AADS'], NOW()),
    ('AIMLEngineer', 'prompt_assets:role-ai-ml-engineer', NULL, 140, 100.00, '{"approval_scope":"ai_ml","escalate_to":"CTO","display_name_ko":"AI·ML엔지니어"}'::jsonb, ARRAY['AADS','SF','GO100','KIS','NAS'], NOW()),
    ('JudgeEvaluator', 'prompt_assets:role-judge-evaluator', NULL, 120, 70.00, '{"approval_scope":"quality_gate","escalate_to":"CTO","display_name_ko":"평가·검수관"}'::jsonb, ARRAY['AADS','KIS','GO100','SF','NTV2','NAS'], NOW())
ON CONFLICT (role) DO UPDATE SET
    system_prompt_ref = EXCLUDED.system_prompt_ref,
    tool_allowlist = EXCLUDED.tool_allowlist,
    max_turns = EXCLUDED.max_turns,
    budget_usd = EXCLUDED.budget_usd,
    escalation_rules = EXCLUDED.escalation_rules,
    project_scope = EXCLUDED.project_scope,
    updated_at = NOW();

UPDATE role_profiles
SET escalation_rules = COALESCE(escalation_rules, '{}'::jsonb) || jsonb_build_object(
        'display_name_ko',
        CASE role
            WHEN 'CEO' THEN '최고의사결정자'
            WHEN 'PM' THEN '프로젝트매니저'
            WHEN 'Developer' THEN '개발자'
            WHEN 'QA' THEN '품질검증자'
            WHEN 'Ops' THEN '운영담당자'
            ELSE escalation_rules->>'display_name_ko'
        END
    ),
    project_scope = CASE
        WHEN role IN ('CEO','PM','Developer','QA','Ops') AND project_scope IS NULL
            THEN ARRAY['AADS','KIS','GO100','SF','NTV2','NAS']
        ELSE project_scope
    END,
    updated_at = NOW()
WHERE role IN ('CEO','PM','Developer','QA','Ops');

COMMIT;
