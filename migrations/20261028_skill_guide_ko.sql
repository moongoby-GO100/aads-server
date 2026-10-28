-- 20261028 시스템 테넌트 공용 스킬 9건에 한글 사용 안내(metadata.guide) 추가.
-- 내용은 migration 158 시드의 description / validation / allowed_tools / risk_tier 에 적힌 사실만 옮겼다.
-- 입력(inputs)은 스키마를 알 수 없어 비워 둔다 — active 버전 input_schema 가 있으면 API 가 채운다.
-- metadata 의 기존 키는 보존(jsonb ||)하고 guide 키만 덮어쓴다. 같은 내용이면 몇 번을 적용해도 결과가 같다.
-- risk_tier / allowed_tools 는 건드리지 않는다. 행 삭제·컬럼 삭제 없음.
-- 되돌림: migrations/rollback/20261028_skill_guide_ko.down.sql
BEGIN;

UPDATE ops_skill_library l
SET metadata = COALESCE(l.metadata, '{}'::jsonb) || jsonb_build_object('guide', g.guide)
FROM (VALUES
    ('aads-bluegreen-release', $json${
        "title_ko": "AADS 무중단(블루/그린) 배포",
        "summary_ko": "AADS 서버·대시보드를 하나의 릴리스 SHA 로 블루/그린 방식으로 교체하는 배포 절차입니다. 배포 등급이라 대표님 승인 후에만 실행됩니다.",
        "what_it_does": [
            "릴리스 대상이 커밋된 깨끗한 SHA 인지 확인합니다.",
            "후보(candidate) 슬롯이 정상인지 확인합니다.",
            "대기(standby) 슬롯이 같은 이미지 digest 인지 확인합니다.",
            "배포 후 5분 동안 P0/P1 장애를 모니터링합니다."
        ],
        "use_when": [
            "AADS 서버나 대시보드의 변경을 운영에 반영해야 할 때",
            "운영 중단 없이 새 버전으로 교체해야 할 때",
            "배포 뒤 이상 여부를 단계별로 확인해야 할 때"
        ],
        "inputs": [],
        "outputs": "단계별 점검 결과(릴리스 SHA, 후보 상태, digest 일치 여부, 5분 모니터링 결과)와 실행 기록(ops_skill_runs)이 남습니다.",
        "approval_ko": "배포 등급이라 대표님 승인 후에만 실행됩니다.",
        "related_tools": ["git", "docker", "deploy.sh", "curl", "query_database"]
    }$json$::jsonb),
    ('runner-recovery', $json${
        "title_ko": "파이프라인 러너 작업 복구",
        "summary_ko": "멈췄거나 실패했거나 승인 대기 중인 파이프라인 러너 작업을 로그 근거 중심으로 확인하고 복구하는 절차입니다. 쓰기 등급이라 대표님 승인 후에만 실행됩니다.",
        "what_it_does": [
            "러너 작업 상태를 다시 조회합니다.",
            "작업 로그를 근거로 확인합니다.",
            "오래 멈춘 작업(stale)과 오류로 끝난 작업을 구분합니다.",
            "필요하면 작업을 종료(terminate_task)합니다."
        ],
        "use_when": [
            "러너 작업이 오래 진행되지 않을 때",
            "러너 작업이 실패해 원인을 확인해야 할 때",
            "승인 대기 상태에서 멈춘 작업을 확인해야 할 때"
        ],
        "inputs": [],
        "outputs": "상태 재조회 결과, 로그 근거, stale/오류 구분 결과와 실행 기록(ops_skill_runs)이 남습니다.",
        "approval_ko": "쓰기 등급이라 대표님 승인 후에만 실행됩니다.",
        "related_tools": ["pipeline_runner_status", "read_task_logs", "terminate_task"]
    }$json$::jsonb),
    ('authenticated-site-collector', $json${
        "title_ko": "로그인 사이트 데이터 수집",
        "summary_ko": "로그인이 필요한 사이트의 데이터를 수집합니다. CAPTCHA·OTP 는 우회하지 않으며, 같은 작업(work_key)으로 이어서 재개합니다. 인증 등급이라 대표님 승인 후에만 실행됩니다.",
        "what_it_does": [
            "수집 전에 시험 실행(dry-run)을 먼저 합니다.",
            "PC 에이전트·브라우저 브리지로 로그인 사이트에서 데이터를 수집합니다.",
            "CAPTCHA/OTP 우회는 차단하며 사람이 처리해야 합니다.",
            "같은 work_key 로 중단된 수집을 이어서 재개합니다."
        ],
        "use_when": [
            "로그인이 필요한 채널의 데이터를 가져와야 할 때",
            "수집이 CAPTCHA/OTP 때문에 멈춘 뒤 이어서 진행해야 할 때"
        ],
        "inputs": [],
        "outputs": "시험 실행 결과와 수집 결과, 실행 기록(ops_skill_runs)이 남습니다.",
        "approval_ko": "인증 등급이라 대표님 승인 후에만 실행됩니다.",
        "related_tools": ["pc_agent", "browser_bridge", "credential_vault", "browser_tasks"]
    }$json$::jsonb),
    ('store-assistant-channel-collector', $json${
        "title_ko": "매장비서 채널 데이터 수집",
        "summary_ko": "배달의민족·스마트플레이스·쿠팡이츠 같은 매장 채널의 데이터를 수집하는 프로필입니다. 인증 등급이라 대표님 승인 후에만 실행됩니다.",
        "what_it_does": [
            "채널별 사이트 프로필을 확인합니다.",
            "계정 정책을 확인합니다.",
            "수동 챌린지(로그인 확인 등)가 끝난 뒤 수집을 재개합니다."
        ],
        "use_when": [
            "매장 채널의 주문·마케팅 데이터를 가져와야 할 때",
            "수동 확인이 필요해 멈춘 채널 수집을 이어서 진행해야 할 때"
        ],
        "inputs": [],
        "outputs": "채널 수집 결과와 실행 기록(ops_skill_runs)이 남습니다.",
        "approval_ko": "인증 등급이라 대표님 승인 후에만 실행됩니다.",
        "related_tools": ["pc_agent", "browser_bridge", "browser_recipes"]
    }$json$::jsonb),
    ('go100-market-open-check', $json${
        "title_ko": "GO100 장 시작 점검",
        "summary_ko": "GO100 장 시작 시점의 진입 상태, 데이터 품질, 시그널을 점검합니다. 금융 등급이라 대표님 승인 후에만 실행되며, 먼저 읽기 전용으로 확인하고 주문 승인 게이트를 지킵니다.",
        "what_it_does": [
            "장 시작 시점의 진입(entry) 상태를 점검합니다.",
            "데이터 품질을 점검합니다.",
            "시그널을 감사합니다.",
            "읽기 전용으로 먼저 확인하고 주문 게이트를 지킵니다."
        ],
        "use_when": [
            "GO100 장 시작 직후 진입·데이터·시그널이 정상인지 확인해야 할 때",
            "진입이 없거나 이상할 때 원인을 점검해야 할 때"
        ],
        "inputs": [],
        "outputs": "종목명이 포함된 점검 결과와 실행 기록(ops_skill_runs)이 남습니다.",
        "approval_ko": "금융 등급이라 대표님 승인 후에만 실행됩니다.",
        "related_tools": ["query_project_database", "run_remote_command", "read_remote_file"]
    }$json$::jsonb),
    ('kis-broker-health', $json${
        "title_ko": "KIS 브로커 상태 점검",
        "summary_ko": "KIS 브로커의 계좌·세션·주문 상태를 점검하는 읽기 전용 보고서입니다. 금융 등급이라 대표님 승인 후에만 실행됩니다.",
        "what_it_does": [
            "브로커 세션 상태를 확인합니다.",
            "계좌와 주문 상태를 점검합니다.",
            "주문 리스크 게이트를 확인합니다.",
            "읽기 전용 보고서를 만듭니다."
        ],
        "use_when": [
            "KIS 브로커 연결이나 세션이 정상인지 확인해야 할 때",
            "주문이 막히거나 이상할 때 계좌·주문 상태를 점검해야 할 때"
        ],
        "inputs": [],
        "outputs": "브로커 세션·주문 리스크 게이트 점검 결과와 실행 기록(ops_skill_runs)이 남습니다.",
        "approval_ko": "금융 등급이라 대표님 승인 후에만 실행됩니다.",
        "related_tools": ["query_project_database", "run_remote_command"]
    }$json$::jsonb),
    ('ntv2-merchant-contract', $json${
        "title_ko": "NTV2 입점 계약 문서 업무",
        "summary_ko": "NewTalk V2 입점사 계약과 온보딩 문서, 템플릿을 다루는 업무입니다. 쓰기 등급이라 대표님 승인 후에만 실행됩니다.",
        "what_it_does": [
            "문서 템플릿 원본을 확인합니다.",
            "테넌트(입점사) 범위를 확인합니다.",
            "문서 미리보기를 제공합니다."
        ],
        "use_when": [
            "NTV2 입점사 계약서나 온보딩 문서를 준비해야 할 때",
            "계약 템플릿 내용을 확인하거나 문서를 내보내야 할 때"
        ],
        "inputs": [],
        "outputs": "문서 미리보기와 실행 기록(ops_skill_runs)이 남습니다.",
        "approval_ko": "쓰기 등급이라 대표님 승인 후에만 실행됩니다.",
        "related_tools": ["read_remote_file", "run_remote_command", "export_data"]
    }$json$::jsonb),
    ('sf-video-pipeline-health', $json${
        "title_ko": "ShortFlow 영상 파이프라인 점검",
        "summary_ko": "ShortFlow 영상 생성 대기열과 작업자(worker) 상태를 점검합니다. 읽기 전용이라 승인 없이 바로 실행됩니다.",
        "what_it_does": [
            "영상 생성 대기열 건수를 확인합니다.",
            "작업자(worker) 상태를 확인합니다.",
            "최근 오류 샘플을 확인합니다."
        ],
        "use_when": [
            "ShortFlow 영상 생성이 늦거나 멈춘 것 같을 때",
            "영상 작업자 상태와 최근 오류를 한 번에 확인하고 싶을 때"
        ],
        "inputs": [],
        "outputs": "대기열 건수, 작업자 상태, 최근 오류 샘플과 실행 기록(ops_skill_runs)이 남습니다.",
        "approval_ko": "읽기 등급이라 승인 없이 바로 실행됩니다.",
        "related_tools": ["run_remote_command", "list_remote_dir", "read_remote_file"]
    }$json$::jsonb),
    ('nas-image-job-health', $json${
        "title_ko": "NAS 이미지 작업 점검",
        "summary_ko": "NAS 이미지 처리 대기열, 저장 공간, 실패 현황을 점검합니다. 읽기 전용이라 승인 없이 바로 실행됩니다.",
        "what_it_does": [
            "저장 공간 용량을 확인합니다.",
            "이미지 작업 대기열을 확인합니다.",
            "최근 실패 내역을 확인합니다."
        ],
        "use_when": [
            "NAS 이미지 처리가 늦거나 실패하는 것 같을 때",
            "NAS 저장 공간이 부족하지 않은지 확인하고 싶을 때"
        ],
        "inputs": [],
        "outputs": "저장 공간, 작업 대기열, 최근 실패 내역과 실행 기록(ops_skill_runs)이 남습니다.",
        "approval_ko": "읽기 등급이라 승인 없이 바로 실행됩니다.",
        "related_tools": ["run_remote_command", "list_remote_dir"]
    }$json$::jsonb)
) AS g(slug, guide)
WHERE l.slug = g.slug
  AND l.tenant_id = '00000000-0000-0000-0000-000000000000'::uuid;

COMMIT;
