# P2 적용 가이드 — 프론트엔드 폴링 완화 (호스트 실행 필요)

> 컨테이너 내부에서는 호스트 `/root/aads/aads-dashboard/` 디렉터리에 직접 접근 불가 + docker compose 빌드 불가.
> 아래 패치를 호스트 셸에서 직접 적용 후 빌드/배포 필요.

## 변경 대상
파일: `/root/aads/aads-dashboard/src/app/chat/page.tsx`
라인: 2486~2500, 2779 부근의 폴링 루프

## 현재 동작
- 기본 interval: 3000ms
- `waitingBg=true`: 매 틱 = **3초마다 폴링**
- `waitingBg=false`: 5틱마다 = 15초마다 폴링
- 3초마다 `streaming-status` + (필요 시) `messages?limit=5`를 호출

장시간 응답 대기(예: 30분 disconnect 윈도우) 시 1세션당 ~600회/30분 폴링 → DB/서버 부하.

## 권장 패치 (3단 점진적 완화)

```diff
--- a/aads-dashboard/src/app/chat/page.tsx
+++ b/aads-dashboard/src/app/chat/page.tsx
@@ -2484,12 +2484,17 @@
     // BUG-SESSION-MIX FIX: cancelled 클로저로 세션 전환 시 in-flight 폴링 응답 폐기
     let cancelled = false;
-    // PERF: 3초 interval, waitingBg=true 3초/아닐 때 15초 폴링 (성능 최적화)
+    // PERF [2026-05-20]: 3초 기준 interval. waitingBg 시 점진적 완화로 장시간 대기 부하 감소.
+    //  - 처음 30초: 매 틱(3초) — 빠른 응답 반영
+    //  - 30~120초: 2틱(6초)
+    //  - 120초 초과: 4틱(12초) — 장시간 끊김 대기 시 부하 감소
+    //  - idle(waitingBg=false): 5틱(15초)
     let tickCount = 0;
     let prevWaitingBg = false; // waitingBg 전환 감지용
+    let waitingBgStartedAt = 0;
     const iv = setInterval(async () => {
       if (cancelled) return;
       // FIX-3: 초기 스크롤 완료 전까지 폴링 skip (간섭 방지)
       if (isInitialLoadRef.current) return;
       const _streaming = streamingRef.current;
       const _waitingBg = waitingBgRef.current;
       // PERF-FIX: waitingBg true->false 전환 시 tickCount 리셋
       // 대기 중 카운터가 증가하여 false 전환 직후 즉시 실행되는 현상 방지
-      if (prevWaitingBg && !_waitingBg) { tickCount = 0; }
+      if (prevWaitingBg && !_waitingBg) { tickCount = 0; waitingBgStartedAt = 0; }
+      if (!prevWaitingBg && _waitingBg) { waitingBgStartedAt = Date.now(); }
       prevWaitingBg = _waitingBg;
       tickCount++;
-      if (!_waitingBg && tickCount % 5 !== 0) return;
+      if (!_waitingBg) {
+        if (tickCount % 5 !== 0) return;
+      } else {
+        const waitedMs = waitingBgStartedAt ? Date.now() - waitingBgStartedAt : 0;
+        const mod = waitedMs > 120_000 ? 4 : (waitedMs > 30_000 ? 2 : 1);
+        if (mod > 1 && tickCount % mod !== 0) return;
+      }
       // ── just_completed 감지: streaming-status 폴링 (스트리밍 중에도 항상 체크) ──
```

## 호스트 적용 단계

```bash
# 1) 소스 패치
cd /root/aads/aads-dashboard
# 위 diff를 패치 파일로 저장 후
git diff src/app/chat/page.tsx
patch -p1 < /tmp/p2_polling.diff   # 또는 수동 편집

# 2) 린트/타입체크
npm run lint -- src/app/chat/page.tsx 2>&1 | tail -20
npx tsc --noEmit 2>&1 | head -20

# 3) 무중단 빌드/배포 (CLAUDE.md R-DOCKER 준수)
docker compose -f /root/aads/aads-dashboard/docker-compose.yml build aads-dashboard
docker compose -f /root/aads/aads-dashboard/docker-compose.yml up -d aads-dashboard

# 4) 검증
docker logs aads-dashboard --tail=30
curl -I https://aads.newtalk.kr/chat | head -5

# 5) 효과 측정 (배포 5분 후)
docker exec aads-server bash -c "grep -E 'streaming-status|messages\\?session_id' /var/log/aads-api.log | wc -l"
# 패치 전 분당 ~120회/세션 → 패치 후 분당 ~40회/세션 예상 (장시간 대기 시)
```

## 영향 범위
- **즉응성**: 첫 30초는 동일(3초 주기) → 사용자 체감 변화 거의 없음.
- **장시간 대기 부하**: 2분 초과 시 4배 완화 → 30분 disconnect 시나리오에서 폴링 횟수 75% 감소.
- **다른 코드 경로**: `_drainTimer`(30ms), `syncTimer`(2s), `fetchKeyStatus`(5분) 등은 영향 없음.
- **회귀 위험**: 낮음. 기존 로직(tickCount % 5)과 동일한 패턴 확장.

## 참고
- 백엔드 P1(상류 SSE 단절 시 자동 재시도)이 이미 핫리로드 적용되어 있으므로, 사실상 30분 disconnect 시나리오 자체가 줄어든다. P2는 보조 부하 절감 차원.
- 같은 파일에서 추가로 검토할 곳: `setInterval`/`setTimeout` 21곳. 특히 `}, 30);`(L3216), `}, 200);`(L2435), `}, 500);`(L2381)은 짧은 주기인데 필요성 확인 가치 있음(별건).
