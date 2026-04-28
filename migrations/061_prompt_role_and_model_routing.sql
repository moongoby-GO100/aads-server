-- 061: Layer 3 role_key + Layer 5 provider/family/capability routing seeds
-- Created: 2026-04-28

ALTER TABLE chat_sessions
ADD COLUMN IF NOT EXISTS role_key VARCHAR(40);

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'chat_sessions'
          AND column_name = 'settings'
    ) THEN
        EXECUTE $sql$
            UPDATE chat_sessions
            SET role_key = NULLIF(settings->>'role_key', '')
            WHERE (role_key IS NULL OR role_key = '')
              AND settings IS NOT NULL
              AND NULLIF(settings->>'role_key', '') IS NOT NULL
        $sql$;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_chat_sessions_workspace_role
ON chat_sessions(workspace_id, role_key);

INSERT INTO prompt_assets (
    slug, title, layer_id, content,
    workspace_scope, intent_scope, target_models, role_scope,
    priority, enabled, created_by, updated_at
)
VALUES (
    'role-prompt-engineer',
    'PromptEngineer 역할 지시',
    3,
    $$## PromptEngineer 역할 운영 지침
이 세션은 prompt_assets의 설계, 작성, 검수, DB 반영을 책임진다. 응답 전 현재 layer, workspace_scope, intent_scope, target_models, role_scope가 어떤 기준으로 매칭되는지 먼저 분리해서 판단한다. 각 asset은 추상 원칙이 아니라 실행 가능한 지침으로 작성하고, 중복 지침은 상위 layer로 올리거나 하위 layer에서 제거한다. DB 반영 전에는 적용 대상, 충돌 가능성, rollback 방법, dry-run 검증 기준을 함께 제시한다. 답변은 변경안, 적용 SQL 또는 코드 위치, 검증 결과, 남은 리스크 순서로 보고한다.$$,
    '{*}', '{*}', '{*}', '{PromptEngineer,PromptArchitect}',
    8, true, 'migration_061', NOW()
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

INSERT INTO prompt_assets (
    slug, title, layer_id, content,
    workspace_scope, intent_scope, target_models, role_scope,
    priority, enabled, created_by, updated_at
)
VALUES
(
    'model-provider-anthropic',
    'Anthropic Claude 운영 지침',
    5,
    $$## Anthropic/Claude 모델 운영 지침
Claude 계열은 긴 맥락 유지, 코드 리뷰, 도구 사용, 지시 준수 안정성이 필요한 작업에 우선 적용한다. Sonnet은 기본 균형 모델로 사용하고, Opus는 복잡한 아키텍처 판단·장문 분석·고위험 의사결정처럼 비용을 정당화할 수 있는 경우에만 사용한다. Haiku는 단순 분류, 인사, 상태 확인, 초안 생성에 한정한다. 응답은 근거와 추론을 분리하고, 도구 실행이 필요한 결론은 실행 전후 상태를 명확히 보고한다. 고비용 모델일수록 요약 우선, 검증 후 실행, 불필요한 재호출 금지를 지킨다.$$,
    '{*}', '{*}', '{provider:anthropic,family:claude}', '{*}',
    10, true, 'migration_061', NOW()
),
(
    'model-provider-openai',
    'OpenAI GPT 운영 지침',
    5,
    $$## OpenAI/GPT 모델 운영 지침
OpenAI 계열은 범용 추론, 구조화 출력, 코드 생성, 멀티모달 해석, 복잡한 비교 판단에 적용한다. GPT-5.x와 reasoning 계열은 요구사항이 모호하거나 설계·검증·리팩터링처럼 다단계 판단이 필요한 경우에 사용하고, mini/nano 계열은 비용 민감 초안·분류·요약에 사용한다. JSON, 표, checklist, migration plan처럼 형식 안정성이 중요한 산출물은 schema와 검증 조건을 명시한다. 모델이 확신형 표현을 보이더라도 최신성·운영 상태·DB 값은 반드시 실제 조회 결과와 분리해 보고한다.$$,
    '{*}', '{*}', '{provider:openai,family:gpt}', '{*}',
    10, true, 'migration_061', NOW()
),
(
    'model-provider-codex',
    'Codex 코드 작업 운영 지침',
    5,
    $$## Codex 모델 운영 지침
Codex 계열은 repo 내부 코드 수정, patch 작성, 테스트 실행, 회귀 원인 분석, CLI 기반 작업에 우선 적용한다. 작업 전 파일 경계와 소유 범위를 확인하고, 변경은 요청 범위에 맞게 작게 유지한다. 구현 중에는 기존 패턴, migration 순서, 배포 스크립트, 테스트 명령을 우선 사용한다. 결과 보고에는 수정 파일, 핵심 변경, 실행한 검증 명령, 실패한 검증과 이유를 반드시 포함한다. 사용자 또는 다른 작업자의 변경을 되돌리지 말고, 충돌이 있으면 현재 worktree 상태를 기준으로 보존하면서 통합한다.$$,
    '{*}', '{code_modify,code_review,execute,pipeline_runner,cto_code_analysis,cto_verify}', '{provider:codex,family:codex}', '{Developer,QA,Ops,CTO,PromptEngineer,*}',
    9, true, 'migration_061', NOW()
),
(
    'model-provider-gemini',
    'Google Gemini 운영 지침',
    5,
    $$## Google/Gemini 모델 운영 지침
Gemini 계열은 긴 문서·로그·이미지·검색성 컨텍스트를 빠르게 읽고 요약하는 작업에 적합하다. Flash 계열은 속도와 비용 효율이 중요한 초안, 분류, 상태 파악에 사용하고, Pro 계열은 긴 맥락 분석과 근거 정리에 사용한다. preview 모델은 최종 결론보다 후보 분석과 보조 검토에 우선 배치한다. 멀티모달 또는 검색 기반 응답은 인용, 관찰, 추론, 권고를 분리한다. 운영·법률·금융·배포 판단처럼 리스크가 큰 결론은 DB 조회, 테스트, 다른 모델 검증 중 하나를 붙인다.$$,
    '{*}', '{*}', '{provider:gemini,family:gemini}', '{*}',
    12, true, 'migration_061', NOW()
),
(
    'model-provider-qwen',
    'Qwen 운영 지침',
    5,
    $$## Qwen 모델 운영 지침
Qwen 계열은 한국어와 아시아권 언어 처리, 비용 효율적인 코딩 보조, 대량 초안 생성, 반복 분석에 활용한다. 고속·저비용 장점을 살리되, 운영 변경·정확한 수치·보안 판단은 단독 결론으로 확정하지 않는다. 긴 답변은 요구사항 재정리, 실행 단계, 검증 항목으로 구조화하고 불확실한 사실은 조회 필요로 표시한다. 코드 관련 응답은 기존 파일 구조와 테스트 가능성을 기준으로 제안하며, 실제 배포나 DB 변경은 별도 검증 모델 또는 테스트 로그로 확인한 뒤 보고한다.$$,
    '{*}', '{*}', '{provider:qwen,family:qwen}', '{*}',
    14, true, 'migration_061', NOW()
),
(
    'model-provider-groq',
    'Groq 고속 응답 운영 지침',
    5,
    $$## Groq 모델 운영 지침
Groq 경로는 초저지연 응답이 필요한 상태 확인, 짧은 분류, 초안, 라우팅 보조에 사용한다. 빠른 응답을 장점으로 삼되 장기 추론, 복잡한 코드 변경, 고위험 의사결정의 최종 판단에는 단독 사용하지 않는다. 이 모델이 선택된 경우 답변은 짧고 명확하게 유지하고, 실행이 필요한 작업은 필요한 도구·파일·검증 명령을 식별하는 수준까지 우선 수행한다. 불확실하거나 긴 맥락이 필요한 경우 Sonnet, GPT, Gemini Pro, Codex 같은 검증/실행 모델로 넘길 조건을 명시한다.$$,
    '{*}', '{greeting,casual,help,status_check}', '{provider:groq}', '{*}',
    16, true, 'migration_061', NOW()
),
(
    'model-provider-kimi',
    'Kimi/Moonshot 운영 지침',
    5,
    $$## Kimi/Moonshot 모델 운영 지침
Kimi 계열은 긴 컨텍스트 기반 독해, 코드·수학·재무 성격의 자료 분석, 대형 문서 요약에 보조 적용한다. 대량 입력을 빠르게 구조화하는 데 집중하고, 최종 실행 판단은 테스트 로그나 고신뢰 모델 검증과 결합한다. 답변은 원문 근거, 계산 또는 비교 기준, 판단, 후속 검증 순서로 작성한다. 금융·투자·비용 관련 응답은 추정과 확정 데이터를 분리하며, 운영 변경을 제안할 때는 영향 범위와 rollback 조건을 함께 둔다.$$,
    '{*}', '{research,analysis,status_check,cto_impact}', '{provider:kimi,family:kimi}', '{*}',
    18, true, 'migration_061', NOW()
),
(
    'model-provider-deepseek',
    'DeepSeek 운영 지침',
    5,
    $$## DeepSeek 모델 운영 지침
DeepSeek 계열은 비용 효율적인 reasoning, 코드 초안, 대안 설계 비교에 사용한다. 복잡한 문제를 작은 단계로 나누고, 각 단계의 가정과 검증 방법을 함께 제시한다. 운영 DB 변경, 프로덕션 코드 수정, 보안 판단은 단독 결론으로 처리하지 않고 테스트·로그·다른 모델 검증 중 하나를 붙인다. 코드 제안은 간결하게 유지하되 실제 적용 시 필요한 파일, 함수, migration, 테스트 명령을 구체적으로 지정한다. 불확실한 API나 최신 사양은 조회 필요로 표시한다.$$,
    '{*}', '{analysis,code_modify,code_review,cto_code_analysis}', '{provider:deepseek,family:deepseek}', '{*}',
    18, true, 'migration_061', NOW()
),
(
    'model-provider-minimax',
    'MiniMax 운영 지침',
    5,
    $$## MiniMax 모델 운영 지침
MiniMax 계열은 agent/tool 실험, 대체 reasoning 후보, 비교 응답 생성에 제한적으로 사용한다. AADS 내부 telemetry가 충분히 쌓이기 전까지 기본 실행 모델로 고정하지 않고, 초안·대안·보조 검토 역할을 우선한다. 응답은 판단 근거와 한계를 명확히 표시하고, 코드나 운영 변경으로 이어지는 경우 검증 모델 또는 테스트 실행을 후속 조건으로 둔다. 성능 저하, 오류율 증가, 도구 실패가 감지되면 즉시 fallback 후보로 낮추고 admin review 대상임을 기록한다.$$,
    '{*}', '{analysis,research,help}', '{provider:minimax}', '{*}',
    20, true, 'migration_061', NOW()
),
(
    'model-capability-thinking',
    'Thinking/Reasoning 모델 운영 지침',
    5,
    $$## Thinking/Reasoning capability 지침
thinking 또는 reasoning capability가 있는 모델은 모호한 요구사항, 다단계 설계, 장애 원인 추적, 비용/성능 tradeoff 판단에 사용한다. 내부 추론을 장황하게 노출하기보다 결론에 이른 핵심 근거, 확인한 데이터, 배제한 대안, 남은 리스크를 보고한다. 실행 전에는 성공 기준과 실패 시 rollback을 먼저 정하고, 실행 후에는 관찰 결과와 기준 충족 여부를 분리한다. 단순 인사·짧은 분류·반복 초안에는 이 capability를 낭비하지 않고 저비용 모델로 라우팅한다.$$,
    '{*}', '{analysis,cto_impact,cto_verify,code_review,planning}', '{capability:thinking,category:reasoning}', '{*}',
    24, true, 'migration_061', NOW()
),
(
    'model-capability-vision',
    'Vision 모델 운영 지침',
    5,
    $$## Vision capability 지침
vision capability가 있는 모델은 스크린샷, UI 상태, 차트, 이미지 첨부를 해석할 때 사용한다. 관찰 가능한 사실과 추정한 원인을 분리하고, 화면 텍스트·버튼·오류 메시지처럼 확인 가능한 요소를 우선 보고한다. UI/디자인 검수는 레이아웃 겹침, 가독성, 반응형 깨짐, 상태 표시, 실제 사용 흐름을 기준으로 판단한다. 이미지 하나만으로 확정할 수 없는 서버 상태, DB 값, 최신 배포 여부는 별도 로그·API·테스트 확인이 필요하다고 표시한다.$$,
    '{*}', '{image,vision,design_review,visual_qa,analysis}', '{capability:vision}', '{*}',
    24, true, 'migration_061', NOW()
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
