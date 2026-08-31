import { getAuthFetchOptions } from "../lib/auth-fetch";

const BASE = "/api/go100";

export interface CardTradeItem {
  id: number;
  go100_card_id: number;
  stock_code: string;
  stock_name: string;
  side: "BUY" | "SELL";
  price: number;
  quantity: number;
  amount: number;
  pnl_amount: number | null;
  pnl_pct: number | null;
  is_paper: boolean;
  trade_date: string | null;
  traded_at: string | null;
}

export interface CardTradesResponse {
  total: number;
  page: number;
  size: number;
  items: CardTradeItem[];
}

export interface CardTradeStats {
  card_id: number;
  has_data: boolean;
  total_trades?: number;
  buy_count?: number;
  sell_count?: number;
  live_count?: number;
  paper_count?: number;
  win_count?: number;
  loss_count?: number;
  win_rate?: number | null;
  total_pnl?: number;
  avg_pnl_pct?: number | null;
  max_pnl_pct?: number | null;
  min_pnl_pct?: number | null;
  first_trade_date?: string | null;
  last_trade_date?: string | null;
  unique_stocks?: number;
  open_positions?: number;
  closed_positions?: number;
  realized_pnl?: number;
}

export async function getCardTrades(
  cardId: number,
  params?: { page?: number; size?: number; is_paper?: boolean }
): Promise<CardTradesResponse> {
  const res = await fetch(
    `${BASE}/strategy-cards/${cardId}/trades?${new URLSearchParams(
      Object.entries(params ?? {})
        .filter(([, v]) => v !== undefined)
        .map(([k, v]) => [k, String(v)])
    ).toString()}`,
    getAuthFetchOptions()
  );
  if (!res.ok) throw new Error(`getCardTrades failed: ${res.status}`);
  return res.json();
}

export async function getCardTradeStats(cardId: number): Promise<CardTradeStats> {
  const res = await fetch(
    `${BASE}/strategy-cards/${cardId}/trade-stats`,
    getAuthFetchOptions()
  );
  if (!res.ok) throw new Error(`getCardTradeStats failed: ${res.status}`);
  return res.json();
}

// ── Workbench API ─────────────────────────────────────────────────────────────
// Endpoint: GET /api/go100/strategy-cards/{card_id}/workbench
// mode: realtime | cumulative | date_range | lifecycle
// is_paper: true | false | (absent = all)
// date_from, date_to: YYYY-MM-DD (only used when mode=date_range)

export interface WorkbenchThresholds {
  stop_loss_pct: number | null;
  take_profit_pct: number | null;
  trailing_stop_pct: number | null;
  time_exit: unknown | null;
  holding_days: number | null;
  max_loss_pct: number | null;
  max_position_count: number | null;
}

export interface WorkbenchCardInfo {
  id: number;
  name: string;
  status: string;
  is_active: boolean;
  is_live: boolean;
  allocated_amount: number | null;
  max_stocks: number;
  version: number;
  thresholds: WorkbenchThresholds;
  updated_at: string | null;
}

export interface WorkbenchWaveReview {
  available: boolean;
  entry_phase?: string | null;
  fixed_wave_peak?: number | null;
  pullback_low?: number | null;
  entry_pullback_depth_pct?: number | null;
  entry_rebound_from_pullback_pct?: number | null;
  exit_to_fixed_wave_peak_pct?: number | null;
  exit_from_pullback_low_pct?: number | null;
  entry_zone_pct?: number | null;
  exit_zone_pct?: number | null;
  entry_from_pullback_pct?: number | null;
  exit_from_peak_pct?: number | null;
  source?: string | null;
  sample_source?: string | null;
  good_entry_zone?: boolean | null;
  premature_exit_candidate?: boolean | null;
  late_exit_candidate?: boolean | null;
  learning_included?: boolean | null;
  data_quality?: Record<string, unknown> | null;
  verdict?: string | null;
}

export interface WorkbenchTradeValueWindow {
  label?: string | null;
  minutes?: number | null;
  start_at?: string | null;
  end_at?: string | null;
  trade_value_krw?: number | null;
  rank?: number | null;
}

export interface CardTradeValueWindowsResponse {
  card_id: number;
  source: string;
  summary: Record<string, unknown>;
  items: Array<{
    stock_code: string;
    windows: Record<string, WorkbenchTradeValueWindow>;
    rise_context?: Record<string, unknown>;
  }>;
}

