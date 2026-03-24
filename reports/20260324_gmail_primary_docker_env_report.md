# AADS 컨테이너 Anthropic OAuth — Gmail 1순위(env) 정렬

**일자**: 2026-03-24 (KST)

## 목적

채팅·직접 Anthropic 호출 경로가 **Gmail OAuth를 기본 슬롯**으로 쓰도록 `docker-compose` 환경 매핑과 앱 변수명을 일치시킴.

## 변경 요약

| 영역 | 내용 |
|------|------|
| `docker-compose.yml` | `ANTHROPIC_API_KEY` ← `ANTHROPIC_AUTH_TOKEN`(Gmail), `ANTHROPIC_API_KEY_FALLBACK` ← `ANTHROPIC_AUTH_TOKEN_2`(Naver) |
| `app/core/anthropic_client.py` | `_API_KEY_GMAIL` / `_API_KEY_NAVER`를 위 매핑과 동일하게 읽도록 정리 |
| `app/services/model_selector.py` | 동일 매핑 + 라벨(Gmail/Naver) 정합 |

## 유의 (호스트 CLI Relay)

**CLI Relay**(1단계)는 컨테이너 env와 별개로 **호스트 `claude` 자격**을 씀. Gmail 강제는 `~/.claude/current.env`·`.credentials`·`CURRENT_OAUTH` 등 호스트 쪽도 Gmail이어야 동일 계정으로 맞춰짐.

## LiteLLM

`.env.litellm`의 `ANTHROPIC_API_KEY_1`이 Gmail, `order: 1` 유지(기존 설정).

## 백업

- **코드**: Git 커밋·푸시로 보관 (`4341bc9`, `fix(auth): Gmail-first OAuth env in compose; R-AUTH safe getenv`).
- **DB/볼륨 전체 덤프**: 이번 작업 범위에서는 별도 실행하지 않음. 운영 정책상 필요 시 `pg_dump` 등 선행.

## 배포

```bash
cd /root/aads/aads-server && docker compose up -d --no-deps aads-server
```

(적용 완료: 2026-03-24 컨테이너 Recreate)

## 검증

- `python3.11 -m py_compile` (수정 모듈)
- pre-commit (커밋 시)
