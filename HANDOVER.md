# AADS HANDOVER

## 현재 진행 상태 (2026-05-18 16:06 KST) - 채팅 응답 사라짐/과거 partial 노출 재발 방지
- 배경: CEO가 채팅창에서 응답이 사라지고 이전에 조치했던 partial/중단 버블 문제가 반복 재발한다고 보고했다.
- 원인: 기존 개선은 `_mark_execution_interrupted()` 경로에는 적용됐지만, 새 응답 시작 전 stale `streaming_placeholder` 정리 경로와 resume task callback 경로가 별도 SQL로 남아 공통 중단 처리 함수를 우회했다. 이 때문에 일부 partial이 `intent=NULL, model_used='interrupted'`로 visible assistant 버블이 되거나, placeholder가 있으면 fallback INSERT가 생략되는 경합이 남았다.
- 조치: `app/services/chat_service.py`에서 stale placeholder 정리를 `_mark_execution_interrupted()`로 통합하고, execution 없는 legacy placeholder도 `interrupted_partial`로 숨긴 뒤 별도 visible fallback 안내만 남기도록 변경했다. `app/main.py`의 resume task cancelled/escaped callback도 직접 INSERT 대신 `_mark_execution_interrupted()`를 호출하도록 변경했다.
- DB 보정: `model_used='interrupted' AND intent IS NULL`이면서 경고문이 아닌 visible partial 8건을 `intent='interrupted_partial'`로 보정했다. 보정 후 visible partial 0건, stale streaming placeholder 0건 확인.
- 검증/배포: `python3 -m py_compile app/services/chat_service.py app/main.py` 통과. `bash /root/aads/aads-server/deploy.sh bluegreen`으로 API active를 `8100(aads-server)`로 전환했고 `/health` OK, active/standby 컨테이너 코드 반영을 확인했다.
- 주의: 이번 조치 파일은 `app/main.py`, `app/services/chat_service.py`이며, 워크트리에는 이번 작업과 무관한 기존 미커밋 파일들이 다수 남아 있다.

## 현재 진행 상태 (2026-05-18 11:19 KST) - Dashboard BG 배포/standby 동기화 보강
- 배경: CEO가 코드 수정 후 UI 반영까지 blue-green 무중단 배포와 전환 후 BG 자동동기화가 정상 작동하지 않는 부분을 전수 검수하고 개선 조치하라고 지시했다.
- 원인: 대시보드 배포는 서버 compose(`/root/aads/aads-server/docker-compose.prod.yml`)를 canonical로 사용하지만, 과거 `/root/aads/aads-dashboard/docker-compose.yml` 경로의 잔여 컨테이너가 있으면 standby 재빌드 단계에서 컨테이너명 충돌 가능성이 있었다. 또한 `UNKNOWN` QA 결과를 성공처럼 기록하는 보고 오류가 있었다.
- 조치: `/root/aads/aads-dashboard/deploy.sh`에 배포 lock(`/tmp/aads-dashboard-deploy.lock`), 외부 compose 잔여 컨테이너 정리, `AADS_RELEASE_SHA` 주입/검증, QA `UNKNOWN` 미통과 처리를 추가했다. `docker-compose.prod.yml`의 dashboard blue/green 서비스에도 `AADS_RELEASE_SHA` env를 추가했다.
- 조치: `scripts/deploy_dashboard.sh`, `scripts/dashboard-rebuild.sh`는 direct compose rebuild를 중단하고 canonical `/root/aads/aads-dashboard/deploy.sh`로만 연결하도록 변경했다. 서버 `deploy.sh`도 프론트 QA `UNKNOWN`을 전체 검증 통과로 표현하지 않고 `frontend_qa=unknown_non_blocking`으로 분리 보고한다.
- 검증: `bash -n deploy.sh`, `bash -n /root/aads/aads-dashboard/deploy.sh`, `bash -n scripts/deploy_dashboard.sh`, `bash -n scripts/dashboard-rebuild.sh`, `docker compose -f docker-compose.prod.yml config --quiet`, `nginx -t` 통과. 실제 `bash /root/aads/aads-dashboard/deploy.sh` 실행 결과 green 전환, 외부 `/login` 200, standby blue 재빌드, 커밋/푸시 후 재배포까지 수행해 양 슬롯 release `f2e3b4c56b88` 확인. QA API는 `UNKNOWN`을 반환해 통과가 아니라 미확정으로 기록했다.
- 주의: QA API가 `UNKNOWN`을 반환하는 원인은 별도 개선 대상이다. 이번 조치 범위는 배포/전환/standby 동기화와 오보고 방지다.

## 현재 진행 상태 (2026-05-16 11:00 KST) - 한루아 기획서 스타일 프리셋 5종 시험 생성 완료
- 배경: CEO가 기획서에 정의된 스타일 프리셋 단계 기준으로 한루아 전신 승인 이후 프리셋 시험 이미지 생성을 이어가라고 지시했다.
- 조치: `scripts/generate_han_rua_doc_style_presets.py`로 기획서 기본 프리셋 5종(봄 데일리 내추럴, 여름 쿨톤, 가을 무드, 겨울 미니멀, 오피스 차분한 미소)을 각 2장씩 생성했다. 사용 모델은 Nano Banana 2 경로인 `gemini-3.1-flash-image-preview`다.
- DB 기록: `media_generation_jobs.id=333~342` 10건이 모두 `succeeded`이며, `ai_persona_references.id=311~320`으로 연결했다. `metadata.reference_set=han_rua_doc_style_preset`, `metadata.style_preset_name/style_preset_slug/trial_index`, `approval_recommended=true`, `approval_recommendation_rank=1~10`을 기록했다. 실제 승인값은 CEO 검토 전이므로 `is_approved=false`다.
- 갤러리: `scripts/export_gallery.py`, `app/api/image.py`, `app/static/gallery/index.html` 경로 기준으로 프리셋 메타(`reference_set`, `style_preset_name`, `style_preset_slug`, `style_preset_trial_index`)를 반환/표시하도록 반영했고, 정적 갤러리와 대시보드 공개 경로에 동기화했다. 접촉시트는 `https://aads.newtalk.kr/reports/gallery/han-rua-doc-style-preset-contact-sheet.jpg`다.
- 배포/검증: API blue 슬롯 `8100`, green 슬롯 `8102`, 공개 URL `https://aads.newtalk.kr/api/v1/image/gallery?limit=3` 모두 프리셋 메타를 반환한다. 공개 접촉시트와 `manifest.json`은 200 OK이며, manifest 기준 `han_rua_doc_style_preset` 10건을 확인했다.
- 주의: 이번 10장은 승인추천 상태이며 CEO 승인 전이다. 커밋/푸시는 아직 수행하지 않았다.

## 현재 진행 상태 (2026-05-16 09:52 KST) - 한루아 후면 전신 프리셋 보강
- 배경: CEO가 한루아 전신 프리셋 세트에 뒷모습 전신도 몇 컷 반영하라고 추가 지시했다.
- 조치: Nano Banana 2(`gemini-3.1-flash-image-preview`)로 89번 얼굴 시드를 strict identity source로 둔 후면 전신 4컷을 추가 생성했다. 구성은 정후면 1장, 후면 좌/우 3/4 각 1장, 후면 워킹 1장이다.
- DB 기록: 신규 `media_generation_jobs.id=317~320` 4건이 모두 `succeeded`이며, `ai_persona_references.id=271~274`로 연결했다. DB `ref_type` 체크 제약상 실제 컬럼은 `fullbody_turn/fullbody_walk`를 사용했고, 세부 후면 구분은 `metadata.rear_ref_type=fullbody_back/fullbody_back_turn_left/fullbody_back_turn_right/fullbody_back_walk`, `metadata.reference_set=han_rua_fullbody_swimfit_rear_preset`로 저장했다.
- 갤러리: `scripts/export_gallery.py`, `app/static/gallery/index.html`, `app/api/image.py`를 보강해 후면 세트 메타데이터와 "한루아 전신 프리셋(후면)" 트랙을 표시하도록 했다. 정적 갤러리와 대시보드 공개 경로에 동기화했다.
- 검증: `python3 -m py_compile scripts/export_gallery.py app/api/image.py` 통과, 갤러리 JS `node --check /tmp/gallery-script.js` 통과. 공개 `https://aads.newtalk.kr/reports/gallery/` 200 OK, `manifest.json` 200 OK, manifest 기준 후면 세트 4건 확인. 스크린샷 캡처는 로컬 CDP `localhost:9222` 응답 없음으로 실패했다.
- 주의: 후면 4컷은 승인추천(`approval_recommended=true`)으로 표시했지만 아직 CEO 승인 전이다. 커밋/푸시/정식 배포는 아직 수행하지 않았다.

## 현재 진행 상태 (2026-05-16 09:42 KST) - 한루아 수영복/핏 전신 프리셋 세트 생성
- 배경: CEO가 전신 이미지를 향후 프리셋으로 활용하려면 금지 조건보다 목적 설명이 중요하며, 몸매가 충분히 드러나는 복장 또는 수영복 등으로 생성하라고 추가 지시했다.
- 안전 범위: 한루아는 DB 기준 24세 성인(`ai_personas.id=3`)으로 확인했다. 프롬프트에는 "성인 24세", "전신 프리셋/가상 피팅용 체형·비율 확인", "비선정적 패션 카탈로그", "노출/란제리/성적 포즈 금지"를 명시했다.
- 조치: 89번 얼굴 시드(`media_generation_jobs.id=89`)를 strict identity source로 사용해 Nano Banana 2(`gemini-3.1-flash-image-preview`)로 `han_rua_fullbody_swimfit_preset` 30장을 생성했다. 복장은 원피스 수영복, 피트니스 바디수트, 요가 유니타드, 탱크 바디수트+바이크 쇼츠 등 체형·비율 확인 가능한 비선정적 전신 프리셋 기준으로 구성했다.
- DB 기록: 정상 reference 30건, 승인추천 20건, CEO 승인 0건이다. 허용 `ref_type` 제약에 맞춰 `fullbody_stand/turn/walk/lean`으로 저장했고, 세트 구분은 `metadata.reference_set=han_rua_fullbody_swimfit_preset`, `swimfit_preset=true`로 기록했다.
- 갤러리: `scripts/export_gallery.py`와 `app/static/gallery/index.html`을 보강해 `reference_set`, `reference_outfit`을 manifest에 포함하고, 카드 라벨을 "한루아 전신 프리셋(수영복/핏)"으로 표시한다. 접촉시트는 `https://aads.newtalk.kr/reports/gallery/han-rua-fullbody-swimfit-preset-contact-sheet.jpg`로 배치했다.
- 검증: `python3 -m py_compile app/api/image.py scripts/export_gallery.py` 통과, 갤러리 JS `node --check` 통과. 공개 URL `https://aads.newtalk.kr/reports/gallery/`, `manifest.json`, 접촉시트 모두 200 OK. DB 기준 `han_rua_fullbody_swimfit_preset` reference 30건/승인추천 20건/승인 0건 확인.
- 주의: 첫 배치 스크립트가 `image_url NOT NULL` 제약을 반영하지 못해 중복 실패 job 22건이 남았다. 정상 갤러리/승인 대상은 `succeeded + reference_set`으로 연결된 30건만 사용한다. 커밋/푸시/정식 배포는 아직 수행하지 않았다.

## 현재 진행 상태 (2026-05-16 09:25 KST) - 한루아 전신 프리셋 생성/승인추천 표시
- 배경: CEO가 한루아 89번 이미지 기반 멀티앵글 얼굴 승인 후 다음 단계로 전신컷을 요청했고, 전신 프리셋 용도라 체형·비율이 확인되는 복장이 필요하다고 추가 지시했다.
- 확인: 기존 한루아 전신 30장은 생성/갤러리 반영은 됐지만 검은 재킷/후디 중심이라 전신 프리셋의 체형 확인 기준에는 부족했다.
- 조치: 89번 얼굴 시드를 strict identity source로 사용해 Nano Banana 2(`gemini-3.1-flash-image-preview`)로 fitted neutral base outfit 전신 프리셋 30장을 추가 생성했다. 생성 중 새 `ref_type=fullbody_preset_*`가 DB 체크 제약에 걸려 실패 처리됐으나, 반환된 이미지가 `media_generation_jobs`에 보존돼 있어 `fullbody_stand/turn/walk/lean` 허용 타입으로 reference를 복구하고 `metadata.reference_set=han_rua_fullbody_preset`, `body_preset=true`로 구분했다.
- 조치: 접촉시트 육안 검수 기준으로 20장을 `approval_recommended=true`로 표시했다. 혼동 방지를 위해 한루아의 과거 얼굴/기존 전신 추천 플래그는 해제하되, 이미 승인된 얼굴 20장의 `is_approved=true` 값은 유지했다.
- 공개 확인: `https://aads.newtalk.kr/reports/gallery/`와 `manifest.json`에 전신 프리셋 30장, 승인추천 20장이 반영됐다. 접촉시트는 `https://aads.newtalk.kr/reports/gallery/han-rua-fullbody-preset-contact-sheet.jpg`로 확인 가능하다.
- 미완료/주의: 전신 프리셋 20장은 아직 CEO 승인 전이다. 승인 후에는 같은 인물성 검증/스타일 프리셋 생성 단계로 넘어가야 한다. 커밋/푸시/정식 배포는 아직 수행하지 않았다.

## 현재 진행 상태 (2026-05-16 08:28 KST) - Kimi K2.6/DeepSeek V4 Pro 적용 및 BG 배포 중단 원인 보강
- 배경: CEO가 Kimi K2.6 채팅 연결, DeepSeek V4 Pro 러너 공식 ID 통일, L/XL 비교, 그리고 blue-green 중 API가 끊긴 원인 확인을 요청했다.
- 원인: `deploy.sh bluegreen`은 비활성 슬롯 빌드/헬스 확인 후 nginx 전환하는 구조는 맞지만, 전환 직후 old slot standby 동기화가 즉시 재빌드될 수 있었다. 이때 `/api/v1/ops/active-streams` 조회 실패가 `0`으로 처리되면 기존 SSE/채팅 스트림이 남아 있어도 old slot이 재시작되어 끊김/502가 발생할 수 있었다.
- 조치: `deploy.sh`에서 active-streams 조회 실패를 `unknown`으로 처리해 busy로 간주되게 했고, old slot standby sync 전에 기본 600초 grace wait를 추가했다. PC Agent `graceful-shutdown`도 전환 직후가 아니라 drain 이후 재빌드 직전에 호출되도록 순서를 변경했다.
- 조치: `litellm-config.yaml`에 `kimi-k2.6`, `deepseek-v4-pro`, `deepseek-v4-flash`를 로드했고, DeepSeek V4 Pro/Flash에는 `thinking.type=disabled`를 설정해 본문 `content`가 비지 않도록 했다. `app/services/model_selector.py`와 `app/services/model_registry.py`도 공식 실행 ID 기준으로 정리했다.
- 검증: `bash -n deploy.sh` 통과. `python3 -m pytest tests/unit/test_model_registry.py tests/unit/test_model_selector_dynamic_routing.py -q` 결과 30개 통과. LiteLLM 실호출 기준 `kimi-k2.6`은 `OK`, `deepseek-v4-pro`는 thinking 비활성 후 `OK` 본문 반환 확인.
- 배포: `docker restart aads-litellm`로 LiteLLM 설정을 반영했고, `bash /root/aads/aads-server/deploy.sh bluegreen` 실행 결과 6단계 검증 통과. nginx API active는 `8102(aads-server-green)`, backup은 `8100(aads-server)`이며 `https://aads.newtalk.kr/api/v1/health`가 `status=ok`를 반환했다.
- 주의: old blue 슬롯 standby 동기화는 600초 grace wait 후 백그라운드에서 진행된다. 즉시 active 서비스는 green으로 정상 제공 중이며, 커밋/푸시는 별도 수행 여부를 최종 보고에서 확인해야 한다.
- 추가 확인(2026-05-16 08:32 KST): 실제 적용 파일 `/etc/nginx/conf.d/aads-upstream.conf`와 상태 파일은 API green `8102`, dashboard green `3101` active로 일치했다. 저장소 사본 `nginx-aads-upstream.conf`가 blue active로 뒤처져 있어 실제 적용 파일과 동일하게 보정했다. `diff -u nginx-aads-upstream.conf /etc/nginx/conf.d/aads-upstream.conf` 출력 없음, `nginx -t` 성공, 외부 `/api/v1/health` 200 OK 확인.

## 현재 진행 상태 (2026-05-16 08:07 KST) - Kimi K2.6 채팅 연결 + DeepSeek V4 Pro 공식 ID 통일
- 배경: CEO가 Kimi K2.6 채팅 즉시 연결, DeepSeek V4 Pro의 노출/실행 모델명 통일, L/XL 규모 코딩 비교를 요청했다.
- 조치: `app/services/model_selector.py`에 `kimi-k2.6`을 Kimi 실행 허용 목록에 추가하고, DeepSeek V4 Pro/Flash가 legacy alias(`deepseek-reasoner`, `deepseek-chat`)가 아닌 공식 API ID(`deepseek-v4-pro`, `deepseek-v4-flash`)로 LiteLLM에 전달되도록 보정했다.
- 조치: `app/services/model_registry.py`의 DeepSeek 실행 ID 정책도 공식 ID 기준으로 맞췄고, `litellm-config.yaml`에 `kimi-k2.6`, `deepseek-v4-pro`, `deepseek-v4-flash`를 추가했다. DB 기준 `chat_model_preferences`에는 `kimi/kimi-k2.6` order 45, `deepseek/deepseek-v4-pro` order 50이 노출된다.
- 조치: `runner_model_config` 기준 L/XL 후보에 `litellm:deepseek-v4-pro`가 포함되어 러너가 공식 ID로 호출할 수 있다.
- 검증: `python3 -m pytest tests/unit/test_model_registry.py tests/unit/test_model_selector_dynamic_routing.py -q` 결과 30개 통과. LiteLLM `/v1/models`에 `kimi-k2.6`, `deepseek-v4-pro`, `deepseek-v4-flash` 노출 확인. LiteLLM 실호출에서 `kimi-k2.6` 0.58초, `deepseek-v4-pro` 0.95초로 `ok` 응답 확인.
- 비교: `/tmp/aads_deepseek_gpt_opus_lxl_benchmark_20260516.json`에 L/XL read-only 비교 결과를 저장했다. L/XL 모두 DeepSeek V4 Pro, GPT-5.5, Claude Opus 4.7 호출 성공.
- 주의: 이번 변경은 로컬 워크트리 및 실행 컨테이너/DB에 반영됐으나, 커밋/푸시/정식 blue-green 배포는 아직 수행하지 않았다. 워크트리에 다른 미커밋 변경이 다수 있어 최종 커밋 시 관련 파일만 분리 스테이징해야 한다.

## 현재 진행 상태 (2026-05-15 17:45 KST) - Google 이미지 모델 등록/실시간 갤러리 보강
- 배경: CEO 지시로 OpenAI 이미지 경로는 제외하고 Google 이미지 모델(Nano Banana/Nano Banana 2/Nano Banana Pro/Imagen 4 계열)을 AADS에 등록해야 했다. 동시에 생성 결과를 모델별·프롬프트별로 실시간 확인 가능한 공개 갤러리가 필요했다. 기존 Imagen 4.0 50장은 CEO 선택 1-B안에 따라 삭제하지 않고 `B안 보존본`으로 분리했다.
- 조치: `app/services/media_generation_service.py`에서 Gemini Pro 이미지 모델의 잘못된 ID(`gemini-3.1-pro-image-preview`)를 공식 ID(`gemini-3-pro-image-preview`)로 정정하고, legacy alias를 canonical ID로 매핑하도록 보강했다. 기본 이미지 라우트가 OpenAI 비활성 상태에 걸리면 Imagen/Gemini 경로로 자동 폴백하도록 수정했다.
- 조치: `app/api/image.py`의 공개 갤러리 API가 `has_image`, `image_url`을 반환하도록 확장했다. `app/static/gallery/index.html`은 실시간 API 우선, `manifest.json` 폴백, 모델/페르소나/트랙 필터, 한글 카드 요약, 프롬프트 보기, `Imagen 4.0` 초도 결과의 `B안 보존본` 분리를 지원하도록 전면 교체했다.
- 조치: `app/api/llm_models.py` seed와 `migrations/096_google_image_models_and_routes.sql`을 추가해 `gemini-2.5-flash-image`, `gemini-3.1-flash-image-preview`, `gemini-3-pro-image-preview`, `imagen-4.0-{standard,fast,ultra}`를 영속 등록하고, OpenAI 기본 이미지/edit_image 라우트는 CEO 지시에 맞춰 비활성 처리하도록 준비했다.
- 확인: DB 실측 기준 `llm_api_keys.OPENAI_API_KEY is_active=false`, `GEMINI_API_KEY/GEMINI_API_KEY_2 is_active=true`이며, `media_generation_jobs` 최근 기록은 `Imagen 4.0` 성공 50건 이후 `gemini-3.1-flash-image-preview`/`gemini-3.1-pro-image-preview` 실패 3건이 남아 있었다. 이는 Pro 이미지 모델 ID 오기와 미반영 코드 상태가 원인이었다.
- 적용: `migrations/096_google_image_models_and_routes.sql`를 Postgres에 재적용해 Google/Gemini 이미지 모델 6종을 `media_image` active/selectable로 등록했고, `model_routing_preferences`에서 OpenAI `gpt-image-2` image/edit_image 경로를 비활성화했다. image 기본값은 `google/imagen-4.0-generate-001`이다.
- 검증: `app.services.media_generation_service`, `app.api.image`, `app.api.llm_models` hot-reload 성공. `generate_image`로 A안 `gemini-3.1-flash-image-preview` job `media-2cfeeba17e9e4d8c`, C안 `gemini-3-pro-image-preview` job `media-ecd863c179ad4a35`가 각각 성공했고 DB `media_generation_jobs`에 저장됐다.
- 공개 확인: `https://aads.newtalk.kr/reports/gallery/` 200 OK, `https://aads.newtalk.kr/reports/gallery/manifest.json` 200 `application/json`, A안/C안 이미지 직접 URL도 200 `image/jpeg` 확인. `scripts/export_gallery.py`는 data URI MIME에 따라 `.jpg/.png/.webp` 확장자를 쓰도록 보정했다.
- 미완료: FastAPI 신규 `/api/v1/image/gallery` 라우트는 서버 route table 재등록 전이라 public API는 아직 404/401 경로가 남아 있다. 현재 CEO 실시간 확인은 정적 manifest 기반 갤러리로 정상 제공한다. 커밋/푸시/정식 배포는 아직 미실행.

## 현재 진행 상태 (2026-05-14 12:08 KST) - 68/211/114 Codex CLI 인증 반영
- 배경: CEO가 각 서버에서 OpenAI/Codex OAuth 승인을 완료한 뒤, 서버별 `~/.codex/auth.json`에 인증 코드가 실제 반영됐는지와 독립 refresh_token 보유 여부 확인이 필요했다.
- 조치: 68(AADS), 211(KIS/GO100), 114(SF/NTV2)에서 `codex login status`, `auth.json` 메타데이터, access_token 만료시각, refresh_token SHA-256 prefix를 토큰 원문 없이 확인했다.
- 확인: 68 refresh hash `402feecaeab86c90`, 211 refresh hash `276586fe417f1d44`, 114 refresh hash `39c79c4780b50dca`로 모두 달라 서버별 독립 refresh_token 보유 상태다. access_token 만료는 각각 2026-05-24 11:33:48/11:37:00/11:38:50 KST로 갱신됐다.
- 검증: `codex exec --skip-git-repo-check` 최소 호출이 68=`OK-68`, 211=`OK-211`, 114=`OK-114`로 성공했다. MCP 원격 실행은 211에서 50초 타임아웃, 114에서 transport closed가 있었으나 직접 SSH 대안 검증으로 성공 확인했다.
- 주의: 211/114 `codex exec` 실행 시 `bubblewrap` 미설치 경고가 출력됐고 bundled bubblewrap fallback으로 계속 실행됐다. 인증 문제는 아니지만 추후 패키지 보강 대상으로 남긴다.

## 현재 진행 상태 (2026-05-13 16:25 KST) - AADS Blue-Green 미진 항목 즉시 보정
- 배경: AADS 무중단 배포 전수 검수 후 남은 권장/미진 항목을 재확인했다. 실측 기준 nginx upstream은 API green `8102` active, dashboard green `3101` active였지만 `.active_port/.active_container` marker는 API blue `8100/aads-server`로 어긋나 있었다.
- 조치: `.active_port=8102`, `.active_container=aads-server-green`으로 marker를 nginx upstream 기준에 맞게 정합했다. active green에는 실행 스트림 4건이 있어 컨테이너 재빌드/재시작은 수행하지 않았다.
- 조치: `docker-compose.prod.yml`의 `aads-server-green`, `aads-dashboard-green` restart policy를 `unless-stopped`로 변경했다. 런타임에도 `docker update --restart unless-stopped`를 적용해 재부팅/daemon 재시작 후 standby 슬롯이 사라지는 문제를 줄인다.
- 확인: `deploy.sh`는 upstream의 non-backup 라인을 우선 읽어 active marker를 보정하고, BG 전환 후 `sync_standby_slot_after_drain`로 old API 슬롯을 drain 후 재빌드한다. dashboard `deploy.sh`도 전환 후 이전 슬롯을 재빌드해 warm standby 동기화한다.
- 검증: `docker compose -f docker-compose.prod.yml config`, `nginx -t`, `127.0.0.1:8100/8102` API health, `127.0.0.1:3100/3101` dashboard `/login`, 외부 `https://aads.newtalk.kr/api/v1/health`와 `/login` 모두 `200` 확인. restart policy inspect에서 green API/dashboard 모두 `unless-stopped`, marker는 `8102/aads-server-green`으로 upstream active와 일치한다.

## 현재 진행 상태 (2026-05-13 13:31 KST) - AADS-185 chat/settings model classification UI
- 배경: CEO 요청으로 chat 모델 드롭다운에서 provider/type 구분을 즉시 강화하고, 같은 분류를 admin settings 모델 UI에도 반영해야 했다. 기존 chat UI는 registry row 위에 static selector label이 우선되는 구간이 있었고, runner settings는 hard-coded grouped model list에 의존하고 있었다.
- 조치: `aads-dashboard/src/lib/modelRegistryPresentation.ts`를 추가해 registry 기반 provider/category/family 표시, legacy stored value(`codex:*`, `litellm:*`) 해석, grouped label 생성 로직을 공통화했다.
- 조치: `aads-dashboard/src/app/chat/page.tsx`에서 active registry row의 `display_name/provider/family/category/execution_model_id`를 우선 사용하도록 selector option 빌드를 조정했다. chat 모델 select는 native `optgroup`으로 provider/category 단위 그룹을 만들고, 닫힌 상태에서도 `Codex/Gemini/DeepSeek/Claude/OpenAI/Local` 분류가 보이도록 option text에 classification을 붙였다. static `MODEL_OPTIONS`는 registry 미로딩/비활성 현재값 fallback에만 남는다.
- 조치: `aads-dashboard/src/app/settings/page.tsx`의 Runner Model Config는 `getLlmModels()`를 같이 읽어 registry metadata를 current configured model rows와 add-model select 양쪽에 붙였다. 기존 hard-coded grouped list는 `LEGACY_RUNNER_MODEL_VALUES`로 축소해 저장 포맷 호환 seed로만 사용하고, 실제 group/provider/category/family 표시는 registry 우선으로 생성한다.
- 조치: `aads-dashboard/src/app/admin/model-routing/page.tsx`에 provider/category/family badge를 추가해 routing model rows도 같은 분류 체계를 보이도록 맞췄다.
- 테스트: `python3 -m pytest tests/unit/test_chat_lightweight_frontend_static.py tests/unit/test_model_routing_admin_static.py -q` → 7 passed. `git diff --check` 통과.
- 프론트 검증 제약: 이 worktree에는 `package.json`, `tsconfig.json`, ESLint config가 없어 TypeScript/ESLint 검증은 실행 불가였다.
- 남은 fallback/주의:
  - `settings/page.tsx`의 `LEGACY_RUNNER_MODEL_VALUES`는 `runner_model_config` 저장값이 아직 `codex:*`, `litellm:*`, bare Claude/Qwen 혼합 포맷을 쓰기 때문에 완전 제거하지 않았다. 다만 registry row가 있으면 group/label/badge는 static 값을 덮지 않고 registry metadata를 우선 사용한다.
  - `chat/page.tsx`의 `STATIC_MODEL_OPTION_MAP`도 registry fetch 실패 또는 현재 세션의 비활성 모델 표시 fallback용으로만 남겨뒀다. registry row가 존재할 때는 name/provider/cost 분류를 static 값이 덮어쓰지 않는다.

## 현재 진행 상태 (2026-05-13 11:49 KST) - PC Ollama Gemma 4 E4B 브릿지 1차 반영
- 배경: CEO 지시로 `gemma4:e4b`를 먼저 PC Ollama에 설치하고 AADS `pc_ollama` 브릿지로 붙인 뒤 품질/속도 실측, `gemma4:26b`는 별도 비교 테스트로만 진행해야 한다.
- 조치: `pc_agent/commands/ollama.py`에 Ollama version/list/ps/pull/chat/benchmark 명령을 추가하고, `pc_agent/commands/__init__.py`에 `ollama_*` command_type을 등록했다. `ollama_chat`/`ollama_benchmark`는 Ollama API의 `prompt_eval_count`, `eval_count`, duration 기반 속도 메트릭을 반환한다.
- 조치: `pc_agent/agent.py`가 `ollama_chat` 핸들러 존재 시 `pc_ollama` capability를 등록하도록 보강했다. PC Agent 배포 버전은 `1.0.23`으로 올렸다.
- 확인: DB `llm_models`에는 `pc_ollama/gemma4:e4b`가 active/executable/pending_verification, `pc_ollama/gemma4:26b`가 inactive/comparison_only로 등록돼 있다. `model_selector.py`에는 `execution_backend=pc_ollama` 경로가 들어와 있으며 `tests/unit/test_model_selector_dynamic_routing.py`에 회귀 테스트가 추가돼 있다.
- 검증: `python3 -m pytest tests/test_pc_agent_command_builder.py tests/unit/test_pc_agent_routing_leases.py tests/unit/test_model_selector_dynamic_routing.py -q` → 56 passed. `docker exec aads-server-green python -m py_compile /app/pc_agent/commands/ollama.py /app/pc_agent/commands/__init__.py /app/pc_agent/agent.py` 통과. `docker exec aads-server-green`에서 `ollama_*` 6개 핸들러 노출 확인.
- 운영 반영: DB seed `migrations/092_pc_ollama_gemma4_bridge.sql`를 idempotent 재적용했다. `aads-server`/`aads-server-green` 양쪽에서 `app.services.model_selector` hot-reload 성공, `/api/v1/health` OK 확인. 커밋 `35a494f feat: add PC Ollama Gemma bridge` 생성.
- 미완료/주의: 2026-05-13 11:48:18 KST에 PC Agent `2e9379a1-fed`가 WebSocket code=1000으로 연결 해제되어 현재 연결 0건이다. 따라서 `self_update`, `ollama pull gemma4:e4b`, 품질/속도 실측은 아직 실행하지 못했다. PC Agent가 재연결되면 `self_update` 후 `ollama_pull`/`ollama_benchmark`를 즉시 재시도해야 한다.

## 현재 진행 상태 (2026-05-13 11:20 KST) - NTV2 Browser Bridge work-session route-execute 프록시
- 배경: NTV2 신상마켓 자동상품등록이 AADS Browser Bridge 세션 확보에는 성공했지만, 후속 `browser_eval`/업로드 명령이 공개 `/pc-agent/route-execute` 경로에서 PC Agent 연결 0건/503 계층에 걸릴 수 있었다.
- 조치: `app/api/browser_bridge.py`에 인증된 `/api/v1/browser-bridge/work-sessions/route-execute` 엔드포인트를 추가했다. 요청의 `work_key`로 work-session을 먼저 확보하고, session_id/label/port를 params에 보강한 뒤 active PC Agent route API로 전달한다.
- 검증: `python3 -m py_compile app/api/browser_bridge.py`, `docker exec aads-server python -m py_compile /app/app/api/browser_bridge.py`, `pytest -q tests/unit/test_browser_bridge.py` 23개 통과. `aads-server`/`aads-server-green` 재시작 후 health OK 확인.
- 운영 확인: NTV2 `php artisan sinsang:register-product --product-id=64003` dry-run이 등록 폼 입력, 이미지 20장 base64 업로드, 폼 검증까지 통과하고 외부 최종 등록 전 `dry_run.stop_before_submit`에서 정상 중단됐다.
- 미완료/주의: `supervisorctl status`상 `mcp-servers:playwright-mcp`는 여전히 STOPPED이며, `supervisorctl start`는 `ERROR (no such file)`을 반환했다. 별도 Playwright MCP 실행 파일/슈퍼바이저 설정 복구가 필요하다.

