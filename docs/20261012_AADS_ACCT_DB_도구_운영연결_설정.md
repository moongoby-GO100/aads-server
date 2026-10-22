# ACCT DB 도구 운영 연결 설정 (query_acct_database)

document_key: `aads-acct-db-tool-cafe24-connection`

## 원인 (코드 확인)

`app/api/ceo_chat_tools_db.py::query_acct_database` 는 `ACCT_DATABASE_URL` 이 없으면
`ACCT_DB_APP_USER/PASSWORD` + SSH 터널(`_SSH_TUNNEL_PROJECTS["ACCT"]`)로 붙는다.
터널 기본 목적지는 `get_server_host("jinah244")` = 5.104.85.244 (사용 중단된 서버)다.
운영 ACCT 는 cafe24_114 의 `acct-pg`(DB `obys`, user `acct_admin`)로 옮겨졌으므로,
변수를 바꾸지 않으면 옛 경로로 가서 "ACCT DB 연결 실패" 가 난다.

## 접속 경로 우선순위

1. `ACCT_APP_DATABASE_URL` — 신규. 운영 ACCT 직결 DSN. **이것만 설정하면 된다.**
2. `ACCT_DATABASE_URL` — 하위 호환 DSN.
3. `ACCT_DB_APP_USER` + `ACCT_DB_APP_PASSWORD` + SSH 터널 — 목적지는 기존 `ACCT_SSH_HOST` / `ACCT_SSH_PORT` /
   `ACCT_SSH_USER` / `ACCT_SSH_KEY_PATH` / `ACCT_DB_HOST` / `ACCT_DB_PORT` 로 바꿀 수 있다(기존 변수).

## 운영 반영 (지시 세션이 수행 — 이 변경은 .env/docker-compose 를 건드리지 않았다)

`.env` 에 추가(형식):

```
ACCT_APP_DATABASE_URL=postgresql://<user>:<password>@<host>:<port>/obys
```

- 값은 `.env` 에만 둔다(R-KEY). 비밀번호에 특수문자가 있으면 URL 인코딩.
- 컨테이너는 `env_file: .env` 로 읽으므로 docker-compose 수정은 필요 없다. 반영은 단일 서비스 무중단 경로로 한다.
- 도달 경로: aads-server(contabo116)에서 `<host>:<port>` 가 닿아야 한다. cafe24_114 의 acct-pg 가 외부 포트를
  열지 않으면 `obys-jinah-tunnel.service` 처럼 호스트 측 터널의 로컬 포트를 `<host>:<port>` 로 지정한다.
  컨테이너에서 호스트 터널로 붙으려면 localhost 가 아니라 호스트 게이트웨이 주소가 필요하다(실측 필요).

## 전표 조회 DB 선택 (2026-10-22, 오비서 H3 P-15)

cafe24_114 `acct-pg` 에는 DB 가 셋(`obys`, `obys_auth`, `acct`)이고 `journal_entry`/`journal_line` 은 `acct` 에만 있다.
위 DSN 의 DB 이름이 `/obys` 이면 전표 쿼리가 "relation does not exist" 로 실패한다. 그래서 `acct_purchase` 의 전표 조회는
DSN 의 DB 이름을 덮어써 `ACCT_LEDGER_DB_NAME`(기본 `acct`)에 붙는다. 터널 모드는 `ACCT_DB_NAME` 대신 이 값을 쓴다.

- 같은 계정이 `acct` DB 에 CONNECT 권한이 있어야 한다. 2026-10-22 읽기 전용 점검에서
  `permission denied for database "acct"` 가 났다 — 권한 부여(운영 DB 쓰기)는 이 작업에서 하지 않았다. 운영 담당이 처리한다.
- 변수 이름 외에 값은 문서에 적지 않는다(R-KEY).

## 진단

연결 실패 시 서버 로그에 `query_acct_database: 연결 실패 | mode=<경로> target=<host:port>` 가 남는다.
자격증명은 포함되지 않는다. mode 는 `ACCT_APP_DATABASE_URL` / `ACCT_DATABASE_URL` / `ssh_tunnel` 중 하나다.

## 그대로 유지한 것

읽기 전용(`default_transaction_read_only`), 민감값 마스킹, `acct.tenant_id` GUC 로 RLS 스코프 설정은 변경 없음.
`query_project_database` 의 ACCT(acct_ro) 경로는 이번 범위가 아니며 여전히 `_SSH_TUNNEL_PROJECTS["ACCT"]` 를 쓴다.
