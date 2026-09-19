# AAG L1/L2 — AADS 아키텍처 결함 리포트

생성 2026-09-19 13:42 KST · 결함 71건 · 판정 불가(UNRESOLVED) 128건

UNRESOLVED 는 **결함 수에 포함하지 않는다**. 판정 못 한 것을 결함으로 세면
숫자가 부풀고, 부풀린 숫자는 아무도 손대지 않아 규칙 전체가 무시된다.

## 스캔 범위

| 대상 | 수 |
|---|---|
| 앱 파이썬 파일 | 1023 |
| 라우터 디렉터리 파일 | 143 |
| APIRouter 정의 모듈 | 138 |
| include_router 호출 | 128 |
| 마운트된 라우트 | 499 |
| 네임스페이스 | 136 |
| 프런트 파일 | 0 |
| 해석된 프런트 호출 | 0 |
| SQL 참조 테이블 | 354 |
| 그래프 노드/엣지 | 983 / 2188 |

## 규칙별 건수

| 규칙 | 심각도 | 건수 |
|---|---|---|
| `DUP_MODULE` | P1 | 0 |
| `DOUBLE_MOUNT` | P1 | 0 |
| `ROUTE_SHADOWED` | P0 | 0 |
| `ORPHAN_ROUTER` | P2 | 67 |
| `TABLE_NO_MODEL` | P1 | 0 |
| `PATH_DRIFT` | P1 | 0 |
| `ROUTE_MISSING` | P0 | 0 |
| `STALE_BACKUP` | P2 | 4 |

| **합계** | | **71** |

## ORPHAN_ROUTER (67건)