## 현재 진행 상태 (2026-05-13 10:25 KST) - Runner reliability hardening 직접 조치
- 배경: `runner-d32984ff`/`runner-a72c6c24`는 변경 누락 또는 git diff 불일치로 반려됐고, 재작업 `runner-3fc39db2`는 로그 없이 진행 중이라 직접 조치로 전환했다.
- 조치: `app/api/pipeline_runner.py`에서 동일 `project + instruction_hash + parallel_group(scope)` 활성 작업이 있으면 새 요청을 `cancelled/dedup_blocked` row로 저장하고, 원본 job/status/phase와 `auto_retryable=false` 로그를 남기도록 보강했다. 실패/누락 의존 작업은 API 제출 시점에 `blocked_dependency`로 터미널 종결한다.
- 조치: `no_changes`, `dedup_blocked`, `blocked_dependency`, `build_fail`, `deploy_failed`, `review_failed`, `auth_unavailable`, `tool_timeout`을 `display_status/status_group/auto_retryable`로 분리하고, `check_task_status`와 Admin Task Board 집계가 같은 분류를 노출하도록 맞췄다.
- 조치: `running/claimed` 작업인데 `task_logs`가 비어 있으면 API 응답에 `health_probe={task_logs: empty, runner_pid, proc_alive, systemd: not_checked_by_api}`를 노출한다. 외부 systemd 명령은 API에서 실행하지 않는다.
- 추가: `migrations/091_pipeline_runner_reliability_statuses.sql`로 기존 terminal-but-not-error 상태를 보정하고, 기존 `instruction_hash` 단독 unique index를 `project + instruction_hash + COALESCE(parallel_group,'')` scope unique index로 교체한다.
- 검증 예정: `python3 -m py_compile`, `pytest -q tests/unit/test_pipeline_runner_reliability.py tests/unit/test_runner_scope_defaults.py`, `bash -n scripts/pipeline-runner.sh`, `git diff --check`.

## 현재 진행 상태 (2026-05-13 10:12 KST) - Browser Bridge work_key별 CDP 포트 재검증/보강
- 배경: NTV2 중국상품소싱 검수 중 `browser_work_key` 세션은 분리됐지만 실제 local-agent metadata가 같은 PC Agent `port=9222`를 공유해 신상마켓 세션과 중국소싱 세션이 충돌할 수 있었다.
- 조치: `app/browser_bridge/service.py`에 기본 업무 포트 매핑을 추가했다. `ntv2-sinsang-registration=9222`, `ntv2-sinsang-direct-registration=9333`, `ntv2-china-sourcing-admin=9444`, `ntv2-vvic-scrape=9555`를 우선 요청하고, 기존 work_key 세션이 다른 work_key와 같은 `agent_id/port`를 공유하면 재사용하지 않고 재생성하도록 보강했다.
- 조치: PC Agent가 여전히 다른 work_key 소유 CDP 포트를 반환하면 `BrowserBridgeError`로 차단해 세션 registry가 잘못된 포트에 재바인딩되지 않게 했다. `app/api/hot_reload.py`에는 `app.browser_bridge.` prefix를 허용해 API 컨테이너 재시작 없이 브릿지 서비스 모듈을 반영할 수 있게 했다.
- 검증: `python3 -m py_compile app/api/hot_reload.py app/browser_bridge/service.py pc_agent/commands/browser_auto.py` 통과. `python3 -m pytest tests/unit/test_browser_bridge.py tests/unit/test_cdp_session_manager.py -q` 39개 통과. active 8100/green 8102 모두 `app.api.hot_reload`, `app.browser_bridge.service` hot-reload 성공 및 `/api/v1/health` OK.
- 운영 확인: `browser_connect(action="ensure_work_session")` 기준 같은 PC Agent `2e9379a1-fed`에서 `ntv2-sinsang-registration`은 `port=9666`, `ntv2-china-sourcing-admin`은 `port=9444`, `ntv2-vvic-scrape`는 `port=9555`로 분리됐다. active session은 기존 `bb-949cbd0dfef4`에서 바뀌지 않았다.
- 주의: 기존 과거 세션 registry에는 `work_key`가 비어 있거나 metadata만 남은 9222 세션들이 있어 status 목록에 보일 수 있다. 신규 호출은 top-level `work_key` 세션을 우선하며 공유 포트 감지 시 재생성한다.

## 현재 진행 상태 (2026-05-13 09:51~09:52 KST) - 미디어 라우팅/어드민 운영 재검증
- 배경: `runner-aafc4150`, `runner-64aadb0d`는 둘 다 `aads-dashboard:deploy_failed`로 남아 있었지만, 실제 운영 파일과 컨테이너 상태가 일치하는지 재검증이 필요했다.
- 확인: `read_remote_file` 기준 `app/services/media_generation_service.py`, `migrations/090_media_llm_routing_admin_hardening.sql`, `aads-dashboard/src/app/admin/model-routing/page.tsx`가 운영 서버에 반영돼 있었다.
- DB 확인: `model_routing_preferences`에 image/edit_image/video/llm 기본 route가 존재하고, `media_generation_jobs` 테이블도 존재한다. `llm_models`에는 미디어/LLM 관련 대상 model_id 11종 이상이 등록돼 있다.
- 운영 조치: `bash /root/aads/aads-dashboard/deploy.sh`를 2026-05-13 09:51 KST에 수동 재실행했고, green 슬롯 기동 → nginx reload → external `/login` 200 → standby blue 동기화 → QA `UNKNOWN`까지 모두 성공했다.
- 현재 상태: 2026-05-13 09:52 KST 기준 `aads-dashboard`, `aads-dashboard-green`, `aads-server`, `aads-server-green` 모두 `healthy`, 외부 `https://aads.newtalk.kr/login`은 `200 OK`다.

## 현재 진행 상태 (2026-05-13) - Model Routing Admin 실제 대시보드 반영 보정
- 배경: `runner-64aadb0d` P1 산출물은 AADS 서버 저장소의 `aads-dashboard/src/app/admin/model-routing/page.tsx`에는 반영됐지만, 실제 배포 대상 저장소 `/root/aads/aads-dashboard`에는 route stats, Registry 컬럼, default 누락 저장 차단이 빠져 있었다.
- 조치: 실제 대시보드 저장소 `src/app/admin/model-routing/page.tsx`에 P1 UI hardening을 적용하고 `dc91387 fix: apply model routing admin hardening` 커밋으로 push했다.
- 검증: `npx eslint src/app/admin/model-routing/page.tsx` 통과, `npm run build` 통과, `bash /root/aads/aads-dashboard/deploy.sh` blue-green 배포 성공. 배포 로그 기준 active dashboard는 blue(`3100`), standby green(`3101`)은 같은 릴리스로 동기화 완료, 프론트 QA는 `UNKNOWN` 결과지만 배포 스크립트상 통과 처리.
- 운영 확인: `docker ps`에서 `aads-dashboard`, `aads-dashboard-green`, `aads-server`, `aads-server-green` 모두 healthy. DB `model_routing_preferences`에는 image/edit_image/video/llm 기본 route가 존재한다.

## 현재 진행 상태 (2026-05-13) - PC Agent 멀티서비스 CDP 격리
- 배경: 중국상품소싱, 신상마켓 상품수집/등록, 사방넷 등록 등 여러 업무가 같은 PC Agent Browser Bridge를 동시에 쓰면 기존 전역 CDP 포트가 마지막 실행 세션으로 덮여 다른 업무 탭을 조작할 위험이 있었다.
- 조치: `pc_agent/commands/browser_auto.py`의 단일 `_ACTIVE_CDP_PORT` 구조를 제거하고 `CDPSessionManager`가 `work_key -> port/profile/pid`를 관리하도록 보강했다. 같은 `work_key`의 기존 CDP만 재사용하고, 다른 업무 또는 외부 CDP가 점유한 포트는 건너뛰며, 포트 풀이 찬 경우 OS 빈 포트로 격리 시도한다.
- 조치: `app/browser_bridge/service.py`의 PC Agent `browser_launch` 파라미터에 정규화된 `work_key`를 주입하고, 이후 local-agent 브라우저 명령에도 세션 `work_key`가 자동 전달되도록 유지했다. `ensure_work_session(work_key=...)`는 active 세션을 바꾸지 않는 업무별 전용 브릿지 세션으로 동작한다.
- 테스트: 실행 중 컨테이너에 수정 파일을 반영한 뒤 `docker exec aads-server python -m pytest tests/unit/test_cdp_session_manager.py tests/unit/test_browser_bridge.py -q` 37개 통과. `docker exec aads-server python -m ruff check pc_agent/commands/browser_auto.py app/browser_bridge/service.py tests/unit/test_cdp_session_manager.py tests/unit/test_browser_bridge.py` 통과. `docker exec aads-server python -m py_compile pc_agent/commands/browser_auto.py app/browser_bridge/service.py` 통과. `rg -n "_ACTIVE_CDP_PORT|global _ACTIVE_CDP_PORT"` 결과 없음.
- 운영 지침: 중국상품소싱은 `browser_work_key="ntv2-china-sourcing-admin"`, 신상마켓 등록은 `browser_work_key="ntv2-sinsang-registration"`, 사방넷 등록은 별도 `browser_work_key`를 지정해 같은 PC Agent 인스턴스 안에서 분리 사용한다.

## 현재 진행 상태 (2026-05-13)
- **AADS-MEDIA-ADMIN-DB-CONFIG-P1-20260513 — DB 기반 미디어/LLM 모델 라우팅 hardening**:
  - 변경 파일: `app/services/media_generation_service.py`, `migrations/090_media_llm_routing_admin_hardening.sql`, `aads-dashboard/src/app/admin/model-routing/page.tsx`, `tests/unit/test_media_generation_service.py`, `tests/unit/test_model_routing_admin_static.py`, `HANDOVER.md`.
  - 백엔드: explicit `imagen-4.0-*` 요청이 DB registry의 `prefix_family='imagen-4.0-*'` row를 참조하되 요청 model_id를 보존하도록 보강했다. explicit provider가 있으면 DB 조회 provider로 덮지 않고, DB default/preference 미구성 시 기존 env/config fallback과 `NOT_CONFIGURED` graceful path를 유지한다.
  - DB migration/seed: `migrations/090_media_llm_routing_admin_hardening.sql` 추가. `model_routing_preferences`와 `runner_model_config`를 idempotent하게 보강하고, 이미지 `gpt-image-2`, `imagen-4.0-*`, `gemini-3.1-flash-image-preview`, 동영상 `sora-2`, `sora-2-pro`, `veo-3.1-generate-preview`, LLM `gpt-5.5`, `claude-opus-4-7`, `gemini-3.1-pro-preview`를 `llm_models`/routing/chat preference/runner 기본 seed에 반영한다. 기존 `settings_ui` 변경은 덮지 않고 누락값만 보강한다.
  - 대시보드: `/admin/model-routing`에서 route별 available/blocked/disabled 요약, registry active/executable/selectable 상태를 표시하고, route에 등록 모델이 있는데 default가 없으면 저장 전 차단한다.
  - 검증 SQL:
    - `SELECT provider, model_id, verification_status, is_selectable, is_executable, capabilities FROM llm_models WHERE model_id IN ('gpt-image-2','imagen-4.0-generate-001','gemini-3.1-flash-image-preview','sora-2','sora-2-pro','veo-3.1-generate-preview','gpt-5.5','claude-opus-4-7','gemini-3.1-pro-preview') ORDER BY provider, model_id;`
    - `SELECT route_key, provider, model_id, is_enabled, is_default, notes FROM model_routing_preferences ORDER BY route_key, display_order;`
    - `SELECT size, models, updated_by FROM runner_model_config WHERE size IN ('XS','S','M','L','XL','AI_REVIEW') ORDER BY size;`
  - 검증 명령: `python3 -m py_compile app/services/media_generation_service.py app/api/llm_models.py` 통과. `python3 -m pytest tests/unit/test_media_generation_service.py tests/unit/test_model_routing_admin_static.py -q` → 14 passed. `git diff --check` 통과.
  - Git/반영 상태: commit 생성 완료. 기본 `.git` metadata가 read-only라 이 worktree의 writable `.git-local` metadata로 커밋했다. push/deploy는 수행하지 않음.

## 현재 진행 상태 (2026-05-12)
- **MediaGenerationService 및 이미지/동영상 공통 job 구조 P0 (AADS-MEDIA-GENERATION-P0-REWORK-20260512)**:
  - 조치: `app/services/media_generation_service.py`를 신설해 `generate_image`, `edit_image`, `generate_video`, `video_status`, `video_download`를 공통 job 구조로 통합했다. 기존 이미지 성공 응답의 `url/provider/prompt` 형태는 유지하고 `job_id/status/model_id`만 추가했다.
  - DB migration: `migrations/088_media_generation_jobs.sql` 추가. `media_generation_jobs` 테이블은 `id`, `job_id`, `kind(image/edit_image/video)`, `provider`, `model_id`, `prompt`, `input_refs`, `status`, `result_uri`, `result_path`, `result_metadata`, `error_message`, `requested_by`, `session_id`, `created_at`, `updated_at`, `completed_at` 및 idempotent index/check constraint를 포함한다.
  - API/도구: `app/api/image.py`, `app/api/ceo_chat_tools.py`, `app/services/tool_registry.py`, `app/services/tool_executor.py`, `app/services/agent_sdk_service.py`, `app/core/prompts/system_prompt_v2.py`에 `generate_image`, `edit_image`, `generate_video`, `video_status`, `video_download`를 등록했다.
  - 모델 문자열: 이미지 `gpt-image-2`, `imagen-4.0-*`, `gemini-3.1-flash-image-preview`; 동영상 `sora-2`, `sora-2-pro`, `veo-3.1-generate-preview`; LLM `gpt-5.5`, `claude-opus-4-7`, `gemini-3.1-pro-preview`를 route recognition fallback에서 인식한다.
  - graceful path: provider key 미설정은 `NOT_CONFIGURED`, P0 adapter 미지원은 `PROVIDER_UNAVAILABLE`, 결과 미준비/부재는 `JOB_NOT_READY`/`RESULT_UNAVAILABLE`로 반환해 도구/API가 크래시하지 않게 했다. 동영상 다운로드 저장 경로는 `AADS_MEDIA_OUTPUT_DIR` 하위로 제한한다.
  - 테스트: `tests/unit/test_media_generation_service.py`, `tests/unit/test_media_generation_tools.py` 추가. `python3 -m py_compile app/services/media_generation_service.py app/api/image.py app/api/ceo_chat_tools.py app/services/tool_executor.py app/services/tool_registry.py app/services/agent_sdk_service.py app/core/prompts/system_prompt_v2.py tests/unit/test_media_generation_service.py tests/unit/test_media_generation_tools.py` 통과. `python3 -m pytest tests/unit/test_media_generation_service.py tests/unit/test_media_generation_tools.py tests/unit/test_tool_layer_audit.py -q` → 13 passed. `git diff --check` 통과.
  - 참고: 추가 확인으로 실행한 `tests/test_agent_sdk.py`와 `tests/unit/test_tools_and_pipeline.py`는 현재 테스트 환경의 `E2B_API_KEY` 누락, 원격 명령 timeout, 기존 Agent hook 기대값 차이로 일부 실패했다. 신규 미디어 경로 실패는 아니다.
  - Git: 기본 `.git` 파일은 `/root/aads/aads-server/.git/worktrees/aads-wt-runner-aafc4150`를 가리키는 read-only bind mount라 index.lock 생성이 차단된다. 같은 worktree에서 `/tmp/aads-wt-runner-aafc4150/.git-local` writable metadata로 커밋을 생성했다.
  - 푸시/배포: 수행하지 않음.

- **Runner 세션 자동 주입 보강 (2026-05-12 15:21 KST)**:
  - 문제: 특정 채팅창에서 `pipeline_runner_submit`/`batch`가 현재 세션을 못 받아 `session_id를 주시면` 식으로 되묻는 응답이 발생했다. 실측 기준 세션 `f31f1238-fdc8-4405-8893-351226e06bda`에서 15:15 KST에 실제 실패 후 수동 UUID 역조회로 재투입한 흔적이 남아 있었다.
  - 조치: `app/services/tool_executor.py`에 `_resolve_bound_chat_session_id()`를 추가해 `ContextVar → 명시 session_id → Agent SDK active chat session` 순으로 세션을 해석하도록 보강했다. `app/api/ceo_chat_tools.py`도 같은 fallback을 사용하도록 맞췄다.
  - 조치: `app/api/ceo_chat.py` 시스템 프롬프트를 수정해 Runner 제출 시 서버가 현재 채팅 세션을 자동 주입하며, 사용자에게 `session_id`를 다시 요구하지 말도록 명시했다.
  - 검증: `pytest -q tests/unit/test_runner_scope_defaults.py` → 10 passed.
  - 주의: 코드 변경만 반영했다. 커밋/푸시/배포는 아직 수행하지 않았다.

- **Browser Bridge 파일 업로드/다운로드/고급 입력 도구 보강 (2026-05-12 14:09 KST)**:
  - 요청: 신상마켓 필수 이미지 업로드가 막히지 않도록 Browser Bridge에 파일 선택/업로드/다운로드/입력 제어 도구를 추가.
  - 조치: `browser_press_key`, `browser_select_option`, `browser_check`, `browser_upload_file`, `browser_download` 도구를 `ceo_chat_tools`, `tool_registry`, `ToolExecutor`, 모델 스트리밍 타임아웃 경로에 등록했다.
  - 조치: `local_agent` Browser Bridge facade가 위 5개 기능을 PC Agent 명령으로 프록시하도록 추가했다. PC Agent CDP 핸들러에는 `browser_press_key`, `browser_select_option`, `browser_check`, `browser_file_upload`, `browser_download`를 추가했다.
  - 운영 사용: 신상마켓 작업은 `browser_work_key="ntv2-sinsang-registration"` 전용 세션에서 `browser_upload_file(selector="input[type=file]", file_paths=[...])`를 사용한다. PC Agent 세션에서는 파일 경로가 CEO PC 로컬 경로 기준이다.
  - 검증: `python3 -m py_compile app/browser_bridge/service.py app/api/ceo_chat_tools.py app/services/tool_executor.py app/services/tool_registry.py app/services/model_selector.py app/services/subagent_service.py app/services/pc_agent_command_builder.py pc_agent/commands/browser_auto.py pc_agent/commands/__init__.py` 통과. `pytest -q tests/unit/test_browser_bridge.py tests/unit/test_tools_and_pipeline.py` → 72 passed.
  - 주의: 코드 변경만 완료했다. 커밋/푸시/배포는 아직 수행하지 않았다.

- **Browser Bridge 업무별 전용 세션 매니저 (AADS-BRIDGE-SESSION-001, 2026-05-12 KST)**:
  - 요청: NTV2/신상마켓 상품등록 세션을 침범하지 않도록 중국상품소싱/검수/VVIC 등 업무별 Browser Bridge 전용 세션을 자동 확보·분리.
  - 조치: `BrowserBridgeSession`에 `work_key`, `protected`를 추가하고 세션 registry 저장/조회에 반영했다. `ntv2-sinsang-registration`은 보호 업무 키이며, `sinsang`/`신상마켓` 라벨 세션도 보호 세션으로 취급한다.
  - 조치: `BrowserBridgeService.ensure_work_session()`을 추가했다. 호출자는 `browser_work_key` 또는 `browser_connect(action="ensure_work_session", work_key="ntv2-china-sourcing-admin")`를 넘기며, 매니저가 기존 전용 세션 재사용/stale 세션 재생성/isolated profile 생성까지 처리한다. 이 경로는 `activate=False`로 동작해 active 세션을 바꾸지 않는다.
  - API/도구: `POST /api/v1/browser-bridge/work-sessions/ensure`, `GET /api/v1/browser-bridge/work-sessions`를 추가했다. `GET /sessions`와 `browser_connect(status)`는 세션 라벨, storage 여부, leased 여부, last_used_at, work_key/protected 매핑을 노출한다.
  - 로그인 자동화: AADS/vault 자동 로그인은 `browser_work_key` 또는 `browser_session_id`가 명시된 분리 세션에서만 수행해 기존 active 세션 쿠키/스토리지와 섞이지 않게 했다.
  - 운영 규칙: 신상마켓 상품등록은 `browser_work_key="ntv2-sinsang-registration"` 전용으로만 사용한다. 중국상품소싱 관리자 검수는 `browser_work_key="ntv2-china-sourcing-admin"`, VVIC 수집은 `browser_work_key="ntv2-vvic-scrape"`를 사용하고 raw `browser_session_id` 공유를 피한다.
  - 테스트: `tests/unit/test_browser_bridge.py`에 보호 신상마켓 세션과 중국상품소싱 세션 분리, 동일 업무 키 재사용, disconnected context 재생성, active 세션 불변 검증 케이스를 추가했다.

- **Chat 보고서 깊이 계약 및 부실보고 재작성 게이트 (2026-05-12 12:45 KST)**:
  - 요청: 채팅창 보고서 출력 품질 개선이 실제 응답 내용까지 개선되는지 확인 후, 문제점·원인·개선 권장안·완료기준이 빈약한 보고를 즉시 개선.
  - 조치: `app/services/output_validator.py`에 `REPORT_STRUCTURE_WEAK` 검사를 추가했다. 보고/분석/CTO/리서치 계열 인텐트가 너무 짧거나 `문제점/리스크`, `원인/근거`, `개선 권장안`, `검증 방법/완료기준`, `다음 단계` 중 핵심 구조를 2개 이상 누락하면 저장 전 재작성 스트림으로 돌린다.
  - 조치: `migrations/087_chat_report_depth_contract.sql`을 추가했다. 신규 L1 `global-report-depth-contract`와 L4 `intent-report-output`, `intent-analysis-output`을 보강해 보고형 응답의 필수 섹션과 품질 하한을 프롬프트 레이어에서도 강제한다.
  - 조치: `tests/unit/test_tools_and_pipeline.py`에 부실 분석 응답 차단 및 구조화 분석 응답 통과 테스트를 추가했다.
  - 검증 예정: `python3 -m pytest tests/unit/test_tools_and_pipeline.py -q`, `python3 -m py_compile app/services/output_validator.py`, 운영 DB 087 적용 및 prompt_assets 검증.
  - 주의: 현재 작업트리에는 이번 작업 전부터 `.active_container`, `.active_port`, `docs/CHANGELOG-go100-direct.md` 변경이 남아 있으며 이번 변경 범위에서 되돌리지 않는다.

- **AADS runtime marker 커밋/ledger 오염 방지 (2026-05-12 11:55 KST)**:
  - 요청: BG 전환 후 `.active_container`/`.active_port` 같은 런타임 marker가 커밋/dirty ledger에 섞이는 문제를 이어서 개선.
  - 조치: `app/services/workspace_change_tracker.py`에 AADS `aads-server` 런타임 상태 파일 ignore 가드를 추가했다. 신규 record/list/finalize 경로에서 `.active_container`, `.active_port`를 workspace change ledger 대상으로 보지 않는다.
  - 조치: `app/services/tool_executor.py`의 run_remote_command 전후 git diff hook 필터에도 동일 파일을 제외했다.
  - 조치: `scripts/pipeline-runner.sh` deploy 단계의 `git add -A` 직후 `.active_container`, `.active_port`를 즉시 unstaging하여 러너 승인/배포 커밋에 marker가 섞이지 않게 했다.
  - 검증: `python3 -m pytest tests/unit/test_workspace_change_tracker.py tests/unit/test_response_completion_contract.py -q` → 7 passed. `python3 -m py_compile app/services/workspace_change_tracker.py app/services/tool_executor.py app/services/chat_service.py` 통과.
  - 주의: 현재 작업트리에는 실제 운영 상태를 반영한 `.active_container=aads-server`, `.active_port=8100` dirty가 남아 있다. 이는 이번 커밋 대상에서 제외해야 한다.

- **Chat completion contract 문서기록 검증 보강 (2026-05-12 11:44 KST)**:
  - 요청: 커밋/푸시/문서기록 실행 시기와 훅 개선안의 권장조치를 실제 반영.
  - 조치: `app/services/response_completion_contract.py`가 세션 ledger 전체 상태(`dirty/committed/pushed/deployed`)를 읽도록 변경했다. 이제 응답이 "문서기록 완료/HANDOVER 업데이트 완료"라고 보고할 때 ledger에 `HANDOVER.md` 또는 `docs/*.md` 변경 근거가 없으면 `document_report_unverified_by_ledger`, 문서 파일이 아직 미커밋/미푸시/미배포 상태면 `document_report_conflicts_with_ledger`로 보정한다.
  - 조치: `tests/unit/test_response_completion_contract.py`에 문서기록 허위 완료 및 pending 문서 완료 보고 차단 테스트를 추가했다.
  - 운영 반영: active `8100`과 standby `8102`에 hot-reload를 호출했다. active는 `app.services.response_completion_contract`와 `app.services.chat_service` 모두 reload OK, standby는 `chat_service` reload OK이며 completion contract 모듈은 아직 미로드 상태라 다음 import 시 최신 파일을 로드한다.
  - 검증: `python3 -m pytest tests/unit/test_response_completion_contract.py -q` → 5 passed. `python3 -m py_compile app/services/response_completion_contract.py app/services/chat_service.py app/services/workspace_change_tracker.py` 통과. 운영 DB `prompt_assets.slug='global-chat-completion-contract'`는 enabled=true, layer_id=1, priority=6 확인.
  - 주의: 커밋/푸시는 아직 수행하지 않았다. 작업트리에 기존 브라우저 브릿지/BG/채팅 완료계약 변경이 섞여 있어, 이 항목 커밋 시에는 completion contract 관련 hunk만 부분 스테이징해야 한다.

- **AADS Blue-Green standby 자동 동기화 보강 (2026-05-12 10:41 KST)**:
  - 요청: B→G 전환 후 B가 자동으로 G와 동기화되어 다음 전환/rollback 때 미반영 슬롯이 노출되지 않는지 확인.
  - 확인: 기존 백엔드 BG는 새 슬롯 빌드→upstream 전환 후 old 슬롯을 drain 뒤 stop하거나, 스트림이 남으면 old 슬롯을 그대로 두었다. 대시보드는 old 슬롯을 warm standby로 유지했지만 재빌드하지 않았다. 따라서 "전환 직후 반대 슬롯도 같은 release로 자동 동기화"는 완전 적용 상태가 아니었다.
  - 조치: `deploy.sh`에 `sync_standby_slot_after_drain`을 추가했다. BG 전환 후 old API 슬롯의 active stream이 0이 될 때까지 기다린 뒤 같은 release로 old 슬롯을 `docker compose up -d --build --no-deps` 재생성하고 health를 확인한다. 스트림이 장시간 유지되면 응답 보존을 우선해 동기화는 스킵 로그를 남긴다.
  - 조치: `aads-dashboard/deploy.sh`는 upstream 전환과 외부 health 통과 후 이전 dashboard 슬롯을 즉시 재빌드해 warm standby를 같은 release로 맞춘다. `AADS_DASHBOARD_STOP_PREVIOUS=true`일 때만 이전처럼 정리한다.
  - 검증 예정: `bash -n /root/aads/aads-server/deploy.sh`, `bash -n /root/aads/aads-dashboard/deploy.sh`, compose config, nginx config. 실제 슬롯 재생성은 active stream 확인 후 BG 배포 시 적용한다.

- **AADS BG 포트 바인딩/주석 정리 (2026-05-12 10:23~10:25 KST)**:
  - 요청: `8080/8100/8102` 포트 의미 혼동을 만든 문제를 개선하고 관련 주석을 정리.
  - 원인: nginx는 host loopback `127.0.0.1:8100/8102/3100/3101`로 프록시하지만 Docker compose 포트가 `0.0.0.0`에 publish되어 외부에서 우회 접근 가능한 형태였다. 또한 `nginx-aads-upstream.conf` 주석이 특정 슬롯을 active로 단정해 실제 deploy 후 상태와 어긋날 수 있었다.
  - 조치: `docker-compose.prod.yml`의 API blue/green `8100/8102`와 dashboard blue/green `3100/3101` 포트를 `127.0.0.1` 바인딩으로 변경했다. 개발 compose의 API/dashboard 단일 포트도 같은 정책으로 맞췄다.
  - 조치: upstream 주석을 "non-backup line이 active이며 deploy.sh가 재작성"하는 설명으로 수정했다. dashboard deploy QA 호출은 잘못된 host `localhost:8080` 대신 `AADS_API_BASE` 기본값 `http://127.0.0.1:8100`을 사용하도록 고쳤다.
  - 즉시/영속 가드: active API `8100`에 실행 스트림 2건이 있어 컨테이너 재생성은 보류했다. 대신 Docker publish 우회 접근을 막기 위해 `DOCKER-USER`에 원래 목적지 포트 `8100/8102/3100/3101` DROP 규칙을 추가하고, IPv6 `INPUT`에도 동일 포트 DROP 규칙을 추가했다. loopback/nginx 접근은 유지된다. 재부팅/배포 후에도 복원되도록 `scripts/apply-bg-port-firewall.sh`와 `scripts/aads-bg-host-only-ports.service`를 추가하고 `deploy.sh`에서도 동일 가드를 재적용한다.
  - 검증: `docker compose -f docker-compose.prod.yml config`, `docker compose -f /root/aads/aads-dashboard/docker-compose.yml config`, `nginx -t`, `curl http://127.0.0.1:8100/api/v1/health`, `curl https://aads.newtalk.kr/api/v1/health` 통과.
  - 주의: compose 포트 바인딩 변경은 컨테이너 재생성 후 `docker ps`의 listen 주소까지 `127.0.0.1`로 반영된다. active 스트림 존재 시 즉시 재생성하면 채팅 끊김이 생길 수 있으므로 blue-green 슬롯 순환으로 적용해야 한다.

- **Chat DB-saved response visibility guard 배포 (2026-05-12 10:16~10:23 KST)**:
  - 요청: `47c6e3de-5b92-4ee7-a175-bd20e3cc8b50` 채팅창에서 새로고침 시 DB에 저장된 응답 버블이 사라지는 현상 즉시 조치.
  - 원인: 프론트 폴링 최적화가 `streaming-status.last_message_id`만 비교했다. 이 값은 placeholder를 제외한 최신 메시지 기준이라, DB에 `streaming_placeholder`가 저장/갱신되어도 같은 값으로 판단해 `/chat/messages` 재조회를 건너뛸 수 있었다.
  - 조치: `aads-dashboard/src/app/chat/page.tsx`에서 `message_revision + placeholder_revision`을 함께 비교하도록 변경했다. 세션 전환 시 revision ref를 초기화하고, DB placeholder가 존재하면 waiting 상태가 아직 false여도 `include_streaming=true`로 메시지를 조회한다.
  - 검증: `npx tsc --noEmit --pretty false` 통과. `npx eslint src/app/chat/page.tsx` 0 errors/기존 warnings 21개. `bash /root/aads/aads-dashboard/deploy.sh` 성공, 활성 슬롯 `blue`, 프론트엔드 QA 통과. 외부 `/chat`은 미로그인 기준 `/login?redirect=%2Fchat` 307 확인.
  - 현재 대상 세션 DB: 2026-05-12 10:22 KST 기준 `streaming_placeholder=0`, visible assistant 메시지 3951건.

- **Chat completion contract hard guard 적용 (2026-05-12 09:55~10:02 KST)**:
  - 요청: "훅으로 명시했는데 채팅창에서 적용이 안 된다"는 문제의 개선안 즉시 적용.
  - 원인: `prompt_assets` 지시는 채팅 system prompt에는 붙지만, 파일 수정 후처리 훅/ledger와 최종 응답 저장 경로가 직접 연결되어 있지 않았다. 그래서 모델이 커밋/푸시/문서기록 상태를 누락하거나 잘못 보고해도 저장 직전 하드 가드가 없었다.
  - 조치: `app/services/response_completion_contract.py`를 추가해 `chat_workspace_change_ledger`의 `dirty/committed/pushed` 상태와 최종 응답 내용을 대조한다. 미커밋/미푸시 변경이 있는데 완료 상태를 누락하거나, ledger와 충돌하는 "커밋/푸시/배포 완료" 문구가 있으면 응답에 `완료 상태 보정` 블록을 자동 추가하고 `quality_details`에 기록한다.
  - 조치: `app/services/chat_service.py`의 최종 저장 직전에 completion contract를 실행하도록 연결했다. 보정 발생 시 SSE delta로 보정 블록을 사용자에게 즉시 보여준 뒤 같은 내용을 DB에 저장한다.
  - 조치: `migrations/086_chat_completion_contract_prompt.sql`로 L1 `global-chat-completion-contract` prompt asset을 추가/갱신했다. 일반 채팅 prompt compile 결과에 해당 asset이 붙는지 active 컨테이너에서 확인했다.
  - 운영 반영: 운영 DB에 086 마이그레이션 적용 완료. active stream 2건이 있던 `aads-server-green:8102`는 건드리지 않고, standby `aads-server:8100`만 `aads-api` 재기동 후 nginx upstream을 8100으로 전환했다. 기존 green 스트림은 보존 상태다.
  - 검증: `python3 -m pytest tests/unit/test_response_completion_contract.py -q` → 3 passed. `python3 -m py_compile app/services/response_completion_contract.py app/services/chat_service.py` 통과. 운영 DB `prompt_assets.slug='global-chat-completion-contract'` 1건 활성. active 컨테이너 `PromptCompiler.compile(... intent='code_modify')` 결과 `asset_applied=True`, `asset_count=13`. 외부 `https://aads.newtalk.kr/api/v1/health` OK.
  - 주의: `chat_service.py`에는 이번 작업 전부터 있던 별도 미커밋 hunk가 같이 남아 있어 커밋 시 completion contract hunk만 부분 스테이징해야 한다.

