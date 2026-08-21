# PROJECT-ALIAS-LAYER: 프로젝트 별칭 레이어

_작성: 2026-08-21 (실측)_

> 출처 태그: `[SRC:CODE]`는 저장소 코드 확인, `[SRC:TEST]`는 회귀 테스트 파일 확인이다.

## 1. 목적

프로젝트 별칭 레이어는 CEO 채팅, 러너, MCP 도구, 워크스페이스, 메모리 테이블에서 섞여 들어오는 프로젝트 표시명을 정규 프로젝트 키로 통일한다. 단일 소스는 `app/core/project_config.py`다. `[SRC:CODE]`

핵심 원칙은 다음과 같다.

| 원칙 | 내용 |
|---|---|
| 정규 키 | 실행 가능한 프로젝트는 `AADS`, `KIS`, `GO100`, `SF`, `NTV2` 5개다. |
| 표시명 | 화면에는 `display_name`을 쓸 수 있지만 저장·도구 호출은 정규 키를 우선한다. |
| 별칭 | 한글명, 소문자, 기존 서비스명은 `aliases`에서 정규 키로 해석한다. |
| display-only | `FOOD`, `NAS`, `CEO`, `WORK` 등은 DB/화면 라벨로만 유효하고 SSH 실행 대상이 아니다. |

## 2. PROJECT_MAP 구조

`PROJECT_MAP`은 서버, 작업 경로, 언어, 표시명, 별칭을 한 곳에서 관리한다. `[SRC:CODE:app/core/project_config.py]`

| key | server_name | server | port | workdir | lang | display_name | aliases |
|---|---|---|---|---|---|---|---|
| `AADS` | `contabo116` | `host.docker.internal` | - | `/root/aads/aads-server` | python | AADS 자율개발시스템 | `AADS`, `aads` |
| `KIS` | `contabo14` | `5.104.86.14` | - | `/root/kis-autotrade-v4` | python | KIS 자동매매 | `KIS`, `kis`, `자동매매`, `kis-autotrade` |
| `GO100` | `contabo14` | `5.104.86.14` | - | `/root/kis-autotrade-v4` | python | 백억이 투자분석 | `GO100`, `go100`, `백억이`, `백억이투자분석` |
| `SF` | `cafe24_114` | `114.207.244.86` | `7916` | `/` | python | ShortFlow 숏폼자동화 | `SF`, `sf`, `ShortFlow`, `shortflow`, `숏폼` |
| `NTV2` | `cafe24_114` | `114.207.244.86` | `7916` | `/srv/newtalk-v2` | php | NewTalk V2 | `NTV2`, `ntv2`, `NewTalk`, `newtalk`, `NEWTALK`, `newtalk-v2` |

`DISPLAY_ONLY_PROJECTS`는 `FOOD`, `NAS`, `CEO`, `WORK`, `LAW`, `DESIGN`, `KAKAOBOT`, `COM`, `TEST`, `QA`, `PLAY`, `DKSEON`, `KNW001`, `VIBE`, `HARNESS`다. 이 값들은 라벨 정규화에는 성공하지만 `is_executable_project()`에서는 실행 대상이 아니다. `[SRC:CODE]`

## 3. 함수 계약

| 함수 | 입력 | 출력 | 실패/예외 동작 |
|---|---|---|---|
| `resolve_project(value)` | 정규 키, 별칭, 표시명, `[KEY] 표시명` | 정규 키 또는 display-only 키 | 공백/미등록 값은 `None` |
| `normalize_project_label(value)` | DB 저장용 임의 라벨 | 가능한 경우 정규 키 | 미등록 일반 문자열은 strip 원문, `[KEY] ...`는 `KEY.upper()` |
| `get_display_name(project)` | 정규 키 또는 별칭 | 표시명 | 미등록 값은 입력값 그대로 |
| `is_executable_project(value)` | 임의 라벨 | `bool` | display-only는 `False` |
| `get_workdir(project)` | 정규 키 | workdir | 미등록 값은 빈 문자열 |
| `get_server(project)` | 정규 키 | 서버 주소 | 미등록 값은 빈 문자열 |