export interface WorkbenchStageRow {
  // event rows (stages 1 & 2)
  stock_code?: string;
  stock_name?: string;
  display_name?: string | null;
  market?: string | null;
  stock_name_missing?: boolean;
  stage?: string;
  event_phase?: string;
  decision?: string;
  reason_code?: string | null;
  reason_text?: string;
  created_at?: string;
  source_ts?: string | null;
  received_at?: string | null;
  source_table?: string | null;
  trade_group_id?: string | null;
  card_version?: number | null;
  // Stage 2 scoring fields
  total_score?: number | null;
  priority_rank?: number | null;
  pass_fail_status?: string | null;
  pass_reasons?: string[] | null;
  fail_reasons?: string[] | null;
  reason_text_detailed?: string | null;
  missing_data?: string[] | null;
  score_breakdown?: Record<string, {
    score: number;
    max: number;
    label?: string;
    intraday_pct?: number | null;
    volume_ratio?: number | null;
    strength?: number | null;
    trade_value?: number | null;
    audit_scope?: string | null;
    value?: number | null;
  }> | null;
  // Stage 1 live market data fields
  change_rate_pct?: number | null;
  volume?: number | null;
  trading_value_krw?: number | null;
  market_trading_value_krw?: number | null;
  market_trading_value_source?: string | null;
  market_trading_value_quote_time?: string | null;
  nxt_trading_value_krw?: number | null;
  nxt_trading_value_source?: string | null;
  nxt_trading_value_quote_time?: string | null;
  total_trading_value_krw?: number | null;
  trading_value_source?: string | null;
  is_nxt?: boolean | null;
  quote_time?: string | null;
  quote_age_sec?: number | null;
  freshness_status?: "fresh" | "ok" | "stale" | "missing" | null;
  upper_limit_price?: number | null;
  distance_to_limit_price?: number | null;
  distance_to_limit_pct?: number | null;
  data_source?: string | null;
  missing_reason?: string | null;
  threshold_values?: { min_change_rate_pct: number; min_trading_value_krw: number } | null;
  threshold_passes?: Record<string, boolean | null> | null;
  candidate_status?: string | null;
  candidate_rejection_reasons?: string[] | null;
  detailed_reason?: string | null;
  intraday_change_rank?: number | null;
  bullish_trade_value_rank?: number | null;
  trade_value_windows?: Record<string, WorkbenchTradeValueWindow> | null;
  candidate_scope?: string | null;
  discovery_source?: string | null;
  cumulative_data_source?: string | null;
  max_seen_change_pct?: number | null;
  last_seen?: string | null;
  first_watch20_at?: string | null;
  first_buy27_at?: string | null;
  first_limitup_at?: string | null;
  last_limitup_at?: string | null;
  limitup_unlock_count?: number | null;
  max_intraday_change_pct?: number | null;
  limitup_locked_bar_count?: number | null;
  timing_source?: string | null;
  intraday_candidate_bucket?: string[] | null;
  intraday_bullish?: boolean | null;
  // Stage 2 trigger/readiness fields
  buy_trigger_price?: number | null;
  distance_to_trigger_price?: number | null;
  distance_to_trigger_pct?: number | null;
  order_readiness?: "ready" | "waiting" | "blocked" | null;
  order_blockers?: string[] | null;
  next_required_action?: string | null;
  // order rows (stages 3 & 5)
  order_id?: number;
  status?: string;
  order_price?: number | null;
  filled_price?: number | null;
  fill_price?: number | null;
  executed_price?: number | null;
  filled_quantity?: number | null;
  order_quantity?: number | null;
  remaining_quantity?: number | null;
  fill_qty?: number | null;
  filled_at?: string | null;
  exit_reason?: string | null;
  exit_result?: string | null;
  // position rows (stage 4)
  id?: number;
  entry_price?: number | null;
  avg_price?: number | null;
  current_price?: number | null;
  pnl_pct?: number | null;
  unrealized_pnl_pct?: number | null;
  remaining_qty?: number | null;
  quantity?: number | null;
  qty?: number | null;
  stop_loss_price?: number | null;
  take_profit_price?: number | null;
  trailing_pct?: number | null;
  entry_date?: string | null;
  // trade rows (stage 5 & 6)
  price?: number | null;
  sell_price?: number | null;
  sold_at?: string | null;
  realized_pnl_pct?: number | null;
  pnl_amount?: number | null;
  review_result?: string | null;
  position_id?: number | null;
  entry_wave_context?: Record<string, unknown> | null;
  exit_wave_context?: Record<string, unknown> | null;
  wave_review?: WorkbenchWaveReview | null;
  is_paper?: boolean;
  trade_date?: string | null;
  traded_at?: string | null;
}