- **PC Agent active-slot 재연결 및 Browser Bridge fallback 보강 (2026-05-12 09:56~10:00 KST)**:
  - 요청: CEO PC Agent가 자동 업데이트/재연결 반영 후에도 다시 연결되지 않는지 확인하고 즉시 조치.
  - 확인: active 포트는 `8102`, active 컨테이너는 `aads-server-green`이다. 외부 도메인과 `8102` 모두 PC Agent `2e9379a1-fed` 연결 1건을 반환했고, old 슬롯 `8100`은 0건으로 정리됐다.
  - 원인: 현재 채팅 MCP 도구 프로세스가 old 컨테이너 `aads-server` 안에서 실행 중이라 로컬 `pc_agent_manager`에는 연결이 없었다. 기존 fallback은 컨테이너 내부에서 `127.0.0.1:8102`를 호출해 active green에 닿지 못했다.
  - 조치: `app/browser_bridge/service.py`의 active API fallback URL 후보에 `.active_container` 기반 `http://aads-server-green:8080` 경로를 추가했다. 컨테이너 내부 fresh process에서 old 컨테이너가 active green route-execute로 우회해 `local_agent` 세션을 생성하는 것을 확인했다.
  - 검증: `python3 -m py_compile app/browser_bridge/service.py app/api/browser_bridge.py app/api/ceo_chat_tools.py` 통과. `python3 -m pytest tests/unit/test_browser_bridge.py` → 18 passed. `docker exec aads-server curl http://aads-server-green:8080/api/v1/pc-agent/health` → connected 1. `docker exec aads-server python3 -c ...ensure_pc_agent_cdp_session...` → `bb-ba65758c530c local_agent 2e9379a1-fed 9222`.
  - 주의: 현재 이 대화에 이미 붙어 있는 MCP 도구 프로세스는 패치 전 로드된 코드라 `browser_connect(ensure_pc_cdp)`가 계속 offline을 반환할 수 있다. 다음 MCP 프로세스 시작 또는 도구 브릿지 재시작 후에는 새 fallback이 적용된다.

- **AADS API/server/dashboard blue-green 강제 범위 확대 (2026-05-12 09:17~KST)**:
  - 요청: API, server, dashboard, Docker 계층까지 BG 적용 여부를 확인하고 즉시 조치.
  - 원인: 백엔드 러너 표준 배포는 `deploy.sh bluegreen`이었지만, 대시보드 러너 후처리가 `docker compose build` 후 `up -d aads-dashboard`로 직접 교체했고, 텔레그램 승인봇/승인 API/watchdog에도 `aads-server`·`aads-dashboard` 직접 restart/compose 경로가 남아 있었다.
  - 조치: `scripts/pipeline-runner.sh.local`의 대시보드 후처리를 `/root/aads/aads-dashboard/deploy.sh` 호출로 변경했다. `scripts/tg_approval_bot.py`, `app/api/approval.py`, `app/api/watchdog.py`는 AADS API/dashboard 직접 restart/compose 명령을 blue-green 배포 스크립트로 리다이렉트한다.
  - 조치: 실제 실행 러너인 `scripts/pipeline-runner.sh`에서도 대시보드 deploy 실패 시 직접 docker compose fallback을 제거했다. `scripts/rebuild_dashboard.sh`, `scripts/rebuild_dashboard_aads188.sh`, `scripts/rebuild-dashboard.sh`, `scripts/build_dashboard.sh`, `scripts/build_dashboard_once.sh`, `scripts/build-dashboard.sh`, `scripts/bg_build_launcher.py`는 직접 compose 대신 대시보드 BG 스크립트 래퍼로 바꿨다.
  - 조치: `app/services/unified_healer.py`도 `docker restart aads-server`/`aads-dashboard`를 blue-green 배포로 리다이렉트한다. `aads-dashboard/deploy.sh`는 이전 슬롯을 즉시 stop하지 않고 기본 warm standby로 유지하며, 필요 시 `AADS_DASHBOARD_STOP_PREVIOUS=true`일 때만 정리한다.
  - 운영 반영: nginx upstream과 `.active_port/.active_container`를 `8102/aads-server-green`으로 정합화했고, active 8102의 스트림 0건을 확인한 뒤 `aads-api`만 reload해 healer 리다이렉트까지 런타임에 반영했다. `aads-pipeline-runner`도 재시작해 수정된 `scripts/pipeline-runner.sh`를 로드했다.
  - 검증: `nginx -t`, `bash -n deploy.sh`, `bash -n scripts/pipeline-runner.sh`, 대시보드/빌드 래퍼 `bash -n`, `python3 -m py_compile app/api/approval.py app/api/watchdog.py app/services/unified_healer.py scripts/tg_approval_bot.py scripts/bg_build_launcher.py` 통과. 외부 `https://aads.newtalk.kr/api/v1/health` 200, `/login` 200, `aads-pipeline-runner` active 확인.
  - 주의: DB/Postgres, Redis, LiteLLM, socket-proxy 같은 의존 컨테이너는 blue-green 대상이 아니며, 직접 재시작 대신 장애 시 수동 승인·별도 복구 기준으로 다뤄야 한다.

- **AADS deploy.sh blue-green 기본 강제 (2026-05-12 09:06 KST)**:
  - 요청: AADS 무중단 배포가 일부 경로에서 서버 재시작/응답 끊김을 유발할 수 있어 즉시 조치.
  - 원인: 러너 표준 경로는 `deploy.sh bluegreen`을 호출하지만, `deploy.sh` 자체 기본값이 `code`였고 `code`/`reload`/`build` 레거시 모드가 active API 재시작 경로를 그대로 열어 두고 있었다.
  - 조치: `deploy.sh` 무인자 기본값을 `bluegreen`으로 변경하고, `code`/`reload`/`build` 요청은 기본적으로 `bluegreen`으로 자동 리다이렉트하도록 가드했다. 불가피한 수동 점검 때만 `AADS_DEPLOY_ALLOW_LEGACY_RESTART=true`를 명시하면 기존 모드를 실행할 수 있다.
  - 의존성 확인: 2026-05-12 09:06 KST 기준 `.active_port=8100`, `.active_container=aads-server`, nginx upstream도 8100 primary/8102 backup으로 일치했다. `aads-server`, `aads-server-green`, `aads-postgres`, `aads-redis`, `aads-litellm`, `aads-dashboard`는 running/healthy 상태였다.
  - 검증: `bash -n deploy.sh` 통과. 양쪽 API `http://127.0.0.1:8100/api/v1/health`, `http://127.0.0.1:8102/api/v1/health` 모두 OK. active 8100에는 스트림 4건이 있어 불필요한 재배포는 실행하지 않았다.
  - 주의: 이 조치는 다음 배포 호출부터 적용된다. 현재 작업트리의 기존 무관 변경 `docs/CHANGELOG-go100-direct.md`는 건드리지 않았다.

- **Chat TODO 패널 UX 보강 (2026-05-12 08:36 KST)**:
  - 요청: 채팅창 상단 TODO 패널을 접을 수 있게 하고, 기본 상태에서 완료 이력보다 진행/대기 항목을 먼저 보이도록 조정.
  - 대시보드 조치: `aads-dashboard/src/app/chat/page.tsx`에 `todoCollapsed`, `showAllTodos` 상태를 추가했다. 세션 전환 시 기본값을 `펼침 + 진행만`으로 초기화하고, 헤더에 `전체/진행만` 토글과 접기 버튼을 넣었다.
  - 표시 정책: 기본 목록은 `pending`/`in_progress`만 노출하고, 완료/실패/skip 항목은 숨긴 뒤 필요 시 `전체` 버튼으로 확장한다. 활성 TODO가 없으면 빈 상태 문구를 보여 주고, 완료 이력이 남아 있으면 확장 가능 여부를 같이 안내한다.
  - 문서 기록: `aads-dashboard/README.md` 주요 기능에 채팅 TODO 패널 기본 동작을 추가했다.
  - 검증: `npx tsc --noEmit --pretty false` 통과. 파일 단위 ESLint는 기존 warning만 있고 새 error 없음. 배포는 다른 세션에서 이미 반영된 상태라 이번 작업에서는 커밋/푸시만 수행 예정.

- **Chat restart resume trigger guard (2026-05-12 08:34 KST)**:
  - 요청: 서버 재시작 후 채팅 응답이 이어서 진행되지 않는 문제의 즉시 개선.
  - 원인: `app/main.py`의 execution resume scanner가 `chat_turn_executions.status IN ('running','retrying')`이어도 `updated_at < NOW() - 90 seconds`가 될 때까지 claim하지 않았다. 재시작 직후에는 DB상 “생성 중”으로 보이지만 새 프로세스 메모리에는 producer가 없어 빈 대기 시간이 생겼다.
  - 조치: 새 API 프로세스 시작 시각보다 이전에 갱신된 running/retrying 실행은 startup scan 5초 후 90초 대기 없이 claim하도록 보강했다. 평시 periodic scanner는 기존 stale 기준을 유지하며 `AADS_EXECUTION_RESUME_STALE_SECONDS`로 조정 가능하다. startup 보조 기준은 `AADS_EXECUTION_RESUME_STARTUP_STALE_SECONDS` 기본 15초다.
  - 변경 파일: `app/main.py`, `docs/chat/CHAT-CHANGELOG.md`, `HANDOVER.md`.
  - 검증: `python3 -m py_compile app/main.py app/routers/chat.py app/services/chat_service.py` 통과. `python3 -m pytest tests/unit/test_chat_service.py -q` → 22 passed. 변경 파일 대상 `git diff --check -- app/main.py docs/chat/CHAT-CHANGELOG.md HANDOVER.md` 통과.
  - 배포: `bash /root/aads/aads-server/deploy.sh code` 성공. 활성 스트림 2건을 감지해 active 직접 재시작 대신 peer slot으로 전환했고, health/DB/채팅/LLM 검증 6단계를 통과했다. 배포 후 active는 `aads-server:8100`, `/api/v1/health` OK, active 컨테이너 소스에서 `reclaim_before` 및 resume env 설정 반영 확인.
  - 주의: 현재 작업트리에 기존 무관 변경 `.active_container`, `.active_port`, `docs/CHANGELOG-go100-direct.md`가 남아 있으며 이번 조치 범위에서 되돌리지 않았다.

- **Chat-embedded Design Studio 운영 카드 추가 (2026-05-12 08:10 KST)**:
  - 요청: 독립 `/design/modifications` 페이지로 분리된 Design Studio를 채팅창 안에서 운영할 수 있게 하고, 채팅 AI가 디자인 수정 요청을 맥락 유지형 작업으로 다룰 수 있게 보강.
  - 대시보드: `aads-dashboard/src/app/chat/page.tsx`에 `디자인수정` 액션 칩과 입력창 상단 Design Studio 패널을 추가했다. 채팅 문장/수정 범위/금지 범위/검수 기준을 카드로 고정하고, `POST /api/v1/admin/design/modification-requests` 및 `build-context`를 호출해 컨텍스트팩까지 생성한다.
  - 대시보드: 생성 후 `Context`, `Workbench` 바로가기와 `AI 운영 지시로 넣기` 버튼을 제공해 사용자가 같은 채팅 AI에게 “컨텍스트팩 기준으로 구현/러너 투입/검수 진행”을 이어서 지시할 수 있다.
  - 백엔드: `app/services/intent_router.py`에서 `design/design_fix` 인텐트가 도구 사용 경로를 타도록 변경했다. `app/services/tool_registry.py`와 `app/services/tool_executor.py`에 `create_design_modification_request` 도구를 등록해 채팅 AI가 직접 디자인 수정 요청 카드와 컨텍스트팩을 생성할 수 있게 했다.
  - 검증: `python3 -m py_compile app/services/intent_router.py app/services/tool_registry.py app/services/tool_executor.py` 통과. `npx eslint src/app/chat/page.tsx` 0 errors/기존 warnings 21개. `npx tsc --noEmit --pretty false` 통과.
  - 미반영: 이 항목 작성 시점에는 커밋/푸시/배포 전이며, `docs/CHANGELOG-go100-direct.md` 기존 무관 변경은 이번 작업 범위에서 제외해야 한다.

- **Chat final response visibility guard (2026-05-12 07:34~KST)**:
  - 요청: 특정 채팅 세션 `8ad08cc2-620c-4a70-8305-74a8d9b43c4e`에서 최종 응답이 작성됐으나 화면에 노출되지 않고 사라진 원인 파악 및 즉시 조치.
  - 실측: 2026-05-12 07:44 KST 재조회 기준 해당 세션은 `chat_messages=1285`, `streaming_placeholder=0`, `chat_sessions.current_execution_id=NULL`이었다. 문제로 지목된 assistant `2851f6d1-a52a-4f3d-a650-7b14e1f918cf`는 2026-05-12 07:20:03 KST에 DB 저장되어 있으며 본문 길이는 2925자였다.
  - 원인: 백엔드 저장 실패가 아니라 프론트 완료 직후 재조회/폴링 경로가 assistant 저장 gap에서 로컬 최종 버블을 `setMessages(processed)`로 덮어쓸 수 있었다. 또한 `done` 수신 직후 서버 최종 메시지 ID를 `/last-response`로 재고정하는 보강이 부족했다.
  - 조치: `aads-dashboard/src/app/chat/page.tsx`에서 세션 메시지 재조회 결과를 기존 메시지와 병합하도록 변경하고, `mergeLatestAssistantFromServer()`를 추가해 `done`, `message_done`, execution replay 완료, just_completed gap에서 `/last-response` 최종 assistant를 조용히 병합한다.
  - 문서: `docs/chat/CHAT-CHANGELOG.md`에 2026-05-12 항목을 추가했다.
  - 검증/반영 확인: `python3 -m pytest tests/unit/test_chat_lightweight_frontend_static.py -q` 3 passed, `npx tsc --noEmit --pretty false` 통과, `npx eslint src/app/chat/page.tsx` 0 errors/기존 warnings 20개. `aads-dashboard` 컨테이너는 healthy이며 2026-05-12 07:42 KST에 시작되었고, 외부 `/chat`은 미로그인 기준 `/login?redirect=%2Fchat` 307 응답을 확인했다. `.active_container`/`.active_port` 파일은 없어 활성 슬롯명은 미확인.

- **Browser Bridge 다중 세션 병렬 고정 지원 (2026-05-12 07:15~KST)**:
  - 요청: 여러 Browser Bridge 세션을 동시에 띄우고 각각 다른 작업에 고정해 진행할 수 있도록 즉시 구현.
  - 조치: `BrowserBridgeService.acquire_playwright_context(session_id=...)`가 특정 세션을 직접 획득하도록 보강했다. 명시 `session_id` 사용 시 전역 active 세션을 바꾸지 않으며, 없을 때만 기존 active/headless fallback 동작을 유지한다.
  - 조치: `browser_navigate`, `browser_snapshot`, `browser_screenshot`, `browser_click`, `browser_fill`, `browser_tab_list`, `capture_screenshot`에 `browser_session_id` 입력을 추가하고 `ToolExecutor`, `ceo_chat_tools`, `tool_registry` 경로를 연결했다.
  - 조치: `/api/v1/browser-bridge/e2e/config?session_id=...`로 특정 세션 E2E 설정 조회를 지원한다. 잘못된 고정 세션 ID는 `mode=unavailable`, `headless_fallback=false`로 명시해 조용한 headless fallback을 막는다.
  - 사용법: 여러 세션을 등록한 뒤 각 작업/러너/채팅 도구 호출에 `browser_session_id="bb-..."`를 넣으면 서로 다른 브라우저 세션에서 병렬 실행된다. 기존 `browser_connect(action="select")` 방식은 하위 호환용 active 세션 선택으로 유지된다.
  - 검증: `pytest tests/unit/test_browser_bridge.py -q` → `12 passed`. `python3 -m compileall app/browser_bridge app/api/browser_bridge.py app/api/ceo_chat_tools.py app/services/tool_executor.py app/services/tool_registry.py` 통과.

## 현재 진행 상태 (2026-05-11)
- **Chat stream interruption / blue-green deploy guard 보강 (2026-05-11 19:30~KST)**:
  - 원인: API/대시보드 재시작 중 SSE 스트림이 끊겼고, 이후 `resume_single_stream_error` 경로에서 Codex Relay 재개가 실패해 `interrupted/recovered` 메시지가 남았다.
  - 실측: active marker는 `aads-server-green:8102`, nginx upstream도 8102 active. 8100/8102 양쪽에 활성·복구 스트림이 남아 다음 blue-green이 backup 슬롯을 재빌드하면 추가 끊김 위험이 있었다.
  - 조치: `deploy.sh`에 target slot active stream preflight를 추가해 busy backup 슬롯 재빌드를 차단하고, old slot drain timeout 시 강제 restart/stop 대신 스트림 보존을 위해 종료를 스킵하도록 수정했다.
  - 검증: `bash -n deploy.sh` 통과. 커밋 `f733749 fix: preserve active chat streams during blue-green deploy`.

- **AADS Design Modification Studio 직접 보강/DB 반영 (2026-05-11 19:20~KST)**:
  - 러너 추가 투입 없이 직접 조치. `runner-54bb2066`은 diff 0건이라 거부했고, 시작 전 queued 상태의 `runner-fb3e9b45`는 중복 충돌 방지를 위해 종료했다.
  - 운영 DB에 `migrations/082_open_design_hub.sql`, `084_design_modification_studio.sql`, `085_design_qa_scores.sql`을 순서대로 적용했다. `design_projects=1`, `design_screens=4`, `design_decisions=2`, `design_modification_requests=0`, `design_qa_scores=0` 확인.
  - 백엔드: `app/api/design_modifications.py`에 `POST /api/v1/admin/design/modification-requests/{request_id}/score`를 추가하고, `app/services/design_qa_scorer.py`의 React inline `fontSize: "2vw"` viewport scaling 탐지를 보강했다.
  - 대시보드: `/design/modifications`, `/design/modifications/new`, `/design/modifications/[id]/context`, `/design/modifications/[id]/workbench` 페이지와 `src/lib/api.ts` Design Modification Studio API 클라이언트를 추가했다. 사이드바에 `Design Studio` 진입 링크를 추가했다.
  - 검증: `python3 -m py_compile app/api/design_modifications.py app/services/design_context_builder.py app/services/design_qa_scorer.py` 통과. `pytest -q tests/unit/test_design_modifications_api.py tests/unit/test_design_qa_scorer.py` → `11 passed`. `npm run build` 통과하며 신규 라우트 4개가 빌드 출력에 포함됨.
  - 배포: 백엔드 `deploy.sh`는 code mode로 정상 종료됐으나 이미지 재빌드가 아니라 score API 파일은 활성 컨테이너에 직접 반영 후 `aads-server-green`의 `aads-api`만 재기동했다. OpenAPI에서 `/api/v1/admin/design/modification-requests/{request_id}/score` 노출 확인, `8102/api/v1/health` OK 확인. 대시보드 `deploy.sh`는 blue-green 성공, 활성 슬롯 `blue`, 외부 `/design/modifications/new`는 미로그인 기준 `/login?redirect=...` 307 정상.
  - 미검증/주의: `npm run lint`는 이번 변경과 무관한 기존 전역 ESLint 오류 273건으로 실패한다. 백엔드 컨테이너 직접 반영분은 다음 정식 이미지 빌드/커밋 전에는 재빌드 시 소스 커밋 기준에 의존한다.

- **AADS-DESIGN-MOD-003 Design Context Pack Builder 추가 (2026-05-11 KST)**:
  - 변경 파일: `app/services/design_context_builder.py`, `app/api/design_modifications.py`, `tests/unit/test_design_context_builder.py`, `tests/unit/test_design_modifications_api.py`, `HANDOVER.md`.
  - 조치: `build_context_pack(request_id)` 서비스를 추가해 `design_projects`, `design_screens`, `design_modification_requests`, `design_token_sets`, `design_visual_snapshots(phase='before')`에서 AI 주입용 context를 조립하고 `design_context_packs`에 저장하도록 구현했다.
  - 조치: context에는 project metadata, screen info, component path candidates, `DESIGN.md` 내용, design tokens, baseline screenshot URL, viewport matrix, allowed/forbidden scope, acceptance criteria를 포함한다. `DESIGN.md`는 repo root와 `docs/` 후보만 읽고, key/token/secret/password 계열 값과 토큰형 문자열은 저장 전 redaction한다.
  - API: `POST /api/v1/admin/design/modification-requests` 요청 생성 엔드포인트와 `POST /api/v1/admin/design/modification-requests/{request_id}/build-context` 빌더 실행 엔드포인트를 추가했다.
  - 테스트: builder 단위 테스트는 mock DB와 임시 `DESIGN.md`로 필수 context 조립, redaction, missing_context 저장을 검증하도록 추가했다. API 테스트에는 요청 생성과 build-context 트리거 회귀 테스트를 보강했다.

- **Pipeline Runner AADS 백엔드/대시보드 라우팅 오분류 패치 (2026-05-11 18:17 KST)**:
  - 증상: AADS 지시문에 `Backend workdir: /root/aads/aads-server`와 `Dashboard workdir: /root/aads/aads-dashboard`가 함께 있으면 `scripts/pipeline-runner.sh`가 대시보드 키워드를 먼저 감지해 백엔드 작업도 `/root/aads/aads-dashboard` worktree에서 실행했다.
  - 확인: `runner-ddb6bb2c`, `runner-5159ac44`의 `/tmp/aads-wt-*`가 `aads-dashboard` remote였고 `migrations/`, `app/`이 없었다. 두 작업은 산출 불가능 상태라 종료했다.
  - 조치: `is_aads_backend_instruction()`을 추가하고 `resolve_project_workdir()`이 AADS 백엔드 명시(`/root/aads/aads-server`, `migrations/`, `app/...`)를 대시보드 키워드보다 우선하도록 수정했다.
  - 운영 반영: `systemctl restart aads-pipeline-runner`로 새 스크립트를 로드했고, 신규 `runner-40d7dc37`이 `aads-server` remote 및 `migrations/` 보유 worktree에서 실행되는 것을 확인했다.
  - 검증: `bash -n scripts/pipeline-runner.sh` 통과.
- **AI 바이브코딩 디자인 수정 상세문서 작성 (2026-05-11 17:37 KST)**:
  - `docs/reports/20260511_AADS_VIBE_CODING_DESIGN_MODIFICATION_PLAYBOOK.md` 신규 작성.
  - 기존 디자인 연구/사용자 여정/스마트 디자인 시스템 문서를 근거로 CEO가 AI에게 세밀한 디자인 수정요청을 넣는 수정 카드, Design Context Pack, Design Memory, Before/After QA 루프, 러너 재개 후 지시서 초안을 정리했다.

- **채팅 세션/턴 TODO 게이트 추가 (2026-05-11 KST)**:
  - `migrations/083_chat_todo_items.sql` 추가. `chat_todo_items` 테이블에 `session_id`, `message_id`, `execution_id`, `title`, `status`, `sort_order`, `source`, `metadata`, `completed_at`와 세션/턴 기준 인덱스 및 partial unique 인덱스를 정의했다.
  - `app/services/chat_todo_service.py` 신규 추가. 세션/턴 todo 생성, 조회, 상태 전환(`pending/in_progress/completed/failed/skipped`), completion gate 평가, prompt block 생성, 감사용 `metadata.audit` 누적 로직을 구현했다.
  - `app/services/chat_service.py`에 복수 작업/도구 실행형 요청 감지 후 turn todo를 생성하는 훅을 연결했다. prompt에 `[세션 TODO 운영 규칙]`을 주입하고, 최종 저장 직전에 completion gate로 미완료 항목을 감지해 status/metadata를 갱신하며 필요한 경우 `[세션 TODO 점검]` 메모를 응답에 덧붙인다.
  - `app/main.py` startup schema 보강에 `ensure_chat_todo_schema()`를 연결해 migration 적용 전에도 신규 테이블/인덱스를 안전하게 보장한다.
  - `app/models/chat.py`에 `ChatTodoItemOut` 스키마를 추가했다.
  - 테스트:
    - `E2B_API_KEY=dummy pytest -q tests/unit/test_chat_todo_service.py tests/unit/test_chat_service.py` → `24 passed`
    - `E2B_API_KEY=dummy pytest -q tests/unit/test_context_continuity.py tests/unit/test_runner_scope_defaults.py tests/unit/test_intent_context_followups.py` → `11 passed`
  - 남은 리스크:
    - completion gate는 현재 응답 본문/도구 사용 흔적 기반 heuristic 판정이다. 항목 표현이 크게 바뀌면 일부 todo가 `pending`으로 남을 수 있다.
    - 실제 운영 Postgres에 `083_chat_todo_items.sql` 적용 자체는 이 세션에서 수행하지 않았고, migration 파일 존재/구조 검증과 startup schema 경로로 적용 가능성만 확인했다.

## 현재 진행 상태 (2026-05-11)
- **PC Agent VVIC 라우팅/락/큐 직접 패치 (2026-05-11 14:57~KST)**:
  - `runner-2db6f7fa`가 `claude_code_work` 진입 후 5분 이상 로그 0건/diff 0건으로 정체되어 강제 종료했다.
  - 직접 조치: `app/services/pc_agent_manager.py`에 capability 기반 agent 선택, per-agent/per-job lease, queue wait, stale lease 회수, routed command 실행 API 기반을 추가했다.
  - 직접 조치: `app/api/pc_agent.py`에 `POST /api/v1/pc-agent/route/execute`, `GET /api/v1/pc-agent/leases`를 추가하고 health 응답에 capabilities/leases를 노출했다.
  - 직접 조치: `pc_agent/agent.py`가 COMMAND_HANDLERS 기반 capabilities를 등록 payload로 전송하고, `pc_agent/commands/browser_auto.py`의 `browser_launch`는 `dedicated=true`/`port=0`에서 전용 프로필과 동적 CDP 포트를 사용하며 `/json/version` 준비를 확인한다.
  - 후속: NTV2 Bridge는 `/api/v1/pc-agent/route/execute` 계약에 맞춰 `job_type=vvic`, `required_capabilities=["vvic","chrome_cdp"]`, `browser_launch` params `dedicated=true`, `port=0`로 연동해야 한다.
- **Pipeline Runner Task Board 상태 표시 개선 (2026-05-11 14:20~14:25 KST)**:
  - 운영 DB 실측: `queued`는 0건이고, terminal 분류는 `blocked_dependency` 2건, `dedup_blocked` 2건, `no_changes` 2건, `done` 354건, `error` 4건이다.
  - `app/api/admin.py`: `/admin/tasks/stats`가 `no_changes`, `dedup_blocked`, `blocked_dependency` 카운트를 별도 반환하도록 보강했다.
  - `aads-dashboard/src/app/admin/tasks/page.tsx`: Admin Task Board에 `No Changes`, `Dedup Blocked`, `Blocked Dependency` 칼럼과 별도 색상/라벨을 추가해 세 terminal 상태가 Error로 보이지 않게 했다.
  - 검증: `python3 -m py_compile app/api/admin.py app/api/pipeline_runner.py`, `npx eslint src/app/admin/tasks/page.tsx`, `npm run build` 통과. 전체 `npm run lint`는 기존 전역 ESLint 오류 248건으로 실패했다.
  - 운영 반영: `bash /root/aads/aads-dashboard/deploy.sh` 성공. 활성 슬롯은 `blue`, 컨테이너 `aads-dashboard`는 `healthy/running`, 외부 `/login` 200 OK, 보호 페이지 `/admin/tasks`는 미로그인 기준 307 리다이렉트 정상.
- **PC Agent Chrome CDP 분리 프로필 반영 (2026-05-11 14:18 KST)**:
  - `pc_agent/commands/browser_auto.py`: `browser_launch()`가 기본 Chrome 프로필을 재사용해 `--remote-debugging-port=9222`가 무시되던 문제를 확인했다.
  - 조치: Windows는 `%LOCALAPPDATA%\\KakaoBot\\cdp-profile`, 비Windows는 `~/.kakaobot-cdp-profile`를 기본 `user_data_dir`로 사용하고 `--user-data-dir`, `--new-window` 옵션을 추가했다.
  - 현재 상태: 서버 측 소스에는 반영됐지만, CEO PC에서 실행 중인 에이전트 바이너리에는 즉시 적용되지 않는다. 실제 PC 에이전트 재배포 또는 재업데이트 후 재검증이 필요하다.
- **Pipeline Runner terminal 상태 분류 보강 (2026-05-11 14:13~14:18 KST)**:
  - `scripts/pipeline-runner.sh`: 변경 0건(`no_changes`)과 중복 차단(`dedup_blocked`)을 실제 실행 실패 `error`가 아니라 `cancelled` terminal 상태로 저장하도록 변경했다.
  - `scripts/pipeline-runner.sh`와 `app/api/pipeline_runner.py`: 선행 job이 `error/rejected/rejected_done/cancelled`이거나 DB에 없는 queued 작업은 `blocked_dependency`로 자동 종결한다.
  - 운영 DB 정리: 선행 `rejected_done`에 묶인 AADS queued 2건을 `blocked_dependency`로 종결했고, 기존 `no_changes`/`dedup_blocked` error 4건을 `cancelled`로 재분류했다.
  - 대시보드 `ChatArtifactPanel.tsx`: `display_status/status_label`을 사용해 `변경 없음`, `중복 차단`, `의존 차단`을 빨간 에러가 아닌 terminal 경고/종결 상태로 표시하고, 세션 안에 부모 job이 없는 의존 작업도 루트에 표시한다.
- **AADS Open Design Hub 기획 문서화 (2026-05-11 13:45 KST)**:
  - `docs/plans/AADS-SMART-DESIGN-SYSTEM.md`를 확장해 전 프로젝트 디자인 운영 체계인 `docs/plans/AADS-OPEN-DESIGN-HUB.md`를 신규 작성했다.
  - 핵심 방향은 공통 토큰, 프로젝트별 adapter, Design Auditor, Project Starter, Admin Design Hub UI를 분리하는 구조다.
  - 첫 러너 작업 범위는 대규모 UI 전면 교체가 아니라 Phase 0 기반(스키마 초안, API 계약, 스캐너 PoC, 구현 분해 문서)으로 제한한다.
- **AADS Open Design Hub Phase 0 직접 보강 (2026-05-11 13:54 KST)**:
  - `runner-0143f0a0`는 `claude_code_work` 중 로그/heartbeat 없이 산출물이 `.codex`만 남은 상태에서 2026-05-11 13:52:59 KST 강제 종료됐다.
  - 대안으로 기존 `app/services/design_audit_service.py` 및 `/api/v1/admin/design/*` read-only API 계약을 기준으로 `docs/plans/AADS-OPEN-DESIGN-HUB-IMPLEMENTATION.md`를 추가했다.
  - `tests/unit/test_design_audit_service.py`를 추가해 색상 탐지, Tailwind arbitrary color 탐지, 이모지 탐지, button class 반복 패턴, allowlist 경로 방어, empty input 동작을 검증한다.
- **Codex `unknown_tool: bash` 재발 방어 (2026-05-11 12:00 KST)**:
  - 증상: Codex CLI `command_execution` 이벤트가 AADS 채팅 도구 이벤트 `tool_use: bash`로 노출되어 CEO 화면에 `unknown_tool: bash` 결과가 반복 출력됐다.
  - 원인: `74c73a6`에서 릴레이 변환 코드는 수정됐으나, `claude-relay.service`는 2026-05-06 16:51 KST부터 계속 실행 중이라 새 코드가 로드되지 않았다. 또한 API 수신부에 구버전 릴레이 이벤트를 막는 2차 방어가 없었다.
  - 조치: `app/services/model_selector.py`에 `_is_internal_cli_command_tool()`을 추가하고 Codex relay에서 `bash`, `shell`, `command_execution` tool event를 `thinking` observation으로 변환하도록 보강했다.
  - 검증: `python3 -m py_compile app/services/model_selector.py scripts/claude_relay_server.py` 통과, `pytest -q tests/unit/test_relay_diagnostics.py tests/unit/test_chat_service.py::test_keyword_fallback_routes_only_explicit_discussion_queries tests/unit/test_chat_service.py::test_broad_tool_group_excludes_run_debate` 12 passed.
  - 운영 반영: `scripts/reload-api.sh` hot reload 완료(`2026-05-11 12:00:36 KST`, 재로드 67개). `claude-relay.service` 본체는 현재 생성 중인 세션을 끊을 수 있어 별도 재시작 필요.
- **114 Codex OAuth `refresh_token_reused` 재발 대응 (2026-05-11 11:53~12:00 KST)**:
  - 실측: 68/211은 `codex exec --skip-git-repo-check ... gpt-5.5` 최소 호출이 성공했지만, 114는 `refresh_token_reused` 및 `token_expired` 401로 실패했다.
  - 원인: 114의 `/root/.codex/auth.json`이 존재하고 `codex login status`도 `Logged in`으로 나오지만, 실제 access token refresh 단계에서 이미 사용된 refresh token으로 판정된다. 2026-05-05에도 같은 유형으로 “auth 파일 존재 여부가 아니라 실제 `codex exec` 성공 여부로 판단해야 한다”는 이력이 있었다.
  - 조치: `scripts/pipeline-runner.sh`에 Codex auth broken 쿨다운을 추가했다. `refresh_token_reused`, `token_expired`, `Please log out and sign in again` 감지 시 `/tmp/aads-codex-auth-disabled-until` 마커를 2시간 생성하고, 이후 같은 서버의 `codex:*` 모델은 즉시 skip 후 다음 모델로 넘어간다.
  - 운영 반영: 211/114 `/root/scripts/pipeline-runner.sh`에 동기화했고 `aads-pipeline-runner`를 재시작했다. 114에는 현재 깨진 OAuth 상태를 반영해 쿨다운 마커를 즉시 생성했다. 211은 Codex 실행 성공 상태라 마커를 생성하지 않았다.
  - 주의: 68의 현재 ChatGPT OAuth 파일을 114로 단순 복사하면 114는 일시 복구될 수 있으나, OAuth refresh token 회전 특성상 다음에는 68 또는 114 중 한쪽이 다시 `refresh_token_reused`로 깨질 수 있다. 114는 독립 device-auth 재로그인이 근본 복구다.

