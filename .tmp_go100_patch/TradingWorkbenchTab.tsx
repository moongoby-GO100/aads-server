"use client";

import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { getAuthFetchOptions } from "@/go100/lib/auth-fetch";
import { StockLabel } from "@/components/common/StockLabel";

// ─────────────────────────────────────────────
// Types
// ─────────────────────────────────────────────

interface CardThresholds {
  stop_loss_pct: number | null;
  take_profit_pct: number | null;
  trailing_stop_pct: number | null;
  time_exit: string | null;
  holding_days: number | null;
  max_loss_pct: number | null;
  max_position_count: number | null;
}

interface WorkbenchCard {
  id: number;
  name: string;
  status: string;
  is_active: boolean;
  is_live: boolean;
  allocated_amount: number;
  max_stocks: number | null;
  thresholds: CardThresholds;
  strategy_definition?: Record<string, unknown> | null;
  updated_at: string;
}

interface WorkbenchStage {
  stage_id: number;
  stage_key: string;
  label: string;
  count: number;
  total_evaluations?: number;
  unique_stocks?: number;
  status: "available" | "unavailable" | "empty";
  updated_at: string | null;
  source: string;
  is_paper_filter_applied: boolean;
  rows: Array<Record<string, unknown>>;
  summary?: Record<string, unknown>;
}