export interface WorkbenchStage {
  stage_id: number;
  stage_key: string;
  label: string;
  count: number;
  status: "available" | "empty" | "unavailable";
  updated_at: string | null;
  source: string;
  is_paper_filter_applied: boolean;
  rows: WorkbenchStageRow[];
  summary: Record<string, unknown>;
  stage_columns?: Array<{ key: string; label: string; type: string; price_key?: string }>;
}

export interface WorkbenchLifecycleItem {
  trade_group_id: string;
  selected_at: string | null;
  trace_gaps: string[];
  source_tables: string[];
  buy_order_id: number;
  stock_code: string;
  stock_name: string;
  buy_price: number | null;
  buy_qty: number | null;
  bought_at: string | null;
  position_id: number | null;
  position_status: string | null;
  unrealized_pnl_pct: number | null;
  stop_loss_price: number | null;
  take_profit_price: number | null;
  trailing_pct: number | null;
  sell_order_id: number | null;
  exit_reason: string | null;
  sold_at: string | null;
  pnl_amount: number | null;
  realized_pnl_pct: number | null;
  entry_wave_context?: Record<string, unknown> | null;
  exit_wave_context?: Record<string, unknown> | null;
  wave_review?: WorkbenchWaveReview | null;
}

export interface WorkbenchPeriodDay {
  trade_date: string;
  sample_count: number;
  win_count: number;
  loss_count: number;
  win_rate: number;
  total_pnl: number;
  avg_pnl_pct: number | null;
  market_regime: string;
  error_count: number;
}

export interface WorkbenchPeriodAnalysis {
  date_from: string;
  date_to: string;
  sample_size: number;
  confidence: "LOW" | "MEDIUM" | "HIGH";
  daily_trend: WorkbenchPeriodDay[];
  pnl_distribution: Array<{ bucket: string; count: number }>;
  exit_performance: Array<{
    exit_reason: string;
    trade_count: number;
    win_rate: number;
    total_pnl: number;
    avg_pnl_pct: number | null;
  }>;
}

export interface WorkbenchDiagnostic {
  stage: number | string;
  key: string;
  error: string;
}

export type DataQualityStatus = "PASS" | "WARN" | "CRITICAL" | "UNKNOWN";
export type TradingSession = "NXT_PRE" | "KRX_REGULAR" | "NXT_AFTER" | "CLOSED";

export interface DataQualitySource {
  source: string;
  label: string;
  status: DataQualityStatus;
  pass: boolean | null;
  actual?: string | null;
  expected?: string | null;
  severity?: string | null;
  message?: string | null;
  checked_at_kst?: string | null;
  latency_ms?: number | null;
}

export interface DataQualitySummary {
  overall_status: DataQualityStatus;
  checked_at_kst: string;
  session: TradingSession;
  sources: DataQualitySource[];
  heal_recent: boolean;
  heal_result?: string | null;
  source?: string;
  note?: string;
  error?: string;
}

export interface WorkbenchPerformance {
  elapsed_ms: number;
  cache_hit: boolean;
  cache_ttl_sec?: number;
  cache_age_sec?: number;
  partial: boolean;
  stale: boolean;
  limited_range?: {
    mode: string;
    date_from: string | null;
    date_to: string | null;
  };
}

export interface WorkbenchData {
  checked_at: string;
  mode: string;
  is_paper_filter: boolean | null;
  data_quality?: DataQualitySummary;
  strategy_type?: string;
  card: WorkbenchCardInfo;
  stages: WorkbenchStage[];
  diagnostics: WorkbenchDiagnostic[];
  filters?: {
    card_version: number;
    current_card_version: number;
    market_regime: string | null;
    available_card_versions: number[];
    available_market_regimes: string[];
  };
  lifecycle_items?: WorkbenchLifecycleItem[];
  period_analysis?: WorkbenchPeriodAnalysis | null;
  performance?: WorkbenchPerformance;
  cache_hit?: boolean;
  elapsed_ms?: number;
}

export type WorkbenchViewMode = "realtime" | "cumulative" | "date_range" | "lifecycle";
export type WorkbenchModeFilter = "all" | "LIVE" | "PAPER";