## 현재 진행 상태 (2026-05-09)
- **Pipeline Runner 모델 설정/원격 LiteLLM/동시성 보강 (2026-05-09 10:36~KST)**:
  - 제출 API 기본 정책을 “어드민 `runner_model_config` 자동 선택”으로 고정했다. `worker_model`은 `worker_model_reason`이 함께 들어온 경우에만 `pipeline_jobs.worker_model`에 저장하며, 사유가 없으면 무시하고 자동 설정값을 사용한다.
  - DB에 `pipeline_jobs.model_override_reason` 컬럼을 추가했다(`migrations/081_pipeline_runner_model_override_reason.sql`).
  - `scripts/pipeline-runner.sh`에 `RUNNER_ENGINE_MODE=general|litellm` 분기를 추가했다. 일반 러너는 원격 프로젝트의 `litellm:*` 작업을 claim하지 않고, LiteLLM 전용 러너는 `model` 또는 `worker_model`이 `litellm:*`인 작업만 claim한다.
  - 211/114처럼 `aads-server` 컨테이너가 없는 서버에서도 `python3 /root/scripts/litellm_runner.py`로 직접 실행하도록 원격 LiteLLM 경로를 보강했다. MCP 서버가 없으면 `litellm_runner.py`가 로컬 파일/git 도구 폴백을 사용한다.
  - 검증: `python3 -m py_compile app/api/pipeline_runner.py app/api/ceo_chat_tools.py app/services/tool_registry.py app/services/tool_executor.py scripts/litellm_runner.py` 통과, `bash -n scripts/pipeline-runner.sh` 통과, 운영 DB 컬럼 생성 확인.
- **채팅 메모리/Auto-RAG 맥락 유지 보강 (2026-05-09 07:02 KST)**:
  - `app/services/chat_embedding_service.py`: `search_semantic()` 결과에 `session_id`를 반환해 Auto-RAG가 same-session/cross-session 출처를 정확히 판정하도록 수정했다. 메시지 임베딩 예약 공통 함수 `schedule_message_embedding()`을 추가했다.
  - `app/services/context_builder.py`: 현재 프롬프트 히스토리에 이미 포함된 `chat_messages.id`를 Auto-RAG로 전달해 동일 메시지가 `<auto_rag_context>`에 중복 주입되지 않도록 했다.
  - `app/services/chat_service.py`: 히스토리 로드 쿼리에 `id`를 포함하고, `streaming_placeholder`를 최종 assistant 응답으로 promote하는 경로에서도 최종 본문 임베딩을 예약하도록 보강했다.
  - 테스트: `pytest -q tests/unit/test_memory_context_regression.py` 3 passed, `python3 -m py_compile app/services/chat_embedding_service.py app/services/auto_rag.py app/services/context_builder.py app/services/chat_service.py tests/unit/test_memory_context_regression.py` 통과, 변경 파일 대상 `git diff --check` 통과.
  - 실측 DB 상태: 신규 누락 방지 패치 적용 후 과거 `chat_messages` 미임베딩 대상 백필을 완료했다. 2026-05-09 09:55 KST 기준 role별 본문 10자 이상 `embedding IS NULL` 대상은 0건이다.
  - 주의: 전체 `git diff --check`는 기존 사용자 변경 파일 `docs/CHANGELOG-direct-edit.md`의 trailing whitespace로 실패한다. 이번 변경 코드 파일에는 whitespace 오류가 없다.

## 현재 진행 상태 (2026-05-06)
- **NewTalk V1 E2E 계정 env/Vault 관리**:
  - `.env.e2e.local`에 뉴톡V1 관리자/도매/소매 E2E 계정을 로컬 전용으로 저장하고 `.gitignore`에 `.env.*` 예외 규칙을 보강했다. 실제 비밀번호는 git 추적 대상에서 제외한다.
  - `.env.e2e.example`에 키 이름과 로그인 URL 템플릿을 추가했다. 관리자/도매는 V1 `https://newtalk.kr/auth/login`, 소매는 `https://pick.newtalk.kr/auth/login` 기준이다.
  - `scripts/seed_e2e_credentials.py`를 추가해 env 값을 AADS `e2e_credentials` Credential Vault에 암호화 저장할 수 있게 했다.

## 현재 진행 상태 (2026-05-06)
- **대시보드 정적 reports HTML 공개 경로 복구 (2026-05-06 14:46 KST)**:
  - 증상: `https://aads.newtalk.kr/reports/20260506_newtalk_ai_virtual_model_fitting_service_plan.html` 접속 시 로그인 페이지로 `307` 리다이렉트되어 브라우저에서 보고서가 열리지 않았다.
  - 원인: HTML 파일은 `aads-dashboard/public/reports/` 및 운영 컨테이너 `/app/public/reports/`에 존재했지만, Next.js `src/middleware.ts`의 인증 미들웨어가 `/reports/*.html` 정적 파일까지 보호 경로로 처리했다.
  - 조치: `aads-dashboard/src/middleware.ts`에 `/reports/<filename>.(html|htm|pdf|txt|md|csv|json)` 정적 파일만 공개 통과시키는 패턴을 추가했다. `/reports` 대시보드 페이지 자체는 기존 인증 정책을 유지한다.
  - 배포: `bash /root/aads/aads-dashboard/deploy.sh` blue-green 성공. 활성 슬롯은 `green`, 컨테이너 `aads-dashboard-green` 상태 `running`.
  - 검증: 내부 `http://127.0.0.1:3100/reports/20260506_newtalk_ai_virtual_model_fitting_service_plan.html` 200 OK, 외부 `https://aads.newtalk.kr/reports/20260506_newtalk_ai_virtual_model_fitting_service_plan.html` 200 OK, `Content-Type: text/html; charset=UTF-8`, `Content-Length: 32058`.
- **deploy_safe 실행 컨텍스트 보강 + 실패 러너 수동 완료 (2026-05-06 14:42 KST)**:
  - 대상: `runner-dbd3068f` (`AADS deploy_safe 실행성 수정`)는 Claude CLI가 root 권한에서 `--dangerously-skip-permissions`를 거부해 작업 시작 전 실패했다.
  - 조치: `app/services/tool_executor.py`에서 실행 컨텍스트를 감지하도록 보강했다. 호스트에서는 `scripts/reload-api.sh`를 실행해 `.active_container` 기준 활성 컨테이너로 위임하고, 컨테이너 내부 `reload`는 `bash /app/scripts/reload-api.sh`를 직접 실행한다. 컨테이너 내부 `bluegreen`/`restart-single`은 호스트 docker/deploy 컨텍스트가 없으면 명확한 오류를 반환한다.
  - 조치: `deploy_safe` post-health를 5초 1회에서 최대 36회 재시도 방식으로 바꿔 supervisor 재기동 지연을 실패로 오판하지 않도록 했다.
  - 검증: `python3 -m compileall app/services/tool_executor.py tests/unit/test_deploy_safe.py` 통과, 운영 활성 컨테이너 `aads-server-green`에서 수정 테스트 `/tmp/test_deploy_safe.py` 기준 `14 passed`.
  - 주의: 운영 컨테이너에는 `tests/`가 볼륨 마운트되어 있지 않아 최신 테스트 파일을 `/tmp/test_deploy_safe.py`로 복사해 검증했다.
- **Chat Lightweight v2.2 도구박스/최종버블 회귀 보강**:
  - Backend: `fields=minimal`은 표시용 preview와 도구 요약 메타(`has_tools`, `tool_count`, `tool_names`)만 반환하고, full `tools_called`는 신규 단건 상세 API `GET /api/v1/chat/messages/{message_id}`에서 lazy hydrate한다.
  - Backend: `normalize_tool_events()`를 추가해 legacy string 배열, Codex relay 구조화 이벤트, tool_result/thinking 이벤트를 동일한 `tools_called` 배열 계약으로 정규화한다. 저장 전과 full 응답 전 모두 이 경로를 사용한다.
  - Frontend: 완료 assistant 버블에서 `tools_called`가 비어도 `has_tools/tool_count/tool_names`가 있으면 도구박스를 숨기지 않고 hydrate 상태를 표시한다. hydrate 후에는 기존 긴 본문을 minimal 200자 preview로 덮어쓰지 않는다.
  - Frontend: 스트리밍 중 누적한 `tool_use/tool_result` 이벤트를 final assistant 메시지에 합쳐 완료 직후 도구박스가 사라지지 않게 했다.
  - Model alias: `codex:gpt-5.5`, `gpt-5.5`, `GPT-5.5 (Codex CLI)`를 Codex 실행 모델 `gpt-5.5`로 정규화한다.
  - 원칙: DB/LLM 원본 메시지, embedding, quality/reflexion/memory/RAG 저장 경로는 축소하지 않는다. 축소는 프론트 표시 API payload에만 적용한다.
  - 검증 명령: `python3 -m pytest tests/unit/test_chat_service.py tests/unit/test_chat_lightweight_frontend_static.py -q`
  - 수동 확인: 세션 `b8a8651b-6226-46df-9a44-36a70e478959`에서 minimal polling 후 도구박스 placeholder, 단건 hydrate 1회, 800자 이상 본문 길이 유지, Codex final 도구 이벤트 보존을 확인한다.
  - 남은 리스크: 실제 브라우저 DOM 확인은 운영 세션 데이터와 인증 토큰이 필요한 경로라 자동 단위 테스트는 정적/서비스 계약 중심으로 커버한다.

## 현재 진행 상태 (2026-05-05)
- **Android Agent Play Protect 차단 대응 (2026-05-05 15:09 KST)**:
  - 원인: 운영 다운로드 APK가 debug 계열 파일명/후보로 제공되고, release APK도 SMS/통화기록/연락처/접근성/알림리스너/디바이스관리 등 고위험 권한을 포함해 Play Protect 차단 가능성이 높았다.
  - 조치: release Manifest를 최소 권한(`INTERNET`, `ACCESS_NETWORK_STATE`, foreground data sync, notification, vibrate)으로 축소하고, 전체 권한 Manifest는 `app/src/debug/AndroidManifest.xml`로 분리했다. `build_release_apk.sh`를 추가하고 운영 APK 라우트 및 BG 빌드를 release 기준으로 전환했다.
  - 즉시 반영: 재시작 없이 실행 중인 `aads-server`, `aads-server-green` 컨테이너의 `/app/android_agent/dist/{aads-agent-debug.apk,aads-agent-release.apk,aads-agent-fresh.apk}`를 새 release APK로 교체했다.
  - 검증: `./build_release_apk.sh` 성공, `./build_debug_apk.sh` 성공, 공개 `/download`, `/download-fresh`, `/download-standard` 3개 URL 모두 sha256 `8aee20a21860d1d440fb81a5fc1809b07d8ee6ffd8c075df4fe04eb1d8f1613e` 확인. `aapt dump permissions` 기준 공개 APK 권한 6개, `apksigner verify` v2 서명 통과.
- **Common Browser Bridge 모듈 스켈레톤 추가 (2026-05-05 KST)**:
  - 공통 계층: `app/browser_bridge/` 추가. `BrowserEndpointKind(cdp/websocket/local_agent/storage_state/headless)`, one-time pairing token, 세션 registry, storageState manager, Playwright context adapter, E2E config adapter를 AADS 채팅과 분리된 모듈로 구성했다.
  - 보안 경계: CDP/WebSocket endpoint는 기본적으로 `localhost`/loopback만 허용한다. pairing token은 원문 저장 없이 hash만 보관하고 1회 사용 후 재사용을 거부한다. storageState는 `.browser_bridge_state/` 하위에만 저장되며 `.gitignore`에 추가했다. `browser_fill` 결과는 입력값을 echo하지 않도록 바꿨다.
  - API: `app/api/browser_bridge.py` 추가 및 `app/main.py` 라우터 등록. `POST /api/v1/browser-bridge/pairings`는 인증된 사용자가 pairing token을 만들고, `POST /api/v1/browser-bridge/sessions/register`는 local bridge/Chrome 쪽에서 token으로 세션을 등록한다. `GET /sessions`, `POST /sessions/select`, `GET /e2e/config`로 등록 세션과 E2E 인터페이스를 조회한다.
  - AADS 도구 연동: `browser_connect` 도구를 추가했다. `status`, `create_pairing`, `select` action을 지원하며 기존 `browser_navigate/snapshot/screenshot/click/fill/tab_list`는 Browser Bridge 활성 세션을 우선 사용하고 없으면 기존 headless Playwright 경로를 사용한다.
  - CEO OTP 흐름: `browser_connect(action="create_pairing")` → CEO 로컬 Chrome/브릿지 에이전트가 `/sessions/register`에 `endpoint.kind=cdp` 또는 `storage_state`로 등록 → CEO가 로컬 Chrome에서 OTP 완료 → AADS browser 도구가 활성 세션을 재사용한다.
  - E2E 인터페이스: `app.browser_bridge.e2e_adapter.build_e2e_config()`를 추가했다. 환경변수 `AADS_BROWSER_BRIDGE_SESSION_ID`, `AADS_BROWSER_BRIDGE_CDP_URL`, `AADS_BROWSER_BRIDGE_WS_URL`, `AADS_BROWSER_BRIDGE_STORAGE_STATE`가 있으면 final Playwright 확인이 bridge 세션을 우선 사용하고, 없으면 headless Playwright 설정을 반환한다. `app/services/visual_qa.py` 캡처도 이 config를 인자로 전달한다.
  - 검증 기록: `tests/unit/test_browser_bridge.py`에 loopback 검증, public CDP 차단, one-time token 재사용 거부, storageState 경로 검증을 추가했다.

## 현재 진행 상태 (2026-05-04)
- **Android Agent 전기능 구현 후속 안정화 + 채팅 응답 표시 복구 문서화/커밋 준비 (2026-05-04 08:20 KST)**:
  - Android: `runner-4f922625` 산출물은 `05c7dc7`로 이미 커밋되어 있으며, `CommandDispatcher.java` 기준 57개 명령/alias가 등록되어 있다. 후속 패치로 `AndroidCommandHandlers.SensorSnapshot.toJson()`에서 `NaN`/`Infinity` 센서값을 JSON 배열에 넣다 실패하지 않도록 non-finite 값을 skip 처리했다.
  - Chat: DB에 저장된 AI 검수/상태 보고(`intent=runner_response`)가 채팅 본문에서 사라지는 원인을 `app/services/chat_service.py`, `app/routers/chat.py`, 대시보드 `src/app/chat/page.tsx` 필터로 확인했다. 자동 트리거/시스템 로그는 계속 숨기되 `runner_response`는 사용자-visible assistant 응답으로 남기도록 조정했다.
  - Runner: AADS Pipeline Runner per-project 동시 실행 상한을 `MAX_CONCURRENT_PER_PROJECT=6`으로 맞추고 `scripts/pipeline-runner.sh`, `scripts/aads-pipeline-runner.service`, `docs/pipeline-runner/*`, `docs/knowledge/CTO-SYSTEM-MAP.md`에 반영했다.
  - 기술문서: `docs/reports/20260504_ANDROID_AGENT_CHAT_VISIBILITY_TECHNICAL.md` 추가. Android 구현 범위, runner_response 표시 복구, Runner timeout/review diff 신뢰도 주의사항, 추후 검증 명령을 기록했다.
  - 주의: `runner-4f922625`는 코드 검수 승인 후 finalize/deploy 단계에서 timeout/error 이력이 있으므로, 배포 완료 보고는 반드시 APK 다운로드/컨테이너/health 실측 후에만 가능하다.

## 현재 진행 상태 (2026-04-28)
- **역할 분류 체계 + 사업화 역할 + Agent Registry 관리 UI 반영 (2026-04-30 18:59 KST)**:
  - DB: `migrations/077_role_taxonomy_and_business_roles.sql` 추가 및 운영 DB 적용 완료. 기존 `role_profiles` 26건에 분류 메타데이터를 반영하고, 사업화 역할 8건(`GTMStrategist`, `BrandMarketingLead`, `SalesPartnershipLead`, `PricingMonetizationStrategist`, `CustomerSuccessLead`, `RevenueOperationsAnalyst`, `FinanceFundraisingLead`, `LegalIPAdvisor`)과 L3 `prompt_assets` 8건을 추가/갱신했다.
  - 분류 결과: 의사결정·전략 4건, 제품·사용자경험 3건, 개발·구현·검증 9건, 보안·리스크·거버넌스 2건, 사업화·매출·시장진입 8건.
  - 백엔드: `GET /api/v1/admin/agents`와 상세 API가 `role_category`, `role_category_label_ko`, `role_group_order`, `lifecycle_stage`, `project_scope`, 활용 기준/지시 방법/템플릿을 반환한다. `/chat/workspaces/{workspace_id}/roles`도 role group order 기준 정렬과 카테고리 메타데이터를 포함한다.
  - 대시보드: `/root/aads/aads-dashboard/src/app/admin/agents/page.tsx` 신규 추가. 분류 필터, 역할 검색, 프로젝트 범위, 활용 기준, 최근 작업 상세를 한 화면에서 확인할 수 있다.
  - 검증: `python3 -m py_compile app/api/admin.py app/services/chat_service.py` 통과, `npx eslint src/app/admin/agents/page.tsx` 통과, `npm run build` 통과, `npx tsc --noEmit --pretty false` 통과, 운영 DB 적용 결과 `UPDATE 26`, `INSERT 0 8`, `INSERT 0 8`, `COMMIT` 확인. 2026-04-30 19:28 KST 기준 백엔드 reload 6단계 검증 통과, 대시보드 blue-green 배포 성공, `/admin/agents` 외부 URL은 인증 리다이렉트(`/login?redirect=%2Fadmin%2Fagents`) 정상.
- **서버114 CROSS-MONITOR 알림 조치 완료 (2026-04-30 09:02 KST)**:
  - 증상: `Exec(114) 심각 — 디스크100% HTTP-health실패` 텔레그램 알림.
  - 실측: 서버114 `/` 디스크는 `875G 중 686G 사용, 181G 여유, 80%`로 100% 상태가 아니며 warning 임계 구간. ShortFlow/NewTalk V2 Docker 컨테이너는 모두 Up.
  - 원인: AADS 헬스체커가 서버114 SSH 포트 `7916`을 HTTP health URL로 하드코딩하고 있었다. 실제 `116.120.58.155:7916`은 `sshd` 포트라 HTTP 요청이 connection refused 처리된다.
  - 조치: `app/services/health_checker.py`, `app/services/server_registry.py`, `app/services/tool_executor.py`의 114/SF/NTV2 HTTP health URL을 `https://sf.newtalk.kr/`, `https://v2.newtalk.kr/`로 교체하고 `aads-api`를 supervisorctl로 재시작했다.
  - 검증: `python3 -m py_compile app/services/health_checker.py app/services/server_registry.py app/services/tool_executor.py` 통과, `_check_http_health("114")`가 `ok=True` 반환, `https://aads.newtalk.kr/api/v1/health` 정상.
- **L3 Role 프롬프트 전문성 강화 DB 반영 완료 (2026-04-29 08:55 KST)**:
  - 신규 마이그레이션: `migrations/065_strengthen_l3_role_prompts.sql` 추가 및 운영 DB 적용 완료.
  - 적용 결과: `prompt_assets` L3 활성 40건 유지, 평균 본문 길이 283자 → 390자, 최대 820자. 핵심 역할 10개에는 판단 기준/필수 확인/작업 절차/산출물/검증/에스컬레이션 구조를 반영했다.
  - 강화 대상: `CTO`, `PM`, `Developer`, `QA`, `SRE`, `SecurityPrivacyOfficer`, `RiskComplianceOfficer`, `DataEngineer`, `PromptContextHarnessEngineer`, `JudgeEvaluator` 및 AADS/GO100/NTV2 핵심 오버레이.
  - `role_profiles.escalation_rules`에 `quality_rubric_version=l3-role-rubric-v1`, `requires_evidence=true`, `requires_verification_before_done=true`를 추가했다.
  - 샘플 매칭 검증: AADS+CTO, AADS+PromptContextHarnessEngineer, GO100+RiskComplianceOfficer/DataEngineer, NTV2+SecurityPrivacyOfficer/UXProductDesigner 모두 공통 역할 + 프로젝트 오버레이 2단 매칭 확인.
- **좌측 채팅 세션 역할 지정 UX 배포 완료 (2026-04-28 18:49 KST)**:
  - 백엔드: `GET /api/v1/chat/workspaces/{workspace_id}/roles` 추가. `role_profiles.project_scope` 기준으로 워크스페이스/프로젝트별 역할 목록을 반환하며, 한글 표시명은 `escalation_rules.display_name_ko`에서 읽는다.
  - 프런트: `aads-dashboard/src/components/chat/Sidebar.tsx`의 각 세션 행에 역할 지정/변경 드롭다운을 추가했다. 저장은 기존 `PUT /chat/sessions/{session_id}`의 `role_key`로 수행된다.
  - DB 실측: `role_profiles` 17건, 한글 표시명 포함. AADS 전용 `PromptContextHarnessEngineer / 프롬프트·컨텍스트·하네스엔지니어` 포함.
  - 배포: `bash /root/aads/aads-dashboard/deploy.sh` blue-green 성공, 활성 슬롯 `green`, `aads-dashboard-green` healthy. 컨테이너 내부 `.next` 번들에서 `getChatWorkspaceRoles`/역할 지정 UI 문자열 확인.
  - 검증: `python3.11 -m py_compile app/routers/chat.py app/services/chat_service.py` 통과, `npm run build` 통과, `npx eslint src/components/chat/Sidebar.tsx` 통과, `/health` 200. 전체 `npm run lint`는 기존 누적 오류 255건으로 실패 상태 유지.
- **채팅 싱킹박스 대화 버블 노출 패치 완료 (2026-04-28 18:00 KST)**:
  - 원인: `/chat` 운영 화면은 `ChatStream.tsx`/`ThinkingIndicator.tsx`가 아니라 `src/app/chat/page.tsx`의 인라인 `MessageItem` 렌더러를 사용한다. 따라서 도구박스 하단에 별도 컴포넌트를 만들어도 실제 대화 버블에는 표시되지 않았다.
  - 대시보드: `ChatMessage.thinking_summary` 타입을 추가하고, 최종 assistant 버블에서 `tools_called` 도구박스 바로 아래에 `thinking_summary/thought_summary` 접이식 사고 과정 박스를 렌더링한다. 저장된 `tools_called` 안의 thinking 이벤트도 `ev.thinking`/`ev.content` 양쪽을 표시한다.
  - 백엔드: LiteLLM/OpenAI 호환 스트림의 `reasoning_content`를 답변 본문에 섞지 않고 `thinking` SSE 이벤트로 분리해 저장한다. Output Validator 재시도 경로도 thinking 누락 없이 `thinking_summary`에 누적한다.
  - 검증: `docker exec aads-server python3 -m py_compile /app/app/services/model_selector.py /app/app/services/chat_service.py` 통과, `aads-dashboard npm run build` 통과.
- **채팅 진행 중 버블 P0 안정화 패치 완료 (2026-04-28 17:47 KST)**:
  - `aads-dashboard/src/app/chat/page.tsx`에서 `streaming_placeholder` 메시지는 800자 초과 긴 메시지 자동 접힘 대상에서 제외했다.
  - `streaming-status.is_streaming=true` 상태에서는 프론트의 180초 타이머가 `waitingBgResponse`를 강제로 끄지 않도록 변경했다. 진행 표시 종료는 서버 `streaming-status`의 `is_streaming/just_completed` 상태 기준으로만 결정한다.
  - 검증: `git diff --check -- src/app/chat/page.tsx` 통과, `npm run build` 통과. `npx eslint src/app/chat/page.tsx`는 기존 누적 9 error/21 warning으로 실패 상태 유지.
- **LLM 최신모델 자동 업데이트 및 GPT-5.5 반영 완료**:
  - `migrations/059_llm_model_discovery.sql`로 `llm_models`에 discovery/execution/verification/pricing/capabilities 컬럼을 추가하고 `llm_model_discovery_runs` 이력 테이블을 도입했다.
  - `app/services/model_registry.py`가 OpenAI/Gemini/LiteLLM catalog를 운영 컨테이너에서 조회해 DB 레지스트리에 병합한다. 최종 startup 기준 OpenAI 115개, Gemini 50개, LiteLLM 76개 발견. Anthropic은 OAuth 실행 가능 상태와 Models API discovery 가능 상태를 분리해, OAuth-only일 때 `oauth_runtime_only_models_api_unavailable` skip 및 `runtime_executable=true`, `auto_discovery_supported=false`, `discovery_requirement=x-api-key required...` 메타데이터로 기록한다.
  - Codex CLI `gpt-5.5`를 `model_registry`, `model_selector`, `claude_relay_server.py`, `pipeline_runner_service.py`, `pipeline-runner.sh`, 대시보드 selector/settings에 반영했다.
  - 실제 Codex relay E2E: `/codex-stream` `model=gpt-5.5`가 `AADS_GPT55_OK`, `model: gpt-5.5`로 응답 확인.
  - API E2E: active 모델 140개, `codex:gpt-5.5`와 `openai:gpt-5.5` 모두 active 확인.
- **LLM 최신모델 자동반영 보강 3차 패치 (2026-04-29)**:
  - DeepSeek canonical ID를 `deepseek-v4-flash`, `deepseek-v4-pro`로 등록했다. `deepseek-chat`, `deepseek-reasoner`는 호환 alias로 유지하며 metadata에 `canonical_model`, `compatibility_alias=true`, `deprecation_date=2026-07-24`를 남긴다.
  - DeepSeek 실행은 LiteLLM proxy 경로로 고정했다. 과거 DB metadata가 `openai_compatible_direct`로 남아 있어도 selector가 `litellm_proxy`로 보정하고 alias 요청은 canonical 실행 ID로 변환한다.
  - Provider summary는 `runtime_executable`, `auto_discovery_supported`, `discovery_requirement`, `active_model_source`, `template_active_model_count`, `discovery_active_model_count`를 노출한다. 확인 API: `/api/v1/llm-models/providers/summary`, `/api/v1/llm-models/discovery-runs?limit=8`.
  - 검증: `E2B_API_KEY=test python3.11 -m pytest tests/unit/test_model_registry.py tests/unit/test_model_selector_dynamic_routing.py tests/unit/test_llm_registry_sync_flow.py -q` 기준 24 passed.
- **model_selector registry 의존성 보강 (2026-04-29)**:
  - `app/services/model_registry.py`가 Anthropic 템플릿에 `accepted_aliases`와 실제 `execution_model_id`를 함께 기록한다. 예: `claude-sonnet` → `claude-sonnet-4-6`.
  - `app/services/model_selector.py`는 입력 모델을 static alias 맵보다 registry row 기준으로 정규화하고, 모델 미가용 시 provider/family/category/capability/cost 유사도로 fallback 후보를 고른다.
  - Codex는 static allowlist 밖 신규 모델도 registry row에 `execution_backend=codex_cli`가 있으면 relay 경로로 라우팅된다.
  - 검증: `pytest -q tests/unit/test_model_registry.py tests/unit/test_model_selector_dynamic_routing.py tests/unit/test_llm_registry_sync_flow.py` 기준 28 passed.
- **채팅 모델 상단고정 provider별 분리 완료**:
  - 원인: `chat_model_preferences`가 `model_id` 단일 PK라 `openai:gpt-5.5`와 `codex:gpt-5.5`가 `gpt-5.5`로 충돌했다.
  - `migrations/060_chat_model_preferences_provider_scope.sql`로 PK를 `preference_key`로 전환했다. 형식은 `provider:model_id`, 자동 라우팅은 `mixture`.
  - `app/api/llm_models.py`, `aads-dashboard/src/components/settings/LlmRegistryWorkspacePanel.tsx`, `aads-dashboard/src/app/chat/page.tsx`를 provider-qualified 기준으로 수정했다.
  - 최종 API 검증: `codex:gpt-5.5:true`, `openai:gpt-5.5:false`. 라우팅 검증: `openai:gpt-5.5 -> openai_compatible_direct`, `codex:gpt-5.5 -> codex_cli`.
  - 서버 `deploy.sh` 6단계 통과, 대시보드 blue-green 배포 및 프론트 QA 통과.
- **채팅 SSE 재진입 UX 3건 패치 완료** (b24b47f + 56ed27c):
  - **BUG #3**: `app/routers/chat.py` streaming-status DB fallback에서 `tool_count`/`last_tool`을 `tools_called` JSON에서 산출 (running/just_completed/placeholder 3분기). asyncpg가 jsonb를 str로 반환하는 케이스도 처리.
  - **Patch A** (`aads-dashboard/src/app/chat/page.tsx:1742`): `streaming-status` 응답의 `partial_content`/`tool_count`/`last_tool`을 즉시 `setStreamBuf`/`setToolStatus`로 주입. 진입 시 빈 버블 방지.
  - **Patch B** (`aads-dashboard/src/app/chat/page.tsx:1322`): `attachExecutionReplay`가 SSE 18종 모두 처리(이전 3종). `tool_use`/`tool_result`/`thinking`/`stream_start`/`stream_reset`/`yellow_limit`/`model_info`/`sdk_*`/`error` 핸들러 추가 — sendMessage 메인 루프와 동등.
  - **배포**: `docker compose build aads-dashboard` (image f9c82f89) → `up -d aads-dashboard` healthy. `bash scripts/reload-api.sh` 68개 모듈 재로드.
  - **푸시 확인**: `b24b47f` (aads-server main), `56ed27c` (aads-dashboard main) — 모두 origin 반영 완료.
  - **문서**: `docs/knowledge/SSE-STREAMING-ARCHITECTURE.md` v2.0 → **v2.1** 업데이트 (Layer 7: Re-attach Full SSE Replay 추가). `docs/chat/CHAT-CHANGELOG.md` 2026-04-28 항목 추가.
  - **별도 보고서**: `reports/20260428_session_fork_analysis.md` — 누적 4000건 세션 분기 권유 정밀 분석 + 개선안 5종.

## 현재 진행 상태 (2026-04-27)

- **5-Layer Prompt 시스템 마감 검증 (직접 작업)**:
  - **DB**: prompt_assets 6 컬럼(layer_id/role_scope/target_models/workspace_scope/intent_scope/model_variants) + 시드 10건 활성 — L1 글로벌 2건, L2 프로젝트 3건, L3 역할 2건, L4 인텐트 2건, L5 모델 1건. compiled_prompt_provenance 테이블 정상.
  - **백엔드**: PromptCompiler.compile()이 5축(workspace/intent/target_models/role_scope) 모두 SQL 필터로 처리. chat_service.py:3873에서 매 채팅 턴 호출.
  - **API**: app/api/admin.py에 /admin/prompt-assets CRUD 5종(GET/POST/PUT/PATCH toggle/DELETE) + preview 완비.
  - **프런트**: aads-dashboard/src/app/admin/prompts/page.tsx(268줄) 5-Layer 카드/필터/편집/미리보기 UI. api.ts에 5종 메서드. Sidebar에 📝 Prompts 메뉴(/admin/prompts) 노출.
  - **provenance 0건 진단 패치**: chat_service.py PromptCompiler 호출부에 [PROMPT_COMPILER] 4단계 진단 로그(enter/compiled/recorded/failed) 추가, session_id를 str() 명시 캐스팅, record_prompt_provenance 실패를 별도 except로 분리. 다음 채팅 턴부터 compiled_prompt_provenance 적재 추적 가능.
  - **Hot-Reload**: scripts/reload-api.sh 62개 모듈 재로드 완료(10:49 KST), SSE 영향 없음.

최종 업데이트: 2026-04-24

## 현재 진행 상태 (2026-04-25)
- **2026-04-25 Governance v2.1 마감 (직접 작업)**:
  - **P0 temperature 배선 완료**: `model_selector.py`에 `contextvars` 기반 `_ctx_temperature`를 도입해 `call_stream()` → `_stream_litellm_anthropic` / `_stream_litellm_openai` / `_stream_cli_relay` 3개 LLM 경로 모두에 인텐트별 temperature를 전달한다. `resolve_intent_temperature()` → `intent_policies.temperature` DB 조회 → 하드코딩 맵 폴백 체인으로 작동. 실측 검증: greeting=0.1, strategy=0.15, code_task=0.15, casual=0.2.
  - **P0 W3 DB 마이그레이션 완료**: `scripts/migrations/20260424_governance_v2_1_w3.sql` 실행으로 `prompt_assets`, `prompt_asset_versions`, `session_blueprints`, `prompt_change_requests`, `cr_approvals`, `compiled_prompt_provenance` 6개 테이블 생성. `session_blueprints`에 `default.standard` 시드 삽입.
  - **P1 prompt_compiler 활성화**: W3 테이블 생성으로 `PromptCompiler.compile()` (chat_service.py L3873)이 실제 `prompt_assets` + `session_blueprints` DB 조회 경로로 작동 시작. `record_prompt_provenance()`로 `compiled_prompt_provenance`에 빌드 이력 저장.
  - **P0 feature_flags.py 호스트 패치**: `governance_enabled()` helper 함수를 호스트 파일에 추가 (로컬 워크트리에만 존재하던 상태 보정).
  - **runner-af09281f 정리**: depends_on이 rejected_done인 영구 대기 러너를 error 상태로 전환.
  - **runner-34c0836a 제출**: Admin Dashboard 4개 페이지(governance/model-parity/deploy/sessions) 일괄 구현 러너 (실행 중).
  - **API Hot-Reload**: 54개 모듈 재로드 완료, health-check 전항목 정상 확인.

