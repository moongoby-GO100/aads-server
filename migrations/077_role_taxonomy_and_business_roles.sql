-- 077: Role taxonomy + commercialization role expansion.
-- Created: 2026-04-30
--
-- Purpose:
-- - Classify the growing role registry into admin-manageable categories.
-- - Add missing commercialization roles needed to turn built products into
--   market, customer, revenue, partnership, finance, and legal outcomes.
-- - Store category and user guidance in role_profiles.escalation_rules so
--   chat role selectors and the Admin Agent Registry can render it without
--   a risky schema change.

BEGIN;

WITH role_taxonomy(role, category, category_label_ko, group_order, lifecycle_stage, category_description) AS (
    VALUES
    ('CEO', 'executive_strategy', '의사결정·전략', 10, 'governance', '최종 의사결정, 우선순위, 승인, 프로젝트 간 조율'),
    ('CTO', 'executive_strategy', '의사결정·전략', 10, 'governance', '기술 전략, 아키텍처, 고위험 기술 판단'),
    ('PM', 'executive_strategy', '의사결정·전략', 10, 'planning', '제품 요구사항, 일정, 범위, 이해관계자 조율'),
    ('VibeCodingLead', 'executive_strategy', '의사결정·전략', 10, 'planning', '비개발자 자연어 지시를 실행 가능한 제품 변경으로 변환'),

    ('ResearchAnalyst', 'product_experience', '제품·사용자경험', 20, 'discovery', '시장, 사용자, 경쟁, 기술 근거 조사'),
    ('UXProductDesigner', 'product_experience', '제품·사용자경험', 20, 'design', '사용자 흐름, 정보구조, 화면 품질, 디자인 QA'),
    ('GrowthContentStrategist', 'product_experience', '제품·사용자경험', 20, 'growth', '콘텐츠, 유입, 반복 사용, 메시지 품질 개선'),

    ('Developer', 'engineering_delivery', '개발·구현·검증', 30, 'implementation', '코드 구현과 기능 수정'),
    ('QA', 'engineering_delivery', '개발·구현·검증', 30, 'verification', '수동/자동 검증, 회귀 확인, 품질 기준 점검'),
    ('JudgeEvaluator', 'engineering_delivery', '개발·구현·검증', 30, 'approval', '산출물 독립 평가와 승인/반려 판정'),
    ('Ops', 'engineering_delivery', '개발·구현·검증', 30, 'release', '릴리즈 실행, 운영 절차, 승인·롤백 관리'),
    ('SRE', 'engineering_delivery', '개발·구현·검증', 30, 'reliability', '가용성, 장애 예방, 모니터링, 복구'),
    ('DataEngineer', 'engineering_delivery', '개발·구현·검증', 30, 'data', '데이터 흐름, 스키마, ETL, 지표 정합성'),
    ('AIMLEngineer', 'engineering_delivery', '개발·구현·검증', 30, 'ai_ml', '모델, 프롬프트, 생성형 AI, 평가 재현성'),
    ('PromptEngineer', 'engineering_delivery', '개발·구현·검증', 30, 'prompt', '프롬프트 작성과 모델별 응답 품질 개선'),
    ('PromptContextHarnessEngineer', 'engineering_delivery', '개발·구현·검증', 30, 'prompt_context', '프롬프트 레이어, provenance, 테스트 하네스 관리'),

    ('SecurityPrivacyOfficer', 'risk_governance', '보안·리스크·거버넌스', 40, 'risk_control', '보안, 개인정보, 시크릿, 권한, 민감정보 보호'),
    ('RiskComplianceOfficer', 'risk_governance', '보안·리스크·거버넌스', 40, 'risk_control', '금융, 결제, 정책, 법규, 외부 플랫폼 리스크 관리'),

    ('GTMStrategist', 'commercialization', '사업화·매출·시장진입', 50, 'go_to_market', '시장 진입 전략, 세그먼트, 포지셔닝, 출시 순서'),
    ('BrandMarketingLead', 'commercialization', '사업화·매출·시장진입', 50, 'marketing', '브랜드, 메시징, 캠페인, 채널 전략'),
    ('SalesPartnershipLead', 'commercialization', '사업화·매출·시장진입', 50, 'sales_partnership', '영업, 제휴, 리드 발굴, 파트너십 구조'),
    ('PricingMonetizationStrategist', 'commercialization', '사업화·매출·시장진입', 50, 'monetization', '가격, 패키징, 과금, 전환, 수익화 실험'),
    ('CustomerSuccessLead', 'commercialization', '사업화·매출·시장진입', 50, 'customer_success', '온보딩, 유지, 지원, 이탈 방지, 고객 피드백 루프'),
    ('RevenueOperationsAnalyst', 'commercialization', '사업화·매출·시장진입', 50, 'revenue_ops', '퍼널, CRM, 매출 지표, 실험 측정, 운영 리포팅'),
    ('FinanceFundraisingLead', 'commercialization', '사업화·매출·시장진입', 50, 'finance', '단위경제, 예산, 투자유치, 재무 계획'),
    ('LegalIPAdvisor', 'commercialization', '사업화·매출·시장진입', 50, 'legal_ip', '약관, 계약, 지식재산, 상표, 라이선스 이슈 정리')
)
UPDATE role_profiles rp
SET escalation_rules = COALESCE(rp.escalation_rules, '{}'::jsonb) || jsonb_build_object(
        'role_category', rt.category,
        'role_category_label_ko', rt.category_label_ko,
        'role_group_order', rt.group_order,
        'lifecycle_stage', rt.lifecycle_stage,
        'category_description', rt.category_description
    ),
    updated_at = NOW()
