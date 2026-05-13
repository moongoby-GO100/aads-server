# PC Agent 멀티서비스 충돌 해결 기획서
> 작성: 2026-05-13 | TASK: AADS-PC-MULTI-SVC | 우선순위: P0

## 1. 문제 정의
PC Agent 1대에서 중국상품소싱, 신상마켓, 사방넷 등 여러 서비스가 동시에 브라우저 브릿지를 사용할 때 충돌 발생.

### 근본 원인
| # | 원인 | 위치 | 영향 |
|---|------|------|------|
| RC-1 | `_ACTIVE_CDP_PORT` 전역변수 1개 | `browser_auto.py:19` | 마지막 browser_launch가 포트 덮어씀 |
| RC-2 | Windows Mutex 단일 인스턴스 강제 | `agent.py:85` | 서비스별 분리 실행 불가 |
| RC-3 | 기존 CDP 세션 무조건 재사용 | `browser_auto.py:653-666` | 다른 서비스 세션을 가져감 |

## 2. 해결 방향
**별도 PC Agent 불필요** → 단일 PC Agent + CDPSessionManager로 work_key별 포트/프로필 격리.

## 3. Phase 구현 계획

### Phase 1: CDP 세션 매니저 (P0, 3~4시간)
- `browser_auto.py`: `_ACTIVE_CDP_PORT` 전역 제거 → `CDPSessionManager` 클래스
- work_key → (port, profile_dir) 매핑 관리
- 모든 browser 명령에서 `_effective_port` → `_resolve_session(params)` 변경
- `browser_launch`에서 work_key별 전용 포트/프로필 자동 할당

### Phase 2: 서버 측 work_key 자동 주입 (P1, 2시간)
- `browser_bridge/service.py`: `_run_browser_command`에서 work_key를 params에 자동 주입
- `pc_agent_manager.py`: `execute_routed_command`에서 job_type의 work_key를 params에 병합

### Phase 3: 서비스별 사전 등록 + 대시보드 (P2, 3시간)
- DB `pc_agent_service_configs` 테이블
- API `/api/v1/pc-agent/services` CRUD
- 대시보드 PC Agent 모니터링 패널

### Phase 4: 클라이언트 빌드 + 배포 (P2, 1시간)
- PC Agent EXE 빌드 → 자동 업데이트 배포

## 4. 서비스별 work_key 매핑
| 서비스 | work_key | 기본 포트 |
|--------|----------|----------|
| 중국상품소싱 | `ntv2-china-sourcing` | 9222 |
| 신상마켓 상품수집/등록 | `ntv2-sinsang-registration` | 9333 |
| 사방넷 상품등록 | `ntv2-sabangnet-register` | 9444 |
| 일반/테스트 | `general` | 9555 |

## 5. 검증 기준
- T-1: 중국상품소싱 + 신상마켓 동시 실행 → 상호 간섭 없음
- T-2: 서비스 A 작업 중 서비스 B browser_launch → A의 탭/URL 변경 없음
- T-3: PC Agent 재시작 후 세션 복구
- T-4: 4개 서비스 동시 browser_navigate 성공