- **2026-04-24 직접 보강**: AADS 채팅 실행 복구를 `execution_id` 중심으로 전환했다. `chat_turn_executions`, `chat_messages.execution_id`, `chat_sessions.current_execution_id`를 도입했고, `app/services/chat_service.py`, `app/routers/chat.py`, `app/services/redis_stream.py`, `app/services/stream_worker.py`, `app/main.py`에서 execution 단위 SSE attach/replay, 단일 assistant row 재사용, execution 기반 resume 스캐너를 반영했다. 기존 `recovered` 추론 복구는 fallback 성격으로 축소됐다.
- **2026-04-24 운영 조치**: 서버 `deploy.sh`의 `code` 모드 health 대기 시간을 기본 30초에서 60초로 늘려, graceful restart 직후 앱이 정상 복귀했는데도 배포 스크립트가 거짓 실패로 종료하던 false negative를 줄였다. 대시보드 `deploy.sh`는 비활성 대상 슬롯 컨테이너가 남아 있을 때 선정리 후 기동하도록 보강했다.
- **2026-04-24 검증 결과**: Governance v2.1 후속 검증을 다시 수행했다. 백엔드 단위테스트는 `python3.11 -m pytest tests/unit/test_governance_v21.py tests/unit/test_governance_change_requests.py tests/unit/test_prompt_compiler.py -q` 기준 `10 passed`였고, 실제 프런트 빌드 루트인 `/root/aads/aads-dashboard`는 `./node_modules/.bin/tsc --noEmit --incremental false` 타입체크가 통과했다. 다만 실제 대시보드 체크아웃에는 `src/app/admin/model-parity/page.tsx`만 존재하고 `governance/emergency/sessions/deploy` 페이지와 Sidebar 링크는 아직 없으며, 현재 워크스페이스의 `aads-dashboard/`는 `src/` 스냅샷만 있어 여기서는 Next 빌드를 돌릴 수 없다. 또한 DB 마이그레이션 실적용 여부는 이 세션의 샌드박스가 `psql` 소켓 생성을 `Operation not permitted`로 차단해 실측하지 못했다.
- **2026-04-24 직접 보강**: Governance v2.1 운영 가시화를 추가했다. `app/api/governance.py`에 `GET /governance/role-profiles`를 추가해 `role_profiles.project_scope/tool_allowlist`를 노출했고, `aads-dashboard/src/app/admin/emergency/page.tsx`에서 `governance_enabled` kill-switch, 기타 feature flag, governance audit log, 역할별 프로젝트 범위를 한 화면에서 제어/확인할 수 있게 했다. `Sidebar.tsx`, `aads-dashboard/src/lib/api.ts`, `tests/unit/test_governance_v21.py`도 함께 갱신했다.
- **2026-04-24 직접 보강**: Governance v2.1 런타임 결함을 보정했다. `app/core/feature_flags.py`에 `governance_enabled()` helper를 추가했고, `app/services/intent_router.py`의 intent temperature 조회를 실제 스키마인 `intent_policies.temperature`로 정렬했다. `app/api/governance.py`는 `temperature` 필드를 조회/저장하도록 보강했고, `tests/unit/test_governance_v21.py`로 회귀 테스트를 추가했다.
- **2026-04-24 직접 보강**: Runner Task Board가 제출 모델(`model`)과 실제 실행 모델(`actual_model`)을 혼동하던 문제를 보강했다. `scripts/pipeline-runner.sh`가 시도 시작 즉시 `pipeline_jobs.actual_model`을 갱신하도록 바꿨고, `/admin/tasks` 목록과 `aads-dashboard/src/app/admin/tasks/page.tsx`가 `actual_model`을 우선 표시하며 상세 패널에 `Actual/Configured/Worker Override`를 분리해 보여준다.
- **2026-04-24 직접 보강**: Admin Dashboard 잔여 누락을 로컬 워크트리에 직접 반영했다. `app/api/admin.py`에 `/admin/sessions`, `/admin/sessions/{job_id}`를 추가했고, `aads-dashboard/src/lib/api.ts`에 sessions/model-parity API 메서드를 보강했으며, `aads-dashboard/src/app/admin/model-parity/page.tsx`를 신규 추가하고 `Sidebar.tsx`에 Governance/Model Parity/Deploy/Sessions 링크를 정리했다.
- **승인 대기**: `runner-db5686da` — `/admin/governance` 세션 거버넌스 대시보드 (백엔드+프론트)
- **승인 대기**: `runner-18ddd734` — `/admin/model-parity` 모델 패리티 대시보드 (백엔드+프론트)
- **2026-04-24 운영 조치**: `claude-relay` 전역 동시성은 Pipeline Runner를 포함하지 않는 것으로 재확인했다. live는 systemd drop-in `/etc/systemd/system/claude-relay.service.d/runtime.conf`로 `CLAUDE_RELAY_MAX_CONCURRENT=5`, `CLAUDE_NONINTERACTIVE_WRAPPER=/root/aads/aads-server/scripts/claude-docker-wrapper-active.sh`를 고정했다. blue-green 전환 후에도 relay/Claude CLI가 `.active_container`를 따라 현재 활성 API 컨테이너를 사용한다.
- **2026-04-24 운영 조치**: 채팅 active stream 계측은 `executing / visible / recovery_pending / recent_placeholders` 기준으로 재정리했다. 재배포 drain에서 실제 활성 스트림이 `2 → 1 → 0`으로 집계되는 것을 확인했고, 이전처럼 resume/placeholder 세션이 있어도 `active=0`으로 보이던 오판을 줄였다.
- **거버넌스 v2.1 Phase 1-A 준비**: `scripts/migrations/20260424_governance_v2_1_tables.sql` 추가 — `governance_events`, `intent_policies`, `role_profiles`, `change_requests` 생성 마이그레이션과 시드(`intent_policies=7`, `role_profiles=5`)를 반영했다.
- **거버넌스 v2.1 P1-D 거버넌스 컬럼 확장 (temperature + project_scope)**: `scripts/migrations/20260424_governance_v2_1_columns.sql` 추가 — `intent_policies.temperature`, `role_profiles.project_scope` 컬럼 확장과 `intent_policies` 기본 temperature 시드 업데이트를 반영했다.
- **migration 054** (`054_llm_key_provider_normalization.sql`) — untracked, DB 정규화 대상 0건으로 적용 무해
- **migration 055** (`chat_model_preferences`) — DB 적용 완료
- **인증 우선순위**: `ANTHROPIC_AUTH_TOKEN_2`(moongoby, priority=1), `ANTHROPIC_AUTH_TOKEN`(moong76, priority=2)
- **2026-04-24 장애 조치**: `llm_models.metadata`가 JSON 문자열 row일 때 `model_selector._route_metadata()`와 `model_registry.sync_model_registry()`가 `dict(...)`로 바로 처리하며 `ValueError`를 내던 공통 장애를 수정했다. `app/services/model_selector.py`, `app/services/model_registry.py`에 metadata coercion을 추가했고, 문자열 metadata 회귀 테스트를 `tests/unit/test_model_selector_dynamic_routing.py`, `tests/unit/test_model_registry.py`에 남겼다.
- **2026-04-24 장애 조치**: `app/services/model_registry.py`의 `filter_executable_models()`에 `_normalize_model_id()`를 추가해 `codex:`, `litellm:`, `claude:` 접두사를 제거한 뒤 `llm_models.model_id`와 비교하도록 수정했다. `claude-sonnet` vs `claude-sonnet-4-6` 같은 버전 suffix는 `startswith`로 허용해 `runner_model_config` 설정이 전부 탈락하면서 `minimax-m2.7` 폴백으로 내려가던 문제를 막는다. 회귀 테스트는 `tests/unit/test_model_registry.py`에 반영했다.
- **AADS-200B backend 반영**: `migrations/056_braming_node_feedback.sql`로 `braming_nodes`에 `ceo_opinion/picked` 컬럼을 추가하고 `braming_node_votes` 테이블을 도입했다. `app/services/braming_service.py`, `app/api/braming.py`는 노드 상세 조회, CEO 의견 저장/삭제, 찬반 투표 토글, Pick/Unpick API와 그래프 응답의 `ceoOpinion/voteSummary/myVote/picked` enrichment를 지원한다. 회귀 테스트는 `tests/unit/test_braming_service.py`, `tests/unit/test_braming_api.py`에 추가했다.
- **AADS-200B frontend 블로커**: 요구된 프론트 경로 `/root/aads/aads-dashboard/src/app/braming/*` 는 현재 워크스페이스 쓰기 허용 범위 밖이라 본 런에서는 수정하지 못했다. 다음 작업은 해당 경로 쓰기 권한이 열린 환경에서 `api.ts`, `page.tsx`, `components/BramingCanvas.tsx`, `components/BramingNode.tsx`, `components/NodeDetailPanel.tsx`를 백엔드 계약에 맞춰 연결하면 된다.

## AADS-190E
- `scripts/claude_relay_server.py`에 Claude/Codex 실행 preflight와 `aads-tools` MCP bridge preflight를 추가했다. `docker exec` 경로와 `python3.11 -m mcp_servers.aads_tools_bridge` 직접 실행 경로를 후보로 두고, 실패 원인을 `docker_container_missing`, `python_module_missing` 같은 분류로 로그에 남긴다.
- `scripts/mcp_config_template.json`의 기본 bridge 실행기를 `python3`로 정리해 템플릿 경로와 relay가 선택하는 docker 경로가 같은 실행기를 가리키도록 맞췄다.
- 같은 파일에서 Claude 기본 실행 경로는 `scripts/claude-docker-wrapper.sh`를 우선 사용하도록 복원했고, Codex/Claude 모두 health 응답에 현재 command mode와 MCP bridge mode를 노출한다.
- `scripts/claude_relay_server.py`와 `app/services/model_selector.py`, `app/services/chat_service.py`는 `user cancelled MCP tool call`을 `session_cancelled_mcp_tool_call`로 재분류하고 `is_error/error_type/cancel_scope/raw_error`를 SSE까지 유지한다. 세션별 취소가 더 이상 일반 user cancel 문자열로만 뭉개지지 않는다.
- `mcp_servers/aads_tools_bridge.py`는 `/app` 외에 저장소 루트도 `sys.path`에 추가해 호스트 `python3.11 -m ...` 직접 실행 경로를 지원한다.
- `app/services/pipeline_runner_client.py`를 추가하고 `app/services/tool_executor.py`, `app/api/ceo_chat_tools.py`의 Pipeline Runner 내부 호출 URL을 공통 helper로 통일했다. 내부 self-call 기본값은 `http://localhost:8080`이며, 필요 시 `PIPELINE_RUNNER_INTERNAL_BASE_URL`로 오버라이드한다.
- `tests/unit/test_relay_diagnostics.py`를 추가해 내부 runner URL helper, direct Python MCP session 주입, relay 취소 재분류를 검증하는 회귀 테스트를 남겼다.

## AADS-190C
- `app/services/llm_account_usage.py` 추가로 `llm_api_keys`, `oauth_usage_log`, `pipeline_jobs.actual_model/worker_model`을 결합한 계정별 LLM 사용량 스냅샷 계층을 도입했다.
- background/provider 분류는 `codex:gpt-5.4`, `litellm:gemini-2.5-flash`, `litellm:openrouter-grok-4-fast`, `litellm:kimi-k2`, `litellm:minimax-m2.7`, `litellm:groq-qwen3-32b`와 같은 접두사/실모델 표기를 모두 인식한다.
- Anthropic 계정은 `oauth_usage_log` 기준 exact per-account 5h/7d 사용량과 recent error, 최신 rate-limit 헤더를 노출하고, 기타 provider는 `pipeline_jobs` 기준 provider-level observed usage 또는 key state only로 구분한다.
- `app/api/ops.py`에 `/api/v1/ops/account-usage` API를 추가했다.
- `tests/unit/test_llm_account_usage.py`로 접두사 기반 provider 매핑과 표시명 보강(Kimi, MiniMax, Codex CLI)을 검증한다.

## AADS-189B
- `app/services/model_registry.py`의 템플릿 metadata에 `execution_backend`, `execution_model_id`, `execution_base_url`를 추가해 “보이는 모델”과 “실제 실행 경로”를 같은 row에 담는다.
- direct provider 후보는 OpenAI, Groq, DeepSeek, OpenRouter, Qwen, Kimi, MiniMax로 정리했고, Anthropic은 `claude_cli_relay`, Codex는 `codex_cli`, Gemini는 `litellm_proxy` backend로 표시한다.
- `app/services/model_selector.py`는 레지스트리 row metadata를 읽어 `openai_compatible_direct` 모델을 우선 direct provider 경로로 호출한다. 정적 allowlist에 없는 신규 모델도 `llm_models`에 row가 있으면 direct route를 탈 수 있다.
- direct provider API 키는 provider별 활성 키 우선, 없으면 환경변수 폴백을 사용한다.
- 회귀 테스트는 `tests/unit/test_model_selector_dynamic_routing.py`에 추가했다. Qwen 신규 동적 row가 LiteLLM 하드코딩 경로가 아니라 direct route로 분기되는지를 검증한다.
- 운영 주의: `llm_models.metadata`는 DB/드라이버 상태에 따라 dict가 아니라 JSON 문자열로 읽힐 수 있다. selector/sync 양쪽 모두 문자열 metadata를 먼저 JSON object로 정규화한 뒤 사용해야 한다.

## AADS-189A
- `migrations/053_llm_model_registry.sql` 추가로 `llm_models`, `llm_key_audit_logs` 테이블을 도입했다.
- `app/services/model_registry.py` 추가로 provider 템플릿 기반 모델 레지스트리, provider 요약, 수동/자동 sync, cache invalidation 공통 계층을 구현했다.
- `app/api/llm_keys.py`는 create/update/activate/deactivate 시 priority 충돌 검증, 감사 로그 적재, stale key cache 제거, registry sync를 수행한다.
- `app/api/llm_models.py`와 `app/main.py` 라우터 등록으로 `/api/v1/llm-models`, `/api/v1/llm-models/providers/summary`, `/api/v1/llm-models/sync` API를 제공한다.
- `app/services/model_selector.py`, `app/services/pipeline_runner_service.py`, `app/api/pipeline_runner.py`, `app/services/code_reviewer.py`가 DB 레지스트리의 실행 가능 모델 필터를 우선 사용하고, 활성 모델이 비어 있으면 기존 하드코딩 경로로 안전 폴백한다.
- `tests/unit/test_model_registry.py`로 provider 정규화, unknown provider review 상태, executable filter 폴백 규칙을 검증한다.

## AADS-188
- `app/api/llm_keys.py` 추가로 `llm_api_keys` 조회·추가·수정·비활성화 API 제공.
- `app/main.py`에 `/api/v1/llm-keys` 라우터 등록.
- 대시보드 Settings 탭에서 LLM API 키 관리 UI를 연동하도록 백엔드 계약 추가.

## AADS-187
- `scripts/update_claude_all_servers.sh` 전면 재작성.
- 서버 114를 첫 순서로 즉시 처리하도록 배치.
- Claude Code CLI, Codex CLI, `claude-agent-sdk` 버전 전후 비교와 변경 시 Telegram 알림 추가.
- `/root/aads/.env` 로드, `/root/tmp` 기반 pip 설치, 서버별 실패 내성, 최종 성공/실패 요약 전송 추가.

## 운영 반영 포인트
- 목표 cron 라인: `0 4 * * * /root/aads/aads-server/scripts/update_claude_all_servers.sh >> /var/log/claude_update.log 2>&1`
- 현재 워크스페이스에는 실제 시스템 crontab과 원격 서버 상태가 없어서 파일 수정만 반영됨.

## AADS-CHAT-OPT (2026-04-28)
- **c46ddbe** `feat(chat): interrupt routing + retry P0 + ext-cache 1h + tool cache (4patch)` — origin/main push 완료, reload-api.sh로 08:31 KST 서버 메모리 반영 완료
- **4-patch 적용**: ①interrupt 자동 라우팅(routers/chat.py L239) ②LLM 재시도 5초×60회(anthropic_client.py L32) ③extended-cache 1h(cache_config.py L21) ④tool execution-scope LRU 캐시(tool_executor.py L88)
- **thinking UI 패치(f89ce6c)**: thinkingBuf 분리 + streamingThinking prop 렌더 — green 컨테이너 15:02 KST 반영
- **빈 버블 패치**: streamingContent 조건에 `&& streamBuf` 추가 — page.tsx L4936 호스트 반영 완료 (streaming=true && streamBuf="" 순간 빈 버블 방지)

## AADS-PROMPT-GOV-V2.1 (2026-04-28 08:25 KST)
- **prompt_assets 24건 시딩 완료** (L1:4 / L2:6 / L3:7 / L4:4 / L5:3) — 5-Layer 구조 모두 채워짐
- **PromptCompiler INSERT 패치**: `_record_provenance()`의 conn release 이슈 수정 — `compiled_prompt_provenance` 1건 첫 실측 INSERT 확인
- **runner-368675d8 승인**: `/admin/prompts` 페이지에 5-Layer CRUD 탭 추가 (Layer 필터 사이드바 + 모달 에디터 + JSON scope 검증)

## AADS-DOCS-INCREMENTAL-SCAN (2026-04-28 14:27 KST)
- `/docs` 문서 스캔을 기존 목록 재사용 + 증분 갱신 방식으로 보강했다.
- Backend: `app/api/project_docs.py`가 5분 메모리 캐시 외에 `/tmp/aads_project_docs_cache.json` 파일 캐시를 저장/복원하고, 강제 스캔 시 `delta.new/updated/removed/unchanged`를 계산한다.
- Frontend: `aads-dashboard/src/app/docs/page.tsx`가 `localStorage(aads.docs.scanResult.v1)`의 기존 목록을 즉시 렌더링한 뒤 백그라운드로 최신 목록을 갱신한다.
- 검증: `docker exec aads-server python3 -m py_compile /app/app/api/project_docs.py`, `npx eslint src/app/docs/page.tsx`, 컨테이너 직접 호출 기준 문서 1,431건 및 2회차 `cache_hit=True` 확인.

## AADS-CHAT-STREAM-PLACEHOLDER (2026-04-28 17:26 KST)
- 진행 중 버블 미표시 원인을 재확인했다. 백엔드는 `streaming_placeholder`와 Redis stream을 생성하지만, 프론트의 폴링 최신 메시지 조회가 `waitingBg=true`일 때도 `include_streaming=true` 없이 `/chat/messages`를 호출해 placeholder 복구 분기가 작동하지 않을 수 있었다.
- Frontend: `aads-dashboard/src/app/chat/page.tsx`의 polling `rawLatest` 조회에 `_waitingBg ? "&include_streaming=true" : ""`를 추가했다. SSE attach가 늦거나 끊겨도 waiting background 상태에서는 DB placeholder를 받아 진행 버블을 유지한다.
- 검증: 변경 diff는 단일 URL 옵션 추가. `npx eslint src/app/chat/page.tsx`와 `npx tsc --noEmit --pretty false`는 기존 누적 오류(admin API 타입 누락, page.tsx 기존 lint 오류)로 실패했고, 이번 수정 라인 신규 오류는 확인되지 않았다.
## 2026-04-29 09:03 KST - L1 Global prompt governance 강화

- 추가: `migrations/066_strengthen_l1_global_prompts.sql`
- 목적: L1 Global 4개 에셋을 운영 규칙 수준으로 확장하고 `global-layer-governance` 신규 추가
- 운영 DB 적용: `prompt_assets.layer_id=1` 활성 5건, 평균 643자
- 컴파일러 검증: CEO/task_query/gpt-5.5/PromptEngineer 샘플에서 L1 5건 모두 `applied_assets` 선택 확인
- 주의: 실제 채팅 provenance row는 다음 채팅 실행부터 신규 L1 5건으로 기록됨

## 2026-04-29 09:19 KST - CTO L3 role prompt scope 정리

- 추가: `migrations/067_refine_cto_role_prompts.sql`
- 목적: 공통 `role-cto-strategist`에서 6개 프로젝트 직접 열거를 제거하고, 프로젝트 전문성은 CTO 오버레이로 분리
- 운영 DB 적용: 공통 CTO 1건 업데이트, `project-role-aads-cto` 갱신, `project-role-go100-cto`/`project-role-ntv2-cto` 신규 추가
- 검증: 공통 CTO 본문에서 `6개 프로젝트|AADS, KIS, GO100, SF, NTV2, NAS` 패턴 0건, AADS/GO100/NTV2 샘플 매칭에서 각각 공통 CTO + 프로젝트 CTO 오버레이 선택 확인
- 주의: 실제 채팅 provenance row는 CTO 역할이 지정된 다음 메시지부터 신규 CTO 에셋 조합으로 기록됨

## 2026-04-29 09:34 KST - L2 Project prompt governance 강화

- 추가: `migrations/068_strengthen_l2_project_prompts.sql`
- 목적: CEO 통합지시 L2 신규 추가, 프로젝트별 서버/경로 계약 정정, AADS/GO100/KIS/NTV2/SF/NAS L2 완료 기준 강화
- 운영 DB 적용: `prompt_assets.layer_id=2` 활성 8건, 평균 721자, 최소 596자, 최대 884자
- 경로 보정: `/srv/newtalk-v2`, `/root/webapp` 구식 경로 패턴 0건 확인. KIS/GO100=`/root/kis-autotrade-v4`, SF=`/data/shortflow`, NTV2=`/var/www/newtalk`, AADS=`/root/aads/aads-server`/`/root/aads/aads-dashboard` 기준 반영
- 컴파일러 매칭 검증: CEO는 `project-ceo-orchestration-context`, AADS는 `project-aads-context`, KIS/GO100/SF/NTV2/NAS는 각 프로젝트 L2 + `project-remote-access-contract` 매칭 확인
- 주의: 실제 채팅 provenance row는 각 워크스페이스의 다음 메시지부터 신규 L2 에셋 조합으로 기록됨

## 2026-04-29 09:52 KST - Project UX role overlays 보강

- 추가: `migrations/069_seed_project_ux_role_overlays.sql`
- 목적: 공통 `UXProductDesigner / UX·제품디자이너` 역할에 AADS/SF/KIS/NAS 전용 L3 프로젝트 오버레이를 추가하고, NAS에도 역할 드롭다운 노출 범위를 확장
- 운영 DB 적용: 신규 UX 오버레이 4건 추가, `role_profiles.role='UXProductDesigner'` project_scope를 `{AADS,SF,NTV2,GO100,KIS,NAS}`로 보정
- 검증: L3 활성 46건, UX 프로젝트 오버레이 6건. AADS/SF/KIS/NAS/GO100/NTV2 각각 `workspace + design_review + UXProductDesigner` 샘플에서 프로젝트별 UX 오버레이 1건씩 매칭 확인
- 주의: 역할 API는 인증 필요로 무토큰 호출 시 401이 정상. 실제 채팅 provenance row는 UXProductDesigner 역할이 지정된 다음 메시지부터 신규 오버레이 조합으로 기록됨

## 2026-04-29 10:02 KST - UXProductDesigner L3 전문 역할 정리

- 추가: `migrations/070_refine_ux_designer_role_prompts.sql`
- 목적: 공통 `role-ux-product-designer`에서 프로젝트별 문구를 제거하고, Product UX Architect / Interaction Designer / UI System Designer / UX Writer / Accessibility·Mobile / Design QA Auditor 하위 전문성을 명시
- 운영 DB 적용: 공통 UX 프롬프트 1,635자로 확장, workspace_scope를 `{AADS,SF,NTV2,GO100,KIS,NAS}`로 정합화, 프로젝트별 UX 오버레이 6건 표준 구조로 재작성
- GO100 분리: `project-role-go100-ux` 신규 추가, 기존 `project-role-go100-ux-growth`는 `GrowthContentStrategist` 전용으로 role_scope 분리
- 검증: 공통 UX 본문에서 `AADS|GO100|NTV2|KIS|SF|NAS` 프로젝트명 패턴 0건. AADS/GO100/KIS/NAS/NTV2/SF 각각 `role-ux-product-designer + project-role-*-ux` 2건 매칭 확인. GO100 Growth는 `role-growth-content + project-role-go100-ux-growth`, GO100 UX는 `role-ux-product-designer + project-role-go100-ux`로 분리 확인
- 주의: `chat_sessions.role_key='UXProductDesigner'` 세션은 현재 0건이므로 실제 provenance 기록은 세션 역할 지정 후 다음 메시지부터 생성됨. API 헬스체크 `http://localhost:8100/health` 200 확인

## 2026-04-29 10:21 KST - PM L3 role prompt 전문성 보강

- 추가: `migrations/071_refine_pm_role_prompts.sql`
- 목적: `PM / 프로젝트매니저`를 `PM / 제품·프로젝트매니저`로 재정의하고, 공통 PM은 요구사항 구조화·우선순위·역할 배정·acceptance criteria·릴리즈 리스크 검수 책임으로 확장
- 운영 DB 적용: PM 관련 L3 활성 에셋 7건(`role-pm-coordinator` + AADS/GO100/KIS/NAS/NTV2/SF PM 오버레이), 평균 596자, 최소 478자, 최대 1,110자
- role profile 보정: `role_profiles.role='PM'`의 `display_name_ko`를 `제품·프로젝트매니저`로 변경하고 `quality_rubric_version='pm-product-project-manager-v1'`, acceptance criteria/역할 배정/릴리즈 리스크 체크 플래그 추가
- 검증: AADS/GO100/KIS/NAS/NTV2/SF 각각 `role=PM`, `intent=status_check`, `model=gpt-5.5` 샘플에서 `role-pm-coordinator + project-role-*-pm` 2건 매칭 확인
- 주의: 실제 채팅 provenance row는 PM 역할이 지정된 세션의 다음 메시지부터 신규 PM 에셋 조합으로 기록됨

## 2026-04-29 11:09 KST - VibeCodingLead 역할 및 역할 활용 팁 반영

- 추가: `migrations/072_seed_vibe_coding_lead_role.sql`
- 목적: 비개발자 CEO/제품 오너의 자연어 지시를 제품 요구사항, 안전한 작업 지시서, 역할 배정, 검증 기준으로 변환하는 `VibeCodingLead / AI 제품구현 총괄·바이브코딩 리드` 역할 신설
- 운영 DB 적용: L3 활성 에셋 8건(`role-vibe-coding-lead` + CEO/AADS/GO100/KIS/NAS/NTV2/SF 오버레이), 평균 536자
- role profile 추가: `role_profiles.role='VibeCodingLead'`, `display_name_ko='AI 제품구현 총괄·바이브코딩 리드'`, `project_scope={AADS,KIS,GO100,SF,NTV2,NAS,CEO,VIBE}`, `when_to_use`/`how_to_instruct`/`instruction_template` 메타데이터 저장
- API/UI 보강: `/chat/workspaces/{workspace_id}/roles` 응답에 역할 활용 팁 메타데이터를 포함하고, 좌측 세션 역할 셀렉터에서 선택된 역할 옆 `?` 툴팁으로 도움말 표시 가능하게 패치
- 검증: CEO/AADS/GO100/KIS/NAS/NTV2/SF 각각 `role=VibeCodingLead`, `intent=product`, `model=gpt-5.5` 샘플에서 공통 역할 + 프로젝트 오버레이 2건 매칭 확인. VIBE 워크스페이스는 공통 역할 1건 매칭 확인. `docker exec aads-server python3 -m py_compile /app/app/services/chat_service.py`, `npx eslint src/components/chat/Sidebar.tsx` 통과
- 주의: DB 역할은 즉시 사용 가능. API/UI 코드 변경은 실행 프로세스/대시보드 번들 반영이 필요하며, 실제 채팅 provenance row는 세션에 `VibeCodingLead` 역할 지정 후 다음 메시지부터 생성됨

## 2026-04-29 12:19 KST - Ops L3 role prompt 전문성 보강

- 추가: `migrations/073_refine_ops_developer_qa_judge_roles.sql`
- 목적: `Ops / 운영담당자`를 `Ops / 배포·운영엔지니어`로 재정의하고, SRE와 역할 경계를 분리. Ops는 릴리즈 실행, runbook, 승인 조건, 롤백, 운영 보고를 책임지도록 보강
- 운영 DB 적용: Ops 관련 L3 활성 에셋 7건(`role-ops-monitor` + AADS/GO100/KIS/NAS/NTV2/SF Ops 오버레이), 평균 579자, 최소 405자, 최대 1,202자
- role profile 보정: `role_profiles.role='Ops'`의 `system_prompt_ref='prompt_assets:role-ops-monitor'`, `display_name_ko='배포·운영엔지니어'`, `quality_rubric_version='ops-release-operations-v1'`, health/active task/rollback/verification 플래그 추가
- 검증: AADS/GO100/KIS/NAS/NTV2/SF 각각 `role=Ops`, `intent=deploy` 샘플에서 `role-ops-monitor + project-role-*-ops` 2건 매칭 확인. API 헬스체크 `http://localhost:8100/health` 200 확인
- 주의: 현재 `chat_sessions.role_key='Ops'` 세션과 최근 24시간 Ops provenance는 0건이므로 실제 provenance 기록은 세션 역할 지정 후 다음 메시지부터 생성됨. 재시작은 불필요

## 2026-04-29 12:36 KST - Developer/QA/JudgeEvaluator 역할 경계 및 프로젝트 오버레이 보강

- 추가: `migrations/074_refine_developer_qa_judge_roles.sql`
- 목적: `Developer`는 구현, `QA`는 재현 가능한 검증, `JudgeEvaluator`는 독립 승인/조건부 승인/반려 판정으로 역할 경계를 분리하고 6개 프로젝트 모두에 전용 L3 오버레이를 부여
- 운영 DB 적용: 공통 L3 3건 갱신(`role-developer-implementer`, `role-qa-verifier`, `role-judge-evaluator`), 프로젝트 오버레이 18건 UPSERT, 기존 `project-role-ntv2-qa-judge` 혼합 오버레이 비활성화
- role profile 보정: `Developer=구현 엔지니어`, `QA=품질검증 엔지니어`, `JudgeEvaluator=독립 평가·검수관`으로 표시명과 `when_to_use`/`how_to_instruct` 메타데이터 추가
- 검증: AADS/GO100/KIS/NAS/NTV2/SF 각각 Developer/QA/JudgeEvaluator 샘플에서 공통 역할 + 프로젝트 오버레이 2건씩 매칭 확인. 관련 활성 L3 에셋 21건, 평균 381자. API 헬스체크 `http://localhost:8100/health` 200 확인
- 주의: DB 에셋 변경이라 재시작은 불필요. 실제 provenance 기록은 해당 역할이 지정된 세션의 다음 메시지부터 생성됨

## 2026-04-29 12:49 KST - CEO PromptContextHarnessEngineer L3 scope 핫픽스

- 추가: `migrations/075_fix_ceo_prompt_context_harness_scope.sql`
- 목적: CEO 통합지시 세션에서 `PromptContextHarnessEngineer` 역할을 선택했는데 L3가 빠지는 문제 수정
- 원인: `role-prompt-context-harness-engineer`가 `workspace_scope={AADS}` 및 제한된 `intent_scope`만 갖고 있어 `workspace=CEO`/일부 intent에서 매칭되지 않음
- 운영 DB 적용: 공통 `role-prompt-context-harness-engineer`에 `CEO` workspace와 `*` intent 추가, `project-role-ceo-prompt-context-harness` 신규 추가, `role_profiles.role='PromptContextHarnessEngineer'`에 `CEO` project_scope 및 provenance 검증 메타데이터 추가
- 검증: CEO + `PromptContextHarnessEngineer` + `status_check` + `gpt-5.5` 샘플에서 L3 2건(`role-prompt-context-harness-engineer`, `project-role-ceo-prompt-context-harness`) 매칭 확인. `aa433b41-0ad2-421c-ae7c-bac4806035cc` 최신 provenance는 L1 5/L2 2/L3 2/L4 1/L5 2, `fallback_used=false`, compile_error 없음. API 헬스체크 `http://localhost:8100/health` 200 확인
- 주의: 실제 CEO 현재 세션 provenance는 다음 메시지부터 신규 L3 조합으로 기록됨. 재시작은 불필요

## 2026-04-29 12:57 KST - Prompt provenance 기반 상태 답변 규칙 보강

- 추가: `migrations/076_enforce_prompt_provenance_status_answers.sql`
- 목적: 시스템 프롬프트/역할 프롬프트 적용 여부 질문에서 모델이 워크스페이스 고정 정체성 문구나 이전 답변 본문으로 오판하지 않고 `compiled_prompt_provenance`를 최종 근거로 답하게 함
- 운영 DB 적용: `global-layer-governance`에 시스템 프롬프트 적용 판정/충돌 처리 규칙 추가, `intent-status-check`에 프롬프트 적용 상태 조회 절차 추가
- 지정 세션 확인: `ed08553d-a842-4967-8867-00e82ddd2eba` 최신 provenance는 `2026-04-29 12:32 KST`, workspace=`GO100`, role=`VibeCodingLead`, `system_prompt_chars=22873`, applied assets 11건, compile_error 없음
- 검증: GO100 + `VibeCodingLead` + `status_check` + `claude-sonnet-4-6` 샘플 매칭에서 L1 5건(`global-layer-governance` 1,328자 포함), L2 2건, L3 2건, L4 1건(`intent-status-check` 862자), L5 1건 선택 확인. API 헬스체크 `http://localhost:8100/health` 200 확인
- 주의: 기존 provenance 행은 컴파일 당시 스냅샷이라 과거 chars가 남는 것이 정상. 신규 보강 규칙은 다음 컴파일/다음 메시지부터 provenance에 반영됨. 재시작은 불필요

## 2026-04-30 06:12 KST - AADS 서버 + 대시보드 전체 blue-green 배포

