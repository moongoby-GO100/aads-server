-- 062: Remote project access contract for KIS/GO100/SF/NTV2 sessions.
BEGIN;

INSERT INTO prompt_assets (
    slug, title, layer_id, content,
    workspace_scope, intent_scope, target_models, role_scope,
    priority, enabled, created_by
)
VALUES (
    'project-remote-access-contract',
    '원격 프로젝트 접근 계약',
    2,
    $$## 원격 프로젝트 접근 계약
KIS/GO100/SF/NTV2 세션에서 코드, DB, 서버 상태, 오류, 개발, 수정, 배포, 원인분석 요청이 들어오면 기억이나 추정으로 답하지 않는다. 현재 세션의 프로젝트를 active_project로 간주하고 도구 호출 시 반드시 project 값을 명시한다. 코드 확인은 list_remote_dir 또는 read_remote_file을 먼저 사용하고, DB 확인은 query_database가 아니라 query_project_database를 사용한다. 파일 경로는 WORKDIR 기준 상대경로를 우선 사용한다. 프로젝트가 명시되지 않아도 워크스페이스 이름에서 active_project를 해석한다. 접근 실패 시 사용한 project/path/query와 오류를 보고하고, 확인하지 못한 내용을 사실처럼 단정하지 않는다.$$,
    ARRAY['KIS','GO100','SF','NTV2']::text[],
    ARRAY['*']::text[],
    ARRAY['*']::text[],
    ARRAY['*']::text[],
    5,
    true,
    'system'
)
ON CONFLICT (slug) DO UPDATE SET
    title = EXCLUDED.title,
    layer_id = EXCLUDED.layer_id,
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
    priority, enabled, created_by
)
VALUES (
    'project-go100-context',
    'GO100 프로젝트 컨텍스트',
    2,
    $$## GO100 실행 기준
GO100은 211 서버의 /root/kis-autotrade-v4를 사용하는 투자 분석·포트폴리오 프로젝트다. KIS와 물리 서버·코드베이스·PostgreSQL DB를 공유할 수 있으나 업무 판단과 보고는 GO100 도메인 기준으로 분리한다. GO100 세션에서 개발·분석·오류 확인 요청이 오면 read_remote_file/list_remote_dir 호출 시 project='GO100'을 사용하고, DB는 query_project_database(project='GO100')로 조회한다. 경로는 /root/kis-autotrade-v4 기준 상대경로를 우선 사용한다. KIS와 공유되는 파일을 읽더라도 보고서는 GO100 영향, KIS 영향, 공통 위험을 분리한다. 금융 데이터, 수익률, 포트폴리오, 추천 로직은 실제 코드와 DB를 확인한 뒤만 결론을 낸다.$$,
    ARRAY['GO100']::text[],
    ARRAY['*']::text[],
    ARRAY['*']::text[],
    ARRAY['*']::text[],
    10,
    true,
    'system'
)
ON CONFLICT (slug) DO UPDATE SET
    title = EXCLUDED.title,
    layer_id = EXCLUDED.layer_id,
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
    priority, enabled, created_by
)
VALUES (
    'intent-remote-code-db-preflight',
    '원격 코드·DB 사전 확인',
    4,
    $$## 원격 코드·DB 사전 확인
code_modify, code_fix, cto_code_analysis, service_inspection, project_db, database_query, remote_execute 인텐트는 답변 전에 실제 근거를 확보한다. 원격 프로젝트는 최소 1회 이상 list_remote_dir/read_remote_file/query_project_database/run_remote_command 중 관련 도구를 사용한다. 수정 지시라면 먼저 관련 파일을 읽고 영향 범위를 설명한 뒤 patch_remote_file 또는 pipeline_runner_submit을 선택한다. Runner로 위임할 때도 확인한 파일명, 쿼리, 로그 근거를 instruction에 포함한다. 직접 확인 없이 수정 가능, 문제 없음, 배포 완료라고 말하지 않는다.$$,
    ARRAY['KIS','GO100','SF','NTV2']::text[],
    ARRAY['code_modify','code_fix','code_review','execute','code_task','cto_code_analysis','service_inspection','project_db','database_query','remote_execute','cto_verify','cto_impact','pipeline_runner']::text[],
    ARRAY['*']::text[],
    ARRAY['*']::text[],
    5,
    true,
    'system'
)
ON CONFLICT (slug) DO UPDATE SET
    title = EXCLUDED.title,
    layer_id = EXCLUDED.layer_id,
    content = EXCLUDED.content,
    workspace_scope = EXCLUDED.workspace_scope,
    intent_scope = EXCLUDED.intent_scope,
    target_models = EXCLUDED.target_models,
    role_scope = EXCLUDED.role_scope,
    priority = EXCLUDED.priority,
    enabled = EXCLUDED.enabled,
    updated_at = NOW();