- [P2] `backend/app/api/go100/bridge.py` 이 APIRouter 를 정의하지만 어떤 엔트리포인트에도 include_router 되지 않았다 — 죽은 코드이거나 등록 누락이다
- [P2] `backend/app/api/v1/picks.py` 이 APIRouter 를 정의하지만 어떤 엔트리포인트에도 include_router 되지 않았다 — 죽은 코드이거나 등록 누락이다
- [P2] `backend/app/api/v1/trading_dashboard_router.py` 이 APIRouter 를 정의하지만 어떤 엔트리포인트에도 include_router 되지 않았다 — 죽은 코드이거나 등록 누락이다
- [P2] `backend/app/api/v4_backtest_analysis.py` 이 APIRouter 를 정의하지만 어떤 엔트리포인트에도 include_router 되지 않았다 — 죽은 코드이거나 등록 누락이다
- [P2] `backend/app/api/v4_desk2_backtest.py` 이 APIRouter 를 정의하지만 어떤 엔트리포인트에도 include_router 되지 않았다 — 죽은 코드이거나 등록 누락이다
- [P2] `backend/app/api/v4_desk_recommend.py` 이 APIRouter 를 정의하지만 어떤 엔트리포인트에도 include_router 되지 않았다 — 죽은 코드이거나 등록 누락이다
- [P2] `backend/app/legacy_v1_bridge.py` 이 APIRouter 를 정의하지만 어떤 엔트리포인트에도 include_router 되지 않았다 — 죽은 코드이거나 등록 누락이다
- [P2] `backend/app/routers/ai_model_dashboard_router.py` 이 APIRouter 를 정의하지만 어떤 엔트리포인트에도 include_router 되지 않았다 — 죽은 코드이거나 등록 누락이다
- [P2] `backend/app/routers/backtest_v2.py` 이 APIRouter 를 정의하지만 어떤 엔트리포인트에도 include_router 되지 않았다 — 죽은 코드이거나 등록 누락이다
- [P2] `backend/app/routers/bt_dashboard.py` 이 APIRouter 를 정의하지만 어떤 엔트리포인트에도 include_router 되지 않았다 — 죽은 코드이거나 등록 누락이다
- [P2] `backend/app/routers/go100/account_integrity_router.py` 이 APIRouter 를 정의하지만 어떤 엔트리포인트에도 include_router 되지 않았다 — 죽은 코드이거나 등록 누락이다
- [P2] `backend/app/routers/go100/ai_router.py` 이 APIRouter 를 정의하지만 어떤 엔트리포인트에도 include_router 되지 않았다 — 죽은 코드이거나 등록 누락이다
- [P2] `backend/app/routers/go100/analyst_report_router.py` 이 APIRouter 를 정의하지만 어떤 엔트리포인트에도 include_router 되지 않았다 — 죽은 코드이거나 등록 누락이다
- [P2] `backend/app/routers/go100/autonomy_router.py` 이 APIRouter 를 정의하지만 어떤 엔트리포인트에도 include_router 되지 않았다 — 죽은 코드이거나 등록 누락이다
- [P2] `backend/app/routers/go100/backtest_router.py` 이 APIRouter 를 정의하지만 어떤 엔트리포인트에도 include_router 되지 않았다 — 죽은 코드이거나 등록 누락이다
- [P2] `backend/app/routers/go100/briefing_router.py` 이 APIRouter 를 정의하지만 어떤 엔트리포인트에도 include_router 되지 않았다 — 죽은 코드이거나 등록 누락이다
- [P2] `backend/app/routers/go100/card_trades_router.py` 이 APIRouter 를 정의하지만 어떤 엔트리포인트에도 include_router 되지 않았다 — 죽은 코드이거나 등록 누락이다
- [P2] `backend/app/routers/go100/chat_router.py` 이 APIRouter 를 정의하지만 어떤 엔트리포인트에도 include_router 되지 않았다 — 죽은 코드이거나 등록 누락이다
- [P2] `backend/app/routers/go100/commander_router.py` 이 APIRouter 를 정의하지만 어떤 엔트리포인트에도 include_router 되지 않았다 — 죽은 코드이거나 등록 누락이다
- [P2] `backend/app/routers/go100/company_analysis_router.py` 이 APIRouter 를 정의하지만 어떤 엔트리포인트에도 include_router 되지 않았다 — 죽은 코드이거나 등록 누락이다
- [P2] `backend/app/routers/go100/condition_search_router.py` 이 APIRouter 를 정의하지만 어떤 엔트리포인트에도 include_router 되지 않았다 — 죽은 코드이거나 등록 누락이다
- [P2] `backend/app/routers/go100/dashboard_router.py` 이 APIRouter 를 정의하지만 어떤 엔트리포인트에도 include_router 되지 않았다 — 죽은 코드이거나 등록 누락이다
- [P2] `backend/app/routers/go100/data_gateway_router.py` 이 APIRouter 를 정의하지만 어떤 엔트리포인트에도 include_router 되지 않았다 — 죽은 코드이거나 등록 누락이다
- [P2] `backend/app/routers/go100/data_router_api.py` 이 APIRouter 를 정의하지만 어떤 엔트리포인트에도 include_router 되지 않았다 — 죽은 코드이거나 등록 누락이다
- [P2] `backend/app/routers/go100/data_status_router.py` 이 APIRouter 를 정의하지만 어떤 엔트리포인트에도 include_router 되지 않았다 — 죽은 코드이거나 등록 누락이다
- [P2] `backend/app/routers/go100/desk_status_router.py` 이 APIRouter 를 정의하지만 어떤 엔트리포인트에도 include_router 되지 않았다 — 죽은 코드이거나 등록 누락이다
- [P2] `backend/app/routers/go100/disclosure_router.py` 이 APIRouter 를 정의하지만 어떤 엔트리포인트에도 include_router 되지 않았다 — 죽은 코드이거나 등록 누락이다
- [P2] `backend/app/routers/go100/feed_router.py` 이 APIRouter 를 정의하지만 어떤 엔트리포인트에도 include_router 되지 않았다 — 죽은 코드이거나 등록 누락이다
- [P2] `backend/app/routers/go100/global_rules_router.py` 이 APIRouter 를 정의하지만 어떤 엔트리포인트에도 include_router 되지 않았다 — 죽은 코드이거나 등록 누락이다
- [P2] `backend/app/routers/go100/goal_router.py` 이 APIRouter 를 정의하지만 어떤 엔트리포인트에도 include_router 되지 않았다 — 죽은 코드이거나 등록 누락이다
- [P2] `backend/app/routers/go100/hypothesis_center_router.py` 이 APIRouter 를 정의하지만 어떤 엔트리포인트에도 include_router 되지 않았다 — 죽은 코드이거나 등록 누락이다
- [P2] `backend/app/routers/go100/live_dashboard_router.py` 이 APIRouter 를 정의하지만 어떤 엔트리포인트에도 include_router 되지 않았다 — 죽은 코드이거나 등록 누락이다
- [P2] `backend/app/routers/go100/live_orders_router.py` 이 APIRouter 를 정의하지만 어떤 엔트리포인트에도 include_router 되지 않았다 — 죽은 코드이거나 등록 누락이다
- [P2] `backend/app/routers/go100/live_trading_router.py` 이 APIRouter 를 정의하지만 어떤 엔트리포인트에도 include_router 되지 않았다 — 죽은 코드이거나 등록 누락이다
- [P2] `backend/app/routers/go100/llm_registry_router.py` 이 APIRouter 를 정의하지만 어떤 엔트리포인트에도 include_router 되지 않았다 — 죽은 코드이거나 등록 누락이다
- [P2] `backend/app/routers/go100/market_router.py` 이 APIRouter 를 정의하지만 어떤 엔트리포인트에도 include_router 되지 않았다 — 죽은 코드이거나 등록 누락이다
- [P2] `backend/app/routers/go100/me_router.py` 이 APIRouter 를 정의하지만 어떤 엔트리포인트에도 include_router 되지 않았다 — 죽은 코드이거나 등록 누락이다
- [P2] `backend/app/routers/go100/monitor_router.py` 이 APIRouter 를 정의하지만 어떤 엔트리포인트에도 include_router 되지 않았다 — 죽은 코드이거나 등록 누락이다
- [P2] `backend/app/routers/go100/news_analysis_router.py` 이 APIRouter 를 정의하지만 어떤 엔트리포인트에도 include_router 되지 않았다 — 죽은 코드이거나 등록 누락이다
- [P2] `backend/app/routers/go100/notification_router.py` 이 APIRouter 를 정의하지만 어떤 엔트리포인트에도 include_router 되지 않았다 — 죽은 코드이거나 등록 누락이다
- [P2] `backend/app/routers/go100/optimizer_router.py` 이 APIRouter 를 정의하지만 어떤 엔트리포인트에도 include_router 되지 않았다 — 죽은 코드이거나 등록 누락이다
- [P2] `backend/app/routers/go100/paper_router.py` 이 APIRouter 를 정의하지만 어떤 엔트리포인트에도 include_router 되지 않았다 — 죽은 코드이거나 등록 누락이다
- [P2] `backend/app/routers/go100/paper_trading_router.py` 이 APIRouter 를 정의하지만 어떤 엔트리포인트에도 include_router 되지 않았다 — 죽은 코드이거나 등록 누락이다
- [P2] `backend/app/routers/go100/performance_stats_router.py` 이 APIRouter 를 정의하지만 어떤 엔트리포인트에도 include_router 되지 않았다 — 죽은 코드이거나 등록 누락이다
- [P2] `backend/app/routers/go100/portfolio_router.py` 이 APIRouter 를 정의하지만 어떤 엔트리포인트에도 include_router 되지 않았다 — 죽은 코드이거나 등록 누락이다
- [P2] `backend/app/routers/go100/reports_router.py` 이 APIRouter 를 정의하지만 어떤 엔트리포인트에도 include_router 되지 않았다 — 죽은 코드이거나 등록 누락이다
- [P2] `backend/app/routers/go100/research_router.py` 이 APIRouter 를 정의하지만 어떤 엔트리포인트에도 include_router 되지 않았다 — 죽은 코드이거나 등록 누락이다
- [P2] `backend/app/routers/go100/review_opinion_router.py` 이 APIRouter 를 정의하지만 어떤 엔트리포인트에도 include_router 되지 않았다 — 죽은 코드이거나 등록 누락이다
- [P2] `backend/app/routers/go100/risk_router.py` 이 APIRouter 를 정의하지만 어떤 엔트리포인트에도 include_router 되지 않았다 — 죽은 코드이거나 등록 누락이다
- [P2] `backend/app/routers/go100/risk_settings_router.py` 이 APIRouter 를 정의하지만 어떤 엔트리포인트에도 include_router 되지 않았다 — 죽은 코드이거나 등록 누락이다
- [P2] `backend/app/routers/go100/scalping_composite_router.py` 이 APIRouter 를 정의하지만 어떤 엔트리포인트에도 include_router 되지 않았다 — 죽은 코드이거나 등록 누락이다
- [P2] `backend/app/routers/go100/scheduler_router.py` 이 APIRouter 를 정의하지만 어떤 엔트리포인트에도 include_router 되지 않았다 — 죽은 코드이거나 등록 누락이다
- [P2] `backend/app/routers/go100/screener_router.py` 이 APIRouter 를 정의하지만 어떤 엔트리포인트에도 include_router 되지 않았다 — 죽은 코드이거나 등록 누락이다
- [P2] `backend/app/routers/go100/screening_router.py` 이 APIRouter 를 정의하지만 어떤 엔트리포인트에도 include_router 되지 않았다 — 죽은 코드이거나 등록 누락이다
- [P2] `backend/app/routers/go100/stocks_router.py` 이 APIRouter 를 정의하지만 어떤 엔트리포인트에도 include_router 되지 않았다 — 죽은 코드이거나 등록 누락이다
- [P2] `backend/app/routers/go100/strategy_router.py` 이 APIRouter 를 정의하지만 어떤 엔트리포인트에도 include_router 되지 않았다 — 죽은 코드이거나 등록 누락이다
- [P2] `backend/app/routers/go100/strategy_whitepaper_router.py` 이 APIRouter 를 정의하지만 어떤 엔트리포인트에도 include_router 되지 않았다 — 죽은 코드이거나 등록 누락이다
- [P2] `backend/app/routers/go100/trade_history_router.py` 이 APIRouter 를 정의하지만 어떤 엔트리포인트에도 include_router 되지 않았다 — 죽은 코드이거나 등록 누락이다
- [P2] `backend/app/routers/go100/trade_modal_router.py` 이 APIRouter 를 정의하지만 어떤 엔트리포인트에도 include_router 되지 않았다 — 죽은 코드이거나 등록 누락이다
- [P2] `backend/app/routers/v4_chart_daily.py` 이 APIRouter 를 정의하지만 어떤 엔트리포인트에도 include_router 되지 않았다 — 죽은 코드이거나 등록 누락이다
- [P2] `backend/app/routers/v4_chart_helpers.py` 이 APIRouter 를 정의하지만 어떤 엔트리포인트에도 include_router 되지 않았다 — 죽은 코드이거나 등록 누락이다
- [P2] `backend/app/routers/v4_chart_minute.py` 이 APIRouter 를 정의하지만 어떤 엔트리포인트에도 include_router 되지 않았다 — 죽은 코드이거나 등록 누락이다
- [P2] `backend/app/routers/v4_chart_period.py` 이 APIRouter 를 정의하지만 어떤 엔트리포인트에도 include_router 되지 않았다 — 죽은 코드이거나 등록 누락이다
- [P2] `backend/app/routers/v4_chart_signals.py` 이 APIRouter 를 정의하지만 어떤 엔트리포인트에도 include_router 되지 않았다 — 죽은 코드이거나 등록 누락이다
- [P2] `backend/app/routers/v4_desk2_live.py` 이 APIRouter 를 정의하지만 어떤 엔트리포인트에도 include_router 되지 않았다 — 죽은 코드이거나 등록 누락이다
- [P2] `backend/app/routers/v4_nxt_test.py` 이 APIRouter 를 정의하지만 어떤 엔트리포인트에도 include_router 되지 않았다 — 죽은 코드이거나 등록 누락이다
- [P2] `backend/app/routers/v4_trades_unified.py` 이 APIRouter 를 정의하지만 어떤 엔트리포인트에도 include_router 되지 않았다 — 죽은 코드이거나 등록 누락이다

