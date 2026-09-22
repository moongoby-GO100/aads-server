-- CEO instruction: split complex browser learning into independently verified recipes.
-- Rollback: disable only global-browser-modular-recipe-contract; no recipe deletion.
BEGIN;
INSERT INTO prompt_assets
 (slug,title,layer_id,content,workspace_scope,role_scope,intent_scope,target_models,priority,enabled,created_by)
VALUES
 ('global-browser-modular-recipe-contract','L1 Global / 단계별 브라우저 레시피 등록·재사용',1,
 $policy$## 복잡한 웹페이지 분석·레시피 운영
1. 복잡한 웹 작업은 독립적으로 재실행 가능한 작은 목적 단위로 나눈다. 로그인은 화면 진입, Vault 입력, 제출·성공 확인을 각각 분리하고 조회·다운로드·집계는 별도로 만든다.
2. 실행 전에 smart_browser(action=list)로 기존 승인본을 찾는다. 각 조각에는 대상 도메인, 실행 레인·업무 키, 시작 조건, 성공 조건, 선행 레시피 이름·버전, 화면 증거와 실패 복귀 지점을 남긴다. 지원하지 않는 metadata는 DB 핸드오버에 기록한다.
3. 실제 화면에서 성공한 조각만 register_e2e로 등록한다. navigate 도구 성공이나 HTTP 200은 로그인 성공이 아니다. Access Denied·CAPTCHA·로그인 오류 화면을 성공 레시피로 등록하지 않는다. 비밀번호 입력 확인은 값 없이 filled/empty만 검사하고 증거에는 비밀값을 남기지 않는다.
4. 비밀번호 관리자/Vault의 정확한 credential을 도메인·테넌트별로 확인한다. 레시피에는 secret 변수와 credential_scope 참조만 저장한다. 채팅·레시피·보고서에 원문 비밀번호·쿠키·토큰을 넣지 않는다. 목록 필터의 미매칭을 전체 Vault 부재로 단정하지 않는다.
5. 실패하면 URL·화면·로그인 상태·레인을 다시 확인하고 시작 조건을 만족하는 마지막 승인 조각부터 재생한다. 인증이 유지되면 로그인 제출을 반복하지 않는다. 일시 오류만 제한 재시도하고 잘못된 비밀번호·CAPTCHA·접근 차단은 원인을 분리해 중단·사용자 복구 경로를 제공한다.
6. 조각별 등록 대기/승인/재생 성공을 구분한다. 등록 승인 및 외부 쓰기 실행 게이트를 유지한다. 다른 세션에서 목록 발견→지정 레인 재생→성공 화면 확인까지 검증한 경우에만 세션 간 활용 완료로 보고한다.
7. PC와 서버 Playwright의 성공 여부는 별개다. 한국 IP 경유가 성공해도 사이트 차단 해제나 로그인 성공으로 보고하지 않는다. 프롬프트 운영 규칙과 코드로 강제되는 체크포인트 자동화는 구분한다.
$policy$,ARRAY['*'],ARRAY['*'],ARRAY['*'],ARRAY['*'],7,true,'ceo-request:7fb5f50a')
ON CONFLICT (slug) DO UPDATE SET content=EXCLUDED.content,title=EXCLUDED.title,
 workspace_scope=EXCLUDED.workspace_scope,role_scope=EXCLUDED.role_scope,
 intent_scope=EXCLUDED.intent_scope,target_models=EXCLUDED.target_models,
 priority=EXCLUDED.priority,enabled=true,updated_at=now();
COMMIT;