UPDATE chat_workspaces
SET settings = COALESCE(settings, '{}'::jsonb) || jsonb_build_object(
    'project_key', 'GO100',
    'server_profile', '211',
    'workdir', '/root/kis-autotrade-v4',
    'db_profile', 'GO100'
)
WHERE upper(name) LIKE '%GO100%' OR name LIKE '%백억%' OR name LIKE '%빡억%';

UPDATE chat_workspaces
SET settings = COALESCE(settings, '{}'::jsonb) || jsonb_build_object(
    'project_key', 'KIS',
    'server_profile', '211',
    'workdir', '/root/kis-autotrade-v4',
    'db_profile', 'KIS'
)
WHERE upper(name) LIKE '%KIS%' OR name LIKE '%자동매매%';

UPDATE chat_workspaces
SET settings = COALESCE(settings, '{}'::jsonb) || jsonb_build_object(
    'project_key', 'SF',
    'server_profile', '114',
    'workdir', '/',
    'db_profile', 'SF'
)
WHERE upper(name) LIKE '%SF%' OR upper(name) LIKE '%SHORTFLOW%';

UPDATE chat_workspaces
SET settings = COALESCE(settings, '{}'::jsonb) || jsonb_build_object(
    'project_key', 'NTV2',
    'server_profile', '114',
    'workdir', '/',
    'db_profile', 'NTV2'
)
WHERE upper(name) LIKE '%NTV2%' OR upper(name) LIKE '%NEWTALK V2%';

DO $$
BEGIN
    IF to_regclass('public.ai_observations') IS NOT NULL THEN
        UPDATE ai_observations
        SET value = 'KIS 작업 디렉터리: /root/kis-autotrade-v4 (서버211). 소스 경로 project_config.py 참조.'
        WHERE category = 'project_pattern' AND key = 'kis_workdir' AND project = 'KIS';

        UPDATE ai_observations
        SET value = 'KIS systemd 서비스는 실제 WorkingDirectory와 상태를 run_remote_command(project=''KIS'', command=''systemctl status ...'')로 확인 후 판단한다.'
        WHERE category = 'project_pattern' AND key = 'kis_systemd' AND project = 'KIS';

        UPDATE ai_observations
        SET value = 'GO100 작업 디렉터리: /root/kis-autotrade-v4 (서버211). KIS와 코드베이스를 공유하되 project=''GO100''으로 도구를 호출한다.'
        WHERE category = 'project_pattern' AND key = 'go100_workdir' AND project = 'GO100';

        UPDATE ai_observations
        SET value = 'GO100 서비스별 소스 경로: KIS와 동일 구조. backend/app/main.py, backend/app/api/v1/, backend/app/services/strategy/, backend/app/services/kis/, backend/app/models/, frontend/. 서비스 상태는 health_check 또는 run_remote_command로 확인 후 판단한다.'
        WHERE category = 'project_pattern' AND key = 'go100_source_map' AND project = 'GO100';
    END IF;

    IF to_regclass('public.memory_facts') IS NOT NULL THEN
        UPDATE memory_facts
        SET detail = 'KIS 작업 디렉터리: /root/kis-autotrade-v4 (서버211). 소스 경로 project_config.py 참조.',
            updated_at = NOW()
        WHERE project = 'KIS' AND subject = 'kis_workdir';

        UPDATE memory_facts
        SET detail = 'GO100 작업 디렉터리: /root/kis-autotrade-v4 (서버211). KIS와 코드베이스를 공유하되 project=''GO100''으로 도구를 호출한다.',
            updated_at = NOW()
        WHERE project = 'GO100' AND subject = 'go100_workdir';

        UPDATE memory_facts
        SET detail = 'GO100 서비스별 소스 경로: KIS와 동일 구조. backend/app/main.py, backend/app/api/v1/, backend/app/services/strategy/, backend/app/services/kis/, backend/app/models/, frontend/. 서비스 상태는 health_check 또는 run_remote_command로 확인 후 판단한다.',
            updated_at = NOW()
        WHERE project = 'GO100' AND subject = 'go100_source_map';
    END IF;
END $$;

COMMIT;