대표 예시는 다음과 같다. `[SRC:TEST:tests/unit/test_project_config_alias.py]`

| 입력 | `resolve_project()` | `normalize_project_label()` |
|---|---|---|
| `백억이` | `GO100` | `GO100` |
| `[AADS] 프로젝트 매니저` | `AADS` | `AADS` |
| `[FOOD] 열정국밥` | `FOOD` | `FOOD` |
| `MYPROJ` | `None` | `MYPROJ` |

## 4. 워크스페이스 연계

`chat_workspaces.project_key`는 워크스페이스 표시명과 실행 프로젝트를 분리하기 위한 저장 컬럼이다. `[SRC:CODE:app/services/chat_service.py]`

| 경로 | 동작 |
|---|---|
| `list_workspaces()` | `SELECT *` 후 `_row_to_dict()`로 `project_key`를 그대로 API 응답에 포함한다. |
| `create_workspace()` | 명시 `project_key`가 없으면 `normalize_project_label(name)`을 저장한다. |
| `update_workspace()` | 이름 변경 시 명시값 우선, 없으면 새 이름에서 `project_key`를 재파생한다. |
| `_workspace_project_key()` | role/profile 조회용으로 별칭과 legacy 토큰을 정규 키로 변환한다. |

## 5. 도구 enum 단일 소스

`app/services/tool_registry.py`는 프로젝트 enum을 하드코딩하지 않고 `project_config.py`에서 가져온다. `[SRC:CODE]`

| 상수 | 값 | 사용처 |
|---|---|---|
| `ALL_PROJECTS` | `["AADS", "KIS", "GO100", "SF", "NTV2"]` | 실행 가능한 프로젝트 목록 |
| `SSH_PROJECT_ENUM` | `ALL_PROJECTS` | SSH/DB/러너 계열 도구 |
| `SEARCH_PROJECT_ENUM` | `ALL_PROJECTS + ["NAS"]` | 검색·이력 조회 계열 도구 |

회귀 테스트는 `tests/unit/test_tool_registry_project_enums.py`에서 도구별 enum이 단일 소스와 일치하는지 확인한다. `[SRC:TEST]`

## 6. 신규 프로젝트 추가 절차

1. `app/core/project_config.py`의 `PROJECT_MAP`에 실행 프로젝트를 추가한다.
2. display-only 라벨만 필요하면 `DISPLAY_ONLY_PROJECTS`에 추가하고 SSH 경로는 부여하지 않는다.
3. 별칭은 `aliases`에 추가하고, 화면명은 `display_name`에 넣는다.
4. `tests/unit/test_project_config_alias.py`에 정규화·표시명·display-only 테스트를 추가한다.
5. 도구 노출이 필요하면 `tests/unit/test_tool_registry_project_enums.py` 기대값을 갱신한다.
6. 워크스페이스 기존 데이터가 오염돼 있으면 SELECT로 건수 확인 후 idempotent UPDATE로 백필한다.

## 7. 검증 명령

```bash
pytest tests/unit/test_project_config_alias.py
pytest tests/unit/test_workspace_project_key.py
pytest tests/unit/test_tool_registry_project_enums.py
python -m py_compile app/core/project_config.py app/services/tool_registry.py app/services/chat_service.py
```

## 8. 운영 판정 기준

프로젝트 이름 변경은 표시명 변경과 내부 키 변경으로 나눠 판단한다.

| 변경 유형 | 위험도 | 기준 |
|---|---|---|
| 표시명 변경 | 낮음 | `display_name`과 `aliases` 추가로 처리 가능 |
| display-only 라벨 추가 | 낮음 | DB/화면 필터용이면 `DISPLAY_ONLY_PROJECTS`만 추가 |
| 실행 프로젝트 키 변경 | 높음 | DB, 러너, 도구 enum, SSH 경로, 기존 작업 이력까지 마이그레이션 필요 |

