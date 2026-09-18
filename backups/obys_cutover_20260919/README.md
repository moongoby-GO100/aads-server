# obys 컷오버 백업 (2026-09-19)

O10 오비서 DB 분리 완료 후, `aads` DB 에서 제거한 `yeoljeong_*` 28테이블의 제거 직전 스냅샷이다.
CEO 승인카드 `eb2f0c27`(2026-09-19 06:38:32 KST) 로 제거했고, 이 파일들이 유일한 복원 경로다.

| 파일 | 크기 | 내용 |
|---|---:|---|
| `yeoljeong_data_backup_20260919.sql` | 25,075,017 B | `COPY public.yeoljeong*` 블록 28개 (31,248행) |
| `yeoljeong_schema_backup_20260919.sql` | 46,135 B | `CREATE TABLE public.yeoljeong*` 28개 |

원본은 `aads-postgres` 컨테이너 `/tmp` 에서 만들어졌다 — 컨테이너를 다시 만들면 사라지는
자리라 2026-09-19 07:0x KST 에 이 경로로 복사했다. 컨테이너 사본은 그대로 뒀다.

## 보관 기준

- **보관 기간: 2026-12-31 까지** (컷오버 후 3개월). 그 전에는 정리 대상에서 제외한다.
- 정리 스크립트가 `backups/` 를 훑는다면 이 디렉터리를 예외로 둔다.
- 삭제는 CEO 승인 사항이다. 삭제 전 `obys` DB 의 28테이블·31,248행이 정상인지 먼저 확인한다.

## 복원 방법

```bash
# 스키마 먼저, 데이터 다음. 대상 DB 를 반드시 확인하고 실행한다.
docker exec -i aads-postgres psql -U aads -d <대상DB> < yeoljeong_schema_backup_20260919.sql
docker exec -i aads-postgres psql -U aads -d <대상DB> < yeoljeong_data_backup_20260919.sql
```

현재 운영 정본은 `obys` DB 다. `aads` 로 되돌리면 이중 진실 소스가 다시 생기므로,
복원은 장애 대응 목적일 때만 하고 복원 후 어느 쪽이 정본인지 먼저 정한다.
