-- 136: Persist user-centered product planning and UI/UX design contract in L1 prompt assets.

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
    created_by
)
VALUES (
    'global-user-centered-product-ux-contract',
    'L1 Global / 사용자 중심 기획·디자인 원칙',
    1,
$$## L1 Global / 사용자 중심 기획·디자인 원칙
기획·디자인·프론트엔드·앱·관리 화면 작업은 구현 편의보다 실제 사용자의 첫 진입, 반복 사용, 실패 복구, 확인 가능성을 우선한다.

필수 적용:
1. 사용자가 앱이나 화면을 열었을 때 가장 자주 쓰는 핵심 업무로 바로 진입하게 설계한다. 설정, 페어링, 권한 요청, 관리자 기능은 보조 경로로 분리한다.
2. 로그인 세션 만료, 권한 부족, 네트워크 실패, 연결 끊김은 막다른 화면이 아니라 다음 행동이 분명한 복구 흐름으로 연결한다.
3. 사용자가 자동화 결과를 신뢰할 수 있도록 현재 상태, 진행 중 작업, 마지막 오류, 승인 필요 항목, 재시도 버튼을 같은 맥락에서 확인할 수 있게 한다.
4. 관리자 설정은 일반 사용자 핵심 화면을 방해하지 않도록 Admin/Settings 메뉴에 배치하고, 일반 실행 화면에는 최소한의 제어만 둔다.
5. 모바일은 한 손 조작, 큰 터치 영역, 텍스트 줄바꿈, 세션 복구, 앱 재실행 후 상태 보존을 우선 검증한다.
6. 버튼·메뉴·라벨은 내부 시스템명보다 사용자가 이해하는 업무명으로 표시한다. 내부 식별자는 API, 로그, 개발 문서에만 노출한다.
7. 기획 보고와 디자인 제안에는 대상 사용자, 첫 실행 경로, 반복 사용 경로, 실패 복구 경로, 검증 기준을 반드시 포함한다.

완료 보고 기준:
- 코드 변경만으로 완료라고 하지 말고, 사용자 첫 화면/주요 route/API/권한·세션 복구 흐름의 검증 여부를 분리해 보고한다.
- 화면 검증이 필요한 작업은 스크린샷 또는 HTTP/API/프로세스 폴백 검증 근거를 남긴다.$$,
    ARRAY['*'],
    ARRAY['planning', 'design', 'frontend', 'code_modify', 'mobile_app', 'ui_review', '*'],
    ARRAY['*'],
    ARRAY['*'],
    8,
    TRUE,
    'migration:136'
)
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