- 대상: `/root/aads/aads-server` `bash deploy.sh bluegreen`, `/root/aads/aads-dashboard` `bash deploy.sh` 순차 실행. 배포 후 nginx upstream은 서버 `127.0.0.1:8100` primary, `127.0.0.1:8102` backup이고 대시보드는 `127.0.0.1:3100` primary, `127.0.0.1:3101` backup 상태.
- 반영 범위: 서버 저장소의 Android/device/tool_executor 변경과 대시보드 채팅 화면 `src/app/chat/page.tsx`, `src/app/chat/types.ts`, 설정 화면 `src/app/settings/page.tsx`, `src/components/settings/LlmRegistryWorkspacePanel.tsx` 변경이 배포 산출물에 포함됨.
- 운영 확인: `docker ps` 기준 `aads-server`, `aads-server-green`, `aads-dashboard`, `aads-postgres`, `aads-litellm`, `aads-searxng`, `aads-redis`가 running/healthy. `curl http://localhost:8100/api/v1/health` 응답 `status=ok`, `graph_ready=true`, `sandbox.status=ok`.
- 외부 확인: `curl https://aads.newtalk.kr/login` 200, `curl -L https://aads.newtalk.kr/chat` 200(`/login?redirect=%2Fchat`) 확인.
- 배포 주의: 서버 blue-green drain 단계에서 활성 스트림 2건이 300초 타임아웃까지 남아 강제 전환됐으나, 전환 후 Health/DB 스키마/채팅 테이블/LLM 검증은 모두 통과. 대시보드 `next build` 성공, 내부/외부 헬스체크 통과, 프론트엔드 QA API는 `UNKNOWN` verdict로 통과 처리됨.
- Git 상태 주의: 서버 저장소와 대시보드 저장소 모두 미커밋 변경이 남아 있음. 별도 지시 전까지 기존 변경은 되돌리지 않음.

## 2026-04-30 07:32 KST - Playwright MCP STOPPED 복구

- 원인: `supervisord.conf`의 `playwright-mcp`가 `npx @playwright/mcp`를 사용하지만 서버 이미지 `Dockerfile`에 `nodejs/npm`이 없어 supervisor 기동 시 `no such file`로 실패.
- 조치: `Dockerfile`에 `nodejs npm` 설치를 추가하고, `supervisord.conf`에서 `playwright-mcp`를 `autostart=true`, `autorestart=true`, `startretries=3`으로 변경. 현재 실행 컨테이너에는 `apt-get install nodejs npm` 후 `supervisorctl reread/update`로 즉시 반영.
- 검증: `supervisorctl status all` 기준 `mcp-servers:playwright-mcp RUNNING`, `/var/log/playwright-mcp.log`에 `Listening on http://localhost:8768` 확인. `curl http://localhost:8768/mcp`는 MCP HTTP 엔드포인트가 살아 있어 `Invalid request`를 반환. AADS 헬스체크 `https://aads.newtalk.kr/api/v1/health`는 `status=ok`.

## 2026-04-30 14:49 KST - aads-redis 자동복구 성공 오알림 차단

- 증상: Telegram에 `자동복구 성공 / 서비스: 68:aads-redis / 명령: docker restart aads-redis / 결과: Restart blocked for aads-redis (use external watchdog)` 알림이 반복됨.
- 원인: `app/services/unified_healer.py`가 보호 컨테이너(`aads-server`, `aads-postgres`, `aads-redis`, `aads-litellm`)의 내부 restart 차단을 `success=True`로 반환해 실제 재시작이 없는데도 성공 알림을 발송할 수 있었음.
- 즉시 조치: 운영 DB `monitored_services`의 `68:aads-redis` `auto_recovery_command`를 `NULL`로 변경해 현재 실행 중인 Healer가 Redis 내부 재시작을 더 이상 시도하지 않도록 차단. Redis 상태는 `PONG`, Docker health `healthy`, `consecutive_failures=0`.
- 코드 조치: `unified_healer.py`에서 보호 컨테이너 restart/stop 차단 결과를 `success=False, blocked=True`로 반환하고, 서비스/error 복구 경로 모두 `blocked`는 성공/실패 텔레그램을 보내지 않고 `auto_recovery_blocked`/`error_recovery_blocked` 로그만 남기도록 패치.
- 검증: `python3 -m py_compile app/services/unified_healer.py` 통과, `bash scripts/reload-api.sh` hot-reload 성공(53개 모듈 재로드), 30초 이상 모니터링 후 `68:aads-redis`는 `ok`, 최근 앱 로그에 `aads-redis|Restart blocked|auto_recovery_blocked` 재발 없음. API health는 blue/green 모두 `status=ok`.
- 주의: 반복 알림 차단은 DB 변경으로 즉시 반영됐고, 코드 패치도 hot-reload로 런타임 반영 완료. Redis 실제 장애 복구는 호스트 cron `/root/aads/aads-server/watchdog-host.sh`의 Layer 0가 담당.

## 2026-04-30 16:14 KST - aads-redis 오알림 재발 경로 추가 차단

- 증상: 위 조치 후에도 CEO Telegram에 동일한 `68:aads-redis / docker restart aads-redis / Restart blocked` 자동복구 성공 알림이 계속 도착.
- 원인 보강: AADS DB 기반 `unified_healer` 외에 호스트 cron/레거시 watchdog 경로가 별도로 존재. `/usr/local/bin/newtalk_claude_monitor.py`는 Claude 프롬프트와 허용 명령 목록에 `docker restart aads-redis`를 보유했고, `/root/aads/scripts/watchdog_daemon.py`는 `recovery_log` 기반 자동복구 성공 알림 경로를 보유.
- 추가 조치: `unified_healer.py`의 `redis_connection_error -> docker restart aads-redis` 매핑 제거, `escalation_engine.py` Docker 자동재시작 allowlist에서 핵심 의존 컨테이너 제거, `newtalk_claude_monitor.py` 허용 명령/프롬프트에서 `aads-redis` 제거, `watchdog_daemon.py` recovery_log 자동실행 경로에서 보호 컨테이너 차단.
- 검증: `python3 -m py_compile app/services/unified_healer.py app/services/escalation_engine.py /root/aads/scripts/watchdog_daemon.py /usr/local/bin/newtalk_claude_monitor.py` 통과. 운영 DB 기준 `monitored_services`의 `68:aads-redis` 자동복구 명령은 `NULL`, 최근 `alert_history/error_log/recovery_log`에 Redis 관련 신규 이력 없음.

## 2026-04-30 16:24 KST - Telegram 반복 알림 추가 소음 차단

- 증상: CEO가 Telegram 알림이 계속 온다고 재보고. Redis 컨테이너 자체는 `Up 9 days (healthy)`이고 DB `monitored_services`의 `68:aads-redis`는 `ok`, `consecutive_failures=0`, `auto_recovery_command=NULL`.
- 확인: `alert_history`, `error_log`, `recovery_log`에는 최근 Redis 관련 신규 이력 0건. Docker `aads-server`/`aads-server-green` 런타임 모두 `unified_healer`의 보호 컨테이너 차단 패치와 `redis_connection_error` 매핑 제거가 반영됨.
- 추가 원인: `/root/aads/aads-server`에서 2026-03-01부터 떠 있던 고아 `uvicorn app.main:app --port 18080` 프로세스가 발견됨. 이 프로세스는 nginx upstream에 연결되지 않았고, 현재 hot-reload/배포 관리 대상 밖이라 오래된 APScheduler 루프를 돌릴 가능성이 있었음.
- 조치: 고아 PID `22500`을 `SIGTERM`으로 종료. `ss -ltnp` 기준 `:18080` 리스너 제거 확인. 또한 `watchdog-host.sh`의 `stale placeholder N건 자동 정리`는 CEO 조치가 필요 없는 루틴 정리라 Telegram `notify` 대신 syslog `logger`만 남기도록 낮춤.
- 추가 소음 차단: `/usr/local/bin/newtalk_claude_monitor.py`의 디스크 경고 기준을 `>85%`에서 `>=90%`로 조정해 현재 `/` 87% 상태가 30분마다 Telegram 경고 후보가 되지 않도록 cross-monitor 기준과 맞춤.
- 검증: `bash -n watchdog-host.sh` 통과, `python3 -m py_compile /usr/local/bin/newtalk_claude_monitor.py` 통과, `:8100/:8102` Docker API만 리스닝, Redis DB 상태 정상. 이후 동일 Redis 문구가 또 오면 68서버 내부 신규 발송이 아니라 Telegram 지연/외부 발송 경로 가능성이 높으므로 수신 시각 기준으로 추적 필요.

## 2026-04-30 16:32 KST - Telegram 반복 알림 2차 차단

- 추가 확인: Redis 관련 신규 이력은 없고, 반복 후보는 `newtalk_claude_monitor`의 `/` 디스크 87% 경고, AADS `alert_eval`의 `disk_full(86.7%, 임계값 80%)`, `meta_watchdog`의 레거시 114 프로세스명 감시로 좁혀짐.
- 조치: `app/services/alert_manager.py`의 디스크 텔레그램 기준을 `>=90%`로 상향하고, `cost_exceed` 중복 억제 기간을 1시간에서 24시간으로 확장. `/root/aads/meta_watchdog.sh`의 `watchdog_114`/`auto_trigger_114` 레거시 재시작 감시는 중지하고 cross_monitor가 114 헬스를 담당하도록 정리.
- 검증: `newtalk_claude_monitor.sh` 수동 실행 결과 현재 `/` 87%에서 `이상 없음 - 정상 종료`. `AlertManager.evaluate_rules()`는 디스크 알림 0건, 비용 조건만 1건이나 24시간 dedup 대상으로 확인. `bash -n /root/aads/meta_watchdog.sh`, `python3 -m py_compile app/services/alert_manager.py /usr/local/bin/newtalk_claude_monitor.py`, `python3 -m pytest tests/test_observability.py -q` 통과.
- 런타임 반영: `bash scripts/reload-api.sh`로 active `aads-server` 57개 모듈, `docker exec aads-server-green bash /app/scripts/reload-api.sh`로 green 45개 모듈 hot-reload 성공. 16:29~16:32 KST 스케줄러 주기 이후 `alert_history` 신규 행 0건 확인.

## 2026-04-30 17:01 KST - aads-socket-proxy Healer 승인요청 오알림 차단

- 증상: Telegram에 `AADS Healer 승인 요청 #103 / 68:aads-socket-proxy 복구 실패 / 마지막 에러: restart aads-socket-proxy: ok` 문구가 수신됨. 실측 기준 운영 DB `approval_queue`의 최대 ID는 63이라 화면의 `#103`은 현재 컨테이너 DB 신규 레코드가 아니며, 별도 런타임/과거 발송 경로 가능성이 있음.
- 원인: `aads-socket-proxy`는 AADS API가 Docker API에 접근하는 통로인데, `monitored_services`에 `docker restart aads-socket-proxy` 자동복구 명령이 남아 있었음. 내부 Healer가 Docker API 통로 자체를 재시작하려는 구조라 간헐 실패/성공 결과가 승인 요청으로 오분류될 수 있음.
- 조치: 운영 DB에서 `68:aads-socket-proxy`의 `auto_recovery_command`를 `NULL`로 제거하고 `last_status=ok`, `consecutive_failures=0`으로 리셋. `app/services/unified_healer.py`의 `PROTECTED_LOCAL_CONTAINERS`에 `aads-socket-proxy`를 추가해 코드상 내부 restart/stop을 영구 차단. `_create_approval_request()`에 24시간 내 동일 `target_server + action_command + title` pending 요청 dedupe를 추가해 같은 승인요청 반복 발송을 막음.
- 114/211 확인: 114는 SSH 가능, `/` 80%, `localhost:8000/health` 200, `https://v2.newtalk.kr/` 307. 211은 SSH 가능, `/` 63%, nginx/postgresql/redis active, `https://go100.newtalk.kr/health` 200. DB `monitored_services` 기준 114/211 전체 `ok`, `consecutive_failures=0`.
- 검증: `python3 -m py_compile app/services/unified_healer.py` 통과. `bash scripts/reload-api.sh` active 47개 모듈, `docker exec aads-server-green bash /app/scripts/reload-api.sh` green 35개 모듈 hot-reload 성공. 17:01 KST Healer 주기 이후 `approval_queue` 신규 0건, `aads-redis`/`aads-socket-proxy` 자동복구 명령 모두 `NULL`, 상태 `ok`.

## 2026-04-30 17:15 KST - Contabo standby Healer #103 오알림 원인 확정 및 차단

- 재확인: 68 운영 DB는 `approval_queue.max_id=63`, `aads-socket-proxy` pending 0건, `aads-socket-proxy` 컨테이너는 `Up 9 days` 상태라 68 운영 서버 자체에서 `#103`이 생성된 것이 아님.
- 원인 확정: Contabo 동기화 서버(`5.104.86.116`)에도 AADS Docker 스택이 실행 중이고, 해당 DB `approval_queue.max_id=103`에 `68:aads-socket-proxy 복구 실패 (1회)` pending 요청이 실제 존재했음. Contabo standby의 `monitored_services`에는 `68:aads-socket-proxy`/`68:aads-redis`가 enabled 상태로 남아 있고 자동복구 명령도 각각 `docker restart aads-socket-proxy`, `docker restart aads-redis`로 남아 있었음.
- 조치: Contabo DB에서 두 감시 항목을 `enabled=false`, `auto_recovery_command=NULL`, `last_status=disabled`, `consecutive_failures=0`으로 변경. 기존 `docker restart aads-socket-proxy` pending 승인요청 42건(`#62`~`#103`)은 `rejected`로 일괄 정리.
- 검증: Healer 주기 경과 후 Contabo DB 기준 `approval_queue.max_id=103`, `socket_pending=0`, `redis_pending=0`. Contabo `monitored_services`의 `aads-redis`/`aads-socket-proxy`는 disabled 유지. 68 운영 DB도 `socket_pending=0` 유지.
- 주의: `/root/aads/aads-server/scripts/sync-to-contabo.sh`가 10분마다 코드/문서를 동기화하므로, standby가 운영 텔레그램 알림을 보내지 않도록 DB 감시 항목 또는 환경변수 분리를 유지해야 함.

## 2026-05-05 10:46 KST - Android Agent 권한 상태 원격 확인 명령 추가

- 배경: CEO가 Galaxy Z Fold6 앱 설치 후 승인한 권한이 현재도 유지되는지 원격 확인 가능 여부를 요청.
- 조치: `CommandDispatcher`에 `permission_status`/`permissions` 명령을 추가하고, `AndroidCommandHandlers.permissionStatus()`에서 런타임 권한과 특수 권한 상태를 JSON으로 반환하도록 구현.
- 확인 범위: SMS 발송/읽기, 연락처, 통화기록, 카메라, 마이크, 위치, 알림, Wi-Fi, 이미지, Bluetooth, 접근성, 알림 접근, 디바이스 관리자, `WRITE_SETTINGS`, 배터리 최적화 예외.
- 검증: `./build_debug_apk.sh` 성공, `android_agent/dist/aads-agent-debug.apk` 1,410,347 bytes(2026-05-05 10:49 KST), `CommandDispatcher` 등록 수 58개.
- 기술문서: `docs/reports/20260505_ANDROID_AGENT_PERMISSION_STATUS_COMMAND.md`.

## 2026-05-06 08:38 KST - Runner 커밋 오염 분리 정리 및 AADS 서버 배포

- 배경: Runner 커밋 `2303faf`에 Common Browser Bridge 구현과 GO100/NTV2/Android/임시 리포트 산출물이 함께 섞여 운영 브랜치 오염 위험이 있었음.
- 정리: `33cf37a chore: remove runner spillover artifacts`로 `.go100-work`, NTV2 기획 HTML, `reports/2026-05-05`, 임시 NTV/GO100 작업물, Contabo 임시 스크립트, debug signing key 등 비-Browser Bridge 산출물 111개 파일을 제거. Browser Bridge 핵심 파일(`app/browser_bridge/*`, `app/api/browser_bridge.py`, `app/main.py` 라우터 연결, `app/api/ceo_chat_tools.py` 도구 연결, `tests/unit/test_browser_bridge.py`)은 유지.
- 별도 분리: 배포 시 bind mount에 함께 반영될 미커밋 Android Agent Play Protect 대응 변경은 `48fc204 fix(android): serve release agent apk`로 별도 커밋 분리.
- 검증: `python3 -m pytest tests/unit/test_browser_bridge.py -q` 8개 통과, `python3 -m py_compile app/api/browser_bridge.py app/browser_bridge/*.py app/api/ceo_chat_tools.py` 통과.
- 배포: `/root/aads/aads-server/deploy.sh` blue-green 경로로 새 `aads-server` 슬롯을 기동. 08:38 KST 기준 `aads-server` Docker health `healthy`, `http://127.0.0.1:8100/health` `status=ok`.
- 운영 확인: `GET /api/v1/browser-bridge/sessions/register`가 `405 Method Not Allowed`를 반환해 Browser Bridge 라우트가 운영 앱에 로딩된 것을 확인. POST 전용 등록 엔드포인트라 405가 정상 노출 신호임.
- Git 상태: `main`은 `origin/main`과 일치하도록 push 완료 후 clean 상태 확인.

## 2026-05-06 08:59 KST - PC Agent 재연결 안정화 및 채팅 경량화 분리 기획

- 배경: CEO 채팅 세션 `f31f1238-fdc8-4405-8893-351226e06bda`에서 PC Agent가 연결됐다가 목록에서 사라지는 현상 보고. 채팅 경량화는 별도 문제/기획 보고로 분리하고 PC Agent 끊김 P0만 즉시 조치.
- 원인: 운영 DB `kakao_pc_agent_tokens`에는 `is_active` 컬럼이 없는데 `app/api/pc_agent.py`가 `is_active = true`를 조회해 토큰 DB 검증 실패 가능성이 있었음. 또한 같은 `agent_id` 재연결 시 예전 WebSocket의 종료 `finally`가 새 연결을 `pc_agent_manager`에서 지울 수 있는 구조였음.
- 조치: `app/api/pc_agent.py` 토큰 검증을 실제 스키마 기준 `token` 조회 + `last_used_at` 갱신으로 수정. 같은 `agent_id` 신규 연결은 기존 WebSocket을 `4010 replaced_by_new`로 닫고 신규 연결이 승리하도록 `_agent_connections` guard 추가. 연결/인증/교체/해제 이벤트는 `pc_agent_connection_events` 테이블에 best-effort 기록.
- 조치: `app/services/pc_agent_manager.py`의 `unregister_agent()`에 WebSocket 일치 guard를 추가해 stale 연결 종료가 최신 연결을 삭제하지 못하게 변경.
- 검증: `python3 -m py_compile app/api/pc_agent.py app/services/pc_agent_manager.py` 통과. `python3 -m pytest tests/unit/test_pc_agent_manager_connection_guard.py tests/test_pc_agent_command_builder.py -q` 30개 통과.
- 별도 기획: 채팅 경량화 문제/개선안은 `docs/reports/20260506_CHAT_LIGHTWEIGHT_PLAN.md`로 분리 문서화. 초기 메시지 로드 축소, `fields=minimal`, revision 기반 polling skip, 메시지 리스트 가상화, artifacts lazy load 순서로 권장.

## 2026-05-06 10:05 KST - Pipeline Runner 중복 제출 구조 차단

- 배경: CEO가 러너 작업지시 시 중복 작업이 많이 생긴다고 보고. 원인 점검 결과 제출 API가 `instruction_hash` 조회 후 `INSERT`하는 구조라 동시 제출 경쟁 조건에서 중복 row가 생길 수 있었고, Shell runner의 `DEDUP_BLOCK`은 실행 직전 차단이라 큐/로그 오염을 막지 못했음.
- 조치: `app/api/pipeline_runner.py`에 동일 `instruction_hash`별 `pg_advisory_xact_lock` 직렬화를 추가하고, active 상태 조회 범위를 `queued/claimed/running/awaiting_approval/approved/deploying/rolling_back`으로 확장. 단건/배치 제출 모두 기존 active job을 재사용하도록 통일.
- DB 조치: `migrations/078_pipeline_runner_active_dedup.sql` 추가. 기존 active 중복 row는 1건만 남기고 나머지를 `error/dedup_blocked`로 정리한 뒤 `uq_pipeline_jobs_active_instruction_hash` partial unique index로 재발을 차단.
- 러너 백스톱: `scripts/pipeline-runner.sh`의 실행 직전 중복 차단 상태를 `cancelled/superseded`에서 `error/dedup_blocked`로 바꿔 대시보드 완료/취소 통계를 오염시키지 않게 수정.
- 문서: `docs/pipeline-runner/PIPELINE-RUNNER-ARCHITECTURE.md`, `docs/pipeline-runner/PIPELINE-RUNNER-API-REFERENCE.md`에 advisory lock + DB unique guard를 반영.

## 2026-05-06 17:05 KST - GPT Codex 도구박스 잔여 회귀 수정

- 배경: CEO가 GPT Codex 실시간 응답에서 도구사용박스가 안 보이거나 부정확하게 표시된다고 보고. 브라우저 검수에서 도구박스는 표시되지만 `tool_result` 중심 이벤트에서 `도구 0개 사용 — ✅ bash`로 카운트가 잘못 나오는 잔여 회귀 확인.
- 조치: 대시보드 `src/app/chat/page.tsx`의 도구박스 카운트 계산을 `tool_use` 수 → `tool_count` → `tool_names` → 전체 tool event 수 순으로 fallback하도록 수정.
- 테스트 보강: `tests/unit/test_chat_lightweight_frontend_static.py`, `tests/unit/test_chat_lightweight_regression.py`가 실제 `/root/aads/aads-dashboard` 소스를 우선 검증하도록 수정하고, tool_result-only 이벤트도 도구 사용으로 집계되는 회귀 테스트 추가.
- 진단 보강: `scripts/thinking_e2e_check.py`가 호스트 실행 시 `localhost:5433`으로 DB 접속 fallback하도록 수정.
- 검증: `pytest tests/unit/test_chat_lightweight_regression.py tests/unit/test_chat_lightweight_frontend_static.py -q` 11개 통과, `npx eslint src/app/chat/page.tsx` 0 errors(기존 warning 20개), `npm run build` 성공.
- 운영 DB 확인: 2026-05-06 GPT Codex 계열 assistant 중 `GPT-5.5 (Codex CLI)` 42건/도구저장 40건, `GPT-5.4 (Codex CLI)` 2건/도구저장 2건. `gpt-5.5`, `codex:gpt-5.5` 별칭 저장 21건은 도구 실행 없는 응답으로 확인.

## 2026-05-09 09:55 KST - 채팅 메모리 임베딩 백필 완료

- 배경: 메모리/맥락유지 개선 후 신규 메시지 임베딩 누락은 줄였지만 과거 `chat_messages`의 assistant 임베딩 누락이 가장 큰 잔여 병목으로 확인됨.
- 조치: `scripts/backfill_chat_embeddings.py` 추가. 기본 canary는 `assistant` 메시지 100건, 최신순, 20건 배치로 `chat_messages.embedding`을 채움. `--dry-run`, `--role`, `--limit`, `--batch-size`, `--order` 옵션을 지원.
- canary 실행: `docker exec aads-server python3 /app/scripts/backfill_chat_embeddings.py --limit 100 --batch-size 20 --role assistant --order newest`.
- 결과: 실행 전 assistant 미임베딩 18,745건, 실행 후 18,645건. 5개 배치에서 100건 처리/100건 업데이트, 오류 0건, 소요 15.53초.
- 전체 백필: canary 이후 assistant/user/system 대상 전체 백필을 완료했다. 마지막 잔여 assistant 3건은 `docker exec aads-server python3 /app/scripts/backfill_chat_embeddings.py --limit 10 --batch-size 5 --role assistant --order newest`로 처리했고 `missing_before=3`, `missing_after=0`, `updated=3`, 오류 0건이었다.
- 검증: `python3 -m py_compile scripts/backfill_chat_embeddings.py app/services/chat_embedding_service.py app/services/chat_service.py app/services/context_builder.py` 통과. `pytest -q tests/unit/test_memory_context_regression.py` 5개 통과.
- DB 확인: 2026-05-09 09:55 KST 기준 `chat_messages` role별 본문 10자 이상 미임베딩 대상은 assistant 0건, user 0건, system 0건이다.

## 2026-05-09 10:08 KST - AADS changelog 커밋/푸시 및 green 슬롯 무중단 전환

- 커밋: `e7ae7a0 docs: sync direct edit changelogs`, `cb768b2 docs: sync go100 direct changelog`를 `origin/main`에 push 완료.
- 배포: 기존 `deploy.sh bluegreen` 실행이 선행 PID에서 진행 중이라 중복 실행은 락으로 차단됨. 해당 배포가 `aads-server-green` 이미지를 재빌드하고 green 컨테이너를 healthy 상태로 기동한 것을 확인.
- 전환: active stream 3건이 8100에 남아 있어 컨테이너 중지는 하지 않고 nginx upstream만 8102 우선, 8100 backup으로 수동 전환 후 `systemctl reload nginx` 완료. `.active_port=8102`, `.active_container=aads-server-green`으로 동기화.
- 검증: `nginx -t` 통과, `https://aads.newtalk.kr/api/v1/health` OK, `docker inspect aads-server-green` running/healthy, `docker exec aads-server-green python3 -c "from app.main import app"` import OK.
- 잔여: untracked `scripts/e2e_disc_v2.py`는 문법이 깨진 임시 테스트 초안으로 커밋에서 제외. 정리/수정 여부는 별도 판단 필요.

## 2026-05-11 10:49 KST - discussion 인텐트 명시 요청 가드 적용

- 배경: CEO 운영 질문이 `discussion`으로 오분류되어 다관점 토론 오케스트레이터가 자동 실행되고, 실측 없는 토론 합성 결과가 일반 답변처럼 저장되는 문제가 확인됨.
- 조치: `intent_router.is_explicit_debate_request()`를 추가해 `토론해봐`, `다관점 토론해`, `run_debate` 같은 명시 실행 요청만 `discussion`으로 허용. `장단점 비교`, `어떻게 해야 할까`, 토론 기능 자체 조치 요청은 `cto_strategy`/`code_modify`/`cto_verify`로 되돌리도록 가드 추가.
- 조치: "다관점 토론은 명시 지시 때만 진행되게 조치해"처럼 토론 기능 정책을 바꾸라는 문장이 `casual`로 빠지지 않도록 키워드 폴백에서도 `code_modify`로 분류되게 보강.
- 조치: `chat_service.send_message_stream()`에 2차 방어선을 추가해 LLM 분류가 `discussion`을 반환해도 명시 토론 요청이 아니면 오케스트레이터 실행을 차단.
- 조치: `tool_registry`의 broad `all` 도구 그룹에서 `run_debate`를 제외해 일반 도구 사용 인텐트에서 모델이 암묵적으로 다관점 토론 도구를 호출하지 못하게 함.
- 검증: `python3 -m py_compile app/services/intent_router.py app/services/chat_service.py app/services/tool_registry.py tests/unit/test_chat_service.py` 통과. `pytest -q tests/unit/test_chat_service.py -k 'discussion or debate or broad_tool_group'` 4개 통과. 운영 컨테이너 `classify()` 샘플 기준 조치 지시는 `code_modify`, 세션 진화 확인 질문은 `cto_verify`, 명시 문장 `다관점 토론해봐`만 `discussion`.
- 운영 반영: `bash scripts/reload-api.sh`로 active `aads-server-green` hot-reload 완료(`재로드=53개`). `https://aads.newtalk.kr/api/v1/health` OK, `aads-server-green` running/healthy.

## 2026-05-11 13:33 KST - PC Agent 트레이 미표시 원인 조치

- 배경: CEO PC에서 PC Agent 종료 후 재다운로드/재실행 시 트레이 아이콘이 보이지 않는 문제가 보고됨.
- 실측: 서버 API는 `connected: 1`이었고, PC 명령으로 `AADS-PC-Agent-Setup-1.0.14.exe` PID `18392`, `18264`가 잔존 실행 중임을 확인. 트레이 종료 요청 후 런처가 에이전트 종료를 크래시로 오인해 백그라운드 에이전트를 재시작하면서 트레이만 사라진 상태로 판단.
- 즉시 조치: PC Agent 명령으로 PID `18392`, `18264` 지연 종료를 실행했고, 서버 `/api/v1/pc-agent/health` 기준 `connected: 0`으로 내려간 것을 확인.
- 코드 조치: `pc_agent/launcher.py`에 `stop_requested` 이벤트를 추가해 트레이 종료 시 런처 루프가 재시작하지 않고 종료되도록 수정. `pc_agent/agent.py`는 `stop()` 호출 시 현재 WebSocket을 `client_stop`으로 닫도록 보강.
- 배포 패키지: `pc_agent/VERSION`을 `1.0.19`로 올리고 active/standby 컨테이너의 `/app/pc_agent`에 반영. 운영 `GET /api/v1/kakao-bot/agent/version`은 `1.0.19`, ZIP 내부도 `VERSION=1.0.19` 및 수정 코드 포함 확인.
- 검증: `python3 -m py_compile pc_agent/agent.py pc_agent/launcher.py` 통과. `pytest tests/unit/test_pc_agent_api_disconnects.py tests/unit/test_pc_agent_manager_connection_guard.py -q` 5개 통과. `aads-server-green` Docker health `healthy`, `/health` `status=ok`.
- 잔여: 현재 서버의 `kakaobot-setup.exe` 바이너리는 Windows 빌드가 필요해 Linux 서버에서 직접 재빌드 불가. `pc_agent/**` 푸시 시 `.github/workflows/build-pc-agent.yml`이 Windows GitHub Actions에서 새 EXE를 빌드/Release 등록하도록 되어 있어 커밋/푸시로 트리거해야 함.

## 2026-05-11 13:52 KST - AADS-204 Open Design Hub Phase 0 직접 구현

- 배경: `runner-0143f0a0`가 5분 이상 `running/claude_code_work` 상태였지만 task 로그 0건, 전용 worktree 변경 0건, 백엔드가 아닌 dashboard 형태 worktree로 확인되어 산출물 없는 점유로 판단.
- 조치: `terminate_task(runner-0143f0a0)`로 러너를 종료하고 새 러너 추가 투입 없이 직접 Phase 0 범위만 구현.
- 구현: `app/services/design_audit_service.py` 신규 추가. raw hex/rgb 색상, Tailwind arbitrary color, JSX/HTML 이모지 아이콘, 반복 button class 패턴을 순수 함수로 탐지.
- API: `app/api/admin.py`에 read-only `GET /api/v1/admin/design/projects`, `GET /api/v1/admin/design/audit/preview` 추가. allowlist 루트 밖 경로 접근은 차단.
- 문서/스키마: `docs/plans/AADS-OPEN-DESIGN-HUB-IMPLEMENTATION.md`에 Phase 1~4 runner 작업 분해를 작성하고, 운영 DB 미적용 초안 `migrations/082_open_design_hub.sql`을 추가.
- 테스트: `tests/unit/test_design_audit_service.py`에 색상/이모지 탐지, button class 반복, allowlist escape 방어, empty input 검증 추가.

## 2026-05-11 15:43 KST - Runner 지시 세션 최근 활성 fallback 차단

- 배경: CEO가 각 채팅창에서 러너에게 지시할 때 “지시한 채팅창”이 아니라 “해당 프로젝트의 최근 활성 세션”으로 귀속되는 문제를 지적.
- 조치: `app/api/ceo_chat_tools.py`의 `pipeline_runner_submit`에서 `params.session_id → chat_session_id → current_chat_session_id`까지만 허용하고, `_find_recent_session(project)` fallback을 제거. 세션이 없으면 제출을 거부하도록 변경.
- 조치: `app/services/tool_executor.py`의 `pipeline_runner_submit`/`pipeline_runner_submit_batch`도 동일하게 최근 세션 fallback을 제거.
- 조치: `app/services/pipeline_runner_service.py`의 레거시 `start_pipeline()`과 완료 후 AI 반응 트리거가 세션 없음 상태에서 최근 세션을 찾아 붙이는 동작을 제거. 세션 없음 작업은 채팅 보고 비활성으로만 처리.
- 테스트: `tests/unit/test_runner_scope_defaults.py`에 세션 없는 제출이 `_find_recent_session()`을 호출하지 않는 회귀 테스트와 현재 세션 전달 테스트 추가.

## 2026-05-11 19:10 KST - AADS-DESIGN-MOD-001 Design Modification Studio DB/API 기반 추가

- 배경: `Design Modification Studio` Phase 1 범위로 프로젝트별 화면 목록, 수정 요청 목록/상세, context pack 미리보기를 위한 영속 스키마와 read-only 백엔드 계약이 필요해짐. 기존 `migrations/082_open_design_hub.sql`의 `design_projects/design_token_sets/design_audit_runs` 초안과 충돌하지 않는 additive 확장이 요구됨.
- 변경 파일: `migrations/084_design_modification_studio.sql`, `app/api/design_modifications.py`, `app/main.py`, `tests/unit/test_design_modifications_api.py`.
- 조치: `design_screens`, `design_modification_requests`, `design_context_packs`, `design_visual_snapshots`, `design_decisions`를 `084` 마이그레이션으로 분리 추가. `project_key`는 기존 `design_projects(project_key)`를 참조하고, 요청 상태/타입, snapshot phase, decision confidence/applies_to에 CHECK 제약과 조회용 인덱스를 부여.
- 조치: 신규 `app/api/design_modifications.py`에 인증 의존(`get_current_user`)과 `get_pool()` 패턴을 따라 `GET /api/v1/admin/design/projects/{project_key}/screens`, `GET /api/v1/admin/design/projects/{project_key}/modification-requests`, `GET /api/v1/admin/design/modification-requests/{request_id}`, `GET /api/v1/admin/design/modification-requests/{request_id}/context-packs`, `GET /api/v1/admin/design/context-packs/{context_pack_id}/preview`를 추가. 스키마 미적용 상태에서는 list는 빈 결과, detail/preview는 `503 design modification schema is not initialized`로 처리.
- 조치: 요청 상세 응답에 화면 메타데이터, visual snapshot 목록, 관련 design decision 목록을 포함해 Phase 2 UI가 별도 write API 없이 workbench 초안을 붙일 수 있게 정리.
- 테스트/검증 명령: `pytest -q tests/unit/test_design_modifications_api.py`, `python3 -m py_compile app/api/design_modifications.py`.
- 리스크: `084`는 `082_open_design_hub.sql`의 `design_projects` 선행 적용을 전제로 한다. 또한 `context`/`sources` JSONB 구조는 Phase 3 builder 구현 전까지 loose schema이므로 프런트엔드에서는 optional 필드 방어가 필요하다.