export async function getCardWorkbench(
  cardId: number,
  params?: {
    mode?: WorkbenchViewMode;
    is_paper?: boolean;
    date_from?: string;
    date_to?: string;
    card_version?: number;
    market_regime?: string;
  },
  signal?: AbortSignal
): Promise<WorkbenchData> {
  const qs = new URLSearchParams();
  if (params?.mode) qs.set("mode", params.mode);
  if (params?.is_paper !== undefined) qs.set("is_paper", String(params.is_paper));
  if (params?.date_from) qs.set("date_from", params.date_from);
  if (params?.date_to) qs.set("date_to", params.date_to);
  if (params?.card_version) qs.set("card_version", String(params.card_version));
  if (params?.market_regime) qs.set("market_regime", params.market_regime);
  const res = await fetch(
    `${BASE}/strategy-cards/${cardId}/workbench${qs.size > 0 ? "?" + qs.toString() : ""}`,
    { ...getAuthFetchOptions(), signal }
  );
  if (!res.ok) {
    const errData = await res.json().catch(() => null) as { detail?: string } | null;
    throw new Error(errData?.detail ?? `getCardWorkbench failed: ${res.status}`);
  }
  return res.json();
}

export async function getCardTradeValueWindows(
  cardId: number,
  stockCodes?: string[],
  signal?: AbortSignal,
): Promise<CardTradeValueWindowsResponse> {
  const qs = new URLSearchParams();
  if (stockCodes && stockCodes.length > 0) {
    qs.set("stock_codes", stockCodes.join(","));
  }
  const res = await fetch(
    `${BASE}/strategy-cards/${cardId}/trade-value-windows${qs.size > 0 ? "?" + qs.toString() : ""}`,
    { ...getAuthFetchOptions(), signal },
  );
  if (!res.ok) {
    const errData = await res.json().catch(() => null) as { detail?: string } | null;
    throw new Error(errData?.detail ?? `getCardTradeValueWindows failed: ${res.status}`);
  }
  return res.json();
}

// ── Improvement Proposals API ─────────────────────────────────────────────────

export type ProposalStatus = "PENDING" | "APPROVED" | "REJECTED" | "APPLIED";
export type ProposalPriority = "INFO" | "P1" | "P2" | "MONITOR";