## STALE_BACKUP (4건)

- [P2] `scripts/db_backup.sh` 은 편집 중 남긴 사본 형식이다 — 저장소에 남으면 검색·grep 결과에 섞여 낡은 코드를 읽게 된다
- [P2] `scripts/disk_monitor_v1_backup.sh` 은 편집 중 남긴 사본 형식이다 — 저장소에 남으면 검색·grep 결과에 섞여 낡은 코드를 읽게 된다
- [P2] `scripts/prepare_mock_trading.py.bak.20260218` 은 편집 중 남긴 사본 형식이다 — 저장소에 남으면 검색·grep 결과에 섞여 낡은 코드를 읽게 된다
- [P2] `scripts/verify_backup.sh` 은 편집 중 남긴 사본 형식이다 — 저장소에 남으면 검색·grep 결과에 섞여 낡은 코드를 읽게 된다

## UNRESOLVED (결함 아님 — 판정 불가)

### SQL_TABLE (128건)
- `backend/app/api/v1/go100_admin_router.py:1887` 테이블 이름이 런타임 보간이라 확정 불가
- `backend/app/api/v1/go100_admin_router.py:1888` 테이블 이름이 런타임 보간이라 확정 불가
- `backend/app/api/v1/go100_admin_router.py:1893` 테이블 이름이 런타임 보간이라 확정 불가
- `backend/app/api/v1/go100_admin_router.py:1898` 테이블 이름이 런타임 보간이라 확정 불가
- `backend/app/api/v1/go100_admin_router.py:1904` 테이블 이름이 런타임 보간이라 확정 불가
- `backend/app/api/v1/go100_admin_router.py:1909` 테이블 이름이 런타임 보간이라 확정 불가
- `backend/app/api/v4_position_api.py:462` 테이블 이름이 런타임 보간이라 확정 불가
- `backend/app/routers/go100/analyst_report_router.py:94` 테이블 이름이 런타임 보간이라 확정 불가
- `backend/app/routers/go100/card_trades_router.py:7642` SQL 조각(문장 키워드로 시작하지 않음) — 테이블 확정 불가
- `backend/app/routers/go100/card_trades_router.py:9280` 테이블 이름이 런타임 보간이라 확정 불가
- `backend/app/routers/go100/card_trades_router.py:9312` 테이블 이름이 런타임 보간이라 확정 불가
- `backend/app/routers/go100/card_trades_router.py:9339` 테이블 이름이 런타임 보간이라 확정 불가
- `backend/app/routers/go100/card_trades_router.py:10215` 테이블 이름이 런타임 보간이라 확정 불가
- `backend/app/routers/go100/card_trades_router.py:10238` 테이블 이름이 런타임 보간이라 확정 불가
- `backend/app/routers/go100/company_analysis_router.py:228` 테이블 이름이 런타임 보간이라 확정 불가
- `backend/app/routers/go100/company_analysis_router.py:318` 테이블 이름이 런타임 보간이라 확정 불가
- `backend/app/routers/go100/company_analysis_router.py:426` 테이블 이름이 런타임 보간이라 확정 불가
- `backend/app/routers/go100/company_analysis_router.py:484` 테이블 이름이 런타임 보간이라 확정 불가
- `backend/app/routers/go100/company_analysis_router.py:600` 테이블 이름이 런타임 보간이라 확정 불가
- `backend/app/routers/go100/company_analysis_router.py:846` 테이블 이름이 런타임 보간이라 확정 불가
- … 외 108건