## 2026-05-12 07:56 KST - 채팅 TODO 조회 UI 및 stale 정리 보강

- 배경: 채팅 TODO 하네스가 DB/프롬프트 내부에만 존재해 CEO가 채팅창에서 todo 작성 여부를 직접 확인할 수 없었고, 완료 판정이 애매한 `in_progress` 항목이 오래 남는 문제가 확인됨.
- 백엔드 조치: `GET /api/v1/chat/sessions/{session_id}/todos`를 추가해 세션별 todo를 조회하도록 했다. 조회 시 기본으로 오래된 `in_progress` 항목을 `pending`으로 되돌리고, 활성 항목이 없으면 첫 active 항목을 다시 `in_progress`로 승격한다.
- 채팅 하네스 조치: TODO 조회 API에서 stale 정리를 기본 수행하도록 해, 채팅창 진입/갱신 시 오래된 진행 상태가 자동 정리되게 했다.
- 대시보드 조치: `/chat` 입력 영역 상단에 세션 TODO 패널을 추가했다. 진행/완료/실패 카운트, 최대 8개 항목, 상태 라벨, 수동 새로고침을 표시하며 스트리밍 중에는 4초, 평시에는 30초 간격으로 갱신한다.
- 검증: `python3 -m pytest tests/unit/test_chat_todo_service.py tests/unit/test_chat_service.py::test_multistep_request_injects_todo_prompt_block tests/unit/test_chat_service.py::test_prepare_turn_todo_context_fails_open_when_schema_missing tests/unit/test_chat_service.py::test_todo_completion_gate_appends_missing_note -q` 8개 통과. `python3 -m py_compile app/services/chat_todo_service.py app/services/chat_service.py app/routers/chat.py` 통과. `npx eslint src/app/chat/page.tsx src/app/chat/types.ts` 0 errors/기존 warning 20개. `npx tsc --noEmit --pretty false` 통과. 테스트용 `JWT_SECRET_KEY=test-secret`로 앱 라우트 등록 확인 결과 `/api/v1/chat/sessions/{session_id}/todos` 등록 확인.

## 2026-05-12 08:15 KST - NTV2 원격 파일 도구 workdir 보정

- 배경: `runner-635be17c` 검수 과정에서 `read_remote_file(project='NTV2', file_path='src/app/Http/Controllers/Api/SourcingRpaController.php')`가 실제 운영 repo `/srv/newtalk-v2`가 아니라 서버 루트 기준 `/src/...`를 읽어 stale 파일을 근거로 반려되는 문제가 확인됨.
- 조치: `app/core/project_config.py`의 NTV2 `workdir`를 `/`에서 `/srv/newtalk-v2`로 변경해 `read_remote_file`, `list_remote_dir`, `run_remote_command`, git 도구가 동일한 운영 Git 루트를 기본 기준으로 사용하도록 보정.
- 검증: `/srv/newtalk-v2` 기준 `git status --short` 깨끗함, `git log -- src/app/Http/Controllers/Api/SourcingRpaController.php`에 `babb193 Persist VVIC batch scrape jobs` 확인, `php -l /srv/newtalk-v2/src/app/Http/Controllers/Api/SourcingRpaController.php` 통과. AADS 측 `python3 -m py_compile app/core/project_config.py`, `get_workdir('NTV2') == '/srv/newtalk-v2'`, 컨테이너 내부 `tool_read_remote_file`/`ToolExecutor.read_remote_file`가 `/srv/newtalk-v2/src/...`를 읽는 것까지 확인.

## 2026-05-12 09:11 KST - 채팅 last-response stale 실행 정리 보강

- 배경: 서버 재시작/프로듀서 유실 뒤 `chat_sessions.current_execution_id`가 죽은 `running/retrying` 실행을 계속 가리키면 `/last-response`가 `generating=true`만 반환해 최종 응답 병합을 막을 수 있는 경로가 확인됨.
- 조치: `app/routers/chat.py`에 `_settle_stale_execution_for_recovery()`를 추가하고 `/streaming-status`, `/last-response`가 동일 helper로 stale 실행을 terminalize하게 했다. 의미 있는 partial은 기존 `streaming_placeholder` row를 최종 assistant로 승격하고, 빈 placeholder는 삭제 후 `message_count`를 보정한다.
- 문서: `docs/chat/CHAT-CHANGELOG.md`, `docs/chat/CHAT-BACKEND-SPEC.md`에 last-response stale settlement 계약을 반영했다.
- 검증: `python3 -m py_compile app/routers/chat.py` 통과. `pytest -q tests/unit/test_tools_and_pipeline.py::TestRegressions::test_streaming_status_checks_db_placeholder tests/unit/test_tools_and_pipeline.py::TestRegressions::test_last_response_settles_stale_running_execution` 2개 통과. `git diff --check -- app/routers/chat.py tests/unit/test_tools_and_pipeline.py docs/chat/CHAT-CHANGELOG.md docs/chat/CHAT-BACKEND-SPEC.md HANDOVER.md` 통과.

## 2026-05-12 09:27 KST - Browser Bridge PC Agent CDP 세션 풀 보강

- 배경: 다중 Browser Bridge 세션은 `browser_session_id` 고정 호출까지 구현돼 있었지만, 세션 레지스트리가 프로세스 메모리라 재시작 후 사라지고, PC Agent가 띄운 Chrome CDP 포트는 CEO PC의 loopback이라 서버 Playwright가 직접 붙을 수 없는 구조적 한계가 확인됨.
- 조치: `SessionRegistry`를 `.browser_bridge_state/sessions.json` 지속 저장 방식으로 보강하고, 세션별 `lease_owner/lease_expires_at`을 추가해 작업별 세션 점유/해제가 가능하게 했다.
- 조치: `BrowserBridgeService.ensure_pc_agent_cdp_session()`을 추가해 PC Agent `browser_launch`를 capability 라우팅으로 실행하고, 결과 포트/프로필/agent_id를 `local_agent` Browser Bridge 세션으로 자동 등록하도록 했다.
- 조치: `local_agent` 세션을 Playwright-like context facade로 연결해 기존 `browser_navigate/snapshot/screenshot/click/fill/tab_list` 도구가 PC Agent의 `browser_*` 명령으로 프록시 실행되게 했다.
- API/도구: `POST /api/v1/browser-bridge/sessions/ensure-pc-cdp`, `/sessions/lease`, `/sessions/release-lease`를 추가하고, `browser_connect(action='ensure_pc_cdp')`를 tool schema와 executor에 노출했다.
- 운영 반영: active `aads-server-green`에 `bash scripts/reload-api.sh`로 hot-reload 적용(`재로드=51개`). MCP group 재시작 후 `mcp-filesystem/git/memory` RUNNING, `playwright-mcp`는 기존 설정대로 STOPPED 상태 유지.
- 검증: `python3 -m pytest tests/unit/test_browser_bridge.py -q` 14개 통과. `python3 -m py_compile app/browser_bridge/models.py app/browser_bridge/registry.py app/browser_bridge/service.py app/api/browser_bridge.py app/api/ceo_chat_tools.py app/services/tool_executor.py app/services/tool_registry.py tests/unit/test_browser_bridge.py` 통과. active 컨테이너 직접 호출 기준 `tool_browser_connect(action='status')` 정상 응답, `/api/v1/pc-agent/health`는 `connected=0`.

## 2026-05-12 09:41 KST - Browser Bridge PC Agent active API fallback

- 배경: PC Agent는 active `aads-server-green:8102`의 `/api/v1/pc-agent/health`에서 `connected=1`로 확인되지만, `browser_connect(action='ensure_pc_cdp')` 도구 프로세스는 자체 `pc_agent_manager` 메모리만 조회해 `no online PC agent`를 반환하는 불일치가 확인됨.
- 조치: `BrowserBridgeService.ensure_pc_agent_cdp_session()`에 로컬 manager가 `PC_AGENT_OFFLINE`을 반환하면 `.active_port` 기준 active API의 `/api/v1/pc-agent/route-execute`로 `browser_launch`를 재시도하는 fallback을 추가. 성공 결과는 기존과 동일하게 `local_agent` Browser Bridge 세션으로 등록한다.
- 검증: active API 직접 호출로 `agent_id=2e9379a1-fed`, `port=9222`, `cdp_ready=true` 확인. `python3 -m py_compile app/browser_bridge/service.py` 통과. `python3 -m pytest tests/unit/test_browser_bridge.py` 16개 통과. 로컬 service 직접 호출로 `bb-3e4b1af2c101` local_agent 세션 생성 확인.

## 2026-05-12 09:49 KST - 채팅 last-response stale recovery 보강

- 배경: 서버 재시작 또는 런타임 유실 뒤 `chat_turn_executions.status='running'`과 `streaming_placeholder`만 남고 실제 in-memory producer는 없는 경우, `last-response`/`streaming-status`가 `updated_at` 최근성만 근거로 최대 5분 동안 `generating=true`를 반환해 최종 응답 복구를 막는 구간이 남아 있었다.
- 조치: `app/routers/chat.py`에 `_has_live_streaming_runtime()`를 추가해 `interrupt_queue`, `_streaming_state`, `_active_bg_tasks`를 함께 보고 실제 live producer 존재를 먼저 판별하도록 보강했다.
- 조치: `_settle_stale_execution_for_recovery()`는 live runtime이 없고 DB상 partial/tool/last_event 진행 흔적이 20초 이상 남아 있으면 recent execution도 즉시 `interrupted`로 정리하고 placeholder 내용을 최종 보존 응답으로 승격하도록 변경했다.
- 검증: active `aads-server-green` 컨테이너 내부 `/app/app/routers/chat.py`에 recovery patch 문자열 존재 확인. `python3 -m py_compile app/routers/chat.py` 통과. `pytest tests/unit/test_tools_and_pipeline.py -q -k 'last_response or streaming_status'` 통과. DB 실측 기준 `running` 2건, `streaming_placeholder` 2건이며 이 중 하나는 현재 활성 채팅 세션 `8ad08...`의 진행 중 응답이다.

## 2026-05-12 09:54 KST - 채팅 최종 저장 placeholder 우선순위 보강

- 배경: DB 실측에서 현재 세션 실행 `60fb54d2...`가 `retrying`이고, `assistant_message_id`는 과거 장애 안내 메시지(3,353자)를 가리키며 최신 응답은 별도 `streaming_placeholder`(2,390자)에 남는 불일치가 확인됨. `last-response` 조회는 placeholder 우선으로 보정됐지만, 최종 저장 함수 `_save_and_update_session()`은 여전히 `assistant_message_id`를 먼저 선택해 과거 row를 최종 응답으로 덮어쓸 수 있었다.
- 조치: `app/services/chat_service.py` 최종 저장 경로가 execution-scoped `streaming_placeholder`를 먼저 승격하고, `chat_turn_executions.assistant_message_id`도 최종 row id로 교체하도록 변경했다.
- 조치: `app/main.py` startup resume claim도 placeholder가 있으면 실행의 `assistant_message_id`를 placeholder id로 정렬하도록 변경했다.
- 검증: `python3 -m py_compile app/main.py app/routers/chat.py app/services/chat_service.py` 통과. `pytest -q tests/unit/test_tools_and_pipeline.py -k 'last_response or streaming_status or final_save'` 3개 통과. `bash scripts/reload-api.sh` 성공(`재로드=45개`). DB 실측 기준 09:54 KST에 `running/retrying` 0건, `streaming_placeholder` 0건.

## 2026-05-13 - DB 기반 미디어/LLM 모델 라우팅 및 어드민 반영

- 작업 ID: `AADS-MEDIA-ADMIN-DB-CONFIG-P1-20260513`, 대상 채팅 세션 `8ad08cc2-620c-4a70-8305-74a8d9b43c4e`.
- 변경 파일: `app/services/media_generation_service.py`, `app/api/llm_models.py`, `app/api/image.py`, `app/api/ceo_chat_tools.py`, `app/services/tool_executor.py`, `app/services/tool_registry.py`, `aads-dashboard/src/app/admin/model-routing/page.tsx`, `aads-dashboard/src/lib/api.ts`, `aads-dashboard/src/components/Sidebar.tsx`, `aads-dashboard/src/app/settings/page.tsx`, `migrations/089_model_routing_preferences.sql`, `tests/unit/test_media_generation_service.py`, `tests/unit/test_model_routing_admin_static.py`.
- DB migration/seed: `migrations/089_model_routing_preferences.sql`가 `model_routing_preferences`를 idempotent 생성하고, `llm_models`/`chat_model_preferences`에 CEO 지정 모델을 seed한다. 기본값은 기존 default가 없을 때만 `image=gpt-image-2`, `edit_image=gpt-image-2`, `video=sora-2`, `llm=gpt-5.5`로 설정한다.
- 라우팅: `MediaGenerationService.resolve_route()` 순서를 `explicit request override > DB default/preference > env/config fallback > NOT_CONFIGURED/disabled/provider unavailable`로 변경했다. `imagen-4.0-*` prefix는 계속 인식하며, disabled/default 미설정/adapter pending은 `availability`, `route_source`, `MODEL_DISABLED`/`NOT_CONFIGURED`/`PROVIDER_UNAVAILABLE` 상태로 반환한다.
- API/Admin: `/api/v1/llm-models/routing-preferences` GET/PUT을 추가했다. 대시보드 `/admin/model-routing`에서 이미지/이미지편집/동영상/LLM별 provider, model_id, availability, enabled/default, notes를 조회하고 기본 모델/활성 상태를 저장할 수 있다.
- 검증 SQL:
  - `SELECT route_key, provider, model_id, is_enabled, is_default, notes FROM model_routing_preferences ORDER BY route_key, display_order;`
  - `SELECT provider, model_id, execution_model_id, is_selectable, is_executable, verification_status FROM llm_models WHERE model_id IN ('gpt-image-2','imagen-4.0-generate-001','gemini-3.1-flash-image-preview','sora-2','sora-2-pro','veo-3.1-generate-preview','gpt-5.5','claude-opus-4-7','gemini-3.1-pro-preview') ORDER BY provider, model_id;`
  - `SELECT preference_key, provider, model_id, is_favorite, is_pinned FROM chat_model_preferences WHERE model_id IN ('gpt-5.5','claude-opus-4-7','gemini-3.1-pro-preview') ORDER BY display_order;`
- 검증 명령: `python3 -m py_compile app/services/media_generation_service.py app/api/llm_models.py app/api/image.py app/services/tool_executor.py app/api/ceo_chat_tools.py app/services/tool_registry.py` 통과. `python3 -m pytest -q tests/unit/test_media_generation_service.py tests/unit/test_model_routing_admin_static.py` 13개 통과. `git diff --check` 통과. `npx tsc --noEmit --pretty false`는 로컬 `tsc`가 없어 npm registry 조회를 시도했고, 네트워크 제한(`ENOTFOUND registry.npmjs.org`)으로 수행되지 않았다.
- 상태: P1 백엔드는 `runner-9852ee94` 승인 후 `113ba80`으로 main/origin 반영됐다. 러너의 대시보드 배포는 nginx 검증 단계에서 실패했으나, 실제 대시보드 저장소에 별도 보정 커밋 `6fd83ff feat: add model routing admin page`를 적용/푸시하고 `bash deploy.sh`로 blue-green 배포를 완료했다. `migrations/089_model_routing_preferences.sql`는 운영 DB에 수동 적용해 `model_routing_preferences` 12건, 최신 chat model preference 3건이 확인됐다.

## 2026-05-12 09:54 KST - 채팅 응답 사라짐 재발 원인 확정 및 검증 갱신

- 원인: 09:50 KST active 컨테이너 재시작(SIGTERM) 중 현재 응답의 in-memory producer가 사라졌고, DB에는 `retrying` 실행과 `streaming_placeholder` 본문만 남았다. 해당 실행의 `assistant_message_id`는 과거 limit 장애 안내 메시지를 가리켜 `COALESCE(am.content, pm.content)` 계열 조회가 최신 placeholder 본문을 놓칠 수 있었다.
- 조치: active 컨테이너에 반영된 `app/routers/chat.py`/`app/main.py`가 running/retrying 상태에서 placeholder 본문을 우선 읽는지 확인했다. `/last-response`와 `/streaming-status`가 live runtime 부재 시 stale 실행을 `interrupted`로 정리하도록 동작 확인했다.
- 추가 보정: `app/api/ceo_chat_tools.py`의 AADS 로컬 파일 workdir을 `/app` 고정에서 `_aads_local_workdir()`으로 변경해 Docker(`/app`)와 호스트 테스트(`/root/aads/aads-server`) 양쪽에서 `read_remote_file`/`patch_remote_file`가 같은 경로를 읽게 했다.
- 검증: `python3 -m py_compile app/api/ceo_chat_tools.py app/main.py app/routers/chat.py` 통과. `pytest -q tests/unit/test_tools_and_pipeline.py` 47개 통과. `pytest -q tests/unit/test_chat_service.py tests/unit/test_context_continuity.py` 25개 통과. 09:54 KST DB 실측 기준 최근 6시간 `running/retrying` 0건, 전체 `streaming_placeholder` 0건.

## 2026-05-12 10:49 KST - AADS Blue-Green 양 슬롯 동기화 및 host-only 포트 정책

- 배경: Blue-Green 전환 후 새 active 슬롯만 최신 빌드가 되고 이전 슬롯이 stale standby로 남으면 다음 전환 시 미반영 코드가 다시 active가 될 수 있는 문제가 확인됨. 또한 기존에 생성된 blue 슬롯은 `0.0.0.0:8100/3100`으로 열려 있어 외부 접근면이 nginx `:443` 밖으로 남을 수 있었다.
- 조치: `deploy.sh`에 `sync_standby_slot_after_drain()`을 추가해 API Blue-Green 전환 성공 후 이전 슬롯의 active stream이 빠진 뒤 같은 release로 재빌드하도록 변경했다. 전환 전 active stream drain 대기는 제거하고, nginx reload 후 old slot drain/sync로 넘겨 신규 요청은 즉시 새 슬롯으로 가게 했다.
- 조치: `/root/aads/aads-dashboard/deploy.sh`도 전환 후 이전 dashboard 슬롯을 stop하지 않고 같은 release로 재빌드해 warm standby로 동기화하도록 변경했다.
- 조치: `docker-compose.prod.yml`의 API/dashboard blue/green publish port를 `127.0.0.1` 바인딩으로 제한하고, 기존 컨테이너가 재생성되기 전까지 `scripts/apply-bg-port-firewall.sh`와 `scripts/aads-bg-host-only-ports.service`로 BG 포트 직접 접근 차단을 보강했다.
- 주석 정리: `nginx-aads-upstream.conf`와 운영 `/etc/nginx/conf.d/aads-upstream.conf`의 stale active-slot 주석을 “non-backup line이 active” 기준으로 정리했다.
- 검증: `bash -n deploy.sh`, `bash -n /root/aads/aads-dashboard/deploy.sh`, `docker compose -f docker-compose.prod.yml config --quiet` 통과. 실측 런타임은 `aads-server-green:8102`와 `aads-dashboard-green:3101`은 loopback 바인딩으로 재생성 완료, active `aads-server:8100`/`aads-dashboard:3100`은 현재 활성 스트림 보호 때문에 다음 BG 순환 시 loopback 바인딩으로 재생성 예정.

## 2026-05-12 10:54 KST - AADS Blue-Green 자동동기화 적용 범위 재점검

- 점검 범위: API blue/green, Dashboard blue/green, `docker-compose.prod.yml` 포트 publish, `/etc/nginx/conf.d/aads-upstream.conf`, 저장소 `nginx-aads-upstream.conf`, systemd host-only guard, 현재 컨테이너 image/port 상태.
- 확인 결과: 파일 기준으로 API `deploy.sh`와 Dashboard `deploy.sh` 모두 전환 후 이전 슬롯을 같은 release로 재빌드하는 standby 동기화가 적용되어 있다. compose 파일도 API `8100/8102`, Dashboard `3100/3101` publish가 모두 `127.0.0.1`로 제한되어 있다.
- 보정: 저장소 `nginx-aads-upstream.conf`의 API active 슬롯이 운영 `/etc/nginx/conf.d/aads-upstream.conf`와 달리 `8102` active로 남아 있어, 현재 운영 기준인 `8100` active / `8102` backup으로 맞췄다. Dashboard `deploy.sh` 상단 설명도 “이전 슬롯 유지”에서 “이전 슬롯 standby 동기화”로 정리했다.
- 런타임 상태: 현재 active API는 `aads-server:8100`, active Dashboard는 `aads-dashboard:3100`이다. green 슬롯(`8102`, `3101`)은 이미 loopback 바인딩과 최신 이미지로 재생성됐지만, blue active 슬롯은 활성 스트림 보호 때문에 아직 기존 image/`0.0.0.0` publish 상태로 살아 있다. host-only firewall guard는 active 상태로 직접 접근 차단을 보강 중이다.
- 검증: `bash -n deploy.sh`, `bash -n /root/aads/aads-dashboard/deploy.sh`, `diff -u nginx-aads-upstream.conf /etc/nginx/conf.d/aads-upstream.conf`, `nginx -t`, API health `8100/8102=200`, active stream `8100=4`, `8102=0`.

## 2026-05-12 10:55 KST - 채팅 완료 직후 버블 소실 DB 노출 폴백

- 배경: 실시간 응답 완료 직후 `/last-response`가 `generating=true`를 반환하면 프론트가 병합을 중단해, DB에는 `streaming_placeholder` 본문이 저장되어 있어도 화면에서 응답 버블이 사라지는 경로가 남아 있었다.
- 조치: Dashboard `src/app/chat/page.tsx`의 `mergeLatestAssistantFromServer()`에 `/chat/messages?...&include_streaming=true` 폴백을 추가했다. `/last-response`가 최종 메시지를 못 주더라도 DB에 저장된 assistant 또는 내용 있는 `streaming_placeholder`를 recovered assistant로 병합해 새로고침/완료 직후 화면에서 버리지 않게 했다.
- 검증: `npx tsc --noEmit` 통과. `npx eslint src/app/chat/page.tsx` 에러 0개, 기존 경고 21개. DB 실측 기준 10:55 KST `streaming_placeholder=2`, visible assistant `26,607`.

## 2026-05-12 11:01 KST - AADS Blue-Green 전체 대상 재점검 및 레거시 우회 차단

- 배경: BG가 필요한 Docker/API/server/dashboard 항목 전체에 전환 후 standby 자동동기화가 적용됐는지 재확인했다. 표준 경로는 맞지만 `scripts/blue_green_deploy.sh`가 이전 구현으로 남아 있어 수동 실행 시 old slot stop 및 미동기화 상태를 만들 수 있었다.
- 조치: `scripts/blue_green_deploy.sh`를 표준 `/root/aads/aads-server/deploy.sh bluegreen` 래퍼로 바꿔 중복 구현을 제거했다.
- 조치: `app/core/prompts/system_prompt_v2.py`, `app/services/ckp_manager.py`의 오래된 `docker compose ... aads-server` 배포 문구를 `bash /root/aads/aads-server/deploy.sh bluegreen`으로 정리했다.
- 검증: `bash -n deploy.sh`, `bash -n scripts/blue_green_deploy.sh`, `bash -n /root/aads/aads-dashboard/deploy.sh`, `python3 -m py_compile app/core/prompts/system_prompt_v2.py app/services/ckp_manager.py`, `docker compose -f docker-compose.prod.yml config --quiet`, `nginx -t` 통과. `8100/8102` API health와 외부 `https://aads.newtalk.kr/api/v1/health`, `/login` 200 확인.
- 남은 상태: active API `aads-server:8100`은 기존 컨테이너라 런타임 publish가 아직 `0.0.0.0:8100->8080`으로 보인다. 저장소 compose는 `127.0.0.1`로 수정됐고 host-only firewall guard가 보강 중이며, 다음 BG 순환에서 active blue가 재생성되면 publish도 loopback으로 맞춰진다.

## 2026-05-12 11:09 KST - 채팅 완료 직후 버블 소실 서비스워커/폴백 보강

- 배경: 대시보드 `public/sw.js`가 `/chat`을 precache하고 캐시명을 `aads-v1`로 고정해, 배포 후에도 브라우저가 구버전 `/chat` shell과 예전 메시지 병합 로직을 실행할 수 있었다. 또한 `page.tsx`의 별도 last-response fallback 루프가 `generating=true`에서 DB 메시지 폴백 없이 중단하는 경로가 남아 있었다.
- 조치: Dashboard `public/sw.js`를 `aads-v2-static-only`로 변경하고 `/chat`, `/api`, `/_next` 요청은 항상 network-only로 처리하게 했다. `src/app/chat/page.tsx`의 별도 last-response fallback도 `generating=true`에서 `mergeLatestAssistantFromServer()`를 호출해 DB에 저장된 assistant 또는 내용 있는 `streaming_placeholder`를 recovered assistant로 병합하도록 보강했다.
- 검증: `npx tsc --noEmit` 통과. `npx eslint src/app/chat/page.tsx` 에러 0개, 기존 경고 21개. 양 dashboard 슬롯(`aads-dashboard`, `aads-dashboard-green`)과 외부 `http://127.0.0.1:3101/sw.js`에서 `/chat`/`/api`/`/_next` network-only 정책 확인. DB 실측 기준 11:08 KST `streaming_placeholder=2`, visible assistant `26,612`, `chat_turn_executions`는 `completed=2,237`, `interrupted=3,613`, `retrying=2`.

## 2026-05-12 11:15 KST - 채팅 버블 소실 최종 재검증

- 재검증: 대시보드 `src/app/chat/page.tsx`의 `mergeLatestAssistantFromServer()`에 `/chat/messages?...&include_streaming=true` 폴백이 적용되어 있고, `public/sw.js`는 `/chat`, `/api`, `/_next`를 network-only로 처리한다.
- 운영 확인: 외부 `https://aads.newtalk.kr/sw.js`, blue `127.0.0.1:3100/sw.js`, green `127.0.0.1:3101/sw.js` 모두 `aads-v2-static-only` 서비스워커를 반환했다. 양 dashboard 컨테이너 `/app/public/sw.js`에도 동일 문자열이 확인됐다.
- DB 실측: `chat_messages.intent='streaming_placeholder'` 0건, `chat_turn_executions.status='running'` 0건, 상태 집계는 `completed=2,238`, `interrupted=3,614`, `retrying=1`이다.
- 검증: `npx tsc --noEmit` 통과. `npx eslint src/app/chat/page.tsx` 에러 0개, 기존 경고 21개.
- 주의: 변경은 운영에 배포됐지만 아직 dashboard/server 저장소에 커밋/푸시하지 않았다.

## 2026-05-12 11:30 KST - 채팅 추가지시 중 이전 응답 버블 보존

- 배경: 스트리밍 중 추가 지시 또는 새 요청을 시작할 때 Dashboard `src/app/chat/page.tsx`가 기존 `streaming_placeholder`를 `prev.filter(m => m.intent !== "streaming_placeholder")`로 제거한 뒤 새 placeholder를 붙이는 경로가 확인됨. 이 때문에 이전 지시에 대한 부분 응답 버블이 화면에서 사라질 수 있었다.
- 조치: `freezeStreamingPlaceholders()`를 추가해 새 요청 시작 전 기존 진행 버블을 삭제하지 않고 `interrupted`/부분 응답 assistant 버블로 고정하도록 변경했다.
- 조치: 서버 최종 메시지 병합 시 같은 `execution_id`의 DB placeholder만 제거하도록 `mergeServerMessagesPreservingLocal()`을 보강했다. 이로써 DB에 저장된 최종 assistant가 들어오면 오래된 placeholder 잔상은 정리하되, 다른 진행 버블은 삭제하지 않는다.
- 검증: `npx tsc --noEmit` 통과. `npx eslint src/app/chat/page.tsx` 에러 0개, 기존 경고 21개. `python3 -m py_compile app/routers/chat.py app/main.py app/services/chat_service.py` 통과. `pytest tests/unit/test_chat_service.py tests/unit/test_chat_lightweight_regression.py tests/unit/test_chat_lightweight_frontend_static.py tests/unit/test_response_completion_contract.py` 36개 통과.

## 2026-05-12 11:34 KST - Browser Bridge CDP 등록 타임아웃 보강

- 배경: `browser_connect(action='ensure_pc_cdp')`가 기존 Browser Bridge 세션은 보지만 새 CDP 등록 실행에서 약 95초 후 `no online PC agent`로 실패했다. 실측 결과 `.active_port`/외부 도메인은 `8100`을 보는데 PC Agent WebSocket은 `8102` 슬롯에 붙어 있었다.
- 원인: `BrowserBridgeService._execute_pc_agent_route_via_active_api()`가 `.active_port` 단일 슬롯만 route-execute fallback으로 시도했다. 또한 컨테이너 내부에서는 `127.0.0.1:8102`가 호스트 포트가 아니므로 `8102 -> aads-server-green:8080` 컨테이너 DNS fallback이 필요했다.
- 조치: `app/browser_bridge/service.py`에 `_active_api_ports()`를 추가해 `8100/8102` 양 슬롯을 fallback 후보로 시도하고, `_active_api_route_urls()`가 `8100 -> aads-server:8080`, `8102 -> aads-server-green:8080`을 직접 포함하도록 변경했다.
- 검증: `pytest -q tests/unit/test_browser_bridge.py tests/unit/test_pc_agent_routing_leases.py` 24개 통과. `curl http://127.0.0.1:8102/api/v1/pc-agent/route-execute` 직접 호출로 `browser_launch` 성공, `agent_id=2e9379a1-fed`, `port=9222`, `cdp_ready=true` 확인. 새 컨테이너 Python 프로세스에서 `ensure_pc_agent_cdp_session()` 성공, `bb-ba65758c530c local_agent 2e9379a1-fed 9222` 확인.
- 주의: 현재 채팅에 붙은 MCP stdio transport는 구버전 모듈을 들고 있어 `pkill -f mcp_servers.aads_tools_bridge`로 종료했으며, 직후 MCP 호출은 `Transport closed`를 반환했다. 서버 전체 재시작은 하지 않았다. 다음 MCP attach는 새 코드 기준으로 떠야 한다.

## 2026-05-12 11:52 KST - 채팅 응답 보존/추가지시 이어쓰기 보강

- 배경: 실시간 응답 완료 후 또는 서버/LLM 연결 끊김 후 DB에 저장된 assistant/`streaming_placeholder` 내용이 화면에서 숨겨지는 경로가 남아 있었다. 또한 스트리밍 중 CEO 추가지시가 반영될 때 기존 응답 버블이 `stream_reset`으로 지워지고 새 응답만 이어지는 UX가 확인됐다.
- 조치: `app/routers/chat.py`의 `/streaming-status`, `/last-response`가 live runtime 없는 DB-only placeholder를 무기한 `generating=true`로 숨기지 않고, 의미 있는 내용은 `interrupted` assistant로 승격해 화면에 노출하도록 보강했다. 빈 placeholder는 삭제해 빈 생성 버블이 남지 않게 했다.
- 조치: `app/services/chat_service.py`에 `_save_interrupted_partial_message()`를 추가해 추가지시 반영 직전 현재까지의 응답을 별도 assistant 버블로 DB 저장하고, SSE `partial_preserved` 이벤트로 프론트에 즉시 병합한다.
- 조치: Dashboard `src/app/chat/page.tsx`가 `partial_preserved` 이벤트를 수신하면 기존 버블을 보존한 뒤 새 stream buffer만 reset하도록 변경했다. `src/services/chatApi.ts`의 SSE/streaming-status 타입도 백엔드 응답 필드에 맞췄다. Service Worker `public/sw.js`의 `/chat`/`/api`/`/_next` network-only 정책은 유지했다.
- 검증: `python3 -m py_compile app/routers/chat.py app/services/chat_service.py` 통과. `pytest -q tests/unit/test_chat_service.py::test_deferred_interrupt_rewrites_no_tool_stream_before_save` 1개 통과. `pytest -q tests/unit/test_tools_and_pipeline.py -k 'last_response_settles_stale_running_execution or settle_stale_execution_recovers_recent_progress_without_live_runtime or settle_stale_execution_keeps_recent_live_runtime'` 3개 통과. Dashboard `npx tsc --noEmit --pretty false` 통과.

## 2026-05-12 12:52 KST - 채팅 DB 저장 응답 새로고침 노출 보장