interface WorkbenchWaveReview {
  available?: boolean;
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

interface LifecycleItem {
  buy_order_id: number | null;
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

interface DiagnosticItem {
  stage: number | string;
  key: string;
  error: string;
}

interface WorkbenchPerformance {
  elapsed_ms?: number;
  cache_hit?: boolean;
  cache_age_sec?: number;
  cache_ttl_sec?: number;
  statement_timeout_ms?: number;
  partial?: boolean;
  stale?: boolean;
}

interface WorkbenchResponse {
  checked_at: string;
  mode: "realtime" | "cumulative" | "date_range" | "lifecycle";
  is_paper_filter: boolean | null;
  card: WorkbenchCard;
  stages: WorkbenchStage[];
  lifecycle_items?: LifecycleItem[];
  diagnostics: DiagnosticItem[];
  performance?: WorkbenchPerformance;
}

// ─────────────────────────────────────────────
// Format helpers
// ─────────────────────────────────────────────

function fmtKst(iso: string | null | undefined): string {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString("ko-KR", {
      timeZone: "Asia/Seoul",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return "—";
  }
}

function fmtPrice(v: number | null | undefined): string {
  if (v == null) return "—";
  return `${Number(v).toLocaleString("ko-KR")}원`;
}

function fmtPct(v: number | null | undefined): string {
  if (v == null) return "—";
  return `${v > 0 ? "+" : ""}${v.toFixed(2)}%`;
}

function fmtCount(v: number | null | undefined): string {
  if (v == null) return "—";
  return v.toLocaleString("ko-KR");
}

function pctColor(v: number | null | undefined): string {
  if (v == null) return "text-gray-400";
  return v > 0 ? "text-red-400" : v < 0 ? "text-blue-400" : "text-gray-400";
}

function candidateScopeLabel(v: unknown): string {
  const scope = safeStr(v);
  if (scope === "current_snapshot_watch") return "현재 +20%";
  if (scope === "today_cumulative_watch") return "오늘 누적 +20%";
  if (scope === "preopen_expected_watch") return "장전 예상";
  if (scope === "watch_discovery") return "+20% 발굴";
  return scope;
}

function safeStr(v: unknown): string {
  if (v == null) return "—";
  return String(v);
}

function safeNum(v: unknown): number | null {
  if (v == null) return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

function waveReviewFrom(raw: unknown): WorkbenchWaveReview | null {
  if (!raw || typeof raw !== "object") return null;
  return raw as WorkbenchWaveReview;
}

function waveReviewText(raw: unknown): string {
  const review = waveReviewFrom(raw);
  if (!review?.available) return "파동 스냅샷 없음";
  const parts: string[] = [];
  const source = review.sample_source ?? review.source;
  if (source === "historical_trade_replay_v1") parts.push("과거복기");
  if (review.entry_phase) parts.push(`진입 ${review.entry_phase}`);
  if (review.entry_zone_pct != null) parts.push(`진입구간 ${(Number(review.entry_zone_pct) * 100).toFixed(0)}%`);
  if (review.exit_zone_pct != null) parts.push(`청산구간 ${(Number(review.exit_zone_pct) * 100).toFixed(0)}%`);
  if (review.entry_pullback_depth_pct != null) parts.push(`눌림 ${Number(review.entry_pullback_depth_pct).toFixed(1)}%`);
  if (review.exit_to_fixed_wave_peak_pct != null) parts.push(`고점대비 ${Number(review.exit_to_fixed_wave_peak_pct).toFixed(1)}%`);
  if (review.exit_from_pullback_low_pct != null) parts.push(`저점대비 +${Number(review.exit_from_pullback_low_pct).toFixed(1)}%`);
  if (review.learning_included) parts.push("학습포함");
  if (parts.length === 0 && review.verdict) parts.push(review.verdict);
  return parts.length > 0 ? parts.join(" · ") : "파동 스냅샷 기록됨";
}

function nestedRecord(raw: unknown, key: string): Record<string, unknown> {
  if (!raw || typeof raw !== "object") return {};
  const value = (raw as Record<string, unknown>)[key];
  return value && typeof value === "object" ? value as Record<string, unknown> : {};
}

function StrategyDefinitionPanel({ card }: { card: WorkbenchCard }) {
  const definition = card.strategy_definition;
  if (!definition || typeof definition !== "object") return null;

  const discovery = nestedRecord(definition, "discovery");
  const selection = nestedRecord(definition, "selection");
  const entry = nestedRecord(definition, "entry");
  const exit = nestedRecord(definition, "exit");
  const contractVersion = safeStr(definition.contract_version);

  return (
    <div className="rounded-2xl border border-emerald-500/20 bg-emerald-500/5 p-4">
      <div className="mb-3 flex flex-col gap-1 sm:flex-row sm:items-center sm:justify-between">
        <p className="text-sm font-semibold text-emerald-300">#119 현재 매매흐름 계약</p>
        <p className="text-xs text-emerald-200/70">{contractVersion}</p>
      </div>
      <div className="grid gap-3 md:grid-cols-4">
        <div className="rounded-lg bg-gray-950/40 p-3">
          <p className="text-xs font-medium text-gray-400">발굴종목</p>
          <p className="mt-1 text-sm text-gray-100">{safeStr(discovery.filter)}</p>
          <p className="mt-1 text-xs text-gray-500">{safeStr(discovery.note)}</p>
        </div>
        <div className="rounded-lg bg-gray-950/40 p-3">
          <p className="text-xs font-medium text-gray-400">매매선정</p>
          <p className="mt-1 text-sm text-gray-100">{safeStr(selection.rule)}</p>
          <p className="mt-1 text-xs text-gray-500">{safeStr(selection.hard_exclusion)}</p>
        </div>
        <div className="rounded-lg bg-gray-950/40 p-3">
          <p className="text-xs font-medium text-gray-400">진입</p>
          <p className="mt-1 text-sm text-gray-100">{safeStr(entry.primary_gate)}</p>
          <p className="mt-1 text-xs text-gray-500">{safeStr(entry.prelock_model)}</p>
        </div>
        <div className="rounded-lg bg-gray-950/40 p-3">
          <p className="text-xs font-medium text-gray-400">청산</p>
          <p className="mt-1 text-sm text-gray-100">{safeStr(exit.contract)}</p>
          <p className="mt-1 text-xs text-gray-500">{safeStr(exit.next_day)}</p>
        </div>
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────
// URL param helpers
// ─────────────────────────────────────────────

type ViewMode = "realtime" | "cumulative" | "date_range" | "lifecycle";
type ModeFilter = "all" | "live" | "paper";

function modeFilterToIsPaper(f: ModeFilter): string {
  if (f === "live") return "false";
  if (f === "paper") return "true";
  return "";
}

// ─────────────────────────────────────────────
// Sub-components
// ─────────────────────────────────────────────

function Spinner() {
  return (
    <div className="flex items-center justify-center py-16">
      <div className="h-8 w-8 animate-spin rounded-full border-2 border-blue-500 border-t-transparent" />
    </div>
  );
}

function TableWrapper({ children }: { children: React.ReactNode }) {
  return (
    <div className="overflow-x-auto rounded-xl border border-white/5">
      <table className="min-w-full text-sm">{children}</table>
    </div>
  );
}

function Th({ children }: { children: React.ReactNode }) {
  return (
    <th className="whitespace-nowrap bg-white/5 px-3 py-2 text-left text-xs font-semibold uppercase tracking-wider text-gray-400">
      {children}
    </th>
  );
}

function Td({
  children,
  className = "",
  right = false,
}: {
  children: React.ReactNode;
  className?: string;
  right?: boolean;
}) {
  return (
    <td
      className={`border-b border-white/5 px-3 py-2 text-sm text-gray-200 ${right ? "text-right tabular-nums" : ""} ${className}`}
    >
      {children}
    </td>
  );
}

function EmptyRow({ colSpan, message }: { colSpan: number; message: string }) {
  return (
    <tr>
      <td colSpan={colSpan} className="py-10 text-center text-sm text-gray-500">
        {message}
      </td>
    </tr>
  );
}

// ─────────────────────────────────────────────
// Stage tables
// ─────────────────────────────────────────────

function Stage1Table({ rows }: { rows: Array<Record<string, unknown>> }) {
  return (
    <TableWrapper>
      <thead>
        <tr>
          <Th>시각</Th>
          <Th>종목</Th>
          <Th>등락률</Th>
          <Th>상한가잔여</Th>
          <Th>거래대금</Th>
          <Th>최고등락률</Th>
          <Th>후보구분</Th>
          <Th>단계</Th>
          <Th>결정</Th>
          <Th>사유</Th>
        </tr>
      </thead>
      <tbody>
        {rows.length === 0 ? (
          <EmptyRow colSpan={10} message="아직 데이터가 없습니다. 전략이 활성화되면 기록이 쌓입니다." />
        ) : (
          rows.map((r, i) => (
            <tr key={i} className="hover:bg-white/5">
              <Td>{fmtKst(safeStr(r.event_at ?? r.created_at))}</Td>
              <Td><StockLabel name={safeStr(r.stock_name)} code={safeStr(r.stock_code)} /></Td>
              <Td right className={pctColor(safeNum(r.change_rate_pct ?? r.change_pct))}>{fmtPct(safeNum(r.change_rate_pct ?? r.change_pct))}</Td>
              <Td right>{fmtPct(safeNum(r.distance_to_limit_pct))}</Td>
              <Td right>{fmtPrice(safeNum(r.total_trading_value_krw ?? r.trading_value_krw))}</Td>
              <Td right className={pctColor(safeNum(r.max_seen_change_pct))}>{fmtPct(safeNum(r.max_seen_change_pct))}</Td>
              <Td>{candidateScopeLabel(r.candidate_scope)}</Td>
              <Td>{safeStr(r.stage)}</Td>
              <Td>{safeStr(r.decision)}</Td>
              <Td className="max-w-xs truncate text-gray-400">{safeStr(r.reason ?? r.reason_text)}</Td>
            </tr>
          ))
        )}
      </tbody>
    </TableWrapper>
  );
}

function Stage2Table({ rows }: { rows: Array<Record<string, unknown>> }) {
  return (
    <TableWrapper>
      <thead>
        <tr>
          <Th>시각</Th>
          <Th>종목</Th>
          <Th>단계</Th>
          <Th>결정</Th>
          <Th>사유</Th>
        </tr>
      </thead>
      <tbody>
        {rows.length === 0 ? (
          <EmptyRow colSpan={5} message="아직 데이터가 없습니다. 전략이 활성화되면 기록이 쌓입니다." />
        ) : (
          rows.map((r, i) => (
            <tr key={i} className="hover:bg-white/5">
              <Td>{fmtKst(safeStr(r.event_at ?? r.created_at))}</Td>
              <Td><StockLabel name={safeStr(r.stock_name)} code={safeStr(r.stock_code)} /></Td>
              <Td>{safeStr(r.stage)}</Td>
              <Td>{safeStr(r.decision)}</Td>
              <Td className="max-w-xs truncate text-gray-400">{safeStr(r.reason ?? r.reason_text)}</Td>
            </tr>
          ))
        )}
      </tbody>
    </TableWrapper>
  );
}

function Stage3Table({ rows }: { rows: Array<Record<string, unknown>> }) {
  return (
    <TableWrapper>
      <thead>
        <tr>
          <Th>주문ID</Th>
          <Th>종목</Th>
          <Th>상태</Th>
          <Th>주문가</Th>
          <Th>체결가</Th>
          <Th>체결량</Th>
          <Th>주문시각</Th>
          <Th>체결시각</Th>
        </tr>
      </thead>
      <tbody>
        {rows.length === 0 ? (
          <EmptyRow colSpan={8} message="아직 데이터가 없습니다. 전략이 활성화되면 기록이 쌓입니다." />
        ) : (
          rows.map((r, i) => (
            <tr key={i} className="hover:bg-white/5">
              <Td>{safeStr(r.order_id)}</Td>
              <Td><StockLabel name={safeStr(r.stock_name)} code={safeStr(r.stock_code)} /></Td>
              <Td>{safeStr(r.status)}</Td>
              <Td right>{fmtPrice(safeNum(r.order_price))}</Td>
              <Td right>{fmtPrice(safeNum(r.fill_price ?? r.filled_price ?? r.executed_price))}</Td>
              <Td right>{fmtCount(safeNum(r.fill_qty ?? r.filled_quantity ?? r.executed_qty))}</Td>
              <Td>{fmtKst(safeStr(r.ordered_at ?? r.created_at))}</Td>
              <Td>{fmtKst(safeStr(r.filled_at ?? r.executed_at))}</Td>
            </tr>
          ))
        )}
      </tbody>
    </TableWrapper>
  );
}

function Stage4Table({ rows }: { rows: Array<Record<string, unknown>> }) {
  return (
    <TableWrapper>
      <thead>
        <tr>
          <Th>종목</Th>
          <Th>매수가</Th>
          <Th>현재가</Th>
          <Th>손익률</Th>
          <Th>보유량</Th>
          <Th>손절가</Th>
          <Th>목표가</Th>
          <Th>매수일</Th>
        </tr>
      </thead>
      <tbody>
        {rows.length === 0 ? (
          <EmptyRow colSpan={8} message="보유 중인 포지션이 없습니다." />
        ) : (
          rows.map((r, i) => {
            const pnl = safeNum(r.unrealized_pnl_pct ?? r.pnl_pct);
            return (
              <tr key={i} className="hover:bg-white/5">
                <Td><StockLabel name={safeStr(r.stock_name)} code={safeStr(r.stock_code)} /></Td>
                <Td right>{fmtPrice(safeNum(r.buy_price ?? r.avg_price ?? r.entry_price))}</Td>
                <Td right>{fmtPrice(safeNum(r.current_price))}</Td>
                <Td right className={pctColor(pnl)}>
                  {fmtPct(pnl)}
                </Td>
                <Td right>{fmtCount(safeNum(r.quantity ?? r.qty ?? r.remaining_qty))}</Td>
                <Td right className="text-blue-400">
                  {fmtPrice(safeNum(r.stop_loss_price))}
                </Td>
                <Td right className="text-red-400">
                  {fmtPrice(safeNum(r.take_profit_price))}
                </Td>
                <Td>{fmtKst(safeStr(r.bought_at ?? r.created_at ?? r.entry_date))}</Td>
              </tr>
            );
          })
        )}
      </tbody>
    </TableWrapper>
  );
}

function Stage5Table({ rows }: { rows: Array<Record<string, unknown>> }) {
  return (
    <TableWrapper>
      <thead>
        <tr>
          <Th>주문ID</Th>
          <Th>종목</Th>
          <Th>상태</Th>
          <Th>청산사유</Th>
          <Th>체결가</Th>
          <Th>체결량</Th>
          <Th>체결시각</Th>
        </tr>
      </thead>
      <tbody>
        {rows.length === 0 ? (
          <EmptyRow colSpan={7} message="아직 데이터가 없습니다. 전략이 활성화되면 기록이 쌓입니다." />
        ) : (
          rows.map((r, i) => (
            <tr key={i} className="hover:bg-white/5">
              <Td>{safeStr(r.order_id)}</Td>
              <Td><StockLabel name={safeStr(r.stock_name)} code={safeStr(r.stock_code)} /></Td>
              <Td>{safeStr(r.status)}</Td>
              <Td className="text-gray-400">{safeStr(r.exit_reason)}</Td>
              <Td right>{fmtPrice(safeNum(r.fill_price ?? r.filled_price ?? r.executed_price))}</Td>
              <Td right>{fmtCount(safeNum(r.fill_qty ?? r.filled_quantity ?? r.executed_qty))}</Td>
              <Td>{fmtKst(safeStr(r.filled_at ?? r.executed_at))}</Td>
            </tr>
          ))
        )}
      </tbody>
    </TableWrapper>
  );
}

interface Stage6Improvement {
  priority: string;
  title: string;
  evidence: string;
  action: string;
}

interface Stage6Summary {
  total_sells: number | null;
  win_count: number | null;
  loss_count: number | null;
  win_rate: number | null;
  total_pnl: number | null;
  avg_pnl_pct: number | null;
  review_basis?: string;
  improvement_items?: Stage6Improvement[];
}

function isSummary(v: unknown): v is Stage6Summary {
  return v != null && typeof v === "object";
}

function Stage6Panel({ cardId, summary, rows }: { cardId: number; summary: unknown; rows: Array<Record<string, unknown>> }) {
  const s = isSummary(summary) ? (summary as Stage6Summary) : null;
  const wr = s ? safeNum(s.win_rate) : null;
  const avgPnl = s ? safeNum(s.avg_pnl_pct) : null;
  const totalPnl = s ? safeNum(s.total_pnl) : null;
  const improvements = s && Array.isArray(s.improvement_items) ? s.improvement_items : [];

  return (
    <div className="space-y-4">
      {s && (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
          {[
            { label: "총 매도", value: fmtCount(safeNum(s.total_sells)) },
            { label: "익절", value: fmtCount(safeNum(s.win_count)), cls: "text-red-400" },
            { label: "손절", value: fmtCount(safeNum(s.loss_count)), cls: "text-blue-400" },
            {
              label: "승률",
              value: wr != null ? `${wr.toFixed(1)}%` : "—",
              cls: wr != null && wr >= 50 ? "text-red-400" : "text-blue-400",
            },
            {
              label: "총손익",
              value: totalPnl != null ? `${totalPnl > 0 ? "+" : ""}${Math.round(totalPnl).toLocaleString("ko-KR")}원` : "—",
              cls: pctColor(totalPnl),
            },
            {
              label: "평균수익률",
              value: fmtPct(avgPnl),
              cls: pctColor(avgPnl),
            },
          ].map(({ label, value, cls }) => (
            <div key={label} className="rounded-xl border border-white/5 bg-gray-900/60 p-3 text-center">
              <p className="text-xs text-gray-500">{label}</p>
              <p className={`mt-1 text-xl font-bold tabular-nums ${cls ?? "text-gray-200"}`}>{value}</p>
            </div>
          ))}
        </div>
      )}

      {improvements.length > 0 && (
        <section className="rounded-2xl border border-amber-400/15 bg-amber-400/5 p-4">
          <h3 className="text-sm font-semibold text-amber-200">자동 복기 및 개선안</h3>
          <div className="mt-3 grid gap-3 lg:grid-cols-2">
            {improvements.map((item, index) => (
              <article key={`${item.priority}-${item.title}-${index}`} className="rounded-xl border border-white/5 bg-gray-950/40 p-3">
                <div className="flex items-center gap-2">
                  <span className="rounded-full bg-amber-400/15 px-2 py-0.5 text-[11px] font-semibold text-amber-200">
                    {item.priority}
                  </span>
                  <p className="text-sm font-semibold text-gray-100">{item.title}</p>
                </div>
                <p className="mt-2 text-xs text-gray-400">근거: {item.evidence}</p>
                <p className="mt-1 text-xs text-gray-300">조치: {item.action}</p>
              </article>
            ))}
          </div>
        </section>
      )}

      <TableWrapper>
        <thead>
          <tr>
            <Th>종목</Th>
            <Th>매도시각</Th>
            <Th>체결가</Th>
            <Th>체결량</Th>
            <Th>손익률</Th>
            <Th>청산사유</Th>
            <Th>건별 복기</Th>
            <Th>매매일지</Th>
          </tr>
        </thead>
        <tbody>
          {rows.length === 0 ? (
            <EmptyRow colSpan={8} message="아직 데이터가 없습니다. 전략이 활성화되면 기록이 쌓입니다." />
          ) : (
            rows.slice(0, 10).map((r, i) => {
              const pnl = safeNum(r.realized_pnl_pct ?? r.pnl_pct);
              const journalDate = safeStr(r.sold_at ?? r.executed_at ?? r.traded_at).slice(0, 10);
              return (
                <tr key={i} className="hover:bg-white/5">
                  <Td><StockLabel name={safeStr(r.stock_name)} code={safeStr(r.stock_code)} /></Td>
                  <Td>{fmtKst(safeStr(r.sold_at ?? r.executed_at ?? r.traded_at))}</Td>
                  <Td right>{fmtPrice(safeNum(r.sell_price ?? r.price))}</Td>
                  <Td right>{fmtCount(safeNum(r.qty ?? r.quantity))}</Td>
                  <Td right className={pctColor(pnl)}>
                    {fmtPct(pnl)}
                  </Td>
                  <Td className="text-gray-400">{safeStr(r.exit_reason)}</Td>
                  <Td className="max-w-xs text-xs text-gray-400">
                    <span className={pnl != null && pnl > 0 ? "text-red-300" : "text-blue-300"}>
                      {safeStr(r.review_result)}
                    </span>
                    <p className="mt-1 whitespace-normal">{safeStr(r.review_note)}</p>
                    <p className="mt-1 whitespace-normal text-cyan-300">파동: {waveReviewText(r.wave_review)}</p>
                  </Td>
                  <Td>
                    <button
                      onClick={() => window.open(
                        `/go100/strategies/${cardId}/trade-journal?stock_code=${safeStr(r.stock_code)}&trade_date=${journalDate}`,
                        '_blank',
                        'noopener'
                      )}
                      className="rounded-lg bg-indigo-500/15 px-2.5 py-1 text-xs font-medium text-indigo-300 transition-colors hover:bg-indigo-500/30"
                    >
                      일지
                    </button>
                  </Td>
                </tr>
              );
            })
          )}
        </tbody>
      </TableWrapper>
    </div>
  );
}

// ─────────────────────────────────────────────
// Lifecycle view
// ─────────────────────────────────────────────

function LifecycleTable({ items }: { items: LifecycleItem[] }) {
  if (items.length === 0) {
    return (
      <div className="rounded-2xl border border-white/5 bg-white/5 py-16 text-center">
        <p className="text-sm text-gray-500">아직 데이터가 없습니다. 전략이 활성화되면 기록이 쌓입니다.</p>
      </div>
    );
  }

  return (
    <TableWrapper>
      <thead>
        <tr>
          <Th>종목</Th>
          <Th>매수가</Th>
          <Th>매수시각</Th>
          <Th>포지션상태</Th>
          <Th>손절가</Th>
          <Th>목표가</Th>
          <Th>청산사유</Th>
          <Th>파동복기</Th>
          <Th>매도시각</Th>
          <Th>손익률</Th>
        </tr>
      </thead>
      <tbody>
        {items.map((item, i) => {
          const linked = item.position_id != null;
          const pnl = item.realized_pnl_pct ?? item.unrealized_pnl_pct;
          return (
            <tr key={i} className="hover:bg-white/5">
              <Td><StockLabel name={item.stock_name} code={item.stock_code} /></Td>
              <Td right>{fmtPrice(item.buy_price)}</Td>
              <Td>{fmtKst(item.bought_at)}</Td>
              <Td>
                {linked ? (
                  <span className="rounded-full bg-blue-500/15 px-2 py-0.5 text-xs text-blue-300">
                    {item.position_status ?? "연결됨"}
                  </span>
                ) : (
                  <span className="rounded-full bg-gray-500/20 px-2 py-0.5 text-xs text-gray-500">연결 불가</span>
                )}
              </Td>
              <Td right className="text-blue-400">
                {linked ? fmtPrice(item.stop_loss_price) : "연결 불가"}
              </Td>
              <Td right className="text-red-400">
                {linked ? fmtPrice(item.take_profit_price) : "연결 불가"}
              </Td>
              <Td className="text-gray-400">{item.exit_reason ?? "—"}</Td>
              <Td className="min-w-[180px] text-xs text-gray-300">{waveReviewText(item.wave_review)}</Td>
              <Td>{fmtKst(item.sold_at)}</Td>
              <Td right className={pctColor(pnl)}>
                {fmtPct(pnl)}
              </Td>
            </tr>
          );
        })}
      </tbody>
    </TableWrapper>
  );
}

// ─────────────────────────────────────────────
// Threshold panel
// ─────────────────────────────────────────────

function ThresholdPanel({ thresholds }: { thresholds: CardThresholds }) {
  const [open, setOpen] = useState(false);

  const items: Array<{ label: string; value: string }> = [
    { label: "손절 기준", value: thresholds.stop_loss_pct != null ? `${thresholds.stop_loss_pct}%` : "전략에 미설정" },
    { label: "익절 기준", value: thresholds.take_profit_pct != null ? `${thresholds.take_profit_pct}%` : "전략에 미설정" },
    { label: "추적 손절", value: thresholds.trailing_stop_pct != null ? `${thresholds.trailing_stop_pct}%` : "전략에 미설정" },
    { label: "시간 청산", value: thresholds.time_exit ?? "전략에 미설정" },
    { label: "최대 보유일", value: thresholds.holding_days != null ? `${thresholds.holding_days}일` : "전략에 미설정" },
    { label: "최대 종목수", value: thresholds.max_position_count != null ? `${thresholds.max_position_count}개` : "전략에 미설정" },
  ];

  return (
    <div className="rounded-2xl border border-white/5 bg-gray-800/40">
      <button
        type="button"
        onClick={() => setOpen((p) => !p)}
        aria-expanded={open}
        className="flex w-full items-center justify-between px-5 py-4 text-left"
      >
        <span className="text-sm font-semibold text-gray-300">전략 임계값</span>
        <svg
          className={`h-4 w-4 text-gray-500 transition-transform ${open ? "rotate-180" : ""}`}
          fill="none"
          viewBox="0 0 24 24"
          stroke="currentColor"
          strokeWidth={2}
          aria-hidden="true"
        >
          <path strokeLinecap="round" strokeLinejoin="round" d="M19 9l-7 7-7-7" />
        </svg>
      </button>

      {open && (
        <div className="grid grid-cols-2 gap-x-6 gap-y-0 border-t border-white/5 px-5 pb-4 sm:grid-cols-3">
          {items.map(({ label, value }) => (
            <div key={label} className="border-b border-white/5 py-3 last:border-0">
              <p className="text-xs text-gray-500">{label}</p>
              <p className={`mt-0.5 text-sm font-medium ${value === "전략에 미설정" ? "text-gray-600 italic" : "text-gray-200"}`}>
                {value}
              </p>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ─────────────────────────────────────────────
// Stage pipeline card
// ─────────────────────────────────────────────

const STAGE_LABELS: Record<number, string> = {
  1: "종목선정 후보",
  2: "매수감시 후보",
  3: "매수주문/체결",
  4: "보유 포지션",
  5: "매도주문/체결",
  6: "일일 리뷰",
};

function StagePipelineCard({
  stage,
  active,
  onClick,
}: {
  stage: WorkbenchStage;
  active: boolean;
  onClick: () => void;
}) {
  const isUnavailable = stage.status === "unavailable";
  const isEmpty = stage.status === "empty";

  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={`rounded-xl border px-4 py-3 text-left transition-colors ${
        active
          ? "border-blue-500 bg-blue-500/10"
          : "border-white/10 bg-gray-900/60 hover:border-white/20 hover:bg-gray-900/80"
      }`}
    >
      <div className="mb-1 flex items-center gap-2">
        <span
          className={`inline-flex h-5 w-5 items-center justify-center rounded-full text-xs font-bold ${
            active ? "bg-blue-500 text-white" : "bg-white/10 text-gray-400"
          }`}
        >
          {stage.stage_id}
        </span>
        <span className={`text-xs font-medium ${active ? "text-blue-300" : "text-gray-400"}`}>
          {stage.label || STAGE_LABELS[stage.stage_id] || `단계 ${stage.stage_id}`}
        </span>
      </div>

      {(stage.stage_id === 1 || stage.stage_id === 2) &&
      stage.total_evaluations != null &&
      stage.unique_stocks != null ? (
        <div>
          <p
            className={`text-2xl font-bold tabular-nums ${
              isUnavailable ? "text-gray-600 line-through" : isEmpty ? "text-gray-600" : active ? "text-white" : "text-gray-200"
            }`}
          >
            {isUnavailable ? "N/A" : `${fmtCount(stage.total_evaluations)}회`}
          </p>
          <p className="mt-0.5 text-xs text-gray-500">
            {isUnavailable ? "" : `${fmtCount(stage.unique_stocks)}종목`}
          </p>
        </div>
      ) : (
        <p
          className={`text-2xl font-bold tabular-nums ${
            isUnavailable ? "text-gray-600 line-through" : isEmpty ? "text-gray-600" : active ? "text-white" : "text-gray-200"
          }`}
        >
          {isUnavailable ? "N/A" : fmtCount(stage.count)}
        </p>
      )}

      {isUnavailable && (
        <p className="mt-1 text-xs text-red-400/70">소스 불가</p>
      )}
    </button>
  );
}

// ─────────────────────────────────────────────
// Stage content panel
// ─────────────────────────────────────────────

function StageContentPanel({
  stage,
  isPaperFilterSet,
  cardId,
}: {
  stage: WorkbenchStage;
  isPaperFilterSet: boolean;
  cardId: number;
}) {
  if (stage.status === "unavailable") {
    return (
      <div className="rounded-2xl border border-red-500/20 bg-red-500/5 py-12 text-center">
        <p className="text-sm font-semibold text-red-400">데이터 소스를 사용할 수 없습니다.</p>
        <p className="mt-1 text-xs text-gray-500">진단 정보를 확인하세요.</p>
      </div>
    );
  }

  const paperNotice =
    isPaperFilterSet && !stage.is_paper_filter_applied && stage.stage_id <= 5 ? (
      <p className="mb-3 rounded-lg border border-amber-500/20 bg-amber-500/10 px-3 py-2 text-xs text-amber-300">
        이 단계는 is_paper 필터가 미적용됩니다 (소스 미지원).
      </p>
    ) : null;

  const table = (() => {
    switch (stage.stage_id) {
      case 1:
        return <Stage1Table rows={stage.rows} />;
      case 2:
        return <Stage2Table rows={stage.rows} />;
      case 3:
        return <Stage3Table rows={stage.rows} />;
      case 4:
        return <Stage4Table rows={stage.rows} />;
      case 5:
        return <Stage5Table rows={stage.rows} />;
      case 6:
        return <Stage6Panel cardId={cardId} summary={stage.summary} rows={stage.rows} />;
      default:
        return (
          <p className="text-sm text-gray-500">알 수 없는 단계입니다.</p>
        );
    }
  })();

  return (
    <div>
      {paperNotice}
      {table}
    </div>
  );
}

// ─────────────────────────────────────────────
// Diagnostics panel
// ─────────────────────────────────────────────

function DiagnosticsPanel({ diagnostics }: { diagnostics: DiagnosticItem[] }) {
  if (diagnostics.length === 0) return null;

  return (
    <div className="rounded-2xl border border-amber-500/20 bg-amber-500/5 p-4">
      <p className="mb-2 text-xs font-semibold uppercase tracking-wider text-amber-400">진단 경고</p>
      <ul className="space-y-1">
        {diagnostics.map((d, i) => (
          <li key={i} className="text-xs text-amber-200/80">
            <span className="font-medium">[{d.stage}단계 · {d.key}]</span> {d.error}
          </li>
        ))}
      </ul>
    </div>
  );
}

// ─────────────────────────────────────────────
// Main component
// ─────────────────────────────────────────────

interface TradingWorkbenchTabProps {
  cardId: number;
}

export function TradingWorkbenchTab({ cardId }: TradingWorkbenchTabProps) {
  const router = useRouter();
  const rawSearchParams = useSearchParams();
  // useSearchParams() may return null when rendered outside a Suspense boundary;
  // fall back to an empty read-only URLSearchParams so callers never need to null-check.
  const searchParams = useMemo(() => rawSearchParams ?? new URLSearchParams(), [rawSearchParams]);

  // Read params from URL
  const wbStage = Number(searchParams.get("wb_stage") ?? "1") as 1 | 2 | 3 | 4 | 5 | 6;
  const wbView = (searchParams.get("wb_view") ?? "realtime") as ViewMode;
  const wbModeFilter = (searchParams.get("wb_mode_filter") ?? "all") as ModeFilter;
  const wbDateFrom = searchParams.get("wb_date_from") ?? "";
  const wbDateTo = searchParams.get("wb_date_to") ?? "";

  // Validate and clamp stage
  const activeStage = wbStage >= 1 && wbStage <= 6 ? wbStage : 1;

  // State
  const [data, setData] = useState<WorkbenchResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const fetchSeqRef = useRef(0);

  // URL param update helper — preserves all other params
  const setParam = useCallback(
    (updates: Record<string, string>) => {
      const params = new URLSearchParams(searchParams.toString());
      for (const [k, v] of Object.entries(updates)) {
        if (v === "") {
          params.delete(k);
        } else {
          params.set(k, v);
        }
      }
      router.replace(`?${params.toString()}`, { scroll: false });
    },
    [router, searchParams]
  );

  // Fetch function
  const fetchData = useCallback(async () => {
    const fetchSeq = fetchSeqRef.current + 1;
    fetchSeqRef.current = fetchSeq;
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;

    const isPaperParam = modeFilterToIsPaper(wbModeFilter);
    const url = new URL(
      `/api/go100/strategy-cards/${cardId}/workbench`,
      typeof window !== "undefined" ? window.location.origin : "http://localhost:8002"
    );
    url.searchParams.set("mode", wbView);
    if (isPaperParam) url.searchParams.set("is_paper", isPaperParam);
    if (wbView === "date_range") {
      if (wbDateFrom) url.searchParams.set("date_from", wbDateFrom);
      if (wbDateTo) url.searchParams.set("date_to", wbDateTo);
    }

    try {
      const authOptions = getAuthFetchOptions();
      const res = await fetch(url.toString(), { ...authOptions, signal: controller.signal });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const json = (await res.json()) as WorkbenchResponse;
      if (fetchSeq !== fetchSeqRef.current) return;
      setData(json);
      setError(null);
    } catch (e) {
      if (e instanceof DOMException && e.name === "AbortError") return;
      if (fetchSeq !== fetchSeqRef.current) return;
      setError(e instanceof Error ? e.message : "알 수 없는 오류");
    } finally {
      if (fetchSeq === fetchSeqRef.current) {
        abortRef.current = null;
        setLoading(false);
      }
    }
  }, [cardId, wbView, wbModeFilter, wbDateFrom, wbDateTo]);

  // Initial fetch + view/filter change
  useEffect(() => {
    setLoading(true);
    void fetchData();
  }, [fetchData]);

  useEffect(() => {
    return () => abortRef.current?.abort();
  }, []);

  // Polling for realtime mode
  useEffect(() => {
    if (wbView !== "realtime") {
      if (intervalRef.current != null) {
        clearInterval(intervalRef.current);
        intervalRef.current = null;
      }
      return;
    }

    const startPolling = () => {
      if (intervalRef.current != null) return;
      intervalRef.current = setInterval(() => {
        if (document.visibilityState !== "hidden") {
          void fetchData();
        }
      }, 30_000);
    };

    const stopPolling = () => {
      if (intervalRef.current != null) {
        clearInterval(intervalRef.current);
        intervalRef.current = null;
      }
    };

    const handleVisibility = () => {
      if (document.visibilityState === "hidden") {
        stopPolling();
      } else {
        startPolling();
      }
    };

    startPolling();
    document.addEventListener("visibilitychange", handleVisibility);

    return () => {
      stopPolling();
      document.removeEventListener("visibilitychange", handleVisibility);
    };
  }, [wbView, fetchData]);

  // Derived
  const isPaperFilterSet = wbModeFilter !== "all";
  const activeStageData = data?.stages.find((s) => s.stage_id === activeStage) ?? null;
  const perf = data?.performance;
  const perfLabel = perf?.elapsed_ms != null
    ? `${Math.round(perf.elapsed_ms).toLocaleString("ko-KR")}ms${perf.cache_hit ? " · 캐시" : ""}`
    : null;

  // View mode tab config
  const viewModes: Array<{ key: ViewMode; label: string }> = [
    { key: "realtime", label: "실시간" },
    { key: "cumulative", label: "누적" },
    { key: "date_range", label: "기간별" },
    { key: "lifecycle", label: "생애주기" },
  ];

  const filterModes: Array<{ key: ModeFilter; label: string }> = [
    { key: "all", label: "전체" },
    { key: "live", label: "실매매" },
    { key: "paper", label: "모의매매" },
  ];

  return (
    <div className="space-y-5">
      {/* ── Header ── */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold text-gray-100">매매운영 워크벤치</h2>
          {data?.checked_at && (
            <p className="mt-0.5 text-xs text-gray-500">
              마지막 조회: {fmtKst(data.checked_at)}
              {perfLabel ? ` · 응답 ${perfLabel}` : ""}
              {loading ? " · 갱신 중" : ""}
            </p>
          )}
        </div>
        <button
          type="button"
          onClick={() => { setLoading(true); void fetchData(); }}
          aria-label="데이터 새로고침"
          className="flex items-center gap-1.5 rounded-lg border border-white/10 bg-white/5 px-3 py-1.5 text-xs text-gray-400 transition-colors hover:bg-white/10 hover:text-gray-200 disabled:opacity-50"
          disabled={loading}
        >
          <svg
            className={`h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`}
            fill="none"
            viewBox="0 0 24 24"
            stroke="currentColor"
            strokeWidth={2}
            aria-hidden="true"
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"
            />
          </svg>
          새로고침
        </button>
      </div>

      {/* ── View mode tabs ── */}
      <div className="flex flex-wrap items-center gap-0 border-b border-white/5">
        {viewModes.map(({ key, label }) => {
          const active = wbView === key;
          return (
            <button
              key={key}
              type="button"
              onClick={() => setParam({ wb_view: key })}
              className={`flex items-center gap-1.5 px-4 py-2.5 text-sm transition-colors ${
                active
                  ? "border-b-2 border-blue-400 text-white"
                  : "text-gray-400 hover:text-gray-200"
              }`}
            >
              {key === "realtime" && (
                <span
                  className={`inline-block h-1.5 w-1.5 rounded-full ${
                    active ? "animate-pulse bg-green-400" : "bg-gray-600"
                  }`}
                  aria-hidden="true"
                />
              )}
              {label}
            </button>
          );
        })}
      </div>

      {/* ── Date inputs for date_range ── */}
      {wbView === "date_range" && (
        <div className="flex flex-wrap items-center gap-3 rounded-xl border border-white/5 bg-gray-900/40 px-4 py-3">
          <label className="flex items-center gap-2 text-xs text-gray-400">
            시작일
            <input
              type="date"
              value={wbDateFrom}
              onChange={(e) => setParam({ wb_date_from: e.target.value })}
              className="rounded-lg border border-white/10 bg-gray-800 px-2 py-1 text-xs text-gray-200 focus:border-blue-500 focus:outline-none"
            />
          </label>
          <label className="flex items-center gap-2 text-xs text-gray-400">
            종료일
            <input
              type="date"
              value={wbDateTo}
              onChange={(e) => setParam({ wb_date_to: e.target.value })}
              className="rounded-lg border border-white/10 bg-gray-800 px-2 py-1 text-xs text-gray-200 focus:border-blue-500 focus:outline-none"
            />
          </label>
        </div>
      )}

      {/* ── Filter bar ── */}
      {wbView !== "lifecycle" && (
        <div className="flex items-center gap-2">
          <span className="text-xs text-gray-500">필터:</span>
          {filterModes.map(({ key, label }) => (
            <button
              key={key}
              type="button"
              onClick={() => setParam({ wb_mode_filter: key })}
              className={`rounded-lg px-3 py-1.5 text-xs font-semibold transition-colors ${
                wbModeFilter === key
                  ? "bg-blue-600 text-white"
                  : "bg-white/5 text-gray-400 hover:bg-white/10"
              }`}
            >
              {label}
            </button>
          ))}
        </div>
      )}

      {/* ── Loading / Error states ── */}
      {loading && !data && <Spinner />}

      {error && !data && (
        <div className="rounded-2xl border border-red-500/20 bg-red-500/5 py-10 text-center">
          <p className="text-sm font-semibold text-red-400">데이터를 불러올 수 없습니다.</p>
          <p className="mt-1 text-xs text-gray-500">{error}</p>
        </div>
      )}

      {error && data && (
        <div className="rounded-xl border border-amber-500/20 bg-amber-500/5 px-4 py-3 text-xs text-amber-200">
          최신 갱신 실패: {error}. 기존 조회 결과를 유지합니다.
        </div>
      )}

      {perf?.partial && (
        <div className="rounded-xl border border-amber-500/20 bg-amber-500/5 px-4 py-3 text-xs text-amber-200">
          일부 단계가 부분 응답입니다. 진단 경고를 확인하세요.
        </div>
      )}

      {data?.card && <StrategyDefinitionPanel card={data.card} />}

      {/* ── Lifecycle view ── */}
      {data && wbView === "lifecycle" && (
        <div className="rounded-2xl border border-white/5 bg-gray-800/40 p-5">
          <h3 className="mb-4 text-base font-semibold text-gray-200">매매 생애주기</h3>
          <LifecycleTable items={data.lifecycle_items ?? []} />
        </div>
      )}

      {/* ── Stage pipeline + content (non-lifecycle) ── */}
      {data && wbView !== "lifecycle" && (
        <>
          {/* Pipeline */}
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
            {data.stages.map((stage) => (
              <StagePipelineCard
                key={stage.stage_id}
                stage={stage}
                active={activeStage === stage.stage_id}
                onClick={() => setParam({ wb_stage: String(stage.stage_id) })}
              />
            ))}
          </div>

          {/* Stage content */}
          <div className="rounded-2xl border border-white/5 bg-gray-800/40 p-5">
            <h3 className="mb-4 text-base font-semibold text-gray-200">
              {activeStageData?.label ?? STAGE_LABELS[activeStage] ?? `단계 ${activeStage}`}
              <span className="ml-2 text-xs font-normal text-gray-500">
                소스: {activeStageData?.source ?? "—"}
              </span>
            </h3>

            {activeStageData ? (
              <StageContentPanel
                stage={activeStageData}
                isPaperFilterSet={isPaperFilterSet}
                cardId={cardId}
              />
            ) : (
              <p className="text-sm text-gray-500">선택한 단계 데이터를 찾을 수 없습니다.</p>
            )}
          </div>

          {/* Thresholds */}
          {data.card && <ThresholdPanel thresholds={data.card.thresholds} />}
        </>
      )}

      {/* Diagnostics */}
      {data?.diagnostics && data.diagnostics.length > 0 && (
        <DiagnosticsPanel diagnostics={data.diagnostics} />
      )}
    </div>
  );
}
