# 지금 상태

_2026-09-14 08:12 KST 자동 생성 — **손으로 고치지 마라.** 고쳐도 다음 실행에 덮어쓴다._

`scripts/generate_now.py` 가 실제로 도는 것에서 뽑는다. 이 문서에 적힌
숫자는 전부 생성 시점의 실측값이다.

---

## 지금 돌고 있는 것

컨테이너 **12개**가 떠 있다. 컨테이너는 프로그램 하나를 담아 두는 상자다.

| 이름 | 무엇인가 | 상태 |
|---|---|---|
| `aads-dashboard` | 화면(웹페이지) | 정상 (Up 34 minutes (healthy)) |
| `aads-dashboard-green` | 화면 예비 슬롯 | 정상 (Up 34 minutes (healthy)) |
| `aads-litellm` | 여러 AI 공급자를 한 규격으로 묶는 중계 | 정상 (Up 4 days (healthy)) |
| `aads-nginx` | 출입구 — 외부 요청을 안쪽으로 넘긴다 | 정상 (Up 4 months) |
| `aads-postgres` | 데이터베이스 — 대화·기억·설정이 전부 여기 있다 | 정상 (Up 7 weeks (healthy)) |
| `aads-redis` | 임시 저장소 — 스트리밍 중간값 | 정상 (Up 4 months (healthy)) |
| `aads-searxng` | 검색 엔진 | 정상 (Up 4 months (healthy)) |
| `aads-server` | 본체 API — 채팅과 모든 기능이 여기서 돈다 | 정상 (Up 7 minutes (healthy)) |
| `aads-server-green` | 본체 API 예비 슬롯 — 배포할 때 번갈아 쓴다 | 정상 (Up 6 minutes (healthy)) |
| `aads-socket-proxy` | 도커 제어 권한을 제한해서 넘기는 중계 | 정상 (Up 4 months) |
| `vigilant_shannon` | — | 정상 (Up 7 hours) |
| `yeoljeong-finance` | — | 정상 (Up 4 days (healthy)) |

## 바깥에서 들어오는 길

nginx 가 외부 요청을 받아 안쪽으로 넘긴다. 공개 경로 **37개**.
지금 요청을 받는 API 슬롯은 **blue** (포트 8100) 이다.

## API 규모

바깥에서 부를 수 있는 창구가 **98개**다. 창구 하나가 기능 하나라고 보면 된다.

| 파일 | 창구 수 |
|---|---:|
| `app/routers/chat.py` | 77 |
| `app/routers/goals.py` | 13 |
| `app/routers/agent_vault.py` | 8 |

## 코드 규모

문서에 손으로 적어 두면 반드시 틀어지는 숫자다. 그래서 여기서 뽑는다.

| 파일 | 무엇인가 | 줄 수 |
|---|---|---:|
| `app/services/chat_service.py` | 채팅 핵심 로직 | 14,909 |
| `app/routers/chat.py` | 채팅 창구 | 4,735 |
| `app/services/context_builder.py` | AI 에게 보낼 맥락 조립 | 617 |
| `aads-dashboard/src/app/chat/page.tsx` | 채팅 화면 (한 파일) | 12,880 |

## 쌓여 있는 데이터

가장 큰 표 8개다. 표 하나가 자료 한 종류다.

| 표 | 크기 | 행 수 |
|---|---:|---:|
| `media_generation_jobs` | 7904 MB | 7,957 |
| `chat_messages` | 1184 MB | 58,969 |
| `memory_facts` | 665 MB | 75,699 |
| `chat_outbox` | 417 MB | 17,591 |
| `claude_max_usage_snapshot` | 238 MB | 112,350 |
| `chat_messages_archive` | 174 MB | 19,002 |
| `chat_execution_checkpoints` | 130 MB | 14,302 |
| `go100_user_memory` | 83 MB | 94,083 |

## 문서 검색

문서 **823건**을 **7,847조각**으로 나눠 두었다. 서버 1대에서 모았다.
그중 **5.2%** 가 검색 가능한 상태다(조각을 숫자로 바꿔 두면 뜻이 비슷한 것을 찾을 수 있다).

채팅에 물으면 이 문서들을 근거로 답한다.

## 최근 배포

배포는 고친 코드를 실제 서비스에 올리는 일이다.

| 번호 | 버전 | 결과 | 단계 | 시각 |
|---:|---|---|---|---|
| 439 | `07321522f348` | 성공 | completed | 09-14 14:50 |
| 438 | `18a6de526d8f` | 성공 | completed | 09-14 14:28 |
| 437 | `a0063f3fd80c` | 성공 | completed | 09-14 14:27 |
| 436 | `a97e0c376b37` | 성공 | completed | 09-14 14:04 |
| 435 | `58fb739b34a8` | 성공 | completed | 09-14 13:34 |

## 오늘 바뀐 것

**aads-server** — 변경 33건

- `579cf12c` fix(embed): 임베딩이 전부 가짜였다 — 주소 불일치와 조용한 더미 폴백
- `07321522` fix(chat): 첫응답 타임아웃 기본값 90→180초 — 컨텍스트 축소가 응답을 죽였다
- `a0063f3f` perf(chat): 폴링 응답에서 화면이 안 읽는 데이터를 뺀다
- `a97e0c37` fix(context): Layer 3 히스토리에 메시지당 상한 추가
- `80e26b73` feat(relay): 프롬프트 캐시 지표를 기록한다 — 측정부터
- `08fc81d2` fix(적재): 텔레그램·슬랙·디스코드 토큰을 마스킹한다
- `58fb739b` fix(chat): 첫 응답 타임아웃을 컨텍스트 크기에 맞춘다
- `e001c96f` fix(disk_guard): 가장 필요한 날에 한 번도 안 돌던 문제
- `cbb4315d` docs(education): 바이브코딩 개발 용어 대사전 교육자료 추가
- `23aab825` fix(runner): 릴레이 슬롯 자격증명 + 검증된 CLI 로 러너 인증 경로를 복구한다
- `591c9e78` docs(R-ERRBOOK): "사람이 지켜야 하는 것" 을 실제 주체로 바로잡는다
- `f3d294db` fix(chat): 추가 지시 완료 상태를 DB에 남기고, 테스트 스텁 누수를 막는다
- … 외 21건

**aads-dashboard** — 변경 13건

- `18a6de5` perf(chat): 유휴 세션의 streaming-status 폴링을 19.5초로
- `5d31c0f` fix(chat): 복구된 추가 지시의 상태 배지가 렌더되지 않던 문제
- `8094084` fix(chat): 추가 지시 버블의 절반이 상태 배지 없이 렌더되던 문제
- `a679652` feat(deploy): 중복 빌드와 병행 빌드를 코드로 막는다
- `ca252ef` perf(chat): 겹치지 않는 재호출을 최소 간격으로 막는다
- `e545c1f` fix(chat): 스크롤이 잡히는 두 원인 — 복원 경로의 O(n^2) 탐색과 제스처 창 조기 해제
- `4024b12` perf(auth): getMe 진행 중 요청 합치기 — 고친 곳이 틀렸던 것을 바로잡는다
- `bcff94c` perf(chat): 남은 중복 호출 두 건 — 효과 재실행과 auth/me
- `69303c4` perf(chat): 진행 중인 같은 GET 을 합친다 — 중복 요청 제거
- `8643a17` fix(deploy): 대시보드 배포 원장에 실제 phase 를 남긴다
- `0d4e9b7` fix: /chat 의 실제 llm-models 호출자를 축약 응답으로 돌린다
- `21026cb` test(chat): repin release baseline after hydration fix
- … 외 1건