- 배경: `aa433b41-0ad2-421c-ae7c-bac4806035cc` 최근 응답 점검 중 대상 세션 자체는 최신 실행이 `completed`였지만, 전역 DB에는 최근 `running` 실행 5건과 `streaming_placeholder` 5건이 남아 있었다. 프론트 일부 메시지 재조회 경로가 `include_streaming=true` 없이 `/chat/messages`를 호출해 DB에 저장된 내용 있는 placeholder를 새로고침/완료 폴링에서 놓칠 수 있었다.
- 조치: Dashboard `src/app/chat/page.tsx`에 `surfaceDbSavedStreamingPlaceholders()`를 추가했다. DB에 저장된 `streaming_placeholder` 본문이 10자 초과면 일반 assistant/recovered 버블로 승격해 병합하고, 빈 placeholder는 active/waiting 경로에서만 생성 중 버블로 유지한다.
- 조치: 초기 로드, 빈 화면 자동 재시도, 이전 메시지 로드, execution replay 완료, just_completed 폴링, SSE 무음 종료 복구, stop 이후 DB 동기화, background stop 동기화의 `/chat/messages` 호출에 `include_streaming=true`를 적용했다.
- 검증: Dashboard `npx tsc --noEmit` 통과. `npx eslint src/app/chat/page.tsx` 에러 0개, 기존 경고 21개. DB 실측 기준 12:51 KST `chat_turn_executions`는 `completed=2256`, `interrupted=3627`, `running=5`, `streaming_placeholder=5`이며 5건 모두 최근 활성 응답으로 강제 정리하지 않았다.

## 2026-05-12 13:11 KST - Runner 제출 세션 오귀속 차단

- 배경: `b3390fab-8b0a-43a0-a1fc-b9ec1ce85f57` 채팅창에서 러너 작업을 지시했지만, 프롬프트에 포함된 다른 GO100 채팅 URL의 `session_id`가 도구 입력으로 전달되며 러너 job이 다른 세션으로 귀속되는 현상이 확인됨.
- 조치: `app/services/agent_sdk_service.py`, `app/services/tool_executor.py`, `app/api/ceo_chat_tools.py`에서 `pipeline_runner_submit`, `pipeline_runner_submit_batch`, `pipeline_c_start`는 현재 채팅 핸들러/ContextVar 세션을 도구 입력의 `session_id`보다 우선하도록 변경했다. 현재 세션이 있으면 URL에서 추출된 다른 세션 ID는 덮어쓰고 경고 로그를 남긴다.
- 조치: 프로젝트 자동 추론도 러너 제출 계열에서는 덮어쓴 현재 세션 기준으로 수행되게 보정했다. 세션이 전혀 없을 때만 외부 직접 호출 fallback으로 입력 `session_id`를 사용하며, 프로젝트 최근 활성 세션 fallback은 계속 금지한다.
- 검증: `python3 -m py_compile app/services/agent_sdk_service.py app/services/tool_executor.py app/api/ceo_chat_tools.py` 통과. `pytest -q tests/unit/test_runner_scope_defaults.py` 9개 통과. 신규 회귀 테스트로 잘못 전달된 `session_id`가 현재 채팅 세션으로 덮어써지는지 확인했다.

## 2026-05-12 13:43 KST - Codex/Claude CLI relay 재시도 2초 30회 적용

- 배경: Codex relay는 기존 `2초, 5초` 2회 재시도였고, Claude CLI relay에는 같은 모델로 이어쓰기 재시도 래퍼가 없어 429/timeout/relay 5xx/일시 연결 끊김 시 응답이 중단될 수 있었다.
- 조치: `app/services/model_selector.py`에 공통 relay 재시도 정책을 추가해 Codex relay와 Claude CLI relay 모두 기본 `2초 간격 x 30회` 재시도로 통일했다. 환경변수 `AADS_RELAY_RETRY_INTERVAL_SECONDS`, `AADS_RELAY_RETRY_MAX_RETRIES`로 조정 가능하다.
- 조치: Claude CLI relay도 partial 응답이 있으면 재시도 요청에 직전 assistant 초안을 붙여 동일 모델이 마지막 문장 다음부터 자연스럽게 이어 쓰도록 보강했다. 명시적 quota/결제/인증/`You've hit your limit ... resets` 계열은 재시도하지 않는다.
- 검증: `python3 -m py_compile app/services/model_selector.py` 통과. `pytest -q tests/unit/test_model_selector_dynamic_routing.py::test_stream_cli_relay_retries_same_model_before_returning_done tests/unit/test_model_selector_dynamic_routing.py::test_stream_codex_relay_retries_same_model_before_returning_done tests/unit/test_model_selector_dynamic_routing.py::test_relay_retry_policy_defaults_to_two_seconds_thirty_retries` 3개 통과.

## 2026-05-12 13:52 KST - Runner/작업조회 도구 현재 채팅 세션 선주입

- 배경: 러너 오귀속 방지 패치 후, 도구 실행 컨텍스트에 현재 채팅 세션이 표시되기 전에 모델이 `session_id: null` 또는 다른 URL의 세션 ID를 만든 경우 `pipeline_runner_submit`이 "현재 채팅 세션을 확인할 수 없습니다"로 차단되거나 `check_task_status`가 `session_id: null`로 표시되는 경로가 남아 있었다.
- 조치: `app/services/model_selector.py`에 `_bind_tool_session_input()`을 추가해 `pipeline_runner_submit`, `pipeline_runner_submit_batch`, `pipeline_c_start`, `pipeline_runner_status`, `check_task_status`, `check_directive_status` 호출은 프론트 `tool_use` 이벤트 표시 전과 실제 실행 전 모두 현재 AADS 채팅 세션 ID를 주입하도록 했다. `scope=all` 요청은 전역 조회 의도를 존중해 세션을 주입하지 않는다.
- 검증: `python3 -m py_compile app/services/model_selector.py app/services/tool_executor.py app/api/ceo_chat_tools.py` 통과. `pytest -q tests/unit/test_runner_scope_defaults.py` 10개 통과. 신규 회귀 테스트로 잘못 전달된 러너 `session_id` 덮어쓰기, `check_task_status(session_id=None)` 현재 세션 주입, `scope=all` 예외를 확인했다.

## 2026-05-12 15:39 KST - Agent SDK 상태조회 세션 바인딩 누락 보정

- 배경: 위 세션 선주입 패치 후에도 Agent SDK 자동 트리거 경로에서는 `check_task_status` 기본 범위 결정이 `current_chat_session_id`만 보고 있어 `session_id: null`처럼 보이거나, 자동 트리거 안내문이 여전히 `session_id` 수동 전달을 요구하는 불일치가 남아 있었다.
- 조치: `app/services/tool_executor.py`, `app/api/ceo_chat_tools.py`의 `_resolve_task_scope()`가 `_resolve_bound_chat_session_id()`를 사용하도록 바꿔 Agent SDK active chat binding까지 같은 규칙으로 적용했다. `app/services/chat_service.py`의 자동 트리거 안내문도 `pipeline_runner_submit`, `pipeline_runner_submit_batch`, `check_task_status` 모두 서버가 현재 채팅 세션을 자동 주입한다고 명시하도록 정리했다.
- 검증: `python3 -m py_compile app/services/tool_executor.py app/api/ceo_chat_tools.py app/services/chat_service.py tests/unit/test_runner_scope_defaults.py` 통과. `pytest -q tests/test_pc_agent_command_builder.py tests/unit/test_runner_scope_defaults.py tests/unit/test_tools_and_pipeline.py` 99개 통과. 신규 회귀 테스트로 Agent SDK active session만 있을 때 `check_task_status`와 `pipeline_runner_status`가 현재 세션 필터를 유지하는지 확인했다.

## 2026-05-12 16:11 KST - PC Agent 브라우저 신규 명령 자동 업데이트 유도

- 배경: Browser Bridge 세션은 `local_agent`로 남아 있었고 `8102` green 슬롯의 `/api/v1/pc-agent/health`는 PC Agent 1개 연결을 보고했지만, `browser_check` 더미 실행이 `지원하지 않는 명령: browser_check`로 실패했다. 원인은 CEO PC에서 실행 중인 PC Agent 코드가 신규 브라우저 명령 핸들러를 아직 받지 못했는데 서버와 로컬 버전이 모두 `1.0.20`이라 자동 업데이트가 버전 차이를 감지하지 못한 상태였다.
- 조치: `app/services/pc_agent_command_builder.py`에 남아 있던 병합 충돌 마커를 제거해 업로드 자연어 명령 빌더를 복구하고, `pc_agent/VERSION`을 `1.0.21`로 올려 PC Agent 5분 주기 자동 업데이트 루프가 새 ZIP 다운로드와 자체 재기동을 수행하도록 유도했다.
- 검증: `curl http://127.0.0.1:8102/api/v1/kakao-bot/agent/version` 응답이 `version=1.0.21`로 변경됨을 확인했다. `python3 -m py_compile app/services/pc_agent_command_builder.py pc_agent/agent.py pc_agent/launcher.py pc_agent/commands/browser_auto.py pc_agent/commands/__init__.py` 통과. `pytest -q tests/test_pc_agent_command_builder.py tests/unit/test_browser_bridge.py tests/unit/test_tools_and_pipeline.py` 107개 통과.
- 남은 확인: CEO PC Agent가 다음 업데이트 주기 후 재접속하면 `browser_check`/`browser_upload_file` 더미 실행이 `지원하지 않는 명령`이 아닌 selector/file validation 오류로 바뀌는지 확인해야 한다. 즉시 필요하면 CEO PC에서 `run.bat` 재실행이 가장 빠른 강제 갱신 경로다.

## 2026-05-12 16:16 KST - Backend Blue-Green 배포 상태 파일 보정

- 배경: nginx upstream은 `8102` green 슬롯이 active였지만 `.active_port`가 `8100`으로 남아 `deploy.sh bluegreen`이 실제 active 슬롯을 전환 대상으로 오판했다. upstream 파일에는 API와 WS upstream의 non-backup 라인이 각각 있어 기존 `grep -c` 기준이 2줄을 보고 상태 파일 fallback으로 떨어졌다.
- 조치: `deploy.sh`의 active port 판정을 non-backup 포트의 고유값(`sort -u`) 기준으로 바꾸고, active container도 판정된 포트에서 직접 동기화하도록 수정했다. 이후 standby `8100`의 stale active task 1건은 DB상 완료 응답 저장을 확인한 뒤 `AADS_DEPLOY_ALLOW_BUSY_TARGET=true`로 standby 한정 rebuild를 허용해 blue-green 전환을 완료했다.
- 검증: `bash -n deploy.sh` 통과. `bash /root/aads/aads-server/deploy.sh bluegreen` 1차는 잘못된 상태 파일 때문에 target busy로 중단됐고, 패치 후 `AADS_DEPLOY_ALLOW_BUSY_TARGET=true bash /root/aads/aads-server/deploy.sh bluegreen`은 Phase 0.5~6 모두 통과했다. 배포 후 `.active_port=8100`, `.active_container=aads-server`, `curl http://127.0.0.1:8100/api/v1/health` 및 `curl https://aads.newtalk.kr/api/v1/health` 모두 `status=ok` 확인.

## 2026-05-13 12:22 KST - DeepSeek 채팅 선택 응답 중단 원인 보정

- 배경: 채팅창에서 DeepSeek V4 Pro/Flash를 선택하면 프론트와 DB 레지스트리는 `deepseek-v4-pro`/`deepseek-v4-flash`를 활성 모델로 노출하지만, 실제 LiteLLM 런타임에는 `deepseek-reasoner`/`deepseek-chat`만 등록되어 있었다. 직접 호출 결과 `deepseek-v4-pro`는 LiteLLM 400 Invalid model, `deepseek-reasoner`는 200 OK였다.
- 조치: `app/services/model_selector.py`에 DeepSeek 표시 모델과 LiteLLM 실행 모델을 분리하는 런타임 alias를 추가했다. 화면/비용/응답 모델 표시는 `deepseek-v4-*`를 유지하고, LiteLLM 호출은 `deepseek-v4-pro -> deepseek-reasoner`, `deepseek-v4-flash -> deepseek-chat`으로 보낸다.
- 조치: `app/services/model_registry.py` 템플릿도 같은 실행 alias를 쓰도록 변경해 향후 레지스트리 재동기화 시 `execution_model_id`가 실제 LiteLLM 모델명으로 저장되게 했다.
- 검증: `python3 -m py_compile app/services/model_selector.py app/services/model_registry.py` 통과. `pytest -q tests/unit/test_model_selector_dynamic_routing.py` 20개 통과. 운영 DB `llm_models` DeepSeek 4건의 `execution_model_id`를 `deepseek-v4-pro -> deepseek-reasoner`, `deepseek-v4-flash -> deepseek-chat`으로 보정했다. 컨테이너 내부 `call_stream(model_override='deepseek-v4-pro')` 실호출에서 `delta='OK'`, `done.model='deepseek-v4-pro'` 확인.

## 2026-05-13 16:25 KST - 채팅 TODO 목록 수동 정리 액션 추가

- 배경: 채팅창 상단 TODO 패널은 조회/접기/진행 필터만 제공해 `pending`, `failed`, `completed`, `skipped` 항목을 사용자가 직접 정리하거나 실패 항목을 재시도할 수 없었다.
- 조치: `app/services/chat_todo_service.py`에 세션 범위 보호가 있는 `update_session_todo_item`, `delete_session_todo_item`, `clear_session_todos`, `retry_failed_session_todos`를 추가했다. `app/routers/chat.py`에는 `PATCH/DELETE /chat/sessions/{session_id}/todos/{todo_id}`, `POST /chat/sessions/{session_id}/todos/clear`, `POST /chat/sessions/{session_id}/todos/retry-failed`를 추가했다.
- 조치: Dashboard `src/app/chat/page.tsx` TODO 패널에 실패 재시도, 완료 비우기, 실패 비우기, 대기 비우기, 항목별 재시도/제외/숨김 버튼을 연결했다. 기본 표시는 진행/대기 우선 정책을 유지한다.
- 검증: `python3 -m py_compile app/models/chat.py app/services/chat_todo_service.py app/routers/chat.py` 통과. `pytest -q tests/unit/test_chat_todo_service.py` 7개 통과. Dashboard `npx tsc --noEmit --pretty false` 통과. `npx eslint src/app/chat/page.tsx` 에러 0개, 기존 경고 21개.
- 배포: 서버 커밋 `5319e7f`와 대시보드 커밋 `7b55c87`을 `origin/main`에 푸시했다. 서버 blue-green 배포 후 nginx upstream은 API `8100` active / `8102` backup이며, `https://aads.newtalk.kr/api/v1/health`가 `status=ok`를 반환했다. 대시보드 blue-green 배포 후 nginx upstream은 dashboard `3100` active / `3101` backup이며, `https://aads.newtalk.kr/login`이 HTTP 200을 반환했다.
- 주의: 대시보드 `bash deploy.sh`는 빌드와 슬롯 전환 이후 `aads-dashboard` 컨테이너명 충돌 메시지로 종료코드 1을 반환해 스크립트의 후속 자동 QA 단계는 실행되지 않았다. 사후 검증 기준으로 `aads-dashboard`와 `aads-dashboard-green`은 모두 healthy이고 외부 `/login` 및 `/chat` 리다이렉트가 정상이다.

## 2026-05-15 18:27 KST - NewTalk AI 6-persona Nano Banana 2 seed generation

- 배경: CEO가 `newtalk-ai-fashion-persona-cards-p0.html`의 상세 페르소나 카드 6명을 `newtalk-ai-model-creation-management-p0.html#console` 기획 기준으로 Nano Banana 2 생성해 갤러리에서 확인 가능하게 요청했다.
- 조치: `ai_personas`에 윤서아, 한루아, 강민채, 정하린, 이도연, 박세린 6명을 상세 카드 기준으로 upsert하고 상태를 `seed_generating`으로 정리했다. `gemini-3.1-flash-image-preview`를 Nano Banana 2 경로로 사용해 각 1장씩 face seed 후보를 생성했다.
- DB 기록: `media_generation_jobs` id `64~69` 6건이 `succeeded`이며, `ai_generation_logs` 6건과 `ai_persona_references` 6건을 `generation_type=face_seed`, `ref_type=face_seed`, `metadata.subtype=candidate`로 연결했다.
- 갤러리: `reports/newtalk-ai-model-gallery-live.html`과 `app/static/gallery/` manifest를 갱신해 모델명, 6명 페르소나 필터, `6명 페르소나 시드` 트랙, 한국어 카드 요약, 프롬프트 토글을 반영했다. 공개 경로는 `https://aads.newtalk.kr/reports/gallery/?t=202605151827`이다.
- 검증: `docker exec aads-server python3 /app/scripts/export_gallery.py` 결과 `Exported 66 images, 69 total`. 공개 URL `curl -I` 200 OK 확인. Browser Bridge CDP 세션 `bb-f8549551378b`에서 갤러리 접근성 트리 기준 최신 카드 `#69~#64` 6건이 모두 Nano Banana 2/성공/페르소나 시드로 표시됨을 확인했다.
- 주의: 기획서의 완전한 1인 모델 생성 기준은 얼굴 후보 50장 생성 후 1장 선택, 다각도 24장, 전신 30장이다. 이번 작업은 6명 각각의 첫 face seed 후보 생성 단계이며, 50장 확장은 CEO 선택 후 진행해야 한다. 커밋/푸시/백엔드 배포는 수행하지 않았다.

## 2026-05-15 18:37 KST - NewTalk AI 6-persona Nano Banana 2 face seeds expanded to 5 each

- 배경: CEO가 얼굴 후보 50장이 아니라 6명 각각 5장씩만 생성하도록 추가 지시했다.
- 조치: 기존 Nano Banana 2 페르소나 시드 `id=64~69`와 2번째 후보 `id=70~75`를 보존하고, 같은 페르소나 카드 기준으로 candidate 3~5를 배치 생성했다. 추가 생성 18건은 모두 `gemini-3.1-flash-image-preview`로 `media_generation_jobs`에 저장됐다.
- DB 기록: 페르소나 프롬프트 기준 최종 카운트는 윤서아/한루아/강민채/정하린/이도연/박세린 각 5건이며, 전체 30건 모두 `succeeded`다. 최종 id 범위는 `64~75`, `76~93`이다.
- 갤러리: `bash scripts/gallery_sync.sh`로 `app/static/gallery/`, `/var/www/aads-public/reports/gallery/`, 대시보드 공개 경로를 동기화했다. 공개 URL은 `https://aads.newtalk.kr/reports/gallery/`이며 manifest 기준 `persona_items=30`, `persona_images=30`이다.
- 검증: 공개 갤러리 HTTP 200, manifest HTTP 200 확인. Browser Bridge 접근성 트리에서 최신 `#93~#64`가 `6명 페르소나 시드`, `Nano Banana 2`, `Google Gemini`, `성공`, 한글 카드 요약, 프롬프트 토글로 표시됨을 확인했다.
- 주의: 이미지 품질 선별, 동일 인물성 embedding 검증, `ai_persona_references`의 approved 대표컷 지정은 아직 미진행이다. 커밋/푸시/백엔드 배포는 수행하지 않았다.

## 2026-05-16 08:26 KST - Han Rua multi-angle approval recommendations

- 배경: CEO가 한루아 89번 시드 기반 멀티앵글 얼굴 결과에서 검토 전 승인추천 20장을 표시하도록 지시했다.
- 조치: `ai_persona_references`에서 한루아 `persona_id=3`의 멀티앵글 29건은 실제 승인값 `is_approved=false`를 유지하고, 추천 20건에만 `metadata.approval_recommended=true`, `approval_recommendation_rank`, `approval_recommendation_reason`을 기록했다.
- 추천 대상: media id `144,145,146,147,149,150,153,170,154,155,156,157,172,159,160,161,162,164,166,173`이며 ref id 기준 `61,62,63,64,66,67,69,86,70,71,72,73,88,75,76,77,78,80,82,89`다.
- 갤러리: `scripts/export_gallery.py`가 `ai_persona_references` 메타데이터를 manifest에 포함하도록 보정했고, `app/static/gallery/index.html`에 승인추천 배지, 추천 사유, ref/angle 표시, `승인추천만` 필터, 추천 건수 칩을 추가했다. `bash scripts/gallery_sync.sh`로 `/var/www/aads-public/reports/gallery/`에 동기화했다.
- 검증: DB 추천 카운트 20건, 공개 manifest 추천 카운트 20건/총 173건 확인. 공개 URL `https://aads.newtalk.kr/reports/gallery/` HTTP 200 확인. Browser Bridge에서 `승인추천만` 필터 적용 시 현재 필터 결과 20건과 `승인추천 #1~#20` 표시를 확인했다.
- 주의: 이번 조치는 CEO 검토용 추천 표시이며 실제 승인 처리(`is_approved=true`)와 embedding similarity 정량 검증은 아직 수행하지 않았다. 커밋/푸시/정식 배포는 수행하지 않았다.

## 2026-05-16 08:41 KST - Gallery approval API deployment recovery

- 배경: 갤러리에서 승인추천 이미지를 바로 승인/취소할 수 있도록 `/api/v1/image/gallery/approve` API와 정적 갤러리 승인 버튼을 반영하던 중, `/api/v1/image/gallery`가 SQL `AmbiguousColumnError`로 500을 반환했다.
- 조치: `app/api/image.py`의 lateral subquery `ORDER BY id`를 `ORDER BY ref.id DESC`로 명확히 고쳐 갤러리 GET 500을 해소했다. 승인 API는 `reference_ids` 직접 승인/취소와 `approve_recommended=true` 전체 추천 승인 경로를 제공한다. 갤러리 UI는 선택승인, 추천 전체승인, 선택승인취소 버튼과 승인완료 배지를 사용한다.
- 배포/복구: `aads-api` 재시작 중 blue 슬롯이 오래 `STOPPING`에 머물렀으나, green 슬롯이 공개 트래픽을 정상 처리했다. 이후 blue 컨테이너를 복구해 API `8102(aads-server-green)` active, API `8100(aads-server)` backup 모두 healthy 상태를 확인했다.
- 검증: `python3 -m py_compile app/api/image.py` 통과. `curl http://127.0.0.1:8100/api/v1/image/gallery?limit=1` 200, `curl http://127.0.0.1:8102/api/v1/image/gallery?limit=1` 200, `curl https://aads.newtalk.kr/api/v1/image/gallery?limit=1` 200 확인. 무효 승인 요청은 `400 {"detail":"승인할 reference_id가 없습니다"}`로 검증 실패를 정상 반환했다. DB 기준 `approval_recommended=20`, `is_approved=0`, `ai_persona_references=84`, `media_generation_jobs(kind=image)=173` 확인. `nginx -t` 성공.
- 주의: CEO 실제 승인은 아직 누르지 않았다. 커밋/푸시는 아직 수행하지 않았고, 작업트리에는 갤러리/모델 관련 이전 미커밋 변경이 함께 남아 있다.

## 2026-05-16 09:04 KST - Han Rua fullbody Reference Set generated

- 배경: CEO가 한루아 얼굴 Reference 추천 20장을 승인한 뒤 다음 단계 진행을 지시했다. 기획서 기준 다음 단계는 확정 얼굴 기반 전신 30장 생성 후 20장 이상 승인이다.
- 조치: 한루아 `persona_id=3`의 승인 얼굴 reference 20건을 DB에서 확인한 뒤, seed image `media_generation_jobs.id=89`를 입력 참조로 Nano Banana 2(`gemini-3.1-flash-image-preview`) 전신 후보 30장을 생성했다.
- DB 기록: 전신 후보 30장은 `media_generation_jobs.id=205~234`로 저장됐고, `ai_persona_references`에는 허용 타입 `fullbody_stand/walk/sit/lean/turn`으로 연결했다. 한루아 상태는 `fullbody`다.
- 갤러리: `app/static/gallery/index.html`에 `한루아 전신 Reference` 트랙 구분을 추가했고, 기본 화면은 성공 이미지만 보이도록 보정했다. `추천 전체승인` 버튼은 현재 화면의 미승인 추천만 승인하도록 조정했다. 전신 30장 중 20장에 `metadata.approval_recommended=true`, 추천 순위와 추천 사유를 기록했다. 정적 갤러리는 `/var/www/aads-public/reports/gallery/`와 대시보드 공개 경로에 동기화했다.
- 검증: `node --check /tmp/gallery-inline.js` 통과. `docker exec aads-server-green python3 /app/scripts/export_gallery.py` 결과 `Exported 165 images, 200 total`. 공개 갤러리 `https://aads.newtalk.kr/reports/gallery/` HTTP 200, 공개 API 최신 40건 기준 `fullbody_in_latest40=30`, `recommended_in_latest40=20` 확인.
- 주의: 전신 후보의 실제 승인(`is_approved=true`)은 아직 CEO 검토 전이다. 동일 인물성 embedding 정량 검증은 아직 미구현/미기록이며, 이번 추천은 접촉시트 육안 검토 기준이다. 커밋/푸시는 아직 수행하지 않았다.

## 2026-05-16 09:50 KST - Han Rua rear-view fullbody preset recommendations

- 배경: CEO가 전신 프리셋 검토용으로 뒷모습 전신컷도 몇 장 반영하고 추천 표시하도록 지시했다.
- 조치: 한루아 전신 프리셋 보강 세트 `han_rua_fullbody_swimfit_rear_preset` 4장을 갤러리 추천 대상으로 반영했다. 대상은 `media_generation_jobs.id=317~320`, `ai_persona_references.id=271~274`이며 `ref_type`은 `fullbody_turn` 3장, `fullbody_walk` 1장이다.
- 추천 표시: 기존 전신 추천 20장 뒤에 `approval_recommendation_rank=21~24`, `approval_recommended=true`, `approval_recommendation_reason=후면 전신 프리셋 보강 추천`을 기록했다. 실제 승인값은 CEO 검토 전이므로 `is_approved=false`로 유지했다.
- 갤러리: `han-rua-fullbody-rear-preset-contact-sheet.jpg` 접촉시트를 생성하고 `bash scripts/gallery_sync.sh`로 `/var/www/aads-public/reports/gallery/`에 동기화했다.
- 검증: DB 조회로 후면 4장 추천/미승인 상태를 확인했고, 공개 갤러리 `https://aads.newtalk.kr/reports/gallery/`와 `manifest.json`이 HTTP 200을 반환했다.
- 주의: 후면 컷은 전신 프리셋 보강용 추천 표시만 완료된 상태이며, CEO가 갤러리에서 승인해야 `is_approved=true`가 된다. 커밋/푸시는 아직 수행하지 않았다.

## 2026-05-16 10:10 KST - Han Rua style preset recovery and gallery track

- 배경: CEO가 전신 승인 후 다음 단계 진행을 지시했고, 스타일 프리셋 12장 생성 중 이미지는 반환됐지만 `ai_persona_references.ref_type='style_preset'`이 체크 제약에 막혀 `media_generation_jobs.id=321~332`가 실패 상태로 남았다.
- 조치: `migrations/097_ai_persona_style_preset_ref_type.sql`을 추가하고 DB에 적용해 `style_preset` reference 타입을 허용했다. 기존 `result_uri`가 존재하던 12개 job은 재생성 없이 `status='succeeded'`로 복구하고 `ai_persona_references.id=299~310`으로 연결했다.
- 추천 표시: 12장 모두 `metadata.reference_set='han_rua_style_preset'`, `approval_recommended=true`, `approval_recommendation_rank=1~12`, `approval_recommendation_reason=전신 승인본 기반 스타일 프리셋 후보`로 기록했다. 실제 승인값은 CEO 검토 전이므로 `is_approved=false`다.
- 갤러리: `app/static/gallery/index.html`에 `한루아 스타일 프리셋` 필터/트랙 라벨을 추가했고, `han-rua-style-preset-contact-sheet.jpg` 접촉시트를 생성했다. `bash scripts/gallery_sync.sh`로 `/var/www/aads-public/reports/gallery/`에 동기화했다.
- 검증: DB 조회 기준 `style_preset` 12건 모두 `succeeded`, reference 연결, 추천 12건 확인. 공개 갤러리 `https://aads.newtalk.kr/reports/gallery/` HTTP 200, 스타일 접촉시트 HTTP 200 `image/jpeg`, 최신 API `limit=12` 기준 style preset 12건 확인. 승인/삭제 API는 빈 요청에 정상 `400`을 반환했다.
- 주의: 동일 인물성 face embedding 정량 검증은 아직 미구현이며, 이번 단계는 승인 전신본 기반 스타일 프리셋 후보 생성/복구와 갤러리 검토 준비다. 커밋/푸시는 아직 수행하지 않았다.

## 2026-05-18 08:04 KST - AADS knowledge-to-wisdom evolution research report

- 배경: CEO가 AADS와 전체 운영 프로젝트에 필요한 자료/지식을 어떻게 수집, 분류, 저장, 관리하고 이를 지혜화해 진화와 발전에 연결할지 최신 자료 기반 심층 연구와 보고서 저장을 요청했다.
- 조치: NIST AI RMF Generative AI Profile, OWASP LLM Top 10 2025, OpenAI Retrieval, Google Vertex AI Grounding/Memory Bank, Anthropic Claude Code Memory, LangChain Long-term Memory, Microsoft GraphRAG, 2026년 agent memory 논문, ByteRover, LightRAG를 교차 조사했다. 내부 AADS 문서와 DB schema도 확인해 현행 `memory_facts`, `ai_observations`, `ai_meta_memory`, `research_archive` 구조에 맞춘 DIKW+E 지식 운영 모델을 작성했다.
- 산출물: `docs/reports/20260518_AADS_KNOWLEDGE_WISDOM_EVOLUTION_RESEARCH.md`를 추가했다.
- 검증: KST 시각 `2026-05-18 08:04:19 KST` 실측. DB 기준 `memory_facts=48347`, `ai_observations=1461`, `ai_meta_memory=4183` 확인. 보고서 파일 markdown 생성 완료.
- 주의: 이번 작업은 연구 보고서 작성/저장 단계다. DB migration, `research_archive` row insert, 대시보드 UI, 자동 ingestion/eval 구현, 커밋/푸시/배포는 수행하지 않았다.

## 2026-05-18 14:40 KST - Chat manual resume retry_count SQL fix

- 배경: 채팅 복구/재연결 후 버블이 2개로 보이는 문제를 조사하던 중, 수동 `POST /api/v1/chat/sessions/{session_id}/resume` 경로의 retry_count SELECT/UPDATE SQL이 `WHERE id = `에서 끊겨 있어 실제 호출 시 DB 문법 오류가 날 수 있음을 확인했다.
- 조치: `app/routers/chat.py`의 수동 resume retry_count SELECT/UPDATE를 `$1` 바인딩 쿼리로 수정하고, UPDATE 시 `updated_at=NOW()`를 함께 기록하도록 보강했다.
- 검증: `python3 -m py_compile app/routers/chat.py` 통과. 실행 중인 `aads-server`, `aads-server-green` 컨테이너 내부 파일에도 `$1` 수정이 반영된 상태를 확인했다.
- 주의: 이 항목은 수동 resume 엔드포인트 안정화이며, 응답 버블 1개 보장 패치는 대시보드 `src/app/chat/page.tsx`에 별도 반영했다.

## 2026-05-18 15:08 KST - Chat interrupted execution fallback guard

- 배경: 세션 `2648cf77-4256-45e8-9cde-0e563ffefe5c`에서 최신 질문 이후 assistant 메시지가 0건으로 남아 응답 버블이 사라지는 현상을 확인했다. 해당 실행 `53241773-856d-48de-bbf7-dfa4085c9643`은 `resume_claimed_by` 후 `interrupted`로 종료됐지만 assistant fallback이 없었다.
- 조치: `app/services/chat_service.py`의 `_mark_execution_interrupted()`가 superseded가 아닌 terminal interruption에서 assistant 메시지 0건을 만들지 않도록 fallback assistant를 1회 insert한다. `app/main.py`의 resume scanner done callback도 resume task cancel/error 시 execution 상태와 fallback assistant를 DB에 동기화한다.
- 데이터 보정: 대상 실행 `53241773-856d-48de-bbf7-dfa4085c9643`에 fallback assistant `2dfd93b3-5929-4c33-91e2-084c8c90cc8d`를 연결해 새로고침 후 빈 응답으로 남지 않게 했다.
- 검증: `python3 -m py_compile app/main.py app/services/chat_service.py` 통과. `bash deploy.sh bluegreen`으로 API active를 `8102 → 8100` 전환했고 health/DB schema/chat table/LLM 검증이 통과했다. active 컨테이너 내부 코드에서 fallback 문자열 반영을 확인했다.
- 주의: 이 조치는 “응답 0건으로 사라짐” 방지용 P0 가드다. 프론트의 local placeholder/DB placeholder 경합 자체는 대시보드 `src/app/chat/page.tsx`의 별도 경로로 계속 관리해야 한다.

## 2026-05-18 15:21 KST - Chat resume dependency conflict guard

- 배경: 이전 개선안이 이미 반영됐는데도 중단/복구 시 응답 버블이 사라지거나 2개처럼 보이는 재발 원인을 재검수했다.
- 원인: `interrupted_partial`는 과거 partial 숨김용 intent인데, 프론트가 현재 진행 중인 placeholder도 30초 타임아웃 시 같은 intent로 바꿔 숨김 필터와 충돌했다. 또한 resume scanner가 메모리 `_streaming_state`가 남아 있으면 stale 여부와 무관하게 DB running 회수를 건너뛰어 오래된 실행이 계속 running으로 남을 수 있었다.
- 조치: `app/main.py`에서 `_streaming_state` skip 조건을 stale-aware로 바꿔 최근 갱신 상태만 보호하고, 오래된 메모리 상태는 회수 가능하게 했다.
- 검증: `python3 -m py_compile app/main.py` 통과. 대시보드 대응 패치는 `/root/aads/aads-dashboard/src/app/chat/page.tsx`에서 현재 partial을 숨김 intent가 아닌 visible interrupted bubble로 보존하도록 반영했다.