export interface ImprovementProposal {
  proposal_id: number;
  issue_type: string;
  priority: ProposalPriority;
  root_cause: string | null;
  proposed_action: string;
  expected_impact: string | null;
  backtest_note: string | null;
  validation_status: string;
  backtest_result: Record<string, unknown>;
  proposed_changes: Record<string, unknown>;
  rollback_card_version: number | null;
  applied_card_version: number | null;
  status: ProposalStatus;
  stock_code: string | null;
  trade_id: number | null;
  auto_generated: boolean;
  source_stage: number | null;
  is_paper: boolean | null;
  approver_id: number | null;
  approved_at: string | null;
  rejection_reason: string | null;
  applied_at: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface ProposalsResponse {
  card_id: number;
  trade_date: string;
  total: number;
  items: ImprovementProposal[];
}

export async function getImprovementProposals(
  cardId: number,
  params?: { trade_date?: string; status?: ProposalStatus }
): Promise<ProposalsResponse> {
  const qs = new URLSearchParams();
  if (params?.trade_date) qs.set("trade_date", params.trade_date);
  if (params?.status) qs.set("status", params.status);
  const res = await fetch(
    `${BASE}/strategy-cards/${cardId}/improvement-proposals${qs.size > 0 ? "?" + qs.toString() : ""}`,
    getAuthFetchOptions()
  );
  if (!res.ok) {
    const errData = await res.json().catch(() => null) as { detail?: string } | null;
    throw new Error(errData?.detail ?? `getImprovementProposals failed: ${res.status}`);
  }
  return res.json();
}

export async function createImprovementProposal(
  cardId: number,
  data: {
    issue_type: string;
    priority?: ProposalPriority;
    proposed_action: string;
    root_cause?: string;
    expected_impact?: string;
    backtest_note?: string;
    stock_code?: string;
    trade_id?: number;
    trade_date?: string;
    auto_generated?: boolean;
    is_paper?: boolean;
  }
): Promise<{ created: boolean; proposal_id?: number; detail?: string }> {
  const res = await fetch(
    `${BASE}/strategy-cards/${cardId}/improvement-proposals`,
    {
      ...getAuthFetchOptions(),
      method: "POST",
      headers: { ...getAuthFetchOptions().headers, "Content-Type": "application/json" },
      body: JSON.stringify(data),
    }
  );
  if (!res.ok) {
    const errData = await res.json().catch(() => null) as { detail?: string } | null;
    throw new Error(errData?.detail ?? `createImprovementProposal failed: ${res.status}`);
  }
  return res.json();
}

export async function updateImprovementProposal(
  cardId: number,
  proposalId: number,
  action: "approve" | "reject" | "apply",
  opts?: {
    rejection_reason?: string;
    backtest_note?: string;
    backtest_result?: Record<string, unknown>;
    proposed_changes?: Record<string, unknown>;
    rollback_card_version?: number;
  }
): Promise<{ updated: boolean; proposal_id: number; new_status: string; applied_card_version?: number | null }> {
  const res = await fetch(
    `${BASE}/strategy-cards/${cardId}/improvement-proposals/${proposalId}`,
    {
      ...getAuthFetchOptions(),
      method: "PATCH",
      headers: { ...getAuthFetchOptions().headers, "Content-Type": "application/json" },
      body: JSON.stringify({ action, ...opts }),
    }
  );
  if (!res.ok) {
    const errData = await res.json().catch(() => null) as { detail?: string } | null;
    throw new Error(errData?.detail ?? `updateImprovementProposal failed: ${res.status}`);
  }
  return res.json();
}

// ── Daily Results Snapshot API ────────────────────────────────────────────────

export type DailyResultMode = "all" | "paper" | "live";

export interface DailyResult {
  id: number;
  go100_card_id: number;
  trade_date: string;
  card_version: number;
  mode: DailyResultMode;
  event_count: number;
  candidate_count: number;
  pass_count: number;
  error_count: number;
  buy_count: number;
  sell_count: number;
  win_count: number;
  loss_count: number;
  win_rate: number | null;
  realized_pnl: number;
  avg_pnl_pct: number | null;
  max_pnl_pct: number | null;
  min_pnl_pct: number | null;
  unique_stocks: number;
  market_regime: string;
  confidence: "HIGH" | "MEDIUM" | "LOW";
  source_range: string;
  computed_at: string | null;
}

export interface DailyResultsResponse {
  total: number;
  date_from: string;
  date_to: string;
  card_version: number;
  source: "persisted";
  items: DailyResult[];
}

export interface DailyResultsRecomputeResponse {
  card_id: number;
  card_version: number;
  date_from: string;
  date_to: string;
  mode: DailyResultMode;
  computed_dates: number;
  ok: number;
  errors: number;
  details: Array<{ trade_date: string; status: string; error?: string; sell_count?: number; win_rate?: number | null }>;
}

export async function getDailyResults(
  cardId: number,
  params?: {
    date_from?: string;
    date_to?: string;
    card_version?: number;
    mode?: DailyResultMode;
  }
): Promise<DailyResultsResponse> {
  const qs = new URLSearchParams();
  if (params?.date_from) qs.set("date_from", params.date_from);
  if (params?.date_to) qs.set("date_to", params.date_to);
  if (params?.card_version) qs.set("card_version", String(params.card_version));
  if (params?.mode) qs.set("mode", params.mode);
  const res = await fetch(
    `${BASE}/strategy-cards/${cardId}/daily-results${qs.size > 0 ? "?" + qs.toString() : ""}`,
    getAuthFetchOptions()
  );
  if (!res.ok) {
    const errData = await res.json().catch(() => null) as { detail?: string } | null;
    throw new Error(errData?.detail ?? `getDailyResults failed: ${res.status}`);
  }
  return res.json();
}

export async function recomputeDailyResults(
  cardId: number,
  params?: { date_from?: string; date_to?: string; mode?: DailyResultMode }
): Promise<DailyResultsRecomputeResponse> {
  const res = await fetch(
    `${BASE}/strategy-cards/${cardId}/daily-results/recompute`,
    {
      ...getAuthFetchOptions(),
      method: "POST",
      headers: { ...getAuthFetchOptions().headers, "Content-Type": "application/json" },
      body: JSON.stringify(params ?? {}),
    }
  );
  if (!res.ok) {
    const errData = await res.json().catch(() => null) as { detail?: string } | null;
    throw new Error(errData?.detail ?? `recomputeDailyResults failed: ${res.status}`);
  }
  return res.json();
}