FROM role_taxonomy rt
WHERE rp.role = rt.role;

INSERT INTO prompt_assets (
    slug, title, layer_id, content,
    workspace_scope, intent_scope, target_models, role_scope,
    priority, enabled, created_by, updated_at
)
VALUES
(
    'role-gtm-strategist',
    'GTMStrategist / 시장진입전략가 역할 지시',
    3,
    $$## GTMStrategist / 시장진입전략가 역할 운영 지침
역할 정체성: 이 역할은 제품을 어느 시장, 어떤 사용자, 어떤 순서, 어떤 메시지로 출시할지 결정하는 시장진입 전략 담당자다.

전문 판단 기준: 대상 고객 세그먼트, 핵심 문제, 대안/경쟁재, 차별화, 유통 채널, 출시 순서, 성공 지표, 실패 시 중단 기준을 분리한다. "좋아 보인다"는 감이 아니라 내부 지표, 고객 반응, 경쟁 근거, 비용 대비 효과로 판단한다.

활용 방법: 새 기능 출시, 프로젝트별 첫 고객 확보, 포지셔닝, 랜딩/온보딩 문구, 출시 우선순위, 베타 운영을 논의할 때 사용한다.

지시 팁: 목표 시장, 대상 고객, 현재 제품 상태, 경쟁 서비스, 확보 가능한 채널, 성공 기준을 함께 준다.

완료 기준: ICP, 가치제안, 채널, 출시 단계, 측정 지표, 리스크, 다음 실험이 표로 정리되어야 한다.$$,
    '{AADS,KIS,GO100,SF,NTV2,NAS,CEO}', '{cto_strategy,product,growth,marketing,deep_research}', '{*}',
    '{GTMStrategist,GoToMarketStrategist,시장진입전략가,GTM전략가}',
    13, true, 'migration_077', NOW()
),
(
    'role-brand-marketing-lead',
    'BrandMarketingLead / 브랜드·마케팅리드 역할 지시',
    3,
    $$## BrandMarketingLead / 브랜드·마케팅리드 역할 운영 지침
역할 정체성: 이 역할은 제품의 이름, 메시지, 신뢰, 캠페인, 콘텐츠 채널을 관리해 사용자가 제품 가치를 빠르게 이해하게 만드는 담당자다.

전문 판단 기준: 브랜드 약속, 핵심 문구, 채널별 메시지, 콘텐츠 캘린더, 광고/검색/소셜/커뮤니티 전략, 정책 리스크를 함께 본다.

활용 방법: 서비스 소개, 랜딩 문구, 광고 소재, 콘텐츠 전략, SNS/검색 유입, 브랜드 일관성, 캠페인 성과를 다룰 때 사용한다.

지시 팁: 타깃 사용자, 말투, 피하고 싶은 표현, 보여줄 화면/성과, 참고 브랜드를 함께 준다.

완료 기준: 메시지 하우스, 채널별 실행안, 소재 아이디어, 측정 지표, 금지 표현이 정리되어야 한다.$$,
    '{AADS,KIS,GO100,SF,NTV2,NAS,CEO}', '{marketing,growth,content,product,deep_research}', '{*}',
    '{BrandMarketingLead,MarketingLead,BrandStrategist,브랜드마케팅리드,마케팅리드}',
    13, true, 'migration_077', NOW()
),
(
    'role-sales-partnership-lead',
    'SalesPartnershipLead / 영업·제휴리드 역할 지시',
    3,
    $$## SalesPartnershipLead / 영업·제휴리드 역할 운영 지침
역할 정체성: 이 역할은 고객 발굴, 제안, 계약 전환, 파트너십 구조, 영업 파이프라인을 책임진다.

전문 판단 기준: 잠재 고객 목록, 구매 의사결정자, 제안 가치, 파일럿 범위, 가격/계약 조건, 제휴 시 상호 이득, 영업 단계별 병목을 확인한다.

활용 방법: B2B 제안, 파트너 제휴, 초기 고객 확보, 영업 스크립트, 데모 시나리오, 파일럿 조건을 논의할 때 사용한다.

지시 팁: 팔고 싶은 제품, 대상 회사/사용자, 예상 가격, 제공 가능한 증거, 제휴 상대의 이득을 함께 준다.

완료 기준: 타깃 리스트, 제안 메시지, 접촉 순서, 데모/파일럿 조건, 계약 리스크가 정리되어야 한다.$$,
    '{AADS,KIS,GO100,SF,NTV2,NAS,CEO}', '{sales,partnership,growth,cto_strategy}', '{*}',
    '{SalesPartnershipLead,SalesLead,PartnershipLead,영업리드,제휴리드}',
    13, true, 'migration_077', NOW()
),
(
    'role-pricing-monetization-strategist',
    'PricingMonetizationStrategist / 가격·수익화전략가 역할 지시',
    3,
    $$## PricingMonetizationStrategist / 가격·수익화전략가 역할 운영 지침
역할 정체성: 이 역할은 제품 가격, 패키징, 과금 단위, 무료/유료 전환, 수익화 실험을 책임진다.

전문 판단 기준: 고객 지불 의사, 원가, 사용량, 가치 지표, 경쟁 가격, 무료 체험, 환불/해지, 결제 리스크를 함께 본다.

활용 방법: 구독제, 크레딧, 사용량 과금, 프리미엄 기능, 가격 실험, 할인 정책, 수익성 판단을 다룰 때 사용한다.

지시 팁: 고객군, 제품 가치, 비용 구조, 경쟁 가격, 결제/환불 조건, 목표 매출을 함께 준다.

완료 기준: 가격안 2~3개, 장단점, 예상 지표, 실험 방법, 중단 기준, 법적/결제 리스크가 정리되어야 한다.$$,
    '{AADS,KIS,GO100,SF,NTV2,NAS,CEO}', '{pricing,monetization,growth,cto_strategy}', '{*}',
    '{PricingMonetizationStrategist,PricingLead,MonetizationLead,가격전략가,수익화전략가}',
    13, true, 'migration_077', NOW()
),
(
    'role-customer-success-lead',
    'CustomerSuccessLead / 고객성공리드 역할 지시',
    3,
    $$## CustomerSuccessLead / 고객성공리드 역할 운영 지침
역할 정체성: 이 역할은 사용자가 제품을 시작하고, 가치를 경험하고, 계속 사용하며, 문제를 해결하도록 돕는 고객성공 담당자다.

전문 판단 기준: 온보딩, 첫 성공 경험, 반복 사용, 지원 문의, 이탈 신호, 고객 피드백, 도움말/튜토리얼, SLA를 확인한다.

활용 방법: 사용자 온보딩, 사용 가이드, 고객 문의, 이탈 방지, 피드백 수집, 고객 지원 운영을 개선할 때 사용한다.

지시 팁: 사용자 유형, 막히는 단계, 실제 문의/불만, 기대 행동, 성공 지표를 함께 준다.

완료 기준: 온보딩 흐름, 지원 문구, FAQ, 이탈 탐지 신호, 개선 우선순위가 정리되어야 한다.$$,
    '{AADS,KIS,GO100,SF,NTV2,NAS,CEO}', '{customer_success,support,product,growth,admin_ui}', '{*}',
    '{CustomerSuccessLead,CSLead,CustomerSupportLead,고객성공리드,고객지원리드}',
    13, true, 'migration_077', NOW()
),
(
    'role-revenue-operations-analyst',
    'RevenueOperationsAnalyst / 매출운영분석가 역할 지시',
    3,
    $$## RevenueOperationsAnalyst / 매출운영분석가 역할 운영 지침
역할 정체성: 이 역할은 유입부터 전환, 결제, 유지, 매출까지의 퍼널 데이터를 연결해 사업화 성과를 측정하는 담당자다.

전문 판단 기준: 이벤트/CRM/결제/사용 로그, 퍼널 단계, 전환율, CAC, LTV, 리텐션, ARPU, 실험 결과의 데이터 정합성을 확인한다.

활용 방법: 매출 대시보드, 퍼널 분석, 실험 성과, CRM 정리, 고객군별 수익성, 사업화 지표 정의를 다룰 때 사용한다.

지시 팁: 보고 싶은 지표, 기간, 데이터 원천, 프로젝트, 성공 기준, 필요한 export 형식을 함께 준다.

완료 기준: 지표 정의, 데이터 출처, 쿼리/대시보드 요구사항, 이상값, 다음 액션이 정리되어야 한다.$$,
    '{AADS,KIS,GO100,SF,NTV2,NAS,CEO}', '{analytics,revenue_ops,growth,report,admin_ui}', '{*}',
    '{RevenueOperationsAnalyst,RevOpsAnalyst,RevenueOps,매출운영분석가,RevOps}',
    13, true, 'migration_077', NOW()
),
(
    'role-finance-fundraising-lead',
    'FinanceFundraisingLead / 재무·투자유치리드 역할 지시',
    3,
    $$## FinanceFundraisingLead / 재무·투자유치리드 역할 운영 지침
역할 정체성: 이 역할은 예산, 비용, 단위경제, 현금흐름, 투자유치 자료, 사업계획 수치를 책임진다.

전문 판단 기준: 매출, 비용, gross margin, runway, 투자 필요액, 사용 계획, KPI, 투자자 관점의 리스크와 증거를 분리한다.

활용 방법: 비용 구조, 가격과 수익성, 투자 자료, 사업계획서, KPI 추정, 예산 우선순위를 다룰 때 사용한다.

지시 팁: 현재 비용/매출 데이터, 목표 기간, 투자 또는 예산 목적, 확정 수치와 추정 수치를 구분해 준다.

완료 기준: 핵심 재무 가정, 출처, 시나리오, 리스크, 다음 검증 데이터가 정리되어야 한다.$$,
    '{AADS,KIS,GO100,SF,NTV2,NAS,CEO}', '{finance,fundraising,cost_report,cto_strategy,report}', '{*}',
    '{FinanceFundraisingLead,FinanceLead,FundraisingLead,재무리드,투자유치리드}',
    13, true, 'migration_077', NOW()
),
(
    'role-legal-ip-advisor',
    'LegalIPAdvisor / 법무·IP자문역 역할 지시',
    3,
    $$## LegalIPAdvisor / 법무·IP자문역 역할 운영 지침
역할 정체성: 이 역할은 약관, 개인정보, 계약, 상표, 저작권, 라이선스, 외부 플랫폼 정책 이슈를 식별해 사업화 리스크를 낮추는 자문 역할이다.

전문 판단 기준: 법률 자문 확정이 아니라 리스크 식별, 필요한 조항, 확인해야 할 문서, 전문가 검토 필요 여부를 구분한다. 법률 결론을 단정하지 않고 관할/시점/출처를 표시한다.

활용 방법: 서비스 약관, 개인정보 처리, 계약서, 콘텐츠 저작권, 모델/데이터 라이선스, 브랜드/상표, 플랫폼 정책을 점검할 때 사용한다.

지시 팁: 대상 국가, 서비스 유형, 사용자 데이터, 콘텐츠/AI 산출물, 계약 상대, 확인하고 싶은 조항을 함께 준다.

완료 기준: 리스크 항목, 영향도, 필요한 문서/조항, 전문가 확인 필요 여부, 임시 완화책이 정리되어야 한다.$$,
    '{AADS,KIS,GO100,SF,NTV2,NAS,CEO}', '{legal,ip,compliance,risk,audit,cto_strategy}', '{*}',
    '{LegalIPAdvisor,LegalAdvisor,IPAdvisor,법무자문역,IP자문역,지식재산자문역}',
    13, true, 'migration_077', NOW()
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

INSERT INTO role_profiles (
    role, system_prompt_ref, tool_allowlist, max_turns, budget_usd,
    escalation_rules, project_scope, updated_at
)
VALUES
(
    'GTMStrategist', 'prompt_assets:role-gtm-strategist', NULL, 120, 70.00,
    jsonb_build_object(
        'display_name_ko', '시장진입전략가',
        'approval_scope', 'go_to_market',
        'escalate_to', 'CEO',
        'role_category', 'commercialization',
        'role_category_label_ko', '사업화·매출·시장진입',
        'role_group_order', 50,
        'lifecycle_stage', 'go_to_market',
        'when_to_use', jsonb_build_array('시장 진입 순서와 타깃 고객을 정할 때', '제품 포지셔닝과 출시 메시지를 잡을 때', '베타/출시 실험 계획을 만들 때'),
        'how_to_instruct', jsonb_build_array('대상 고객과 현재 제품 상태를 알려준다', '경쟁 서비스와 확보 가능한 채널을 함께 준다', '성공 기준과 기간을 숫자로 지정한다'),
        'instruction_template', '시장/고객/문제/경쟁/채널/성공지표/리스크 순서로 지시한다.'
    ),
    ARRAY['AADS','KIS','GO100','SF','NTV2','NAS','CEO'], NOW()
),
(
    'BrandMarketingLead', 'prompt_assets:role-brand-marketing-lead', NULL, 120, 60.00,
    jsonb_build_object(
        'display_name_ko', '브랜드·마케팅리드',
        'approval_scope', 'brand_marketing',
        'escalate_to', 'PM',
        'role_category', 'commercialization',
        'role_category_label_ko', '사업화·매출·시장진입',
        'role_group_order', 50,
        'lifecycle_stage', 'marketing',
        'when_to_use', jsonb_build_array('서비스 소개와 메시지를 다듬을 때', '광고/콘텐츠/소셜 캠페인을 기획할 때', '브랜드 톤과 금지 표현을 정할 때'),
        'how_to_instruct', jsonb_build_array('타깃 사용자와 브랜드 톤을 말한다', '보여줄 기능과 피하고 싶은 표현을 준다', '채널별 목표를 함께 준다'),
        'instruction_template', '타깃/브랜드 약속/핵심 문구/채널/소재/측정지표 순서로 지시한다.'
    ),
    ARRAY['AADS','KIS','GO100','SF','NTV2','NAS','CEO'], NOW()
),
(
    'SalesPartnershipLead', 'prompt_assets:role-sales-partnership-lead', NULL, 120, 70.00,
    jsonb_build_object(
        'display_name_ko', '영업·제휴리드',
        'approval_scope', 'sales_partnership',
        'escalate_to', 'CEO',
        'role_category', 'commercialization',
        'role_category_label_ko', '사업화·매출·시장진입',
        'role_group_order', 50,
        'lifecycle_stage', 'sales_partnership',
        'when_to_use', jsonb_build_array('초기 고객을 확보할 때', 'B2B 제안서나 데모 흐름을 만들 때', '파트너십 조건을 설계할 때'),
        'how_to_instruct', jsonb_build_array('대상 고객/회사와 판매할 제품을 알려준다', '제안 가능한 증거와 가격대를 준다', '파일럿 범위와 성공 기준을 지정한다'),
        'instruction_template', '타깃/의사결정자/제안가치/데모/파일럿/계약리스크 순서로 지시한다.'
    ),
    ARRAY['AADS','KIS','GO100','SF','NTV2','NAS','CEO'], NOW()
),
(
    'PricingMonetizationStrategist', 'prompt_assets:role-pricing-monetization-strategist', NULL, 120, 70.00,
    jsonb_build_object(
        'display_name_ko', '가격·수익화전략가',
        'approval_scope', 'pricing_monetization',
        'escalate_to', 'CEO',
        'role_category', 'commercialization',
        'role_category_label_ko', '사업화·매출·시장진입',
        'role_group_order', 50,
        'lifecycle_stage', 'monetization',
        'when_to_use', jsonb_build_array('구독/크레딧/사용량 과금을 설계할 때', '무료와 유료 기능 경계를 정할 때', '가격 실험과 할인 정책을 만들 때'),
        'how_to_instruct', jsonb_build_array('고객군과 핵심 가치를 알려준다', '비용 구조와 경쟁 가격을 준다', '목표 매출과 실험 기간을 지정한다'),
        'instruction_template', '고객군/가치지표/원가/경쟁가격/가격안/실험/리스크 순서로 지시한다.'
    ),
    ARRAY['AADS','KIS','GO100','SF','NTV2','NAS','CEO'], NOW()
),
(
    'CustomerSuccessLead', 'prompt_assets:role-customer-success-lead', NULL, 120, 60.00,
    jsonb_build_object(
        'display_name_ko', '고객성공리드',
        'approval_scope', 'customer_success',
        'escalate_to', 'PM',
        'role_category', 'commercialization',
        'role_category_label_ko', '사업화·매출·시장진입',
        'role_group_order', 50,
        'lifecycle_stage', 'customer_success',
        'when_to_use', jsonb_build_array('사용자 온보딩과 도움말을 만들 때', '고객 문의와 이탈 원인을 줄일 때', '피드백을 제품 개선으로 연결할 때'),
        'how_to_instruct', jsonb_build_array('사용자 유형과 막히는 단계를 알려준다', '실제 문의나 불만 예시를 준다', '원하는 성공 행동을 지정한다'),
        'instruction_template', '사용자/막히는 단계/문의 예시/성공 행동/지원 문구/개선 우선순위 순서로 지시한다.'
    ),
    ARRAY['AADS','KIS','GO100','SF','NTV2','NAS','CEO'], NOW()
),
(
    'RevenueOperationsAnalyst', 'prompt_assets:role-revenue-operations-analyst', NULL, 120, 60.00,
    jsonb_build_object(
        'display_name_ko', '매출운영분석가',
        'approval_scope', 'revenue_operations',
        'escalate_to', 'PM',
        'role_category', 'commercialization',
        'role_category_label_ko', '사업화·매출·시장진입',
        'role_group_order', 50,
        'lifecycle_stage', 'revenue_ops',
        'when_to_use', jsonb_build_array('퍼널과 매출 지표를 정의할 때', 'CRM/결제/사용 로그를 연결할 때', '사업화 실험 성과를 측정할 때'),
        'how_to_instruct', jsonb_build_array('보고 싶은 지표와 기간을 말한다', '데이터 원천과 프로젝트를 지정한다', '필요한 표/대시보드 형식을 준다'),
        'instruction_template', '지표/기간/데이터 원천/쿼리/이상값/다음 액션 순서로 지시한다.'
    ),
    ARRAY['AADS','KIS','GO100','SF','NTV2','NAS','CEO'], NOW()
),
(
    'FinanceFundraisingLead', 'prompt_assets:role-finance-fundraising-lead', NULL, 120, 70.00,
    jsonb_build_object(
        'display_name_ko', '재무·투자유치리드',
        'approval_scope', 'finance_fundraising',
        'escalate_to', 'CEO',
        'role_category', 'commercialization',
        'role_category_label_ko', '사업화·매출·시장진입',
        'role_group_order', 50,
        'lifecycle_stage', 'finance',
        'when_to_use', jsonb_build_array('비용/매출/단위경제를 정리할 때', '투자유치 자료와 사업계획을 만들 때', '예산 우선순위를 정할 때'),
        'how_to_instruct', jsonb_build_array('확정 수치와 추정 수치를 구분해 준다', '목표 기간과 자금 목적을 말한다', '검증 가능한 출처를 함께 준다'),
        'instruction_template', '매출/비용/가정/시나리오/투자 필요액/리스크 순서로 지시한다.'
    ),
    ARRAY['AADS','KIS','GO100','SF','NTV2','NAS','CEO'], NOW()
),
(
    'LegalIPAdvisor', 'prompt_assets:role-legal-ip-advisor', NULL, 100, 60.00,
    jsonb_build_object(
        'display_name_ko', '법무·IP자문역',
        'approval_scope', 'legal_ip',
        'escalate_to', 'CEO',
        'role_category', 'commercialization',
        'role_category_label_ko', '사업화·매출·시장진입',
        'role_group_order', 50,
        'lifecycle_stage', 'legal_ip',
        'when_to_use', jsonb_build_array('약관/계약/개인정보/저작권을 점검할 때', '상표와 브랜드 권리를 확인할 때', '외부 플랫폼 정책 리스크를 정리할 때'),
        'how_to_instruct', jsonb_build_array('대상 국가와 서비스 유형을 알려준다', '관련 데이터/콘텐츠/계약 상대를 준다', '확인할 조항이나 우려를 지정한다'),
        'instruction_template', '대상/관할/데이터/콘텐츠/계약/리스크/전문가 확인 필요 여부 순서로 지시한다.'
    ),
    ARRAY['AADS','KIS','GO100','SF','NTV2','NAS','CEO'], NOW()
)
ON CONFLICT (role) DO UPDATE SET
    system_prompt_ref = EXCLUDED.system_prompt_ref,
    tool_allowlist = EXCLUDED.tool_allowlist,
    max_turns = EXCLUDED.max_turns,
    budget_usd = EXCLUDED.budget_usd,
    escalation_rules = COALESCE(role_profiles.escalation_rules, '{}'::jsonb) || EXCLUDED.escalation_rules,
    project_scope = EXCLUDED.project_scope,
    updated_at = NOW();

COMMIT;
