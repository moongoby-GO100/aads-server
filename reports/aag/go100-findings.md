# AAG L1/L2 — AADS 아키텍처 결함 리포트

생성 2026-09-19 07:56 KST · 결함 218건 · 판정 불가(UNRESOLVED) 128건

UNRESOLVED 는 **결함 수에 포함하지 않는다**. 판정 못 한 것을 결함으로 세면
숫자가 부풀고, 부풀린 숫자는 아무도 손대지 않아 규칙 전체가 무시된다.

## 스캔 범위

| 대상 | 수 |
|---|---|
| 앱 파이썬 파일 | 1022 |
| 라우터 디렉터리 파일 | 143 |
| APIRouter 정의 모듈 | 138 |
| include_router 호출 | 128 |
| 마운트된 라우트 | 502 |
| 네임스페이스 | 136 |
| 프런트 파일 | 0 |
| 해석된 프런트 호출 | 0 |
| SQL 참조 테이블 | 362 |
| 그래프 노드/엣지 | 991 / 2201 |

## 규칙별 건수

| 규칙 | 심각도 | 건수 |
|---|---|---|
| `DUP_MODULE` | P1 | 0 |
| `DOUBLE_MOUNT` | P1 | 0 |
| `ROUTE_SHADOWED` | P0 | 3 |
| `ORPHAN_ROUTER` | P2 | 67 |
| `TABLE_NO_MODEL` | P1 | 144 |
| `PATH_DRIFT` | P1 | 0 |
| `ROUTE_MISSING` | P0 | 0 |
| `STALE_BACKUP` | P2 | 4 |

| **합계** | | **218** |

## ROUTE_SHADOWED (3건)

- [P0] `backend/app/main.py` 에 `GET /api/v1/dashboard/summary` 가 2번 등록됐다 — `backend/app/api/v1/dashboard_router.py:609` 만 살고 `backend/app/routers/v4_compat.py:516` 는 도달 불가다(FastAPI 는 먼저 등록된 라우트를 쓴다). 예외가 나지 않으므로 HTTP 로는 보이지 않는다
- [P0] `backend/app/main.py` 에 `GET /api/v4/fund/status` 가 2번 등록됐다 — `backend/app/api/v4_position_api.py:247` 만 살고 `backend/app/routers/fund.py:43` 는 도달 불가다(FastAPI 는 먼저 등록된 라우트를 쓴다). 예외가 나지 않으므로 HTTP 로는 보이지 않는다
- [P0] `backend/app/main.py` 에 `GET /api/v4/system/status` 가 2번 등록됐다 — `backend/app/routers/system.py:24` 만 살고 `backend/app/routers/v4_system.py:30` 는 도달 불가다(FastAPI 는 먼저 등록된 라우트를 쓴다). 예외가 나지 않으므로 HTTP 로는 보이지 않는다

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

## TABLE_NO_MODEL (144건)

