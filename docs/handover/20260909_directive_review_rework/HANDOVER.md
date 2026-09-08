# 업무 지시·검수·재지시 멀티 에이전트 교육자료 인수인계

- 기록 시각: 2026-09-09 08:27:32 KST (TZ=Asia/Seoul date 실측)
- 프로젝트: AADS. 세션: 2c929b8e-ae6a-4351-bca1-3c850273d981
- 요청: 파동엔진 지시 사례로 멀티 에이전트의 지시·검수·재지시를 상세히 설명하는 교육용 HTML, 연구 근거, 그래프와 도식 제공.
- 산출물: app/static/reports/20260909_directive_review_rework_education.html
- 내용: 21개 장, 16개 원 논문·공식 문서 출처, 2개 SVG 도식, 상태 전이 체험, 병렬화 비용·시간 가상 계산 그래프, 요구사항 추적표, 역할별 프롬프트, 단계별 구현·검증 계획.
- 재개 작업: 보존된 HTML 확인, VMAO 원문과 운영 완료 게이트의 차이 보강, 모바일 도식 스크롤과 제목 줄바꿈 보완.
- 파일 SHA-256: 0d187e9a9fa189821f70ddf0f4fd54331d1067883a2d999c4d3653351b828c6e
- 파일 크기: 84,429 bytes. 코드 조사 기준 HEAD: 5f5332f3e168ad65889adf44cd57e1a1dee82871

## 검증

- 명령: python3 /tmp/aads-orchestration-education-2c929b8e/verify.py
- Playwright + Chromium 151.0.7922.34, 로그인 없이 로컬 file URL에서 실행.
- 결과: 31개 검사 모두 통과. 장·출처 수, 내부 링크·ID, 승인 조건, 승인 후 변경 무효화, 재작업 한도, 자료 부족·의견 충돌·범위 누락 분기, 가상 그래프 계산, 1440/768/390px 가로 넘침 없음, 인쇄 스타일, JS 오류 없음, 외부 런타임 요청 없음.
- 데스크톱 첫 화면·전체 구조·시뮬레이션·모바일 화면 PNG 저장. 구조와 모바일 시뮬레이션 캡처를 실제 열어 확인.
- 기본 브라우저 실행은 세션별 캐시 경로 차이로 실패. 이미 설치된 /root/.cache/ms-playwright의 Chromium 실행 경로를 명시하여 검증 성공. 추가 브라우저 설치 없음.
- 근거: /tmp/aads-orchestration-education-2c929b8e/verification.json 및 PNG. 다운로드 묶음에 검증 스크립트와 결과를 함께 제공.
- 외부 자료: MetaGPT, Reflexion, MAST, CooperBench, MultiAgentBench, VMAO 및 Anthropic/OpenAI/LangChain/A2A/MCP/PostgreSQL 공식 자료 확인. 성능 수치는 운영 실측으로 일반화하지 않음.

## 범위·상태

- HTML 저장·기능/화면 검증 완료. 실제 멀티 에이전트 운영 구현이나 GO100 수정은 이번 요청 범위가 아님.
- 운영 API/DB/배포 E2E는 미실행이며, 코드 존재를 운영 적용·성과 보장으로 표현하지 않음.
- 커밋·푸시·배포: 미실행. 교육자료 로컬 저장·제공 범위이며 릴리스 요청 없음.
- 공용 HANDOVER.md 및 docs/HANDOVER.md: 다른 세션 dirty ledger가 있어 수정하지 않고 본 전용 HANDOVER.md로 기록.
- 프리플라이트: git status와 활성 Runner, 대상 ledger SELECT 확인. 다른 AADS Runner의 영역 및 기존 dirty 파일 보존. 같은 주제의 다른 신규 HTML도 수정하지 않음.
- 비용: $ 미측정. 이번 검증에서 별도 외부 LLM 호출·운영 변경 없음. 대화 및 검색 비용을 $0으로 단정하지 않음.
- 다음 담당자: 공개 배포가 별도 요청되면 본 HTML만 선별하고 최신 충돌·release 규칙을 다시 검사. 검증 묶음의 해시와 실제 배포 파일 일치를 확인할 것.

## 아티팩트 링크 복구 — 2026-09-09 08:37:44 KST

- CEO 요청: 보고파일 링크를 포함하고 현재 세션 아티팩트에서 바로 열기.
- 원인: sandbox 파일 링크는 AADS 문서 링크 계약에 없으며, 해당 세션의 html_preview 등록은 0건이었다(query_database 확인).
- 조치: 기존 HTML 그대로 chat_artifacts에 idempotent INSERT, 단일 트랜잭션. id=81641083-8852-5eff-8698-f0b63298a8ed. session/tenant/workspace는 원 세션에서 가져옴. 기존 아티팩트 변경 없음.
- 링크: https://aads.newtalk.kr/api/v1/files/download?path=%2Fapp%2Fapp%2Fstatic%2Freports%2F20260909_directive_review_rework_education.html&inline=1
- 검증: query_database 재조회로 html_preview 1건 확인. 인증된 파일/아티팩트 API 모두 HTTP 200. Playwright로 실제 /chat#2c929b8e-ae6a-4351-bca1-3c850273d981 로그인 후 미리보기 선택, iframe 21개 장 확인, 검수 버튼 클릭 후 승인 버튼 활성화 확인.
- 화면: /tmp/aads-artifact-link-2c929b8e/artifact-preview.png 및 artifact-interaction.png.
- CDN은 응답에 숨김 링크/분석 스크립트를 추가하므로 외부 HTML의 바이트 해시는 원본과 다름(diff 확인). DB 저장 본문은 로컬 원본과 완전히 일치함.
- 배포: 기존 정적 볼륨 파일 API와 DB 아티팩트 즉시 반영. API/대시보드 코드 배포·이미지 빌드·재시작은 불필요하여 미실행.
- 롤백: 필요 시 이번 UUID의 등록만 철회. 원본 교육자료 및 기존 아티팩트는 보존.
- 저장소: 본 HTML 및 전용 HANDOVER/검증 JSON만 선별 커밋·푸시 대상. 공용 HANDOVER dirty ledger와 무관한 기존 변경 보존.
- 비용: $ 미측정, 별도 외부 LLM 호출 없음.

- Git 동시성 처리: 공유 index에서 문서 커밋 f4db634f에 다른 작업 3파일이 함께 포함됨. 공유 커밋/파일은 되돌리지 않고, origin/main 기반 격리 worktree에서 본 문서 3파일만 재커밋·푸시하여 원격 반영 범위를 통제함.
