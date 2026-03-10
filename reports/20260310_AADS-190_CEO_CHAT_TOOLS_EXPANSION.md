# AADS-190: CEO Chat 도구 확장 — 프로젝트 DB 쿼리 + 내보내기 + 스케줄러

**날짜**: 2026-03-10
**상태**: ✅ 완료 (배포됨)

## 개요
CEO 채팅에서 모든 프로젝트(KIS, GO100, SF, NTV2)의 DB를 직접 조회하고,
결과를 Excel/CSV/PDF로 내보내며, 동적 스케줄 작업을 관리하는 기능 추가.

## 신규 도구 (6개)

| 도구 | 등급 | 설명 |
|------|------|------|
| `query_project_database` | 🟡 Yellow | 프로젝트 DB SELECT 쿼리 실행 |
| `list_project_databases` | 🟢 Green | 접속 가능한 프로젝트 DB 목록 조회 |
| `export_data` | 🟡 Yellow | 데이터를 Excel/CSV/PDF로 내보내기 |
| `schedule_task` | 🟡 Yellow | 동적 예약 작업 등록 (cron/interval/once) |
| `unschedule_task` | 🟡 Yellow | 예약 작업 삭제 |
| `list_scheduled_tasks` | 🟢 Green | 예약 작업 목록 조회 |

## 프로젝트별 DB 연결 구성

| 프로젝트 | DB 타입 | 호스트 | 접속 방식 |
|----------|---------|--------|-----------|
| KIS | PostgreSQL | 211.188.51.113 (host.docker.internal) | asyncpg 직접 |
| GO100 | PostgreSQL | KIS와 동일 DB (별칭) | asyncpg 직접 |
| SF | MariaDB | 116.120.58.155:3306 | SSH 터널 (subprocess) |
| NTV2 | MySQL 8.0 | 116.120.58.155:3307 (Docker) | SSH 터널 (subprocess) |

## 신규/수정 파일

### 신규 생성
- `app/api/ceo_chat_tools_db.py` — 프로젝트 DB 쿼리 엔진 (asyncpg + pymysql + SSH 터널)
- `app/api/ceo_chat_tools_export.py` — Excel/CSV/PDF 내보내기 (openpyxl)
- `app/api/ceo_chat_tools_scheduler.py` — APScheduler 동적 작업 관리

### 수정
- `app/services/tool_registry.py` — 6개 도구 스키마 + INTENT 매핑 등록
- `app/services/tool_executor.py` — 6개 도구 디스패치 핸들러 추가
- `app/services/agent_sdk_service.py` — 등급(Yellow/Green) + SDK wrapper 추가
- `app/services/agent_hooks.py` — query_project_database SQL 검증 훅
- `app/api/ceo_chat_tools.py` — 원격 명령 화이트리스트 확장 (docker/nginx/supervisor 등 22개 추가)
- `app/main.py` — APScheduler 인스턴스를 scheduler 도구에 공유 (`set_scheduler()`)
- `docker-compose.yml` — DB 환경변수 패스스루 + 내보내기 볼륨 마운트
- `pyproject.toml` — pymysql, openpyxl 의존성 추가

### 인프라
- `/etc/nginx/conf.d/aads.conf` — `/exports/` location 추가 (HTTPS)
- `/var/www/aads_exports/` — 내보내기 파일 저장소 (SELinux httpd_sys_content_t 적용)
- `.env` — KIS/SF/NTV2 DB 접속 정보 추가

## 보안
- SQL: SELECT/WITH/EXPLAIN만 허용, 세미콜론 주입 차단, 민감 컬럼 마스킹
- DB 비밀번호: `.env`에만 저장, docker-compose는 `${VAR:-}` 참조
- 원격 명령: 화이트리스트 기반 (rm, kill, shutdown 등 차단)
- 내보내기 파일: Content-Disposition attachment 강제, autoindex off

## Docker 이미지 리빌드
- `docker compose build --no-cache aads-server` 완료
- pymysql=1.4.6, openpyxl=3.1.5 영구 설치 확인
- 컨테이너 healthy 상태 운영 중
