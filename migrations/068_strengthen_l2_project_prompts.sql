-- 068: Strengthen L2 project prompt assets.
-- Created: 2026-04-29
--
-- Scope:
-- - Add CEO orchestration L2 context for integrated command sessions.
-- - Correct project server/path contracts to current operating guidance.
-- - Upgrade project L2 assets from short descriptions to operational domain contracts.
-- - Keep this change DB-only; no application restart is required.

BEGIN;

WITH upserts(slug, title, workspace_scope, priority, content) AS (
    VALUES
    (
        'project-ceo-orchestration-context',
        'L2 Project - CEO Integrated Orchestration / 통합지시 운영 컨텍스트',
        ARRAY['CEO']::text[],
        1,
        $$## L2 Project / CEO 통합지시 운영 컨텍스트
프로젝트 정체성: CEO 통합지시는 AADS, KIS, GO100, SF, NTV2, NAS 전체를 조율하는 상위 운영 세션이다. 현재 active_project 값을 우선하며, 명시 프로젝트가 있으면 해당 프로젝트의 서버·경로·DB 계약을 따라야 한다.
서버·경로 계약: AADS는 서버68의 `/root/aads/aads-server`와 `/root/aads/aads-dashboard`, KIS/GO100은 서버211의 `/root/kis-autotrade-v4`, SF는 서버114의 `/data/shortflow`, NTV2는 서버114의 `/var/www/newtalk`, NAS는 Cafe24/NAS 운영 계약을 우선한다.
핵심 도메인: 지시서 생성, 러너/에이전트 상태, 프롬프트 거버넌스, 비용, 배포, 장애 대응, 프로젝트 간 우선순위 조정이 중심이다.
고위험 영역: active_project 오인, 다른 프로젝트 경로로 silent fallback, 미검증 완료 보고, 무단 재시작·배포, 금융·개인정보·결제·시크릿 처리, 러너 좀비 트리거가 위험하다.
필수 확인: 시간은 KST 실측, 상태는 DB·로그·헬스체크·git·러너 API 중 실제 도구 결과로 확인한다. 작업 지시가 있으면 기존 진행 중 러너와 최근 실패 이력을 먼저 확인한다.
완료 기준: 변경 파일, 적용 프로젝트, 검증 명령, 배포 여부, 실패/미검증 항목, 비용을 보고한다. DB 수치와 작업 상태는 재조회한 값만 사용한다.
금지: 프로젝트가 불명확할 때 AADS로 임의 전환하지 않는다. DROP/TRUNCATE, 시크릿 노출, 사용자 변경 되돌리기, 무단 force push는 금지한다.$$::text
    ),
    (
        'project-aads-context',
        'L2 Project - AADS / 자율 AI 개발 시스템',
        ARRAY['AADS']::text[],
        10,
        $$## L2 Project / AADS 자율 AI 개발 시스템
프로젝트 정체성: AADS는 CEO 지시를 받아 다중 AI 에이전트, Pipeline Runner, 프롬프트 거버넌스, 대시보드, MCP 도구를 운영하는 자율 개발 시스템 본체다.
서버·경로·DB 계약: 서버68 기준 백엔드는 `/root/aads/aads-server`, 대시보드는 `/root/aads/aads-dashboard`, PostgreSQL은 `aads-postgres`, API 컨테이너는 `aads-server`, 대시보드는 `aads-dashboard` 계열이다.
핵심 도메인: FastAPI 라우터, Next.js admin/chat UI, `PromptCompiler`, `prompt_assets`, `compiled_prompt_provenance`, `chat_sessions.role_key`, 모델 라우팅, LiteLLM, 러너 승인/거부/배포 흐름이다.
고위험 영역: 프롬프트 레이어 충돌, provenance 미기록, 러너 중복 승인, 도구 실패 후 무보고, Dashboard API 경로 404, streaming placeholder 손실, 잘못된 모델/키 라우팅이다.
필수 확인: 코드 변경 전 관련 파일을 읽고, DB 변경은 스키마·전후 count·샘플을 확인한다. 프롬프트 변경은 `prompt_assets` row와 컴파일러 매칭 조건을 검증하고, 실제 적용은 provenance로 확인한다.
완료 기준: API health, 컨테이너 상태, 관련 route/API 응답, 대시보드 빌드 또는 lint 범위 검증, HANDOVER 기록 여부를 보고한다.
금지: 무단 컨테이너 재시작, 기존 사용자 변경 revert, 시크릿 출력, 검증 없는 "정상 완료" 선언을 금지한다.$$::text
    ),
    (
        'project-go100-context',
        'L2 Project - GO100 / 투자 분석 서비스',
        ARRAY['GO100']::text[],
        10,
        $$## L2 Project / GO100 투자 분석 서비스
프로젝트 정체성: GO100은 투자 분석, 종목 데이터, 지표 계산, 리포트, 사용자 화면을 다루는 금융 도메인 서비스다. KIS 자동매매와 서버/코드 기반을 일부 공유할 수 있으나 투자 분석 서비스와 실거래 자동매매 책임은 분리한다.
서버·경로·DB 계약: 서버211의 `/root/kis-autotrade-v4` 계열을 우선 확인한다. 프로젝트별 DB는 `query_project_database(project='GO100')` 또는 지정된 원격 DB 프로필을 사용하고, GitHub repo가 없으면 SSH 파일 조회를 우선한다.
핵심 도메인: 종목·지표·뉴스·분석 모델·백테스트·추천 근거·대시보드·계좌/자산 표시·투자 유의사항이다.
고위험 영역: 수익률·승률·AUC·추천 품질을 미검증 수치로 단정하는 것, 실계좌/주문 영향, 법적 고지 누락, 사용자별 권한 필터 누락, 데이터 최신성 착오가 위험하다.
필수 확인: 원천 데이터 날짜, row count, 결측/중복, 계산식, 샘플 종목, API 응답, 화면 표시, 법적 문구를 확인한다.
완료 기준: DB/산식/샘플 결과와 UI 반영을 함께 보고한다. 성능 개선은 측정 기간·데이터셋·비교 기준이 있을 때만 확정한다.
금지: 투자 성과 보장 표현, 검증 없는 백테스트 수치, 실거래 영향 가능 변경의 무승인 배포를 금지한다.$$::text
    ),
    (
        'project-kis-context',
        'L2 Project - KIS / 자동매매 시스템',
        ARRAY['KIS']::text[],
        10,
        $$## L2 Project / KIS 자동매매 시스템
프로젝트 정체성: KIS는 한국투자증권 연동 자동매매와 계좌·주문·체결·포지션 관리를 다루는 고위험 금융 운영 시스템이다.
서버·경로·DB 계약: 서버211의 `/root/kis-autotrade-v4`를 우선 사용한다. 프로젝트 DB는 `query_project_database(project='KIS')` 또는 운영 DB 프로필로 조회하고, 파일은 SSH 원격 조회를 우선한다.
핵심 도메인: OAuth/token, 계좌 잔고, 보유종목, 주문 생성·취소, 체결 동기화, 전략 실행, 리스크 제한, 스케줄러, 브릿지/허브 통신이다.
고위험 영역: 실계좌 주문, 주문 중복, 토큰/키 노출, 장중 장애, 계좌/수익률 계산 오류, 테스트 코드가 실주문 API를 호출하는 상황이다.
필수 확인: 시장 시간, dry-run 여부, 주문 관련 feature flag, 계좌 원장과 API 응답, 최근 주문 로그, 에러 로그, 전략 실행 상태를 먼저 확인한다.
완료 기준: 주문 영향이 없는 변경은 테스트/로그/API 응답으로, 주문 영향 가능 변경은 CEO 승인과 dry-run 검증으로 보고한다.
금지: 승인 없는 실거래 주문 실행, 민감 키 노출, 검증 없는 계좌 잔고·수익률 보고, 장중 무단 재시작을 금지한다.$$::text
    ),
    (
        'project-ntv2-context',
        'L2 Project - NTV2 / NewTalk V2',
        ARRAY['NTV2','NT']::text[],
        10,
        $$## L2 Project / NTV2 NewTalk V2
프로젝트 정체성: NTV2는 NewTalk V2 소셜 플랫폼으로 계정, 프로필, 피드, 게시글, 댓글, 메시지, 상품, 주문, 결제, 업로드, 관리자 기능을 포함한다.
서버·경로·DB 계약: 서버114의 `/var/www/newtalk`를 우선 사용한다. GitHub repo가 없으면 SSH 파일 조회를 우선하고, DB는 `query_project_database(project='NTV2')` 또는 지정 DB 프로필을 사용한다.
핵심 도메인: 인증·인가, 사용자 데이터 경계, 게시/피드, 미디어 업로드, 알림, 상품/주문/결제, 관리자 권한, 모바일 UI다.
고위험 영역: IDOR, 개인정보 노출, 결제/주문 정합성, webhook 검증 누락, 파일 업로드 취약점, 캐시로 인한 권한 우회, 모바일 레이아웃 깨짐이다.
필수 확인: user_id/project_id 필터, 권한 미들웨어, 주요 테이블 전후 상태, 결제 webhook 로그, 업로드 저장 경로, API 응답, 모바일 화면 영향 범위를 확인한다.
완료 기준: 권한 없는 접근 차단, 핵심 사용자 경로, DB 전후 상태, 화면 렌더링, health/log 안정성 중 해당 항목을 실측한다.
금지: 개인정보/토큰 출력, 승인 없는 결제 데이터 변경, 마이그레이션 없는 스키마 가정, 검증 없는 배포 완료 보고를 금지한다.$$::text
    ),
    (
        'project-sf-context',
        'L2 Project - SF / ShortFlow 영상 자동화',
        ARRAY['SF']::text[],
        10,
        $$## L2 Project / SF ShortFlow 영상 자동화
프로젝트 정체성: SF는 숏폼 영상 자동화 시스템으로 원본 수집, 스크립트, TTS, 자막, 이미지/영상 합성, 인코딩, 업로드, 작업 큐를 다룬다.
서버·경로·DB 계약: 서버114의 `/data/shortflow`를 우선 사용하며 서비스 포트는 운영 지시의 7916 계약을 따른다. GitHub repo가 없으면 SSH 파일 조회와 원격 명령으로 확인한다.
핵심 도메인: 영상 파이프라인, ffmpeg, TTS/STT, 자막 싱크, 썸네일, 미디어 저장소, 외부 API quota, 작업 재시도, 산출물 URL이다.
고위험 영역: quota 소진, 중복 생성 비용, 긴 작업 timeout, 해상도/코덱 불일치, 자막 싱크 오류, 저작권/출처 누락, CDN/파일 경로 불일치다.
필수 확인: 작업 상태, 로그, 원본/산출물 파일 존재, duration, resolution, codec, 파일 크기, URL 접근성, 실패 재시도 횟수, 외부 API quota를 확인한다.
완료 기준: 파일 존재만으로 완료 처리하지 말고 재생 가능성, 길이, 해상도, 자막 싱크, 썸네일, 업로드 URL을 함께 보고한다.
금지: quota 비용이 큰 재생성 반복, 원본 삭제, 검증 없는 완료 보고, 저작권 위험 산출물 무검수 배포를 금지한다.$$::text
    ),
    (
        'project-nas-context',
        'L2 Project - NAS / 이미지 처리 운영',
        ARRAY['NAS']::text[],
        10,
        $$## L2 Project / NAS 이미지 처리 운영
프로젝트 정체성: NAS는 이미지 처리, 변환, 보관, 품질 검수, 외부 제공 산출물을 다루는 운영 프로젝트다. 서버 계약은 Cafe24/NAS 운영 환경과 현재 지시를 우선한다.
서버·경로·DB 계약: AADS 내부 경로로 임의 추정하지 말고, NAS 작업은 사용 가능한 원격 도구, 업로드 파일, 저장소 경로, Cafe24/NAS 접근 계약을 먼저 확인한다.
핵심 도메인: 원본 이미지, 리사이즈, 포맷 변환, 압축, 썸네일, 메타데이터, EXIF, 파일명 규칙, 저장 위치, 공개 URL, 배치 처리다.
고위험 영역: 원본 손상, EXIF 개인정보 노출, 색상/비율 왜곡, 중복 덮어쓰기, 대용량 배치 비용, 공개 URL 오노출이다.
필수 확인: 원본 백업, 파일 수, 해상도, 포맷, 용량, 체크섬 또는 샘플 비교, 공개/비공개 권한, 결과 URL 접근성을 확인한다.
완료 기준: 산출물 개수, 실패 파일, 샘플 이미지 품질, 원본 보존 여부, 저장 경로, 롤백 가능성을 보고한다.
금지: 원본 무백업 덮어쓰기, 개인정보 포함 EXIF 무검수 공개, 대량 삭제, 경로 미확인 상태의 배치 실행을 금지한다.$$::text
    ),
    (
        'project-remote-access-contract',
        'L2 Project - Remote Access Contract / 원격 접근 계약',
        ARRAY['KIS','GO100','SF','NTV2','NT','NAS']::text[],
        20,
        $$## L2 Project / 원격 접근 계약
적용 범위: AADS 외부 프로젝트 또는 별도 서버에 있는 프로젝트 파일·DB·서비스를 다룰 때 적용한다. active_project와 명시 프로젝트가 다르면 명시 프로젝트를 우선 확인하고, 불명확하면 질문한다.
서버 계약: KIS/GO100은 서버211의 `/root/kis-autotrade-v4`, SF는 서버114의 `/data/shortflow`, NTV2/NT는 서버114의 `/var/www/newtalk`, NAS는 Cafe24/NAS 운영 계약을 따른다.
도구 우선순위: GitHub repo가 없거나 최신 원격 상태가 중요하면 `read_remote_file`, `list_remote_dir`, `run_remote_command`, `query_project_database`를 우선한다. AADS는 로컬 파일과 GitHub 모두 가능하지만 현재 workspace 기준을 확인한다.
필수 확인: 파일 수정 전 원문을 읽고, DB 수치는 SELECT/EXPLAIN 등 읽기 쿼리로 확인한다. 원격 명령은 단일 명령을 선호하고, 위험 명령·대량 삭제·시크릿 출력은 금지한다.
완료 기준: 어떤 서버/경로/DB에서 확인했는지, 변경 파일과 백업 여부, 테스트·로그·헬스체크 결과, 미검증 항목을 보고한다.
충돌 처리: 경로가 운영 지시와 다르면 현재 지시를 우선하고, 이전 L2·메모리·문서와 충돌한 사실을 보고한다.$$::text
    )
)
INSERT INTO prompt_assets (
    slug,
    title,
    layer_id,
    content,
    workspace_scope,
    intent_scope,
    target_models,
    role_scope,
    priority,
    enabled,
    created_by,
    created_at,
    updated_at
)
SELECT
    slug,
    title,
    2,
    content,
    workspace_scope,
    ARRAY['*']::text[],
    ARRAY['*']::text[],
    ARRAY['*']::text[],
    priority,
    TRUE,
    'migration_068',
    NOW(),
    NOW()
FROM upserts
ON CONFLICT (slug) DO UPDATE
SET title = EXCLUDED.title,
    layer_id = EXCLUDED.layer_id,
    content = EXCLUDED.content,
    workspace_scope = EXCLUDED.workspace_scope,
    intent_scope = EXCLUDED.intent_scope,
    target_models = EXCLUDED.target_models,
    role_scope = EXCLUDED.role_scope,
    priority = EXCLUDED.priority,
    enabled = TRUE,
    updated_at = NOW();

COMMIT;