- [P1] 테이블 `account_snapshots` 을 3개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/api/v1/dashboard_router.py`, `backend/app/routers/go100/live_dashboard_router.py`, `backend/app/routers/go100/live_trading_router.py`)
- [P1] 테이블 `active_symbols` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/routers/go100/data_status_router.py`)
- [P1] 테이블 `and` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/services/go100/live_trading/live_service.py`)
- [P1] 테이블 `both` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/services/data_pipeline/collector_investor.py`)
- [P1] 테이블 `financial_ratios` 을 4개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/services/discovery/feature_engine.py`, `backend/app/services/go100/backtest/data_gate.py`, `backend/app/services/go100/data_collectors/company_shadow_backfill.py`)
- [P1] 테이블 `global_index_snapshot` 을 2개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/routers/go100/market_router.py`, `backend/app/tasks/global_index_fetcher.py`)
- [P1] 테이블 `go100_accounts` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/services/execution/fund_commander.py`)
- [P1] 테이블 `go100_alerts` 을 10개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/routers/go100/ai_router.py`, `backend/app/routers/go100/monitor_router.py`, `backend/app/routers/v4_data_collection.py`)
- [P1] 테이블 `go100_chat_sessions` 을 3개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/routers/go100/chat_router.py`, `backend/app/services/go100/chat_message_store.py`, `backend/app/services/go100/pdf_export_service.py`)
- [P1] 테이블 `go100_daily_briefings` 을 3개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/routers/go100/feed_router.py`, `backend/app/routers/go100/market_router.py`, `backend/app/services/go100/briefing/daily_briefing_service.py`)
- [P1] 테이블 `go100_daily_equity` 을 2개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/routers/go100/performance_stats_router.py`, `backend/app/routers/go100/research_router.py`)
- [P1] 테이블 `go100_daily_ohlcv` 을 2개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/services/go100/backtest/historical_replay.py`, `backend/app/services/go100/screening/realtime_screening_service.py`)
- [P1] 테이블 `go100_daily_returns` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/api/v1/go100_admin_router.py`)
- [P1] 테이블 `go100_dart_disclosures` 을 9개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/routers/go100/company_analysis_router.py`, `backend/app/routers/go100/disclosure_router.py`, `backend/app/routers/go100/screener_router.py`)
- [P1] 테이블 `go100_data_collection_log` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/api/v1/go100_admin_router.py`)
- [P1] 테이블 `go100_data_integrity_log` 을 8개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/routers/go100/card_trades_router.py`, `backend/app/routers/go100/dashboard_router.py`, `backend/app/routers/go100/data_status_router.py`)
- [P1] 테이블 `go100_delisted_ohlcv` 을 2개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/services/go100/backtest/data_loader.py`, `backend/app/services/go100/backtest/ohlcv_cache.py`)
- [P1] 테이블 `go100_episodic_memory` 을 3개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/services/go100/ai/hallucination_guard.py`, `backend/app/services/go100/ai/hypothesis_engine.py`, `backend/app/services/go100/memory/episodic_memory.py`)
- [P1] 테이블 `go100_etf_distributions` 을 2개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/routers/go100/etf_router.py`, `backend/app/services/go100/ai/tool_executors.py`)
- [P1] 테이블 `go100_etf_info` 을 2개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/routers/go100/etf_router.py`, `backend/app/services/go100/ai/tool_executors.py`)
- [P1] 테이블 `go100_evolution_loop_log` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/routers/go100/research_router.py`)
- [P1] 테이블 `go100_evolution_loops` 을 3개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/services/go100/agents/agent_validator.py`, `backend/app/services/go100/ai/evolution_feedback.py`, `backend/app/services/go100/scheduler/go100_scheduler.py`)
- [P1] 테이블 `go100_experience_log` 을 2개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/services/go100/ai/tool_executors.py`, `backend/app/services/go100/backtest/historical_replay.py`)
- [P1] 테이블 `go100_feature_snapshots` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/api/v1/go100_admin_router.py`)
- [P1] 테이블 `go100_fundamentals` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/services/go100/collectors/collect_fundamentals.py`)
- [P1] 테이블 `go100_fundamentals_pit` 을 2개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/services/go100/ai/feature_engine.py`, `backend/app/services/go100/ai/tool_executors.py`)
- [P1] 테이블 `go100_goals` 을 10개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/routers/go100/ai_router.py`, `backend/app/services/go100/ai/data_queries.py`, `backend/app/services/go100/ai/goal_engine.py`)
- [P1] 테이블 `go100_hypothesis_backtests` 을 4개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/services/go100/ai/agent_tools.py`, `backend/app/services/go100/ai/hypothesis_draft.py`, `backend/app/services/go100/ai/hypothesis_executors.py`)
- [P1] 테이블 `go100_investor_flow` 을 3개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/routers/go100/momentum_router.py`, `backend/app/routers/go100/screener_router.py`, `backend/app/services/position/lifecycle.py`)
- [P1] 테이블 `go100_kis_daily_ohlcv` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/routers/go100/data_status_router.py`)
- [P1] 테이블 `go100_minute_bars` 을 2개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/services/go100/live_trading/scalping_entry_engine.py`, `backend/app/services/position/lifecycle.py`)
- [P1] 테이블 `go100_model_registry` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/api/v1/go100_admin_router.py`)
- [P1] 테이블 `go100_momentum_scores` 을 3개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/routers/go100/momentum_router.py`, `backend/app/services/go100/ai/momentum_ml_predictor.py`, `backend/app/services/go100/momentum/batch_job.py`)
- [P1] 테이블 `go100_news_items` 을 29개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/api/v1/go100_admin_router.py`, `backend/app/desk_engine/trigger_desk3.py`, `backend/app/desk_engine/weekly_reviewer.py`)
- [P1] 테이블 `go100_notification_settings` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/services/go100/notification/notification_service.py`)
- [P1] 테이블 `go100_notifications` 을 3개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/routers/go100/feed_router.py`, `backend/app/routers/v4_data_collection.py`, `backend/app/services/go100/notification/notification_service.py`)
- [P1] 테이블 `go100_nxt_ohlcv_daily` 을 2개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/services/go100/limitup_analyzer.py`, `backend/app/services/system/orchestrator.py`)
- [P1] 테이블 `go100_orderbook_snapshot` 을 5개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/api/v1/go100_admin_router.py`, `backend/app/routers/go100/data_status_router.py`, `backend/app/services/go100/live_trading/live_engine.py`)
- [P1] 테이블 `go100_portfolio_snapshots` 을 5개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/routers/go100/paper_router.py`, `backend/app/services/go100/live_trading/live_engine.py`, `backend/app/services/go100/paper_trading/paper_engine.py`)
- [P1] 테이블 `go100_portfolios_portfolio_id_seq` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/routers/go100/strategy_router.py`)
- [P1] 테이블 `go100_push_subscriptions` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/services/go100/notification/notification_service.py`)
- [P1] 테이블 `go100_sector_correlation` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/services/go100/ai/tool_executors.py`)
- [P1] 테이블 `go100_sector_price` 을 2개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/services/go100/ai/tool_executors.py`, `backend/app/services/market_data_service.py`)
- [P1] 테이블 `go100_signal_log` 을 3개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/routers/go100/company_analysis_router.py`, `backend/app/routers/go100/feed_router.py`, `backend/app/routers/go100/research_router.py`)
- [P1] 테이블 `go100_signals` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/api/v1/go100_admin_router.py`)
- [P1] 테이블 `go100_stock_profiles` 을 2개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/api/v1/go100_admin_router.py`, `backend/app/services/go100/agents/stock_profiler.py`)
- [P1] 테이블 `go100_strategy_cards_go100_card_id_seq` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/routers/go100/strategy_router.py`)
- [P1] 테이블 `go100_strategy_store` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/services/go100/strategy/card_service.py`)
- [P1] 테이블 `go100_system_config` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/api/v1/go100_admin_router.py`)
- [P1] 테이블 `go100_tick_data` 을 8개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/api/v1/go100_admin_router.py`, `backend/app/routers/go100/card_trades_router.py`, `backend/app/routers/go100/data_status_router.py`)
- [P1] 테이블 `go100_trade_decision_logs` 을 12개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/routers/go100/card_trades_router.py`, `backend/app/routers/go100/data_status_router.py`, `backend/app/routers/go100/live_dashboard_router.py`)
- [P1] 테이블 `go100_trades_effective` 을 3개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/routers/go100/card_trades_router.py`, `backend/app/routers/v4_chart_signals.py`, `backend/app/services/go100/daily_results_service.py`)
- [P1] 테이블 `go100_user_profiles` 을 3개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/services/go100/ai/tool_executors.py`, `backend/app/services/go100/position_sizing_engine.py`, `backend/app/services/go100/user/profile_service.py`)
- [P1] 테이블 `go100_user_settings` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/services/go100/ai/agent_tools.py`)
- [P1] 테이블 `index_daily` 을 20개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/api/v4_backtest_analysis.py`, `backend/app/routers/go100/dashboard_router.py`, `backend/app/routers/go100/market_router.py`)
- [P1] 테이블 `investor_daily` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/services/confirmation_entry_engine.py`)
- [P1] 테이블 `investor_trend` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/services/go100/ai/tool_executors.py`)
- [P1] 테이블 `kis_configs` 을 18개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/api/v1/accounts_router.py`, `backend/app/core/account_mode.py`, `backend/app/routers/v4_kis.py`)
- [P1] 테이블 `live_positions` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/services/go100/account_holdings_loader.py`)
- [P1] 테이블 `news_themes` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/services/desk_filters/node_detector_desk1.py`)
- [P1] 테이블 `orderbook_snapshots` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/services/go100/ai/orderbook_features.py`)
- [P1] 테이블 `pg_locks` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/services/go100/live_trading/direct_db.py`)
- [P1] 테이블 `pg_stat_user_tables` 을 5개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/api/v4_position_api.py`, `backend/app/routers/v4_admin.py`, `backend/app/routers/v4_dashboard.py`)
- [P1] 테이블 `portfolios` 을 2개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/api/v1/dashboard_router.py`, `backend/app/api/v1/portfolio_router.py`)
- [P1] 테이블 `positions` 을 9개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/api/v1/dashboard_router.py`, `backend/app/routers/position.py`, `backend/app/services/compound_growth_tracker.py`)
- [P1] 테이블 `prev_close` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/routers/go100/card_trades_router.py`)
- [P1] 테이블 `price_tick_snapshots` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/routers/go100/company_analysis_router.py`)
- [P1] 테이블 `s4_cfg_latest` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/routers/go100/card_trades_router.py`)
- [P1] 테이블 `scalping_features_daily` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/services/scalping/universe_refresher.py`)
- [P1] 테이블 `social_accounts` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/api/v1/user_settings_router.py`)
- [P1] 테이블 `stock_fundamentals` 을 12개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/services/fundamental_collector.py`, `backend/app/services/go100/ai/data_queries.py`, `backend/app/services/go100/ai/feature_engine.py`)
- [P1] 테이블 `stock_master` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/services/go100/ai/wave_analysis_tool.py`)
- [P1] 테이블 `strategies` 을 4개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/routers/v4_compat.py`, `backend/app/routers/v4_settings.py`, `backend/app/routers/v4_trading.py`)
- [P1] 테이블 `strategy_performance` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/services/strategy_card_service.py`)
- [P1] 테이블 `target` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/services/go100/ai/tool_executors.py`)
- [P1] 테이블 `trades` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/api/v1/dashboard_router.py`)
- [P1] 테이블 `user_strategies` 을 3개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/routers/v4_compat.py`, `backend/app/routers/v4_settings.py`, `backend/app/routers/v4_trading.py`)
- [P1] 테이블 `users` 을 30개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/api/v1/accounts_router.py`, `backend/app/api/v1/admin_router.py`, `backend/app/api/v1/auth_router.py`)
- [P1] 테이블 `v4_account_holdings` 을 18개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/api/v1/accounts_router.py`, `backend/app/api/v4_position_api.py`, `backend/app/routers/account_sync_router.py`)
- [P1] 테이블 `v4_account_sync_config` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/services/report/report_generator.py`)
- [P1] 테이블 `v4_account_sync_log` 을 3개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/api/v4_position_api.py`, `backend/app/routers/account_sync_router.py`, `backend/app/services/trading/account_sync_manager.py`)
- [P1] 테이블 `v4_alerts` 을 2개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/api/v4_alert_api.py`, `backend/app/services/trading/alert_manager.py`)
- [P1] 테이블 `v4_backtest_desk_detail` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/routers/v4_dashboard.py`)
- [P1] 테이블 `v4_backtest_equity` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/routers/v4_dashboard.py`)
- [P1] 테이블 `v4_backtest_regime_analysis` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/api/v4_backtest_analysis.py`)
- [P1] 테이블 `v4_chat_messages` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/services/llm/chat_session.py`)
- [P1] 테이블 `v4_chat_sessions` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/services/llm/chat_session.py`)
- [P1] 테이블 `v4_commander_scan_cache` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/routers/v4_dashboard.py`)
- [P1] 테이블 `v4_daily_portfolio` 을 2개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/api/v4_position_api.py`, `backend/app/services/trading/compound_engine.py`)
- [P1] 테이블 `v4_desk3_pool` 을 5개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/services/data_collection_service.py`, `backend/app/services/desk_filters/fractal_backtest.py`, `backend/app/services/fundamental_collector.py`)
- [P1] 테이블 `v4_desk4_watchlist` 을 5개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/services/data_collection_service.py`, `backend/app/services/desk_filters/fractal_backtest.py`, `backend/app/services/desk_filters/node_detector_desk4.py`)
- [P1] 테이블 `v4_desk5_watchlist` 을 5개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/services/data_collection_service.py`, `backend/app/services/desk_filters/fractal_backtest.py`, `backend/app/services/fundamental_collector.py`)
- [P1] 테이블 `v4_desk5_weekly_review` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/desk_engine/weekly_reviewer.py`)
- [P1] 테이블 `v4_desk_fund` 을 16개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/api/v4_desk_recommend.py`, `backend/app/api/v4_position_api.py`, `backend/app/routers/v4_dashboard.py`)
- [P1] 테이블 `v4_desk_portfolio_summary` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/desk_engine/scheduler.py`)
- [P1] 테이블 `v4_desk_positions` 을 5개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/desk_engine/position_manager.py`, `backend/app/desk_engine/scheduler.py`, `backend/app/routers/v4_compat.py`)
- [P1] 테이블 `v4_excluded_stocks` 을 2개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/services/go100/ai/explosion_detector.py`, `backend/app/services/go100/live_trading/excluded_stock_guard.py`)
- [P1] 테이블 `v4_fund_lending` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/services/trading/fund_lending_manager.py`)
- [P1] 테이블 `v4_fund_pool_snapshot` 을 4개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/routers/v4_dashboard.py`, `backend/app/services/adaptive/fund_rebalancer.py`, `backend/app/services/adaptive/regime_weight.py`)
- [P1] 테이블 `v4_hav_hypotheses` 을 4개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/routers/go100/hypothesis_center_router.py`, `backend/app/services/go100/ai/feedback_loop.py`, `backend/app/services/go100/ai/hypothesis_engine.py`)
- [P1] 테이블 `v4_investor_daily` 을 45개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/api/v1/go100_admin_router.py`, `backend/app/desk_engine/trigger_desk5.py`, `backend/app/routers/go100/market_router.py`)
- [P1] 테이블 `v4_investor_trend` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/services/go100/agents/agent_desk5.py`)
- [P1] 테이블 `v4_market_calendar` 을 12개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/api/v1/go100_admin_router.py`, `backend/app/routers/go100/data_status_router.py`, `backend/app/routers/v4_data_pipeline.py`)
- [P1] 테이블 `v4_market_ranking` 을 5개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/services/brain/chief_analyst.py`, `backend/app/services/data_pipeline/collector_ranking.py`, `backend/app/services/market_data_service.py`)
- [P1] 테이블 `v4_market_regime_daily` 을 36개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/api/v4_backtest_analysis.py`, `backend/app/routers/go100/card_trades_router.py`, `backend/app/routers/regime.py`)
- [P1] 테이블 `v4_meta_rules` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/services/go100/ai/hypothesis_engine.py`)
- [P1] 테이블 `v4_minute_collect_progress` 을 6개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/api/v1/go100_admin_router.py`, `backend/app/routers/go100/data_status_router.py`, `backend/app/services/data_pipeline/collector_minute.py`)
- [P1] 테이블 `v4_news` 을 2개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/services/go100/agents/agent_desk3.py`, `backend/app/services/go100/agents/news_agent.py`)
- [P1] 테이블 `v4_news_feed` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/services/growth_score_engine.py`)
- [P1] 테이블 `v4_news_summary` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/services/feature_engine.py`)
- [P1] 테이블 `v4_ohlcv_clean` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/services/go100/ai/tool_executors.py`)
- [P1] 테이블 `v4_ohlcv_daily` 을 10개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/services/desk2_conditions/c5_theme_simultaneous.py`, `backend/app/services/feature_engine.py`, `backend/app/services/go100/agents/agent_desk3.py`)
- [P1] 테이블 `v4_ohlcv_minute` 을 57개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/api/v1/go100_admin_router.py`, `backend/app/api/v4_desk2_backtest.py`, `backend/app/routers/bt_chart.py`)
- [P1] 테이블 `v4_ohlcv_minute_2026_09` 을 2개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/services/go100/live_trading/card310_mtf_profit_taker.py`, `backend/app/services/go100/live_trading/card310_signal_validator.py`)
- [P1] 테이블 `v4_order_executions` 을 3개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/api/v4_desk_recommend.py`, `backend/app/api/v4_position_api.py`, `backend/app/services/trading/v4_trade_bridge.py`)
- [P1] 테이블 `v4_orderbook_realtime` 을 8개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/api/v1/go100_admin_router.py`, `backend/app/routers/go100/card_trades_router.py`, `backend/app/routers/go100/data_status_router.py`)
- [P1] 테이블 `v4_position_transfers` 을 3개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/api/v4_desk_recommend.py`, `backend/app/services/trading/split_transfer_engine.py`, `backend/app/services/trading/v4_pipeline_orchestrator.py`)
- [P1] 테이블 `v4_regime_strategy_weights` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/routers/v4_trading.py`)
- [P1] 테이블 `v4_scalping_signals` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/services/go100/live_trading/live_engine.py`)
- [P1] 테이블 `v4_scalping_universe` 을 4개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/api/v4_desk_recommend.py`, `backend/app/routers/go100/card_trades_router.py`, `backend/app/services/go100/live_trading/scalping_entry_engine.py`)
- [P1] 테이블 `v4_scoring_weights` 을 3개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/routers/v4_dashboard.py`, `backend/app/services/adaptive/weekly_scoring.py`, `backend/app/services/scoring/composite_scorer.py`)
- [P1] 테이블 `v4_sector_daily` 을 12개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/routers/go100/market_router.py`, `backend/app/services/backtest/backtest_engine_v2.py`, `backend/app/services/data_pipeline/collector_theme_sector.py`)
- [P1] 테이블 `v4_signals` 을 6개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/api/v4_desk_recommend.py`, `backend/app/api/v4_position_api.py`, `backend/app/api/v4_signal_api.py`)
- [P1] 테이블 `v4_stage_transitions` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/services/trading/compound_engine.py`)
- [P1] 테이블 `v4_stock_sector` 을 7개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/services/go100/ai/feature_engine.py`, `backend/app/services/go100/universe/advanced_filters.py`, `backend/app/services/market_data_service.py`)
- [P1] 테이블 `v4_strategies` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/routers/v4_compat.py`)
- [P1] 테이블 `v4_strategy_cards` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/services/data_pipeline/data_integrity.py`)
- [P1] 테이블 `v4_strategy_results` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/services/desk2_conditions/dcs_evaluator.py`)
- [P1] 테이블 `v4_system_state_log` 을 7개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/routers/v4_admin.py`, `backend/app/routers/v4_ai_trading.py`, `backend/app/routers/v4_compat.py`)
- [P1] 테이블 `v4_theme_activity_daily` 을 2개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/services/market_data_service.py`, `backend/app/services/scoring/theme_scorer.py`)
- [P1] 테이블 `v4_theme_master` 을 6개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/routers/go100/card_trades_router.py`, `backend/app/routers/go100/market_router.py`, `backend/app/routers/v4_stock_screener.py`)
- [P1] 테이블 `v4_theme_stock` 을 7개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/routers/go100/card_trades_router.py`, `backend/app/routers/go100/market_router.py`, `backend/app/routers/v4_stock_screener.py`)
- [P1] 테이블 `v4_theme_stock_mapping` 을 2개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/services/go100/limitup_analyzer.py`, `backend/app/services/scoring/theme_scorer.py`)
- [P1] 테이블 `v4_tick_strength` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/services/feature_engine.py`)
- [P1] 테이블 `v4_top20_history` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/desk_engine/trigger_desk3.py`)
- [P1] 테이블 `v4_trade_analysis` 을 10개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/api/v1/dashboard_router.py`, `backend/app/routers/v4_admin.py`, `backend/app/routers/v4_ai_trading.py`)
- [P1] 테이블 `v4_trade_log` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/services/risk/reentry_guard.py`)
- [P1] 테이블 `v4_trades` 을 15개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/api/v1/dashboard_router.py`, `backend/app/routers/v4_dashboard.py`, `backend/app/services/execution/fill_sync.py`)
- [P1] 테이블 `v4_universe_stocks` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/routers/v4_backtest.py`)
- [P1] 테이블 `v4_vi_occurrences` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/services/trading/desk2/backtest/historical_price_feeder.py`)
- [P1] 테이블 `v4_volume_retention` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/services/go100/ai/explosion_detector.py`)
- [P1] 테이블 `v_unified_positions` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/services/trading/position_reader.py`)
- [P1] 테이블 `v_unified_trades` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/services/trading/position_reader.py`)
- [P1] 테이블 `windows` 을 1개 파일이 참조하지만 CREATE TABLE 정의를 코드에서 찾을 수 없다 (예: `backend/app/routers/go100/card_trades_router.py`)

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

