# 러너·터미널 인증: 슬롯 accessToken 대여 (AADS-RUNNER-SLOT-LEASE)

2026-09-14 도입. contabo116 의 릴레이 슬롯 자격증명을 **복사하지 않고**
임시 accessToken 만 다른 서버에 밀어 넣는다.

## 왜 이렇게 하나

러너는 `.env` 고정 토큰(`ANTHROPIC_AUTH_TOKEN`, `_2`)만 읽었다. 계정1 은 429,
계정2 는 401 revoked 라 contabo14·cafe24_114 러너가 통째로 죽어 있었다.
자격증명 파일을 그대로 복사하는 안(A안)은 3대가 각자 refresh 를 돌려
서로를 무효화한다 — `flock` 은 호스트 로컬이라 조정이 안 된다.

그래서 **영구 열쇠(`refreshToken`)는 contabo116 밖으로 내보내지 않는다.**
나가는 것은 2시간짜리 임시 출입증(`accessToken`)뿐이다.

| 필드 | 성격 | 원격 서버로 나가나 |
|---|---|---|
| `accessToken` | 임시 출입증(약 2시간) | **예** |
| `refreshToken` | 영구 열쇠(무한 재발급) | **아니오** |

## 구성

| 역할 | 파일 | 위치 |
|---|---|---|
| 발급자 | `scripts/slot_lease_push.sh` | contabo116 |
| 발급 타이머 | `scripts/aads-claude-lease-push.{service,timer}` | contabo116, 10분 주기 |
| **소비자 설치기** | `scripts/deploy_lease_clients.sh` | contabo116 에서 실행 |
| 소비자(러너) | `/root/scripts/runner.env` | 대상 서버 |
| 소비자(터미널) | `/usr/local/bin/claude` 래퍼 | 대상 서버 |
| 배치 결과 | `/root/.claude/lease.env` | 대상 서버, 10분마다 갱신 |

러너는 `current.env` **다음에** `runner.env` 를 source 하므로 대여 토큰이
죽은 고정 토큰을 이긴다. 만료·부재면 조용히 고정 토큰으로 되돌아간다.

## 새 서버를 추가할 때 — 명령 한 줄

    LEASE_CLIENT_TARGETS="root@<새IP>" \
      bash /root/aads/aads-server/scripts/deploy_lease_clients.sh

선행 조건 두 개뿐이다.

1. contabo116 → 대상 서버로 **비밀번호 없는 SSH**(BatchMode)가 열려 있을 것.
2. 대상 서버에 `/usr/bin/claude` 바이너리가 설치돼 있을 것(래퍼가 exec 한다).

기본 대상은 `root@5.104.86.14 root@114.207.244.86` 이다. 인자 없이 실행하면
그 2대에 재설치한다. **멱등이다** — 몇 번을 돌려도 같고, 기존 파일은
`.bak_lease_<날짜>` 로 남는다. 래퍼는 `bash -n` 구문검사를 통과해야만
교체된다(깨진 래퍼는 그 서버의 `claude` 를 통째로 죽이기 때문).

설치 후 발급 대상에도 새 서버를 넣어야 실제 토큰이 흐른다.
`slot_lease_push.sh` 의 대상 목록을 확인하라 — 설치만 하고 발급을 빠뜨리면
`lease.env` 가 영원히 생기지 않고 조용히 고정 토큰으로 폴백한다.

## 검증

대상 서버에서 — 영구 열쇠가 안 나갔는지, 토큰이 신선한지 두 가지를 본다.

    python3 -c "import json,datetime; \
      d=json.load(open('/root/.claude-lease/slot1/.claude/.credentials.json'))['claudeAiOauth']; \
      print('has_refresh=',bool(d.get('refreshToken'))); \
      print('expiresAt=',datetime.datetime.fromtimestamp(d['expiresAt']/1000))"

`has_refresh=False` 여야 정상이다. `True` 면 A안이 섞여 들어온 것이니 즉시 멈춰라.

contabo116 에서 타이머:

    systemctl list-timers aads-claude-lease-push.timer --no-pager

E2E 는 read-only 스모크 러너를 대상 프로젝트로 제출해 확인한다
(GO100→contabo14, SF/NTV2→cafe24_114, AADS→contabo116).

## 알려진 함정

- **1번 계정 주간한도**: 터미널에서 `AADS_LEASE_SLOT=2 claude -p "..."` 로 2번 슬롯을 쓴다.
- **디스크**: 러너는 clean worktree 에 5GB 를 요구한다. 부족하면 인증과 무관하게
  `worktree_disk_low` 로 사전 차단된다. 인증 문제로 오진하지 마라.
- **호스트 CLI 버전**: 2026-09-14 기준 호스트 `claude` 2.1.270 은 한도 오류를 냈고
  번들 2.1.259 는 같은 자격증명으로 성공했다. 버전 차이를 먼저 의심하라.
