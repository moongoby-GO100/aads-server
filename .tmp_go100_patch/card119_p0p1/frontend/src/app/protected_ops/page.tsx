"use client";
// GO100-STRATEGY-OPS-UI-003, 2026-07-22 — 전략별 매매 운영 페이지 (전용 경로)

import { Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import Link from "next/link";
import { StockLabel } from "@/components/common/StockLabel";
import WaveStatePanel from "@/go100/components/WaveStatePanel";
import {
  getCardWorkbench,
  getCardTradeValueWindows,
  getImprovementProposals,
  createImprovementProposal,
  updateImprovementProposal,
  getDailyResults,
  recomputeDailyResults,
} from "@/go100/api/cardTradesApi";
import type {
  WorkbenchData,
  WorkbenchViewMode,
  WorkbenchStage,
  WorkbenchStageRow,
  WorkbenchLifecycleItem,
  ImprovementProposal,
  ProposalsResponse,
  DailyResult,
  DailyResultsResponse,
  DailyResultMode,
  DataQualitySummary,
  DataQualityStatus,
  DataQualitySource,
  WorkbenchTradeValueWindow,
  CardTradeValueWindowsResponse,
} from "@/go100/api/cardTradesApi";

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
      second: "2-digit",
    });
  } catch {
    return "—";
  }
}

function fmtTimeOnly(iso: string | null | undefined): string {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleTimeString("ko-KR", {
      timeZone: "Asia/Seoul",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
  } catch {
    return "—";
  }
}

function sourceFreshness(sourceTs?: string | null, receivedAt?: string | null): { label: string; delayed: boolean } {
  if (!sourceTs || !receivedAt) return { label: "미수집", delayed: true };
  const lagSeconds = Math.max(0, (new Date(receivedAt).getTime() - new Date(sourceTs).getTime()) / 1000);
  return {
    label: `${lagSeconds.toFixed(1)}초`,
    delayed: lagSeconds > 5,
  };
}

function fmtPrice(v: number | null | undefined): string {
  if (v == null) return "—";
  return `${Number(v).toLocaleString("ko-KR")}원`;
}

function fmtPct(v: number | null | undefined): string {
  if (v == null) return "—";
  return `${v > 0 ? "+" : ""}${Number(v).toFixed(2)}%`;
}

function fmtCount(v: number | null | undefined): string {
  if (v == null) return "—";
  return Number(v).toLocaleString("ko-KR");
}

function fmtTradeValue(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(Number(v))) return "—";
  return `${(Number(v) / 1e8).toFixed(1)}억`;
}

function fmtRank(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(Number(v))) return "—";
  return `${Number(v).toLocaleString("ko-KR")}위`;
}

function fmtTradeSource(v: unknown): string {
  const raw = safeStr(v).toUpperCase();
  if (!raw || raw === "—") return "소스없음";
  if (raw === "NXT" || raw === "NEXTTRADE") return "NXT";
  if (raw === "KIWOOM") return "키움";
  if (raw === "KIS") return "KIS";
  if (raw === "KRX" || raw === "MXT") return "KRX";
  if (raw === "NXT_NOT_COLLECTED") return "NXT 미수집";
  return raw;
}

function pctColor(v: number | null | undefined): string {
  if (v == null) return "text-gray-400";
  return v > 0 ? "text-red-400" : v < 0 ? "text-blue-400" : "text-gray-400";
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

type Card119ScopeKind = "current" | "cumulative" | "preopen" | "other";

function card119ScopeKind(scope: unknown): Card119ScopeKind {
  const raw = safeStr(scope);
  if (raw === "current_snapshot_watch" || raw === "current_snapshot_ge20") return "current";
  if (raw === "today_cumulative_watch" || raw === "today_cumulative_ge20") return "cumulative";
  if (raw === "preopen_expected_watch") return "preopen";
  return "other";
}

// ─────────────────────────────────────────────
// URL helpers
// ─────────────────────────────────────────────

type ViewMode = WorkbenchViewMode;
type ModeFilter = "all" | "live" | "paper";

function filterToIsPaper(f: ModeFilter): boolean | undefined {
  if (f === "live") return false;
  if (f === "paper") return true;
  return undefined;
}

// ─────────────────────────────────────────────
// UI atoms
// ─────────────────────────────────────────────

function Spinner({ size = "md" }: { size?: "sm" | "md" }) {
  const s = size === "sm" ? "h-4 w-4" : "h-8 w-8";
  return (
    <div className="flex items-center justify-center py-12">
      <div className={`${s} animate-spin rounded-full border-2 border-blue-500 border-t-transparent`} />
    </div>
  );
}

function TW({ children }: { children: React.ReactNode }) {
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

function KpiCard({
  label,
  value,
  sub,
  accent,
}: {
  label: string;
  value: string;
  sub?: string;
  accent?: string;
}) {
  return (
    <div className="rounded-xl border border-white/5 bg-[#0c1523] p-4">
      <p className="text-xs text-gray-500">{label}</p>
      <p className={`mt-1 text-2xl font-black tabular-nums ${accent ?? "text-white"}`}>{value}</p>
      {sub && <p className="mt-0.5 text-xs text-gray-400">{sub}</p>}
    </div>
  );
}

// ─────────────────────────────────────────────
// Stage tables
// ─────────────────────────────────────────────

function FreshnessBadge({ status }: { status?: string | null }) {
  if (!status) return <span className="text-gray-600 text-[10px]">—</span>;
  const map: Record<string, string> = {
    fresh: "text-green-400",
    ok: "text-teal-400",
    stale: "text-amber-400",
    missing: "text-red-400",
  };
  const labelMap: Record<string, string> = {
    fresh: "신선",
    ok: "보통",
    stale: "지연",
    missing: "없음",
  };
  return (
    <span className={`text-[10px] font-medium ${map[status] ?? "text-gray-400"}`}>
      {labelMap[status] ?? status}
    </span>
  );
}

type SortDir = "asc" | "desc";

function SortableTh({
  children,
  sortKey,
  activeKey,
  dir,
  onSort,
}: {
  children: React.ReactNode;
  sortKey: string;
  activeKey: string;
  dir: SortDir;
  onSort: (key: string) => void;
}) {
  const active = sortKey === activeKey;
  return (
    <Th>
      <button
        type="button"
        onClick={() => onSort(sortKey)}
        className="flex w-full items-center gap-1 whitespace-nowrap text-left"
      >
        <span>{children}</span>
        <span className={`text-[9px] ${active ? "text-blue-300" : "text-gray-600"}`}>
          {active ? (dir === "asc" ? "▲" : "▼") : "↕"}
        </span>
      </button>
    </Th>
  );
}

function stage1SortValue(row: WorkbenchStageRow, key: string): string | number | null {
  const record = row as Record<string, unknown>;
  const windows = row.trade_value_windows ?? {};
  const windowKeyMap: Record<string, string> = {
    open_5m_trade_value: "open_5m",
    open_10m_trade_value: "open_10m",
    open_30m_trade_value: "open_30m",
    open_60m_trade_value: "open_60m",
    recent_5m_trade_value: "recent_5m",
    recent_10m_trade_value: "recent_10m",
    recent_30m_trade_value: "recent_30m",
    recent_60m_trade_value: "recent_60m",
  };
  if (windowKeyMap[key]) {
    return safeNum(windows[windowKeyMap[key]]?.trade_value_krw);
  }
  if (key === "stock") {
    return safeStr(row.display_name ?? row.stock_name ?? row.stock_code);
  }
  if (key === "current_price") {
    return safeNum(row.current_price);
  }
  if (key === "total_trading_value") {
    return safeNum(row.total_trading_value_krw ?? row.trading_value_krw);
  }
  if (key === "decision") {
    return safeStr(row.decision);
  }
  if (key === "threshold") {
    const passes = Object.values(row.threshold_passes ?? {});
    return passes.filter(Boolean).length;
  }
  if (key === "supply_reason") {
    return safeNum(row.total_trading_value_krw ?? row.trading_value_krw) ?? safeStr(row.reason_text);
  }
  if (key === "freshness") {
    return safeNum(row.quote_age_sec);
  }
  const raw = record[key];
  return safeNum(raw) ?? (raw == null ? null : String(raw));
}

function Stage1TradeValueWindowsCell({ windows }: { windows?: Record<string, WorkbenchTradeValueWindow> | null }) {
  if (!windows || Object.keys(windows).length === 0) {
    return (
      <div className="min-w-[180px] rounded border border-amber-500/20 bg-amber-500/5 px-2 py-1 text-[10px] leading-tight text-amber-300">
        시간대 거래대금 계산 대기
        <div className="mt-0.5 text-gray-500">메인 목록은 먼저 표시됩니다.</div>
      </div>
    );
  }
  const groups = [
    {
      label: "장시작",
      items: [
        ["open_5m", "5분"],
        ["open_10m", "10분"],
        ["open_30m", "30분"],
        ["open_60m", "1시간"],
      ],
    },
    {
      label: "최근",
      items: [
        ["recent_5m", "5분"],
        ["recent_10m", "10분"],
        ["recent_30m", "30분"],
        ["recent_60m", "1시간"],
      ],
    },
  ] as const;
  return (
    <div className="min-w-[220px] space-y-1 text-[10px] leading-tight">
      {groups.map((group) => (
        <div key={group.label}>
          <div className="mb-0.5 text-gray-600">{group.label}</div>
          <div className="grid grid-cols-4 gap-1">
            {group.items.map(([key, label]) => {
              const item = windows?.[key];
              return (
                <div key={key} className="rounded border border-white/5 bg-white/[0.02] px-1.5 py-1 text-right">
                  <div className="text-gray-500">{label}</div>
                  <div className="font-medium text-gray-200">{fmtTradeValue(safeNum(item?.trade_value_krw))}</div>
                  <div className="text-gray-500">{fmtRank(safeNum(item?.rank))}</div>
                </div>
              );
            })}
          </div>
        </div>
      ))}
    </div>
  );
}

function Stage1StockLink({ row }: { row: WorkbenchStageRow }) {
  const code = safeStr(row.stock_code);
  if (!code || code === "—") {
    return <StockLabel name={row.display_name ?? safeStr(row.stock_name)} code={code} />;
  }
  return (
    <Link
      href={`/stock/${encodeURIComponent(code)}`}
      target="_blank"
      rel="noopener noreferrer"
      className="inline-flex rounded-sm hover:text-sky-300 hover:underline focus:outline-none focus:ring-1 focus:ring-sky-400"
      title="종목분석 새 탭 열기"
      aria-label={`${code} 종목분석 새 탭 열기`}
      onClick={(event) => event.stopPropagation()}
    >
      <StockLabel name={row.display_name ?? (row.stock_name !== row.stock_code ? safeStr(row.stock_name) : null)} code={code} />
    </Link>
  );
}

function compareStage1Rows(a: WorkbenchStageRow, b: WorkbenchStageRow, key: string, dir: SortDir): number {
  const av = stage1SortValue(a, key);
  const bv = stage1SortValue(b, key);
  if (av == null && bv == null) return 0;
  if (av == null) return 1;
  if (bv == null) return -1;
  const base = typeof av === "number" && typeof bv === "number"
    ? av - bv
    : String(av).localeCompare(String(bv), "ko-KR", { numeric: true });
  return dir === "asc" ? base : -base;
}

function Stage1SupplyReasonCell({
  row,
  totalTradeValue,
  marketTradeValue,
  nxtTradeValue,
  isNxt,
}: {
  row: WorkbenchStageRow;
  totalTradeValue: number | null;
  marketTradeValue: number | null;
  nxtTradeValue: number | null;
  isNxt: boolean;
}) {
  const changeRate = safeNum(row.change_rate_pct);
  const reasonText = safeStr(row.reason_text);
  const drivers: Array<{ label: string; value: string }> = [];
  if (changeRate != null) drivers.push({ label: "현재 흐름", value: `${fmtPct(changeRate)} 상승` });
  if (totalTradeValue != null) drivers.push({ label: "누적 수급", value: fmtTradeValue(totalTradeValue) });
  if (marketTradeValue != null && nxtTradeValue != null) {
    drivers.push({ label: "시장별 수급", value: `KRX ${fmtTradeValue(marketTradeValue)} / NXT ${fmtTradeValue(nxtTradeValue)}` });
  } else if (marketTradeValue != null) {
    drivers.push({ label: "시장별 수급", value: `KRX ${fmtTradeValue(marketTradeValue)}` });
  } else if (nxtTradeValue != null) {
    drivers.push({ label: "시장별 수급", value: `NXT ${fmtTradeValue(nxtTradeValue)}` });
  } else if (isNxt) {
    drivers.push({ label: "시장별 수급", value: "NXT 수급 미수집" });
  }
  if (row.intraday_change_rank != null) drivers.push({ label: "상승 순위", value: fmtRank(safeNum(row.intraday_change_rank)) });
  if (row.bullish_trade_value_rank != null) drivers.push({ label: "수급 순위", value: fmtRank(safeNum(row.bullish_trade_value_rank)) });

  return (
    <div className="max-w-sm space-y-1">
      <div className="text-xs text-gray-300">{reasonText === "—" ? "상승/수급 근거 미수집" : reasonText}</div>
      {drivers.length > 0 && (
        <div className="space-y-0.5">
          {drivers.slice(0, 4).map((driver) => (
            <div key={`${driver.label}-${driver.value}`} className="flex justify-between gap-3 text-[10px]">
              <span className="text-gray-600">{driver.label}</span>
              <span className="text-gray-400">{driver.value}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function Stage1CandidatePoolPanel({ summary, cardId }: { summary?: Record<string, unknown>; cardId?: number }) {
  const staticCount = safeNum(summary?.static_universe_count);
  const intradayCount = safeNum(summary?.intraday_top50_unique_count);
  const visibleCount = safeNum(summary?.visible_count);
  const top50Count = safeNum(summary?.top50_count);
  const rejectedCount = safeNum(summary?.rejected_count);
  const dataMissingCount = safeNum(summary?.data_missing_count);
  const qualifiedCount = safeNum(summary?.qualified_count);
  const sortOrder = safeStr(summary?.sort_order);
  const backfill = summary?.backfill as Record<string, unknown> | undefined;
  const backfillStatus = safeStr(backfill?.status);
  const backfillCount = safeNum(backfill?.enqueued_count);
  const windowSummary = summary?.trade_value_windows as Record<string, unknown> | undefined;
  const windowStatus = safeStr(windowSummary?.status);
  const isCard119 = Number(cardId) === 119;
  const currentGe20Count = safeNum(summary?.current_ge20_count);
  const cumulativeGe20Count = safeNum(summary?.cumulative_ge20_count);
  const cumulativeOnlyCount = safeNum(summary?.cumulative_only_count);
  const decisionLogsCount = safeNum(summary?.decision_logs_count);
  const strategyRunEventsCount = safeNum(summary?.strategy_run_events_count);
  const bothSourcesCount = safeNum(summary?.both_sources_count);
  const policyLabel = isCard119
    ? "#119 기준: 독립 발굴 + 등락률 20% 이상 대상후보, 실진입 27% 이상"
    : "#303 기준: 등락률 3% 이상 + 누적 거래대금 Top 50";
  if (isCard119) {
    return (
      <div className="space-y-2 rounded-md border border-white/10 bg-white/[0.02] p-3 text-xs">
        <div className="grid gap-2 sm:grid-cols-4">
          <div>
            <p className="text-[10px] text-gray-500">오늘 누적 +20% 후보</p>
            <p className="mt-0.5 font-semibold text-sky-300">{fmtCount(cumulativeGe20Count)}<span className="ml-1 text-[10px] font-normal text-gray-500">종목</span></p>
          </div>
          <div>
            <p className="text-[10px] text-gray-500">현재 +20% 유지</p>
            <p className="mt-0.5 font-semibold text-emerald-300">{fmtCount(currentGe20Count)}<span className="ml-1 text-[10px] font-normal text-gray-500">종목</span></p>
          </div>
          <div>
            <p className="text-[10px] text-gray-500">누적 전용 (20% 이탈)</p>
            <p className="mt-0.5 font-semibold text-amber-300">{fmtCount(cumulativeOnlyCount)}<span className="ml-1 text-[10px] font-normal text-gray-500">종목</span></p>
          </div>
          <div>
            <p className="text-[10px] text-gray-500">실진입 게이트</p>
            <p className="mt-0.5 font-semibold text-red-300">+27% 이상</p>
          </div>
        </div>
        {(decisionLogsCount != null || strategyRunEventsCount != null) && (
          <div className="grid gap-2 sm:grid-cols-3 border-t border-white/10 pt-2">
            <div>
              <p className="text-[10px] text-gray-500">의사결정 로그 발굴</p>
              <p className="mt-0.5 font-semibold text-indigo-300">{fmtCount(decisionLogsCount)}<span className="ml-1 text-[10px] font-normal text-gray-500">종목</span></p>
            </div>
            <div>
              <p className="text-[10px] text-gray-500">실행 이벤트 발굴</p>
              <p className="mt-0.5 font-semibold text-violet-300">{fmtCount(strategyRunEventsCount)}<span className="ml-1 text-[10px] font-normal text-gray-500">종목</span></p>
            </div>
            <div>
              <p className="text-[10px] text-gray-500">양쪽 소스 확인</p>
              <p className="mt-0.5 font-semibold text-fuchsia-300">{fmtCount(bothSourcesCount)}<span className="ml-1 text-[10px] font-normal text-gray-500">종목</span></p>
            </div>
          </div>
        )}
        <div className="flex flex-wrap gap-x-4 gap-y-1 text-[10px] text-gray-500">
          <span>{policyLabel}</span>
          {backfillStatus !== "—" && (
            <span className={backfillStatus === "failed" ? "text-red-400" : "text-amber-300"}>
              백필: {backfillStatus}
              {backfillCount != null ? ` ${backfillCount.toLocaleString("ko-KR")}건` : ""}
            </span>
          )}
        </div>
      </div>
    );
  }
  return (
    <div className="space-y-2 rounded-md border border-white/10 bg-white/[0.02] p-3 text-xs">
      <div className="grid gap-2 sm:grid-cols-4">
        <div>
          <p className="text-[10px] text-gray-500">정적 유니버스</p>
          <p className="mt-0.5 font-semibold text-gray-200">{fmtCount(staticCount)}</p>
        </div>
        <div>
          <p className="text-[10px] text-gray-500">선정대상</p>
          <p className="mt-0.5 font-semibold text-sky-300">{fmtCount(top50Count ?? intradayCount)}</p>
        </div>
        <div>
          <p className="text-[10px] text-gray-500">통과 / 탈락</p>
          <p className="mt-0.5 font-semibold text-emerald-300">
            {fmtCount(qualifiedCount)} <span className="text-[10px] font-normal text-red-300">/ {fmtCount(rejectedCount)}</span>
          </p>
        </div>
        <div>
          <p className="text-[10px] text-gray-500">데이터부족 / 시간대수급</p>
          <p className={`mt-0.5 font-semibold ${windowStatus === "available" ? "text-emerald-300" : "text-amber-300"}`}>
            {fmtCount(dataMissingCount)} / {windowStatus === "—" ? "미확인" : windowStatus}
          </p>
        </div>
      </div>
      <div className="flex flex-wrap gap-x-4 gap-y-1 text-[10px] text-gray-500">
        <span>{policyLabel}</span>
        <span>화면 표시: {fmtCount(visibleCount)}</span>
        <span>정렬: {sortOrder === "—" ? "누적 거래대금 내림차순" : sortOrder}</span>
        <span>21봉: 일반 구간 warmup, 장초반 fast-wave 4봉 예외</span>
        {backfillStatus !== "—" && (
          <span className={backfillStatus === "failed" ? "text-red-400" : "text-amber-300"}>
            백필: {backfillStatus}
            {backfillCount != null ? ` ${backfillCount.toLocaleString("ko-KR")}건` : ""}
          </span>
        )}
      </div>
    </div>
  );
}

function Stage1Table({
  rows,
  stageColumns = [],
  summary,
  cardId,
}: {
  rows: WorkbenchStageRow[];
  stageColumns?: Array<{ key: string; label: string; type: string; price_key?: string }>;
  summary?: Record<string, unknown>;
  cardId?: number;
}) {
  const isCard119 = Number(cardId) === 119;
  const isCard303 = Number(cardId) === 303;
  const pageSize = isCard119 ? 200 : isCard303 ? 50 : 30;
  const [page, setPage] = useState(1);
  const [sort, setSort] = useState<{ key: string; dir: SortDir }>({ key: "total_trading_value", dir: "desc" });
  const tradeValueKeys = useMemo(
    () => new Set([
      "trading_value_krw",
      "total_trading_value_krw",
      "market_trading_value_krw",
      "nxt_trading_value_krw",
      "intraday_change_rank",
      "bullish_trade_value_rank",
      "change_rate_pct",
    ]),
    [],
  );
  const visibleStageColumns = useMemo(
    () => stageColumns.filter((col) => !tradeValueKeys.has(col.key)),
    [stageColumns, tradeValueKeys],
  );
  const discoveryMinChangePct = isCard119 ? 20 : 3;
  const eligibleRows = useMemo(
    () => rows.filter((row) => {
      if (isCard119 || isCard303) return true;
      const changeRate = safeNum(row.change_rate_pct);
      return changeRate == null || changeRate >= discoveryMinChangePct;
    }),
    [rows, discoveryMinChangePct, isCard119, isCard303],
  );
  const currentWatchRows = useMemo(
    () => eligibleRows.filter((row) => card119ScopeKind(row.candidate_scope) === "current"),
    [eligibleRows],
  );
  const cumulativeOnlyRows = useMemo(
    () => eligibleRows.filter((row) => card119ScopeKind(row.candidate_scope) === "cumulative"),
    [eligibleRows],
  );
  const sortedRows = useMemo(
    () => [...eligibleRows].sort((a, b) => compareStage1Rows(a, b, sort.key, sort.dir)),
    [eligibleRows, sort.key, sort.dir],
  );
  const backfill = summary?.backfill as Record<string, unknown> | undefined;
  const backfillStatus = safeStr(backfill?.status);
  const backfillCount = safeNum(backfill?.enqueued_count);
  const emptyMessage = backfillStatus !== "—"
    ? `대상종목 데이터가 없습니다. 백필 큐 연결: ${backfillStatus}${backfillCount != null ? ` ${backfillCount.toLocaleString("ko-KR")}건` : ""}.`
    : "아직 데이터가 없습니다. 전략이 활성화되면 기록이 쌓입니다.";
  const totalPages = Math.max(1, Math.ceil(sortedRows.length / pageSize));
  const safePage = Math.min(page, totalPages);
  const pageRows = sortedRows.slice((safePage - 1) * pageSize, safePage * pageSize);
  const handleSort = useCallback((key: string) => {
    setSort((prev) => ({
      key,
      dir: prev.key === key && prev.dir === "desc" ? "asc" : "desc",
    }));
  }, []);

  useEffect(() => {
    setPage(1);
  }, [rows, sort.key, sort.dir]);

  const decisionColor = (d: string) => {
    if (d === "pass") return "text-green-400";
    if (d === "fail") return "text-red-400";
    if (d === "skip") return "text-gray-500";
    return "text-amber-400";
  };
  const candidateStatusLabel = (status: string | null | undefined) => {
    if (status === "qualified") return "선정 통과";
    if (status === "rejected") return "선정 탈락";
    if (status === "data_missing") return "데이터 부족";
    if (status === "stale") return "시세 지연";
    return "대기";
  };
  return (
    <div className="space-y-3">
      <Stage1CandidatePoolPanel summary={summary} cardId={cardId} />
      <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-gray-500">
        <span>
          {isCard119
            ? <>오늘 누적 후보 {eligibleRows.length.toLocaleString("ko-KR")}종목 <span className="text-emerald-400">(현재 +20% {currentWatchRows.length.toLocaleString("ko-KR")}종목</span> · <span className="text-amber-400">누적 전용 {cumulativeOnlyRows.length.toLocaleString("ko-KR")}종목)</span></>
            : isCard303
              ? <>Top50 전체 {eligibleRows.length.toLocaleString("ko-KR")}개 <span className="text-emerald-400">통과 {eligibleRows.filter((r) => r.candidate_status === "qualified").length.toLocaleString("ko-KR")}</span> · <span className="text-red-300">탈락 {eligibleRows.filter((r) => r.candidate_status === "rejected").length.toLocaleString("ko-KR")}</span> · <span className="text-amber-300">데이터부족 {eligibleRows.filter((r) => r.candidate_status === "data_missing" || r.candidate_status === "stale").length.toLocaleString("ko-KR")}</span></>
              : <>전체 대상종목 {eligibleRows.length.toLocaleString("ko-KR")}개{rows.length !== eligibleRows.length ? ` · ${discoveryMinChangePct}% 미만 ${(rows.length - eligibleRows.length).toLocaleString("ko-KR")}개 제외` : null}</>
          }
        </span>
        <span>{safePage} / {totalPages} 페이지</span>
      </div>
      <TW>
      <thead>
        <tr>
          <SortableTh sortKey="stock" activeKey={sort.key} dir={sort.dir} onSort={handleSort}>종목</SortableTh>
          {isCard119 && <Th>발굴구분</Th>}
          <SortableTh sortKey="current_price" activeKey={sort.key} dir={sort.dir} onSort={handleSort}>현재가</SortableTh>
          <SortableTh sortKey="change_rate_pct" activeKey={sort.key} dir={sort.dir} onSort={handleSort}>등락률</SortableTh>
          <SortableTh sortKey="total_trading_value" activeKey={sort.key} dir={sort.dir} onSort={handleSort}>누적 거래대금 / NXT</SortableTh>
          {!isCard119 && (
            <SortableTh sortKey="recent_5m_trade_value" activeKey={sort.key} dir={sort.dir} onSort={handleSort}>시간대 거래대금</SortableTh>
          )}
          {visibleStageColumns.map((col) => (
            <SortableTh key={col.key} sortKey={col.key} activeKey={sort.key} dir={sort.dir} onSort={handleSort}>{col.label}</SortableTh>
          ))}
          <SortableTh sortKey="decision" activeKey={sort.key} dir={sort.dir} onSort={handleSort}>단계 상태</SortableTh>
          <SortableTh sortKey="supply_reason" activeKey={sort.key} dir={sort.dir} onSort={handleSort}>상승/수급 근거</SortableTh>
          <SortableTh sortKey="freshness" activeKey={sort.key} dir={sort.dir} onSort={handleSort}>신선도</SortableTh>
        </tr>
      </thead>
      <tbody>
        {sortedRows.length === 0 ? (
          <EmptyRow colSpan={8 + visibleStageColumns.length} message={emptyMessage} />
        ) : (
          pageRows.map((r, i) => {
            const candidateStatus = r.candidate_status;
            const rowBg = candidateStatus === "rejected" ? "bg-red-500/5" : candidateStatus === "data_missing" ? "bg-amber-500/5" : "";
            const totalTradeValue = safeNum(r.total_trading_value_krw ?? r.trading_value_krw);
            const marketTradeValue = safeNum(r.market_trading_value_krw);
            const nxtTradeValue = safeNum(r.nxt_trading_value_krw);
            const isNxt = Boolean(r.is_nxt) || nxtTradeValue != null;
            const scopeKind = card119ScopeKind(r.candidate_scope);
            return (
            <tr key={`${safeStr(r.stock_code)}-${i}`} className={`hover:bg-white/5 ${rowBg}`}>
              <Td>
                <Stage1StockLink row={r} />
                {isCard119 && scopeKind === "cumulative" && (
                  <div className="text-[10px] text-amber-400 mt-0.5">
                    누적기록
                    {r.max_seen_change_pct != null ? ` 최고 +${r.max_seen_change_pct.toFixed(1)}%` : ""}
                    {r.discovery_source && card119ScopeKind(r.discovery_source) !== "cumulative" && (
                      <span className="ml-1 text-gray-500">
                        ({r.discovery_source === "both" ? "양소스"
                          : r.discovery_source === "go100_strategy_run_events" ? "실행이벤트"
                          : "의사결정로그"})
                      </span>
                    )}
                  </div>
                )}
                {isCard119 && scopeKind === "current" && (
                  <div className="text-[10px] text-emerald-400 mt-0.5">
                    현재유지
                    {r.cumulative_data_source && (
                      <span className="ml-1 text-gray-500">
                        (누적:{r.cumulative_data_source === "both" ? "양소스"
                          : r.cumulative_data_source === "go100_strategy_run_events" ? "실행이벤트"
                          : "의사결정로그"})
                      </span>
                    )}
                  </div>
                )}
		                {r.missing_reason && (
		                  <div className="text-[10px] text-amber-500 mt-0.5">{r.missing_reason}</div>
		                )}
              </Td>
              {isCard119 && (
                <Td>
                  <div className="space-y-1 text-xs">
                    <span className={`inline-flex rounded border px-1.5 py-0.5 text-[10px] font-semibold ${scopeKind === "cumulative" ? "border-amber-400/30 bg-amber-400/10 text-amber-300" : scopeKind === "preopen" ? "border-sky-400/30 bg-sky-400/10 text-sky-300" : "border-emerald-400/30 bg-emerald-400/10 text-emerald-300"}`}>
                      {scopeKind === "cumulative" ? "누적 전용" : scopeKind === "preopen" ? "장전 예상" : "현재 +20%"}
                    </span>
                    <div className="text-[10px] leading-tight text-gray-500">
                      {scopeKind === "cumulative"
                        ? `최고 ${fmtPct(safeNum(r.max_seen_change_pct))}`
                        : scopeKind === "preopen"
                          ? "장전 예상 후보"
                          : "실시간 후보"}
                    </div>
                    {r.last_seen && <div className="text-[10px] text-gray-600">최근 {fmtTimeOnly(r.last_seen)}</div>}
                  </div>
                </Td>
              )}
              <Td right>
                {r.current_price != null ? (
                  <div className="font-medium text-gray-200">{fmtPrice(safeNum(r.current_price))}</div>
                ) : <span className="text-gray-600">—</span>}
              </Td>
              <Td right>
                <span className={pctColor(safeNum(r.change_rate_pct))}>{fmtPct(safeNum(r.change_rate_pct))}</span>
              </Td>
              <Td right>
                <div className="flex flex-col items-end gap-0.5 text-[10px] leading-tight">
                  <span className="text-xs font-medium text-gray-100">합계 {fmtTradeValue(totalTradeValue)}</span>
                  <span className="text-gray-500">
                    KRX {fmtTradeValue(marketTradeValue)}
                    <span className="ml-1 text-gray-600">{fmtTradeSource(r.market_trading_value_source)}</span>
                  </span>
                  {isNxt && (
                    <span className={nxtTradeValue != null ? "text-sky-300" : "text-amber-500"}>
                      NXT {nxtTradeValue != null ? fmtTradeValue(nxtTradeValue) : "미수집"}
                      <span className="ml-1 text-gray-500">{fmtTradeSource(r.nxt_trading_value_source)}</span>
                    </span>
                  )}
                  {isNxt && r.nxt_trading_value_quote_time && (
                    <span className="text-gray-600">NXT시각 {fmtTimeOnly(r.nxt_trading_value_quote_time)}</span>
                  )}
                </div>
              </Td>
              {!isCard119 && (
                <Td>
                  <Stage1TradeValueWindowsCell windows={r.trade_value_windows} />
                </Td>
              )}
              {visibleStageColumns.map((col) => {
                const rv = (r as Record<string, unknown>)[col.key];
                return (
                  <Td key={col.key} right>
                    {col.type === "price" ? (
                      rv != null ? <span className="text-xs text-red-300">{fmtPrice(safeNum(rv as number))}</span> : <span className="text-gray-600">—</span>
                    ) : col.type === "pct" ? (
                      <span className={pctColor(safeNum(rv as number))}>{fmtPct(safeNum(rv as number))}</span>
                    ) : col.type === "pct_with_price" ? (
                      rv != null ? (
                        <>
                          <div className="text-xs text-amber-300">{fmtPrice(safeNum((r as Record<string, unknown>)[col.price_key ?? ""] as number))}</div>
                          <div className="text-[10px] text-gray-500">{fmtPct(safeNum(rv as number))}</div>
                        </>
                      ) : <span className="text-gray-600">—</span>
                    ) : col.type === "time" ? (
                      rv != null ? <span className="text-xs tabular-nums text-gray-300">{fmtTimeOnly(String(rv))}</span> : <span className="text-gray-600">—</span>
                    ) : col.type === "number" ? (
                      rv != null ? <span className="text-xs">{Number(rv).toLocaleString("ko-KR")}</span> : <span className="text-gray-600">—</span>
                    ) : col.type === "trade_value" ? (
                      rv != null ? <span className="text-xs">{fmtTradeValue(safeNum(rv as number))}</span> : <span className="text-gray-600">—</span>
                    ) : <span className="text-gray-600">{String(rv ?? "—")}</span>}
                  </Td>
                );
              })}
              <Td>
                <span className={`font-medium text-sm ${decisionColor(safeStr(r.decision))}`}>
                  {(isCard303 || isCard119) ? candidateStatusLabel(candidateStatus) : safeStr(r.decision) === "pass" ? "선정 통과" : safeStr(r.decision) === "fail" ? "선정 탈락" : safeStr(r.decision) || "—"}
                </span>
                {(isCard303 || isCard119) && (
                  <div className="mt-1 text-[10px] leading-snug text-gray-500">
                    {(r.candidate_rejection_reasons && r.candidate_rejection_reasons.length > 0)
                      ? r.candidate_rejection_reasons.join(" · ")
                      : r.detailed_reason || r.reason_text || "조건 확인 대기"}
                  </div>
                )}
              </Td>
              <Td>
                <Stage1SupplyReasonCell
                  row={r}
                  totalTradeValue={totalTradeValue}
                  marketTradeValue={marketTradeValue}
                  nxtTradeValue={nxtTradeValue}
                  isNxt={isNxt}
                />
              </Td>
              <Td>
                <FreshnessBadge status={r.freshness_status} />
                {r.quote_age_sec != null && (
                  <div className="text-[10px] text-gray-600">{Math.round(r.quote_age_sec)}초 전</div>
                )}
              </Td>
            </tr>
            );
          })
        )}
      </tbody>
      </TW>
      {eligibleRows.length > pageSize && (
        <div className="flex flex-wrap items-center justify-end gap-2">
          <button
            type="button"
            onClick={() => setPage((prev) => Math.max(1, prev - 1))}
            disabled={safePage <= 1}
            className="rounded-md border border-white/10 px-3 py-1 text-xs text-gray-300 disabled:cursor-not-allowed disabled:opacity-40"
          >
            이전
          </button>
          <button
            type="button"
            onClick={() => setPage((prev) => Math.min(totalPages, prev + 1))}
            disabled={safePage >= totalPages}
            className="rounded-md border border-white/10 px-3 py-1 text-xs text-gray-300 disabled:cursor-not-allowed disabled:opacity-40"
          >
            다음
          </button>
        </div>
      )}
    </div>
  );
}

function ScoreBar({ score, max }: { score: number; max: number }) {
  const pct = max > 0 ? Math.min(100, Math.round((score / max) * 100)) : 0;
  const color = pct >= 70 ? "bg-green-500" : pct >= 40 ? "bg-amber-500" : "bg-gray-600";
  return (
    <div className="flex items-center gap-1.5">
      <div className="h-1.5 w-16 rounded-full bg-white/10 overflow-hidden">
        <div className={`h-full rounded-full ${color}`} style={{ width: `${pct}%` }} />
      </div>
      <span className="tabular-nums text-xs text-gray-300">{score}</span>
    </div>
  );
}

function Stage2Table({ rows }: { rows: WorkbenchStageRow[] }) {
  const MAX_SCORE = 100;
  const keyLabel: Record<string, string> = {
    hard_gate: "하드게이트",
    momentum: "모멘텀",
    trade_value: "거래대금",
    freshness: "신선도",
    session_evidence: "NXT/오픈",
    volume: "거래량",
    strength: "체결강도",
    nxt_evidence: "NXT",
    volatility_penalty: "변동리스크",
  };
  return (
    <TW>
      <thead>
        <tr>
          <Th>순위</Th>
          <Th>종목</Th>
          <Th>점수</Th>
          <Th>상태</Th>
          <Th>현재가 / 등락률</Th>
          <Th>매수트리거</Th>
          <Th>주문준비</Th>
          <Th>통과/탈락 사유</Th>
          <Th>구성요소</Th>
          <Th>시각</Th>
        </tr>
      </thead>
      <tbody>
        {rows.length === 0 ? (
          <EmptyRow colSpan={10} message="매수대기 중인 종목이 없습니다. 전략이 활성화되면 기록이 쌓입니다." />
        ) : (
          rows.map((r, i) => {
            const freshness = sourceFreshness(r.source_ts, r.received_at);
            const score = safeNum(r.total_score) ?? 0;
            const rank = safeNum(r.priority_rank) ?? (i + 1);
            const passReasons = Array.isArray(r.pass_reasons) ? r.pass_reasons : [];
            const failReasons = Array.isArray(r.fail_reasons) ? r.fail_reasons : [];
            const missingData = Array.isArray(r.missing_data) ? r.missing_data : [];
            const status = safeStr(r.pass_fail_status);
            const statusColor = status === "pass" ? "text-green-400" : status === "soft_gate_fail" ? "text-amber-400" : "text-gray-400";
            const statusLabel = status === "pass" ? "통과" : status === "soft_gate_fail" ? "조건미충족" : status === "fail" ? "탈락" : status || "—";
            const breakdown = r.score_breakdown ?? {};
            const detailedReason = safeStr(r.reason_text_detailed || r.reason_text);
            const displayName = r.display_name ?? r.stock_name ?? "";
            const orderReadiness = r.order_readiness;
            const orderReadinessColor = orderReadiness === "ready" ? "text-green-400" : orderReadiness === "waiting" ? "text-amber-400" : "text-red-400";
            const orderReadinessLabel = orderReadiness === "ready" ? "준비됨" : orderReadiness === "waiting" ? "대기중" : orderReadiness === "blocked" ? "차단됨" : "—";
            return (
              <tr key={i} className={`hover:bg-white/5 ${status === "pass" ? "bg-green-500/3" : ""}`}>
                <Td className="tabular-nums font-bold text-gray-300">#{rank}</Td>
                <Td>
                  <StockLabel
                    name={displayName !== safeStr(r.stock_code) ? displayName : null}
                    code={safeStr(r.stock_code)}
                  />
                  {r.stock_name_missing && (
                    <span className="ml-1 text-[10px] text-amber-500">미확인</span>
                  )}
                  {r.missing_reason && (
                    <div className="text-[10px] text-amber-500 mt-0.5">{r.missing_reason}</div>
                  )}
                </Td>
                <Td><ScoreBar score={score} max={MAX_SCORE} /></Td>
                <Td className={`font-medium text-xs ${statusColor}`}>{statusLabel}</Td>
                <Td right>
                  {r.current_price != null ? (
                    <>
                      <div className="font-medium text-gray-200">{fmtPrice(safeNum(r.current_price))}</div>
                      <div className={`text-xs ${pctColor(safeNum(r.change_rate_pct))}`}>{fmtPct(safeNum(r.change_rate_pct))}</div>
                      <FreshnessBadge status={r.freshness_status} />
                    </>
                  ) : <span className="text-gray-600">—</span>}
                </Td>
                <Td right>
                  {r.buy_trigger_price != null ? (
                    <>
                      <div className="text-xs text-red-300 font-medium">{fmtPrice(safeNum(r.buy_trigger_price))}</div>
                      {r.distance_to_trigger_price != null && (
                        <div className="text-[10px] text-gray-500">
                          {fmtPrice(safeNum(r.distance_to_trigger_price))} ({fmtPct(safeNum(r.distance_to_trigger_pct))})
                        </div>
                      )}
                    </>
                  ) : <span className="text-gray-600">—</span>}
                </Td>
                <Td>
                  <div>
                    <span className={`text-xs font-medium ${orderReadinessColor}`}>{orderReadinessLabel}</span>
                    {r.next_required_action && (
                      <div className="text-[10px] text-gray-500 mt-0.5 max-w-[120px] truncate" title={r.next_required_action}>
                        {r.next_required_action}
                      </div>
                    )}
                    {Array.isArray(r.order_blockers) && r.order_blockers.length > 0 && (
                      <div className="flex flex-col gap-0.5 mt-0.5">
                        {r.order_blockers.slice(0, 2).map((b, bi) => (
                          <span key={bi} className="text-[10px] text-red-400">{b}</span>
                        ))}
                      </div>
                    )}
                  </div>
                </Td>
                <Td className="max-w-xs">
                  {passReasons.length > 0 && (
                    <div className="flex flex-wrap gap-0.5 mb-0.5">
                      {passReasons.slice(0, 2).map((reason, ri) => (
                        <span key={ri} className="rounded-full bg-green-500/15 px-1.5 py-0.5 text-[10px] text-green-400">{reason}</span>
                      ))}
                    </div>
                  )}
                  {failReasons.length > 0 && (
                    <div className="flex flex-wrap gap-0.5">
                      {failReasons.slice(0, 2).map((reason, ri) => (
                        <span key={ri} className="rounded-full bg-red-500/15 px-1.5 py-0.5 text-[10px] text-red-400">{reason}</span>
                      ))}
                    </div>
                  )}
                  {passReasons.length === 0 && failReasons.length === 0 && detailedReason && (
                    <span className="text-xs text-gray-400 truncate block max-w-[180px]">{detailedReason}</span>
                  )}
                  {missingData.length > 0 && (
                    <span className="text-[10px] text-gray-600">미수집: {missingData.join(", ")}</span>
                  )}
                </Td>
                <Td className="text-xs">
                  <div className="flex flex-col gap-0.5 min-w-[110px]">
                    {Object.entries(breakdown).filter(([, v]) => v && typeof v === "object").map(([key, comp]) => {
                      const c = comp as { score: number; max: number; label?: string; value?: number | null };
                      const compPct = c.max > 0 ? Math.round((c.score / c.max) * 100) : 0;
                      return (
                        <div key={key} className="flex items-center gap-1">
                          <span className="w-16 shrink-0 text-gray-600 text-[10px]">{c.label ?? keyLabel[key] ?? key}</span>
                          <div className="h-1 w-10 rounded-full bg-white/10 overflow-hidden">
                            <div
                              className={`h-full rounded-full ${c.score < 0 ? "bg-red-500" : compPct >= 70 ? "bg-green-500" : "bg-amber-500"}`}
                              style={{ width: `${Math.abs(Math.min(compPct, 100))}%` }}
                            />
                          </div>
                          <span className={`text-[10px] tabular-nums ${c.score < 0 ? "text-red-400" : "text-gray-400"}`}>
                            {c.score < 0 ? c.score : `${c.score}/${c.max}`}
                          </span>
                        </div>
                      );
                    })}
                  </div>
                </Td>
                <Td className={`text-xs ${freshness.delayed ? "text-amber-300" : "text-green-400"}`}>
                  {fmtTimeOnly(r.source_ts ?? r.created_at)}
                  {freshness.delayed && <span className="block text-[10px]">{freshness.label}</span>}
                </Td>
              </tr>
            );
          })
        )}
      </tbody>
    </TW>
  );
}

function Stage3Table({ rows }: { rows: WorkbenchStageRow[] }) {
  return (
    <TW>
      <thead>
        <tr>
          <Th>주문ID</Th>
          <Th>종목</Th>
          <Th>상태</Th>
          <Th>주문가</Th>
          <Th>체결가</Th>
          <Th>체결량</Th>
          <Th>체결시각</Th>
        </tr>
      </thead>
      <tbody>
        {rows.length === 0 ? (
          <EmptyRow colSpan={7} message="아직 데이터가 없습니다. 전략이 활성화되면 기록이 쌓입니다." />
        ) : (
          rows.map((r, i) => {
            const status = safeStr(r.status);
            const statusColor = status === "FILLED" ? "text-green-400" : status === "CANCELLED" || status === "REJECTED" ? "text-red-400" : "text-amber-400";
            return (
              <tr key={i} className="hover:bg-white/5">
                <Td className="text-xs text-gray-500">{safeStr(r.order_id)}</Td>
                <Td><StockLabel name={safeStr(r.stock_name)} code={safeStr(r.stock_code)} /></Td>
                <Td className={statusColor}>{status}</Td>
                <Td right>{fmtPrice(safeNum(r.order_price))}</Td>
                <Td right>{fmtPrice(safeNum(r.filled_price ?? r.fill_price ?? r.executed_price))}</Td>
                <Td right>{fmtCount(safeNum(r.filled_quantity ?? r.fill_qty))}</Td>
                <Td className="text-xs">{fmtKst(safeStr(r.filled_at ?? r.created_at))}</Td>
              </tr>
            );
          })
        )}
      </tbody>
    </TW>
  );
}

function Stage4Table({ rows }: { rows: WorkbenchStageRow[] }) {
  return (
    <TW>
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
            const pnl = safeNum(r.pnl_pct ?? r.unrealized_pnl_pct);
            return (
              <tr key={i} className="hover:bg-white/5">
                <Td><StockLabel name={r.display_name ?? safeStr(r.stock_name)} code={safeStr(r.stock_code)} /></Td>
                <Td right>{fmtPrice(safeNum(r.entry_price ?? r.avg_price))}</Td>
                <Td right>{fmtPrice(safeNum(r.current_price))}</Td>
                <Td right className={pctColor(pnl)}>{fmtPct(pnl)}</Td>
                <Td right>{fmtCount(safeNum(r.remaining_qty ?? r.quantity))}</Td>
                <Td right className="text-blue-400">{fmtPrice(safeNum(r.stop_loss_price))}</Td>
                <Td right className="text-red-400">{fmtPrice(safeNum(r.take_profit_price))}</Td>
                <Td className="text-xs">{fmtKst(safeStr(r.entry_date ?? r.created_at))}</Td>
              </tr>
            );
          })
        )}
      </tbody>
    </TW>
  );
}

function Stage5Table({ rows }: { rows: WorkbenchStageRow[] }) {
  return (
    <TW>
      <thead>
        <tr>
          <Th>주문ID</Th>
          <Th>종목</Th>
          <Th>상태</Th>
          <Th>구분</Th>
          <Th>청산사유</Th>
          <Th>체결가</Th>
          <Th>체결량</Th>
          <Th>손익률</Th>
          <Th>손익금</Th>
          <Th>잔여수량</Th>
          <Th>체결시각</Th>
        </tr>
      </thead>
      <tbody>
        {rows.length === 0 ? (
          <EmptyRow colSpan={11} message="아직 데이터가 없습니다. 전략이 활성화되면 기록이 쌓입니다." />
        ) : (
          rows.map((r, i) => {
            const status = safeStr(r.status);
            const statusColor = status === "FILLED" ? "text-green-400" : status === "CANCELLED" || status === "REJECTED" ? "text-red-400 font-semibold" : "text-amber-400";
            const pnl = safeNum(r.pnl_pct ?? r.realized_pnl_pct);
            const pnlAmount = safeNum(r.pnl_amount);
            const exitResult = safeStr(
              r.exit_result ??
              (pnlAmount != null ? (pnlAmount > 0 ? "익절" : pnlAmount < 0 ? "손절" : "보합") : "미분류")
            );
            const exitColor = exitResult === "익절" ? "text-red-400" : exitResult === "손절" ? "text-blue-400" : "text-gray-400";
            return (
              <tr key={i} className={`hover:bg-white/5 ${status === "CANCELLED" || status === "REJECTED" ? "bg-red-500/5" : ""}`}>
                <Td className="text-xs text-gray-500">{safeStr(r.order_id)}</Td>
                <Td><StockLabel name={r.display_name ?? safeStr(r.stock_name)} code={safeStr(r.stock_code)} /></Td>
                <Td className={statusColor}>{status}</Td>
                <Td className={`text-xs font-semibold ${exitColor}`}>{exitResult}</Td>
                <Td className="text-gray-400 text-xs">{safeStr(r.exit_reason)}</Td>
                <Td right>{fmtPrice(safeNum(r.filled_price ?? r.fill_price))}</Td>
                <Td right>{fmtCount(safeNum(r.filled_quantity ?? r.fill_qty))}</Td>
                <Td right className={pctColor(pnl)}>{fmtPct(pnl)}</Td>
                <Td right className={pctColor(pnlAmount)}>
                  {pnlAmount != null ? `${pnlAmount > 0 ? "+" : ""}${Math.round(pnlAmount).toLocaleString("ko-KR")}원` : "—"}
                </Td>
                <Td right className={(safeNum(r.remaining_quantity) ?? 0) > 0 ? "text-red-400 font-bold" : "text-gray-500"}>
                  {fmtCount(safeNum(r.remaining_quantity))}
                </Td>
                <Td className="text-xs">{fmtKst(safeStr(r.filled_at))}</Td>
              </tr>
            );
          })
        )}
      </tbody>
    </TW>
  );
}

// ─────────────────────────────────────────────
// Stage 6: Daily review + improvement proposals
// ─────────────────────────────────────────────

interface S6SummaryItem {
  priority: string;
  title: string;
  evidence: string;
  action: string;
}

interface S6Summary {
  total_sells?: number | null;
  win_count?: number | null;
  loss_count?: number | null;
  win_rate?: number | null;
  total_pnl?: number | null;
  avg_pnl_pct?: number | null;
  review_basis?: string;
  improvement_items?: S6SummaryItem[];
}

function Stage6Panel({
  stage,
  cardId,
  cardVersion,
  proposals,
  proposalsLoading,
  onSaveProposal,
  onApprove,
  onReject,
  onApply,
}: {
  stage: WorkbenchStage;
  cardId: number;
  cardVersion: number;
  proposals: ImprovementProposal[];
  proposalsLoading: boolean;
  onSaveProposal: (item: S6SummaryItem) => void;
  onApprove: (id: number, backtestNote: string, rollbackVersion: number, proposedChanges: Record<string, unknown>) => void;
  onReject: (id: number, reason: string) => void;
  onApply: (id: number) => void;
}) {
  const sum = (stage.summary ?? {}) as S6Summary;
  const wr = safeNum(sum.win_rate);
  const avgPnl = safeNum(sum.avg_pnl_pct);
  const totalPnl = safeNum(sum.total_pnl);
  const items: S6SummaryItem[] = Array.isArray(sum.improvement_items) ? sum.improvement_items : [];

  const [rejectTarget, setRejectTarget] = useState<number | null>(null);
  const [rejectReason, setRejectReason] = useState("");
  const [approveTarget, setApproveTarget] = useState<number | null>(null);
  const [backtestNote, setBacktestNote] = useState("");
  const [proposedChanges, setProposedChanges] = useState("");
  const [saving, setSaving] = useState<string | null>(null);

  const handleSave = (item: S6SummaryItem) => {
    setSaving(item.title);
    onSaveProposal(item);
    setTimeout(() => setSaving(null), 1500);
  };

  const pendingIds = new Set(proposals.filter((p) => p.status === "PENDING").map((p) => p.proposal_id));
  const savedTitles = new Set(proposals.map((p) => p.issue_type));

  return (
    <div className="space-y-5">
      {/* KPI summary */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
        {[
          { label: "총 매도", value: fmtCount(safeNum(sum.total_sells)) },
          { label: "익절", value: fmtCount(safeNum(sum.win_count)), accent: "text-red-400" },
          { label: "손절", value: fmtCount(safeNum(sum.loss_count)), accent: "text-blue-400" },
          { label: "승률", value: wr != null ? `${wr.toFixed(1)}%` : "—", accent: wr != null && wr >= 50 ? "text-red-400" : "text-blue-400" },
          { label: "총손익", value: totalPnl != null ? `${totalPnl > 0 ? "+" : ""}${Math.round(totalPnl).toLocaleString("ko-KR")}원` : "—", accent: pctColor(totalPnl) },
          { label: "평균수익률", value: fmtPct(avgPnl), accent: pctColor(avgPnl) },
        ].map(({ label, value, accent }) => (
          <div key={label} className="rounded-xl border border-white/5 bg-gray-900/60 p-3 text-center">
            <p className="text-xs text-gray-500">{label}</p>
            <p className={`mt-1 text-xl font-bold tabular-nums ${accent ?? "text-gray-200"}`}>{value}</p>
          </div>
        ))}
      </div>

      {/* Auto-generated improvement items from workbench */}
      {items.length > 0 && (
        <section className="rounded-2xl border border-amber-400/15 bg-amber-400/5 p-4">
          <div className="mb-3 flex items-center justify-between">
            <h3 className="text-sm font-semibold text-amber-200">자동 복기 개선안 후보</h3>
            <span className="text-xs text-gray-500">저장 후 승인하면 권고사항으로 기록됩니다</span>
          </div>
          <div className="grid gap-3 lg:grid-cols-2">
            {items.map((item, idx) => {
              const alreadySaved = savedTitles.has(item.title);
              return (
                <article
                  key={`${item.priority}-${idx}`}
                  className="rounded-xl border border-white/5 bg-gray-950/40 p-3"
                >
                  <div className="flex items-start justify-between gap-2">
                    <div>
                      <span className="mr-2 rounded-full bg-amber-400/15 px-2 py-0.5 text-[11px] font-semibold text-amber-200">
                        {item.priority}
                      </span>
                      <span className="text-sm font-semibold text-gray-100">{item.title}</span>
                    </div>
                    {!alreadySaved ? (
                      <button
                        type="button"
                        onClick={() => handleSave(item)}
                        disabled={saving === item.title}
                        className="shrink-0 rounded-lg border border-blue-500/30 bg-blue-500/10 px-2 py-1 text-xs font-semibold text-blue-300 transition hover:bg-blue-500/20 disabled:opacity-50"
                      >
                        {saving === item.title ? "저장 중…" : "개선안 저장"}
                      </button>
                    ) : (
                      <span className="shrink-0 rounded-full bg-green-500/15 px-2 py-0.5 text-xs text-green-400">
                        저장됨
                      </span>
                    )}
                  </div>
                  <p className="mt-2 text-xs text-gray-400">근거: {item.evidence}</p>
                  <p className="mt-1 text-xs text-gray-300">조치: {item.action}</p>
                </article>
              );
            })}
          </div>
        </section>
      )}

      {/* Saved proposals with approval workflow */}
      {proposalsLoading ? (
        <Spinner size="sm" />
      ) : proposals.length > 0 ? (
        <section className="rounded-2xl border border-white/8 bg-gray-900/40 p-4">
          <h3 className="mb-3 text-sm font-semibold text-gray-300">개선안 승인 현황</h3>
          <div className="space-y-3">
            {proposals.map((p) => {
              const statusColor =
                p.status === "PENDING" ? "text-amber-300 bg-amber-400/10 border-amber-400/20" :
                p.status === "APPROVED" ? "text-green-300 bg-green-400/10 border-green-400/20" :
                p.status === "APPLIED" ? "text-blue-300 bg-blue-400/10 border-blue-400/20" :
                "text-gray-400 bg-gray-400/10 border-gray-400/20";

              return (
                <div key={p.proposal_id} className={`rounded-xl border p-3 ${statusColor}`}>
                  <div className="flex flex-wrap items-start justify-between gap-2">
                    <div className="min-w-0">
                      <div className="flex items-center gap-2 flex-wrap">
                        <span className="text-xs font-bold">{p.priority}</span>
                        <span className="text-sm font-semibold text-gray-100">{p.issue_type}</span>
                        <span className={`rounded-full px-2 py-0.5 text-[10px] font-bold ${statusColor}`}>
                          {p.status === "PENDING" ? "검토 대기" : p.status === "APPROVED" ? "승인됨" : p.status === "APPLIED" ? "적용됨" : "거절됨"}
                        </span>
                      </div>
                      <p className="mt-1 text-xs text-gray-300">{p.proposed_action}</p>
                      {p.root_cause && <p className="mt-0.5 text-xs text-gray-500">근거: {p.root_cause}</p>}
                      {p.backtest_note && <p className="mt-0.5 text-xs text-blue-300">백테스트: {p.backtest_note}</p>}
                      {Object.keys(p.proposed_changes ?? {}).length > 0 && (
                        <p className="mt-0.5 text-xs text-violet-300">구조화 변경: {JSON.stringify(p.proposed_changes)}</p>
                      )}
                      {p.rollback_card_version && <p className="mt-0.5 text-xs text-gray-500">롤백 기준: 카드 v{p.rollback_card_version}</p>}
                      {p.applied_card_version && <p className="mt-0.5 text-xs text-green-300">적용 버전: 카드 v{p.applied_card_version}</p>}
                      {p.rejection_reason && (
                        <p className="mt-0.5 text-xs text-red-400">거절 사유: {p.rejection_reason}</p>
                      )}
                      {p.approved_at && (
                        <p className="mt-0.5 text-xs text-gray-500">
                          {p.status === "REJECTED" ? "거절" : "승인"}: {fmtKst(p.approved_at)}
                        </p>
                      )}
                    </div>

                    {p.status === "PENDING" && (
                      <div className="flex shrink-0 gap-2">
                        <button
                          type="button"
                          onClick={() => setApproveTarget(p.proposal_id)}
                          className="rounded-lg bg-green-500/20 border border-green-500/30 px-3 py-1.5 text-xs font-semibold text-green-300 transition hover:bg-green-500/30"
                        >
                          승인
                        </button>
                        <button
                          type="button"
                          onClick={() => setRejectTarget(p.proposal_id)}
                          className="rounded-lg bg-red-500/10 border border-red-500/20 px-3 py-1.5 text-xs font-semibold text-red-300 transition hover:bg-red-500/20"
                        >
                          거절
                        </button>
                      </div>
                    )}
                    {p.status === "APPROVED" && (
                      <button
                        type="button"
                        onClick={() => onApply(p.proposal_id)}
                        className="shrink-0 rounded-lg border border-blue-500/30 bg-blue-500/15 px-3 py-1.5 text-xs font-semibold text-blue-300 hover:bg-blue-500/25"
                      >
                        검증 버전 적용
                      </button>
                    )}
                  </div>

                  {approveTarget === p.proposal_id && (
                    <div className="mt-3 rounded-lg border border-green-500/20 bg-gray-900/60 p-3">
                      <label className="block text-xs text-gray-400 mb-1">백테스트 결과 요약 (필수)</label>
                      <textarea
                        value={backtestNote}
                        onChange={(e) => setBacktestNote(e.target.value)}
                        placeholder="기간, 표본수, 주요 성과와 검증 결론을 입력하세요"
                        className="min-h-20 w-full rounded-lg border border-white/10 bg-gray-800 px-3 py-2 text-sm text-gray-200 placeholder-gray-600 focus:border-green-500 focus:outline-none"
                      />
                      <label className="mb-1 mt-3 block text-xs text-gray-400">적용할 설정 JSON (필수)</label>
                      <textarea
                        value={proposedChanges}
                        onChange={(e) => setProposedChanges(e.target.value)}
                        placeholder={'예: {"risk_params":{"stop_loss_pct":-3.0},"max_stocks":2}'}
                        className="min-h-20 w-full rounded-lg border border-white/10 bg-gray-800 px-3 py-2 font-mono text-xs text-gray-200 placeholder-gray-600 focus:border-violet-500 focus:outline-none"
                      />
                      <p className="mt-1 text-xs text-gray-500">롤백 기준 카드 v{cardVersion}</p>
                      <div className="mt-2 flex justify-end gap-2">
                        <button type="button" onClick={() => { setApproveTarget(null); setBacktestNote(""); setProposedChanges(""); }} className="px-3 py-1 text-xs text-gray-400">취소</button>
                        <button
                          type="button"
                          disabled={!backtestNote.trim() || !proposedChanges.trim()}
                          onClick={() => {
                            try {
                              const parsed = JSON.parse(proposedChanges) as Record<string, unknown>;
                              onApprove(p.proposal_id, backtestNote.trim(), cardVersion, parsed);
                              setApproveTarget(null);
                              setBacktestNote("");
                              setProposedChanges("");
                            } catch {
                              window.alert("적용할 설정은 올바른 JSON 객체여야 합니다.");
                            }
                          }}
                          className="rounded-lg bg-green-500/20 px-3 py-1 text-xs font-semibold text-green-300 disabled:opacity-40"
                        >
                          백테스트 확인 후 승인
                        </button>
                      </div>
                    </div>
                  )}

                  {/* Reject reason input */}
                  {rejectTarget === p.proposal_id && (
                    <div className="mt-3 rounded-lg border border-red-500/20 bg-gray-900/60 p-3">
                      <label className="block text-xs text-gray-400 mb-1">거절 사유 (선택)</label>
                      <input
                        type="text"
                        value={rejectReason}
                        onChange={(e) => setRejectReason(e.target.value)}
                        placeholder="거절 사유를 입력하세요"
                        className="w-full rounded-lg border border-white/10 bg-gray-800 px-3 py-2 text-sm text-gray-200 placeholder-gray-600 focus:border-red-500 focus:outline-none"
                      />
                      <div className="mt-2 flex gap-2 justify-end">
                        <button
                          type="button"
                          onClick={() => { setRejectTarget(null); setRejectReason(""); }}
                          className="px-3 py-1 text-xs text-gray-400 hover:text-gray-200"
                        >
                          취소
                        </button>
                        <button
                          type="button"
                          onClick={() => {
                            onReject(p.proposal_id, rejectReason);
                            setRejectTarget(null);
                            setRejectReason("");
                          }}
                          className="rounded-lg bg-red-500/20 px-3 py-1 text-xs font-semibold text-red-300 hover:bg-red-500/30"
                        >
                          거절 확정
                        </button>
                      </div>
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </section>
      ) : items.length === 0 ? (
        <div className="rounded-xl border border-white/5 py-10 text-center">
          <p className="text-sm text-gray-500">
            {safeNum(sum.total_sells) === 0 || sum.total_sells == null
              ? "청산 완료 거래가 없습니다. 마감 후 복기 데이터가 생성됩니다."
              : "개선안이 없습니다. 현재 전략 조건을 유지하며 관찰 중입니다."}
          </p>
        </div>
      ) : null}

      {/* Trade rows table */}
      {stage.rows.length > 0 && (
        <div>
          <h4 className="mb-2 text-xs font-semibold uppercase tracking-wider text-gray-500">건별 복기 내역</h4>
          <TW>
            <thead>
              <tr>
                <Th>종목</Th>
                <Th>매도시각</Th>
                <Th>체결가</Th>
                <Th>체결량</Th>
                <Th>손익률</Th>
                <Th>손익금</Th>
                <Th>구분</Th>
                <Th>청산사유</Th>
                <Th>파동복기</Th>
                <Th>매매일지</Th>
              </tr>
            </thead>
            <tbody>
              {stage.rows.slice(0, 20).map((r, i) => {
                const pnl = safeNum(r.pnl_pct ?? r.realized_pnl_pct);
                const pnlAmount = safeNum(r.pnl_amount);
                const exitResult = safeStr(r.exit_result ?? r.review_result);
                const exitColor = exitResult === "익절" ? "text-red-400" : exitResult === "손절" ? "text-blue-400" : "text-gray-400";
                return (
                  <tr key={i} className="hover:bg-white/5">
                    <Td><StockLabel name={r.display_name ?? safeStr(r.stock_name)} code={safeStr(r.stock_code)} /></Td>
                    <Td className="text-xs">{fmtKst(safeStr(r.traded_at ?? r.sold_at))}</Td>
                    <Td right>{fmtPrice(safeNum(r.price ?? r.sell_price))}</Td>
                    <Td right>{fmtCount(safeNum(r.quantity ?? r.qty))}</Td>
                    <Td right className={pctColor(pnl)}>{fmtPct(pnl)}</Td>
                    <Td right className={pctColor(pnlAmount)}>
                      {pnlAmount != null ? `${pnlAmount > 0 ? "+" : ""}${Math.round(pnlAmount).toLocaleString("ko-KR")}원` : "—"}
                    </Td>
                    <Td className={`text-xs font-semibold ${exitColor}`}>{exitResult}</Td>
                    <Td className="text-xs text-gray-400">{safeStr(r.exit_reason)}</Td>
                    <Td className="min-w-[180px] text-xs text-gray-300">
                      <div>{safeStr(r.wave_review?.entry_phase) || "파동 스냅샷 미기록"}</div>
                      <div className="mt-0.5 text-gray-500">{safeStr(r.wave_review?.verdict) || "복기 판정 대기"}</div>
                      {r.wave_review?.available && <span className="mt-1 inline-block rounded bg-emerald-500/10 px-1.5 py-0.5 text-[10px] font-semibold text-emerald-300">학습가능</span>}
                    </Td>
                    <Td>
                      <button
                        type="button"
                        onClick={() => {
                          const code = safeStr(r.stock_code);
                          if (!code) return;
                          const d = safeStr(r.traded_at ?? r.sold_at).slice(0, 10);
                          const qs = new URLSearchParams({ stock_code: code });
                          if (d) qs.set("trade_date", d);
                          window.open(`/go100/strategies/${cardId}/trade-journal?${qs.toString()}`, "_blank", "noopener,noreferrer");
                        }}
                        className="rounded-md border border-white/10 bg-white/5 px-2 py-1 text-[11px] font-medium text-gray-300 transition hover:bg-white/10 hover:text-white disabled:opacity-40"
                        disabled={!safeStr(r.stock_code)}
                      >
                        일지
                      </button>
                    </Td>
                  </tr>
                );
              })}
            </tbody>
          </TW>
        </div>
      )}
    </div>
  );
}

// ─────────────────────────────────────────────
// Lifecycle table
// ─────────────────────────────────────────────

function LifecycleTable({ items }: { items: WorkbenchLifecycleItem[] }) {
  if (items.length === 0) {
    return (
      <div className="rounded-2xl border border-white/5 py-16 text-center">
        <p className="text-sm text-gray-500">
          아직 데이터가 없습니다. 전략이 활성화되면 매매 생애주기가 표시됩니다.
        </p>
      </div>
    );
  }
  return (
    <TW>
      <thead>
        <tr>
          <Th>종목</Th>
          <Th>매수가</Th>
          <Th>매수시각</Th>
          <Th>포지션</Th>
          <Th>손절가</Th>
          <Th>목표가</Th>
          <Th>청산사유</Th>
          <Th>매도시각</Th>
          <Th>손익률</Th>
        </tr>
      </thead>
      <tbody>
        {items.map((item, i) => {
          const pnl = item.realized_pnl_pct ?? item.unrealized_pnl_pct;
          return (
            <tr key={i} className="hover:bg-white/5">
              <Td>
                <StockLabel name={item.stock_name} code={item.stock_code} />
                <div className="mt-1 text-[10px] text-blue-300">trace: {item.trade_group_id}</div>
                <div className="text-[10px] text-gray-600">원천: {item.source_tables.join(", ") || "미수집"}</div>
                {item.trace_gaps.length > 0 && (
                  <div className="text-[10px] text-amber-300">미수집: {item.trace_gaps.join(", ")}</div>
                )}
              </Td>
              <Td right>{fmtPrice(item.buy_price)}</Td>
              <Td className="text-xs">
                {fmtKst(item.bought_at)}
                {item.selected_at && <div className="text-[10px] text-gray-600">선정 {fmtKst(item.selected_at)}</div>}
              </Td>
              <Td>
                {item.position_id ? (
                  <span className="rounded-full bg-blue-500/15 px-2 py-0.5 text-xs text-blue-300">
                    {item.position_status ?? "연결됨"}
                  </span>
                ) : (
                  <span className="text-xs text-gray-600">미수집</span>
                )}
              </Td>
              <Td right className="text-blue-400">
                {item.position_id ? fmtPrice(item.stop_loss_price) : "—"}
              </Td>
              <Td right className="text-red-400">
                {item.position_id ? fmtPrice(item.take_profit_price) : "—"}
              </Td>
              <Td className="text-xs text-gray-400">{item.exit_reason ?? "—"}</Td>
              <Td className="text-xs">{fmtKst(item.sold_at)}</Td>
              <Td right className={pctColor(pnl)}>{fmtPct(pnl)}</Td>
            </tr>
          );
        })}
      </tbody>
    </TW>
  );
}

// ─────────────────────────────────────────────
// DailyResultsSection — persisted snapshot + on-demand fallback
// ─────────────────────────────────────────────

function dailyResultsToPeriodAnalysis(
  items: DailyResult[],
  dateFrom: string,
  dateTo: string
): NonNullable<WorkbenchData["period_analysis"]> {
  const sorted = [...items].sort((a, b) => a.trade_date.localeCompare(b.trade_date));
  const sampleSize = sorted.reduce((s, r) => s + r.sell_count, 0);
  const confidence =
    sampleSize >= 100 ? ("HIGH" as const) : sampleSize >= 30 ? ("MEDIUM" as const) : ("LOW" as const);
  const daily_trend = sorted.map((r) => ({
    trade_date: r.trade_date,
    sample_count: r.sell_count,
    win_count: r.win_count,
    loss_count: r.loss_count,
    win_rate: r.win_rate ?? 0,
    total_pnl: r.realized_pnl,
    avg_pnl_pct: r.avg_pnl_pct,
    market_regime: r.market_regime,
    error_count: r.error_count,
  }));
  return {
    date_from: dateFrom,
    date_to: dateTo,
    sample_size: sampleSize,
    confidence,
    daily_trend,
    pnl_distribution: [],
    exit_performance: [],
  };
}

function DailyResultsSection({
  data,
  dailyResults,
  dailyResultsLoading,
  recomputeLoading,
  recomputeMsg,
  onRecompute,
}: {
  data: WorkbenchData;
  dailyResults: DailyResultsResponse | null;
  dailyResultsLoading: boolean;
  recomputeLoading: boolean;
  recomputeMsg: string | null;
  onRecompute: () => void;
}) {
  const hasPersistedData = (dailyResults?.items ?? []).length > 0;
  const latestComputedAt =
    hasPersistedData
      ? dailyResults!.items.reduce(
          (latest, r) =>
            !r.computed_at ? latest : !latest ? r.computed_at : r.computed_at > latest ? r.computed_at : latest,
          null as string | null
        )
      : null;

  const analysisFromPersisted =
    hasPersistedData && dailyResults
      ? dailyResultsToPeriodAnalysis(dailyResults.items, dailyResults.date_from, dailyResults.date_to)
      : null;

  const analysisToShow = analysisFromPersisted ?? data.period_analysis ?? null;

  return (
    <div>
      {/* Source / recompute bar */}
      <div className="mb-3 flex flex-wrap items-center gap-2">
        {dailyResultsLoading ? (
          <span className="text-[11px] text-gray-500">스냅샷 확인 중…</span>
        ) : hasPersistedData ? (
          <span className="rounded-full border border-emerald-500/30 bg-emerald-500/10 px-2 py-0.5 text-[11px] font-semibold text-emerald-300">
            스냅샷 · {dailyResults!.items.length}일
            {latestComputedAt && (
              <span className="ml-1 font-normal text-emerald-400/70">
                ({new Date(latestComputedAt).toLocaleDateString("ko-KR")} 집계)
              </span>
            )}
          </span>
        ) : (
          <span className="rounded-full border border-white/10 bg-white/5 px-2 py-0.5 text-[11px] text-gray-500">
            스냅샷 없음 · 실시간 계산
          </span>
        )}
        <button
          type="button"
          onClick={onRecompute}
          disabled={recomputeLoading}
          className="ml-auto rounded-lg border border-white/10 bg-white/5 px-3 py-1 text-xs font-semibold text-blue-300 transition hover:bg-white/10 disabled:opacity-50"
        >
          {recomputeLoading ? "집계 중…" : "결과 저장"}
        </button>
        {recomputeMsg && (
          <span className="text-[11px] text-gray-400">{recomputeMsg}</span>
        )}
      </div>

      {/* Persisted daily table (only when snapshot exists) */}
      {hasPersistedData && dailyResults && (
        <div className="mb-4 overflow-x-auto rounded-2xl border border-white/5 bg-[#0b1421]" data-testid="ops-daily-results-table">
          <table className="min-w-full text-xs">
            <thead>
              <tr>
                {["날짜", "매도", "승", "패", "승률", "실현손익", "평균수익률", "레짐", "이벤트", "오류"].map((h) => (
                  <th key={h} className="whitespace-nowrap bg-white/5 px-3 py-2 text-left text-[10px] font-semibold uppercase tracking-wider text-gray-400">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {dailyResults.items.length === 0 ? (
                <tr>
                  <td colSpan={10} className="py-8 text-center text-gray-500">저장된 일별 결과가 없습니다.</td>
                </tr>
              ) : (
                [...dailyResults.items]
                  .sort((a, b) => a.trade_date.localeCompare(b.trade_date))
                  .map((r) => (
                    <tr key={`${r.trade_date}-${r.mode}`} className="border-t border-white/5 hover:bg-white/[0.02]">
                      <td className="px-3 py-1.5 text-gray-400">{r.trade_date}</td>
                      <td className="px-3 py-1.5 text-gray-200 tabular-nums">{r.sell_count}</td>
                      <td className="px-3 py-1.5 text-red-300 tabular-nums">{r.win_count}</td>
                      <td className="px-3 py-1.5 text-blue-300 tabular-nums">{r.loss_count}</td>
                      <td className={`px-3 py-1.5 tabular-nums ${r.win_rate != null ? (r.win_rate >= 50 ? "text-red-300" : "text-blue-300") : "text-gray-500"}`}>
                        {r.win_rate != null ? `${r.win_rate.toFixed(1)}%` : "—"}
                      </td>
                      <td className={`px-3 py-1.5 tabular-nums ${r.realized_pnl >= 0 ? "text-red-300" : "text-blue-300"}`}>
                        {Math.round(r.realized_pnl).toLocaleString("ko-KR")}원
                      </td>
                      <td className={`px-3 py-1.5 tabular-nums ${r.avg_pnl_pct != null ? (r.avg_pnl_pct >= 0 ? "text-red-300" : "text-blue-300") : "text-gray-500"}`}>
                        {r.avg_pnl_pct != null ? `${r.avg_pnl_pct >= 0 ? "+" : ""}${r.avg_pnl_pct.toFixed(2)}%` : "—"}
                      </td>
                      <td className="px-3 py-1.5 text-gray-500">{r.market_regime}</td>
                      <td className="px-3 py-1.5 text-gray-400 tabular-nums">{r.event_count}</td>
                      <td className={`px-3 py-1.5 tabular-nums ${r.error_count > 0 ? "text-amber-300" : "text-gray-600"}`}>{r.error_count}</td>
                    </tr>
                  ))
              )}
            </tbody>
          </table>
        </div>
      )}

      {/* Period analysis chart/table — use persisted trend if available, fall back to computed */}
      {analysisToShow && <PeriodAnalysisPanel analysis={analysisToShow} />}
      {!analysisToShow && !dailyResultsLoading && (
        <div className="rounded-2xl border border-white/5 py-14 text-center text-sm text-gray-500">
          기간 분석 데이터가 없습니다. 날짜 범위를 선택하거나 &quot;결과 저장&quot;을 눌러 집계하세요.
        </div>
      )}
    </div>
  );
}

function PeriodAnalysisPanel({ analysis }: { analysis: NonNullable<WorkbenchData["period_analysis"]> }) {
  const trend = analysis.daily_trend ?? [];
  const pnlValues = trend.map((d) => d.total_pnl);
  const minPnl = Math.min(...pnlValues, 0);
  const maxPnl = Math.max(...pnlValues, 0);
  const span = Math.max(maxPnl - minPnl, 1);
  const points = trend.map((d, index) => {
    const x = trend.length <= 1 ? 50 : (index / (trend.length - 1)) * 100;
    const y = 92 - ((d.total_pnl - minPnl) / span) * 80;
    return `${x},${y}`;
  }).join(" ");
  const maxBucket = Math.max(...analysis.pnl_distribution.map((item) => item.count), 1);

  return (
    <section data-testid="ops-period-analysis" className="mb-4 space-y-4">
      <div className={`rounded-xl border px-4 py-3 text-sm ${analysis.confidence === "LOW" ? "border-amber-500/30 bg-amber-500/10 text-amber-200" : "border-blue-500/20 bg-blue-500/5 text-blue-200"}`}>
        표본 {fmtCount(safeNum(analysis.sample_size))}건 · 신뢰도 {analysis.confidence}
        {analysis.confidence === "LOW" && " — 표본 30건 미만이므로 해석에 주의하십시오."}
      </div>
      {trend.length === 0 ? (
        <div className="rounded-2xl border border-white/5 py-14 text-center text-sm text-gray-500">선택한 기간·레짐에 청산 거래가 없습니다.</div>
      ) : (
        <div className="grid gap-4 xl:grid-cols-2">
          <div className="rounded-2xl border border-white/5 bg-[#0b1421] p-4">
            <h3 className="text-sm font-semibold text-gray-300">일별 실현손익 추이</h3>
            <svg viewBox="0 0 100 100" className="mt-3 h-48 w-full overflow-visible" preserveAspectRatio="none" aria-label="일별 실현손익 추이 차트">
              <line x1="0" y1={92 - ((0 - minPnl) / span) * 80} x2="100" y2={92 - ((0 - minPnl) / span) * 80} stroke="#334155" strokeWidth="0.7" />
              <polyline points={points} fill="none" stroke="#60a5fa" strokeWidth="2" vectorEffect="non-scaling-stroke" />
              {trend.map((d, index) => {
                const x = trend.length <= 1 ? 50 : (index / (trend.length - 1)) * 100;
                const y = 92 - ((d.total_pnl - minPnl) / span) * 80;
                return <circle key={d.trade_date} cx={x} cy={y} r="1.5" fill={d.total_pnl >= 0 ? "#fb7185" : "#60a5fa"} />;
              })}
            </svg>
            <div className="mt-2 max-h-44 overflow-auto">
              {trend.map((d) => (
                <div key={d.trade_date} className="grid grid-cols-[86px_1fr_1fr_1fr] gap-2 border-t border-white/5 py-1.5 text-xs">
                  <span className="text-gray-500">{d.trade_date.slice(5)}</span>
                  <span className={pctColor(d.total_pnl)}>{Math.round(d.total_pnl).toLocaleString("ko-KR")}원</span>
                  <span className="text-gray-400">승률 {safeNum(d.win_rate)?.toFixed(1) ?? "—"}%</span>
                  <span className="text-gray-500">{d.market_regime} · 오류 {d.error_count}</span>
                </div>
              ))}
            </div>
          </div>
          <div className="rounded-2xl border border-white/5 bg-[#0b1421] p-4">
            <h3 className="text-sm font-semibold text-gray-300">손익률 분포</h3>
            <div className="mt-4 space-y-3">
              {analysis.pnl_distribution.map((item) => (
                <div key={item.bucket} className="grid grid-cols-[64px_1fr_36px] items-center gap-3 text-xs">
                  <span className="text-gray-500">{item.bucket}</span>
                  <div className="h-3 rounded-full bg-white/5"><div className="h-3 rounded-full bg-blue-500" style={{ width: `${Math.max((item.count / maxBucket) * 100, 3)}%` }} /></div>
                  <span className="text-right text-gray-300">{item.count}</span>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}
      <div className="rounded-2xl border border-white/5 bg-[#0b1421] p-4">
        <h3 className="mb-3 text-sm font-semibold text-gray-300">청산 유형별 성과</h3>
        <TW>
          <thead><tr><Th>청산 유형</Th><Th>표본</Th><Th>승률</Th><Th>평균수익률</Th><Th>총손익</Th></tr></thead>
          <tbody>
            {analysis.exit_performance.length === 0 ? <EmptyRow colSpan={5} message="청산 성과 데이터가 없습니다." /> : analysis.exit_performance.map((row) => (
              <tr key={row.exit_reason}>
                <Td>{row.exit_reason}</Td><Td right>{fmtCount(safeNum(row.trade_count))}</Td><Td right>{safeNum(row.win_rate)?.toFixed(1) ?? "—"}%</Td>
                <Td right className={pctColor(row.avg_pnl_pct)}>{fmtPct(row.avg_pnl_pct)}</Td>
                <Td right className={pctColor(row.total_pnl)}>{Math.round(row.total_pnl).toLocaleString("ko-KR")}원</Td>
              </tr>
            ))}
          </tbody>
        </TW>
      </div>
    </section>
  );
}

// ─────────────────────────────────────────────
// Funnel bar chart (sidebar)
// ─────────────────────────────────────────────

function FunnelChart({ stages }: { stages: WorkbenchStage[] }) {
  const maxCount = Math.max(...stages.map((s) => s.count), 1);
  return (
    <div className="rounded-2xl border border-white/5 bg-[#0b1421] p-4">
      <div className="mb-3 flex items-center justify-between">
        <h3 className="text-sm font-semibold text-gray-300">오늘 전환 퍼널</h3>
        <span className="text-xs text-gray-600">단계별 건수</span>
      </div>
      <div className="flex items-end gap-1.5 h-36">
        {stages.map((s) => {
          const pct = s.count > 0 ? Math.max((s.count / maxCount) * 100, 8) : 4;
          const isUnavail = s.status === "unavailable";
          return (
            <div key={s.stage_id} className="flex flex-1 flex-col items-center gap-1">
              <span className="text-xs font-bold text-gray-200 tabular-nums">{isUnavail ? "—" : s.count}</span>
              <div
                className={`w-full rounded-t-md ${isUnavail ? "bg-gray-700/40" : "bg-gradient-to-b from-blue-500 to-blue-800"}`}
                style={{ height: `${pct}%`, minHeight: "4px" }}
              />
              <span className="text-[9px] text-gray-600 text-center leading-tight">
                {["선정", "대기", "진입", "보유", "청산", "복기"][s.stage_id - 1] ?? `S${s.stage_id}`}
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────
// Per-stage KPI derivation
// ─────────────────────────────────────────────

function getStageKpis(stage: WorkbenchStage, card: WorkbenchData["card"]): Array<{ label: string; value: string; sub?: string }> {
  const sum = (stage.summary ?? {}) as Record<string, unknown>;
  const rows = stage.rows;
  switch (stage.stage_id) {
    case 1: {
      const isCard119Kpi = Number(card.id) === 119;
      const passCount = rows.filter((r) => r["decision"] === "pass").length;
      const totalCount = rows.length;
      const passRate = totalCount > 0 ? ((passCount / totalCount) * 100).toFixed(1) : "—";
      if (isCard119Kpi) {
        const cumulGeCount = safeNum(sum["cumulative_ge20_count"]);
        const currGeCount = safeNum(sum["current_ge20_count"]);
        return [
          { label: "오늘 누적 +20% 후보", value: fmtCount(cumulGeCount), sub: "결정로그 기준" },
          { label: "현재 +20% 유지", value: fmtCount(currGeCount), sub: "스냅샷 기준" },
          { label: "실진입 게이트", value: "+27%", sub: "상한가권 잠김 조건 포함" },
          { label: "소스", value: "card119_independent", sub: stage.updated_at ? fmtTimeOnly(stage.updated_at) : undefined },
        ];
      }
      return [
        { label: "현재 대상종목", value: fmtCount(stage.count), sub: `최근 ${rows.length}건 표시` },
        { label: "선정 통과율", value: `${passRate}%`, sub: `${passCount}/${totalCount}` },
        { label: "대기 전환율", value: safeNum(sum["conversion_rate"]) != null ? `${safeNum(sum["conversion_rate"])!.toFixed(1)}%` : "—", sub: "선정→대기" },
        { label: "소스", value: stage.source.replace("go100_", "").replace(/_/g, " ").substring(0, 16), sub: stage.updated_at ? fmtTimeOnly(stage.updated_at) : undefined },
      ];
    }
    case 2:
      return [
        { label: "현재 대기", value: fmtCount(stage.count) },
        { label: "신호 대기", value: "—" },
        { label: "평균 대기", value: "—" },
        { label: "소스", value: stage.source.replace("go100_", "").substring(0, 16), sub: stage.updated_at ? fmtTimeOnly(stage.updated_at) : undefined },
      ];
    case 3: {
      const filledCount = rows.filter((r) => r["status"] === "FILLED").length;
      return [
        { label: "발생 신호", value: fmtCount(stage.count) },
        { label: "체결 건수", value: fmtCount(filledCount) },
        { label: "체결률", value: filledCount > 0 && rows.length > 0 ? `${((filledCount / rows.length) * 100).toFixed(0)}%` : "—" },
        { label: "소스", value: stage.is_paper_filter_applied ? "paper" : "live", sub: stage.updated_at ? fmtTimeOnly(stage.updated_at) : undefined },
      ];
    }
    case 4: {
      const maxStocks = card.max_stocks ?? 0;
      return [
        { label: "보유 종목", value: `${stage.count}${maxStocks > 0 ? `/${maxStocks}` : ""}`, sub: maxStocks > 0 && stage.count >= maxStocks ? "⚠ 한도 도달" : undefined },
        { label: "평가손익", value: "—" },
        { label: "위험 경보", value: "—" },
        { label: "소스", value: stage.is_paper_filter_applied ? "paper" : "live", sub: stage.updated_at ? fmtTimeOnly(stage.updated_at) : undefined },
      ];
    }
    case 5: {
      const failed = rows.filter((r) => r["status"] === "CANCELLED" || r["status"] === "REJECTED").length;
      return [
        { label: "오늘 청산", value: fmtCount(stage.count) },
        { label: "실현손익", value: "—" },
        { label: "청산 실패", value: fmtCount(failed), sub: failed > 0 ? "⚠ 확인 필요" : undefined },
        { label: "소스", value: stage.is_paper_filter_applied ? "paper" : "live", sub: stage.updated_at ? fmtTimeOnly(stage.updated_at) : undefined },
      ];
    }
    case 6: {
      const s = sum as S6Summary;
      const wr = safeNum(s.win_rate);
      return [
        { label: "복기 대상", value: fmtCount(safeNum(s.total_sells)) },
        { label: "정상 실행", value: fmtCount(safeNum(s.win_count)), sub: "익절" },
        { label: "개선 후보", value: fmtCount(Array.isArray(s.improvement_items) ? s.improvement_items.length : 0) },
        { label: "승률", value: wr != null ? `${wr.toFixed(1)}%` : "—", sub: wr != null && wr < 50 ? "⚠ 재검토 필요" : undefined },
      ];
    }
    default:
      return [{ label: "건수", value: fmtCount(stage.count) }, { label: "소스", value: stage.source }];
  }
}

// ─────────────────────────────────────────────
// Stage pipeline funnel card
// ─────────────────────────────────────────────

const STAGE_NAMES = ["대상종목 선정", "매수대기 발굴", "매수신호·진입", "보유종목 관리", "익절·손절", "마감 복기"];
const STAGE_SUBS = [
  (s: WorkbenchStage) => {
    const newCount = s.rows.filter((r) => (r["decision"] as string) === "pass").length;
    return `통과 ${newCount}`;
  },
  (s: WorkbenchStage) => `전환율 ${s.count > 0 ? "—" : "—"}`,
  (s: WorkbenchStage) => `주문대기 ${s.rows.filter((r) => (r["status"] as string) === "PENDING").length}`,
  (s: WorkbenchStage) => `위험 — · 정상 —`,
  (s: WorkbenchStage) => `체결 ${s.rows.filter((r) => (r["status"] as string) === "FILLED").length} · 실패 ${s.rows.filter((r) => (r["status"] as string) === "CANCELLED" || (r["status"] as string) === "REJECTED").length}`,
  (_s: WorkbenchStage) => `복기 생성 예정`,
];

// ─────────────────────────────────────────────
// Data reliability banner (GO100-303)
// ─────────────────────────────────────────────

const DQ_STATUS_STYLE: Record<DataQualityStatus, { chip: string; dot: string; label: string }> = {
  PASS: { chip: "border-emerald-500/30 bg-emerald-500/10 text-emerald-300", dot: "bg-emerald-400", label: "정상" },
  WARN: { chip: "border-amber-500/30 bg-amber-500/10 text-amber-300", dot: "bg-amber-400", label: "주의" },
  CRITICAL: { chip: "border-red-500/30 bg-red-500/10 text-red-300", dot: "bg-red-400", label: "위험" },
  UNKNOWN: { chip: "border-white/10 bg-white/5 text-gray-400", dot: "bg-gray-500", label: "미확인" },
};

const DQ_SESSION_LABEL: Record<string, string> = {
  NXT_PRE: "NXT 프리마켓",
  KRX_REGULAR: "정규장",
  NXT_AFTER: "NXT 애프터",
  CLOSED: "장 마감",
};

function DqSourceChip({ src }: { src: DataQualitySource }) {
  const style = DQ_STATUS_STYLE[(src.status ?? "UNKNOWN") as DataQualityStatus] ?? DQ_STATUS_STYLE.UNKNOWN;
  const title = [src.source, src.message, src.actual, src.checked_at_kst ? `점검 ${fmtTimeOnly(src.checked_at_kst)}` : null]
    .filter(Boolean)
    .join(" · ");
  return (
    <span
      title={title}
      className={`inline-flex items-center gap-1 rounded-md border px-1.5 py-0.5 text-[10px] font-medium ${style.chip}`}
    >
      <span className={`h-1.5 w-1.5 rounded-full ${style.dot}`} />
      {src.label}
    </span>
  );
}

function DataReliabilityBanner({ dq }: { dq: DataQualitySummary }) {
  const style = DQ_STATUS_STYLE[dq.overall_status] ?? DQ_STATUS_STYLE.UNKNOWN;
  const sessionLabel = DQ_SESSION_LABEL[dq.session] ?? dq.session;
  return (
    <div
      data-testid="ops-data-reliability"
      className="mb-3 flex flex-wrap items-center gap-x-3 gap-y-1.5 rounded-xl border border-white/5 bg-[#0b1421] px-3 py-2"
    >
      <span className={`inline-flex shrink-0 items-center gap-1.5 rounded-lg border px-2 py-0.5 text-[11px] font-bold ${style.chip}`}>
        <span className={`h-1.5 w-1.5 rounded-full ${style.dot}`} />
        데이터 신뢰도 {style.label}
      </span>
      <span className="shrink-0 rounded-md border border-white/10 bg-white/5 px-1.5 py-0.5 text-[10px] text-gray-400">
        {sessionLabel}
      </span>
      <div className="flex flex-wrap items-center gap-1.5">
        {dq.sources.map((s) => (
          <DqSourceChip key={s.source} src={s} />
        ))}
      </div>
      {dq.heal_recent && (
        <span className="shrink-0 rounded-md border border-blue-500/25 bg-blue-500/10 px-1.5 py-0.5 text-[10px] text-blue-300">
          자동복구 시도됨
        </span>
      )}
      <span className="ml-auto shrink-0 text-[10px] text-gray-600">
        점검 {fmtTimeOnly(dq.checked_at_kst)} KST
      </span>
    </div>
  );
}

function StagePill({
  stage,
  idx,
  active,
  onClick,
}: {
  stage: WorkbenchStage;
  idx: number;
  active: boolean;
  onClick: () => void;
}) {
  const isUnavail = stage.status === "unavailable";
  const sub = STAGE_SUBS[idx]?.(stage) ?? "";
  return (
    <button
      type="button"
      onClick={onClick}
      data-testid={`ops-stage-${stage.stage_id}`}
      className={`relative rounded-xl border px-3 py-2.5 text-left transition-all ${
        active
          ? "border-blue-500 bg-blue-500/10 shadow-sm shadow-blue-500/10"
          : "border-white/8 bg-[#0d1726] hover:border-white/15 hover:bg-[#0f1d2f]"
      }`}
    >
      {idx < STAGE_NAMES.length - 1 && (
        <span className="absolute -right-[9px] top-1/2 z-10 -translate-y-1/2 text-[18px] text-gray-600 pointer-events-none">›</span>
      )}
      <div className="text-[10px] text-gray-600 mb-0.5">0{stage.stage_id}</div>
      <div className={`text-[11px] font-bold leading-tight ${active ? "text-blue-200" : "text-gray-400"}`}>
        {STAGE_NAMES[idx] ?? stage.label}
      </div>
      <div className={`mt-1.5 text-2xl font-black tabular-nums ${isUnavail ? "text-gray-700 line-through" : active ? "text-white" : "text-gray-200"}`}>
        {isUnavail ? "N/A" : stage.count}
      </div>
      <div className={`mt-0.5 text-[10px] ${isUnavail ? "text-red-500/60" : "text-gray-600"}`}>
        {isUnavail ? "소스 불가" : sub}
      </div>
    </button>
  );
}

// ─────────────────────────────────────────────
// Main operations page inner component
// ─────────────────────────────────────────────

function OperationsInner() {
  const params = useParams<{ id: string }>();
  const rawSearchParams = useSearchParams();
  const searchParams = useMemo(
    () => rawSearchParams ?? new URLSearchParams(),
    [rawSearchParams]
  );
  const router = useRouter();

  const cardId = Number(params?.id ?? "0");

  // URL state
  const activeStage = Math.min(Math.max(Number(searchParams.get("stage") ?? "1"), 1), 6) as 1 | 2 | 3 | 4 | 5 | 6;
  const defaultViewMode: ViewMode = activeStage === 5 ? "cumulative" : "realtime";
  const viewMode = (searchParams.get("view") ?? defaultViewMode) as ViewMode;
  const modeFilter = (searchParams.get("mode") ?? "all") as ModeFilter;
  const dateFrom = searchParams.get("date_from") ?? "";
  const dateTo = searchParams.get("date_to") ?? "";
  const marketRegime = searchParams.get("market_regime") ?? "";
  const requestedCardVersion = Number(searchParams.get("card_version") ?? "0") || undefined;

  const setParam = useCallback(
    (updates: Record<string, string>) => {
      const p = new URLSearchParams(searchParams.toString());
      for (const [k, v] of Object.entries(updates)) {
        v === "" ? p.delete(k) : p.set(k, v);
      }
      router.replace(`?${p.toString()}`, { scroll: false });
    },
    [router, searchParams]
  );

  // Data state
  const [data, setData] = useState<WorkbenchData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [stale, setStale] = useState(false);
  const [partial, setPartial] = useState(false);
  const [stage1TradeValues, setStage1TradeValues] = useState<CardTradeValueWindowsResponse | null>(null);

  const [proposals, setProposals] = useState<ImprovementProposal[]>([]);
  const [proposalsLoading, setProposalsLoading] = useState(false);

  // Persisted daily results (date_range mode)
  const [dailyResults, setDailyResults] = useState<DailyResultsResponse | null>(null);
  const [dailyResultsLoading, setDailyResultsLoading] = useState(false);
  const [recomputeLoading, setRecomputeLoading] = useState(false);
  const [recomputeMsg, setRecomputeMsg] = useState<string | null>(null);

  const requestSeqRef = useRef(0);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const abortCtrlRef = useRef<AbortController | null>(null);
  const stage1TradeValueRequestRef = useRef<{ signature: string; fetchedAt: number }>({ signature: "", fetchedAt: 0 });

  const fetchData = useCallback(async () => {
    const requestId = ++requestSeqRef.current;
    // Abort in-flight previous request to prevent race conditions
    abortCtrlRef.current?.abort();
    const abortCtrl = new AbortController();
    abortCtrlRef.current = abortCtrl;

    setLoading(true);
    try {
      const isPaper = filterToIsPaper(modeFilter);
      const result = await getCardWorkbench(
        cardId,
        {
          mode: viewMode,
          is_paper: isPaper,
          ...(viewMode === "date_range" && dateFrom ? { date_from: dateFrom } : {}),
          ...(viewMode === "date_range" && dateTo ? { date_to: dateTo } : {}),
          ...(requestedCardVersion ? { card_version: requestedCardVersion } : {}),
          ...(viewMode === "date_range" && marketRegime ? { market_regime: marketRegime } : {}),
        },
        abortCtrl.signal,
      );
      if (requestId === requestSeqRef.current) {
        setData(result);
        setStage1TradeValues(null);
        setError(null);
        setStale(false);
        setPartial(!!(result.performance?.partial ?? (result.diagnostics?.length ?? 0) > 0));
      }
    } catch (e) {
      if ((e as Error)?.name === "AbortError") return; // cancelled — ignore
      if (requestId === requestSeqRef.current) {
        const msg = e instanceof Error ? e.message : "알 수 없는 오류";
        setError(msg);
        if (data) setStale(true);
      }
    } finally {
      if (requestId === requestSeqRef.current) setLoading(false);
    }
  }, [cardId, viewMode, modeFilter, dateFrom, dateTo, requestedCardVersion, marketRegime]); // eslint-disable-line react-hooks/exhaustive-deps

  const fetchProposals = useCallback(async () => {
    if (!cardId) return;
    setProposalsLoading(true);
    try {
      const res = await getImprovementProposals(cardId);
      setProposals(res.items);
    } catch {
      // Proposals endpoint may not exist in dev, silently continue
    } finally {
      setProposalsLoading(false);
    }
  }, [cardId]);

  const fetchDailyResults = useCallback(async () => {
    if (!cardId || viewMode !== "date_range") return;
    setDailyResultsLoading(true);
    try {
      const mode: DailyResultMode = modeFilter === "live" ? "live" : modeFilter === "paper" ? "paper" : "all";
      const res = await getDailyResults(cardId, {
        date_from: dateFrom || undefined,
        date_to: dateTo || undefined,
        mode,
      });
      setDailyResults(res);
    } catch {
      setDailyResults(null);
    } finally {
      setDailyResultsLoading(false);
    }
  }, [cardId, viewMode, modeFilter, dateFrom, dateTo]);

  const handleRecompute = useCallback(async () => {
    if (!cardId) return;
    setRecomputeLoading(true);
    setRecomputeMsg(null);
    try {
      const mode: DailyResultMode = modeFilter === "live" ? "live" : modeFilter === "paper" ? "paper" : "all";
      const result = await recomputeDailyResults(cardId, {
        date_from: dateFrom || undefined,
        date_to: dateTo || undefined,
        mode,
      });
      setRecomputeMsg(`${result.ok}일 저장 완료 (오류 ${result.errors}건)`);
      void fetchDailyResults();
    } catch (e) {
      setRecomputeMsg(`저장 실패: ${e instanceof Error ? e.message : "알 수 없는 오류"}`);
    } finally {
      setRecomputeLoading(false);
    }
  }, [cardId, modeFilter, dateFrom, dateTo, fetchDailyResults]);

  useEffect(() => {
    void fetchData();
  }, [fetchData]);

  useEffect(() => {
    if (activeStage === 6) void fetchProposals();
  }, [activeStage, fetchProposals]);

  useEffect(() => {
    if (viewMode === "date_range") void fetchDailyResults();
    else setDailyResults(null);
  }, [viewMode, fetchDailyResults]);

  useEffect(() => {
    if (!data || activeStage !== 1 || viewMode !== "realtime") return;
    const stage1 = data.stages.find((s) => s.stage_id === 1);
    const stockCodes = (stage1?.rows ?? [])
      .map((row) => row.stock_code)
      .filter((code): code is string => Boolean(code))
      .slice(0, 100);
    if (stockCodes.length === 0) {
      setStage1TradeValues(null);
      stage1TradeValueRequestRef.current = { signature: "", fetchedAt: 0 };
      return;
    }
    const signature = String(cardId) + ":" + stockCodes.join(",");
    const now = Date.now();
    const lastRequest = stage1TradeValueRequestRef.current;
    if (lastRequest.signature === signature && now - lastRequest.fetchedAt < 60_000) return;
    stage1TradeValueRequestRef.current = { signature, fetchedAt: now };
    const abortCtrl = new AbortController();
    void getCardTradeValueWindows(cardId, stockCodes, abortCtrl.signal)
      .then(setStage1TradeValues)
      .catch(() => {
        stage1TradeValueRequestRef.current = { signature: "", fetchedAt: 0 };
        setStage1TradeValues(null);
      });
    return () => abortCtrl.abort();
  }, [activeStage, cardId, data, viewMode]);

  // Polling for realtime mode
  useEffect(() => {
    if (viewMode !== "realtime") {
      if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
      return;
    }
    const start = () => {
      if (pollRef.current) return;
      pollRef.current = setInterval(() => {
        if (document.visibilityState !== "hidden") void fetchData();
      }, 30_000);
    };
    const stop = () => { if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; } };
    const onVis = () => { if (document.visibilityState === "hidden") stop(); else start(); };
    start();
    document.addEventListener("visibilitychange", onVis);
    return () => { stop(); document.removeEventListener("visibilitychange", onVis); };
  }, [viewMode, fetchData]);

  const handleSaveProposal = useCallback(async (item: S6SummaryItem) => {
    if (!cardId) return;
    const todayKst = new Date().toLocaleDateString("sv-SE", { timeZone: "Asia/Seoul" });
    try {
      await createImprovementProposal(cardId, {
        issue_type: item.title,
        priority: (item.priority as "INFO" | "P1" | "P2" | "MONITOR") ?? "P2",
        proposed_action: item.action,
        root_cause: item.evidence,
        auto_generated: true,
        trade_date: todayKst,
      });
      void fetchProposals();
    } catch { /* silently ignore */ }
  }, [cardId, fetchProposals]);

  const handleApprove = useCallback(async (
    id: number,
    backtestNote: string,
    rollbackVersion: number,
    proposedChanges: Record<string, unknown>,
  ) => {
    try {
      await updateImprovementProposal(cardId, id, "approve", {
        backtest_note: backtestNote,
        backtest_result: { summary: backtestNote, verified_at: new Date().toISOString() },
        proposed_changes: proposedChanges,
        rollback_card_version: rollbackVersion,
      });
      void fetchProposals();
    } catch { /* silently ignore */ }
  }, [cardId, fetchProposals]);

  const handleApply = useCallback(async (id: number) => {
    try {
      await updateImprovementProposal(cardId, id, "apply");
      await Promise.all([fetchProposals(), fetchData()]);
    } catch { /* silently ignore */ }
  }, [cardId, fetchData, fetchProposals]);

  const handleReject = useCallback(async (id: number, reason: string) => {
    try {
      await updateImprovementProposal(cardId, id, "reject", { rejection_reason: reason });
      void fetchProposals();
    } catch { /* silently ignore */ }
  }, [cardId, fetchProposals]);

  // Derived
  const card = data?.card;
  const stages = data?.stages ?? [];
  const activeStageData = stages.find((s) => s.stage_id === activeStage) ?? null;
  const activeStageDataWithWindows = useMemo(() => {
    if (!activeStageData || activeStageData.stage_id !== 1 || !stage1TradeValues) return activeStageData;
    const byCode = new Map(stage1TradeValues.items.map((item) => [item.stock_code, item]));
    return {
      ...activeStageData,
      rows: activeStageData.rows.map((row) => {
        const item = row.stock_code ? byCode.get(row.stock_code) : undefined;
        if (!item) return row;
        return {
          ...row,
          trade_value_windows: item.windows,
          rise_context: item.rise_context,
        };
      }),
      summary: {
        ...activeStageData.summary,
        trade_value_windows: stage1TradeValues.summary,
      },
    };
  }, [activeStageData, stage1TradeValues]);
  const isLive = card?.is_live ?? false;
  const maxStocks = card?.max_stocks ?? 0;
  const openCount = stages.find((s) => s.stage_id === 4)?.count ?? 0;
  const showMaxPositionWarning = maxStocks > 0 && openCount >= maxStocks;
  const diagnostics = data?.diagnostics ?? [];
  const kpis = activeStageData ? getStageKpis(activeStageData, card ?? { id: cardId, name: "", status: "", is_active: false, is_live: false, allocated_amount: null, max_stocks: 2, version: 1, thresholds: { stop_loss_pct: null, take_profit_pct: null, trailing_stop_pct: null, time_exit: null, holding_days: null, max_loss_pct: null, max_position_count: null }, updated_at: null }) : [];

  const VIEW_TABS: Array<{ key: ViewMode; label: string }> = [
    { key: "realtime", label: "실시간" },
    { key: "cumulative", label: "누적" },
    { key: "date_range", label: "기간 분석" },
    { key: "lifecycle", label: "건별 추적" },
  ];

  const MODE_FILTERS: Array<{ key: ModeFilter; label: string }> = [
    { key: "all", label: "전체" },
    { key: "live", label: "실매매" },
    { key: "paper", label: "모의매매" },
  ];

  // ── Render ──
  return (
    <div className="min-h-screen bg-[#070b14] pb-24">
      {/* App bar */}
      <div className="sticky top-0 z-20 flex items-center justify-between border-b border-white/5 bg-[#080f1b]/95 px-4 py-3 backdrop-blur sm:px-6">
        <div className="flex min-w-0 items-center gap-3">
          <Link
            href={`/go100/strategies/${cardId}`}
            className="flex items-center gap-1.5 rounded-lg border border-white/10 bg-white/5 px-2.5 py-1.5 text-xs text-gray-400 transition hover:bg-white/10 hover:text-gray-200"
          >
            ← 전략 상세
          </Link>
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <h1 className="truncate text-base font-black text-white">
                {card?.name ?? `전략 #${cardId}`}
              </h1>
              {isLive ? (
                <span className="flex items-center gap-1 rounded-full border border-green-500/30 bg-green-500/10 px-2 py-0.5 text-[11px] font-bold text-green-300">
                  <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-green-400" />
                  LIVE
                </span>
              ) : (
                <span className="rounded-full border border-amber-500/30 bg-amber-500/10 px-2 py-0.5 text-[11px] font-bold text-amber-300">
                  PAPER
                </span>
              )}
              {maxStocks > 0 && (
                <span className="rounded-full border border-white/10 bg-white/5 px-2 py-0.5 text-[11px] text-gray-400">
                  최대 {maxStocks}종목
                </span>
              )}
            </div>
            {data?.checked_at && (
              <p className="text-[10px] text-gray-600 mt-0.5">
                마지막 이벤트 {fmtKst(data.checked_at)} KST
                {stale && " · 데이터 지연 ⚠"}
                {data?.performance?.limited_range && " · 최근 30일 조회"}
              </p>
            )}
          </div>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          {loading && <div className="h-3.5 w-3.5 animate-spin rounded-full border border-blue-400 border-t-transparent" />}
          {!loading && stale && (
            <span className="rounded-full border border-amber-500/30 bg-amber-500/10 px-2 py-0.5 text-[10px] text-amber-300">지연</span>
          )}
          {!loading && partial && !stale && (
            <span className="rounded-full border border-amber-400/30 bg-amber-400/10 px-2 py-0.5 text-[10px] text-amber-200">일부 로딩</span>
          )}
          {!loading && data?.performance?.cache_hit && (
            <span title={`캐시 ${data.performance.cache_age_sec ?? 0}초 전`} className="rounded-full border border-blue-500/20 bg-blue-500/10 px-2 py-0.5 text-[10px] text-blue-300">캐시</span>
          )}
          {!loading && data?.performance?.elapsed_ms != null && (
            <span className="text-[10px] text-gray-600">{data.performance.elapsed_ms}ms</span>
          )}
          <button
            type="button"
            data-testid="ops-refresh"
            onClick={() => { void fetchData(); }}
            disabled={loading}
            className="rounded-lg border border-white/10 bg-white/5 px-3 py-1.5 text-xs font-semibold text-green-300 transition hover:bg-white/10 disabled:opacity-50"
          >
            ↻ 지금 갱신
          </button>
        </div>
      </div>

      <div className="mx-auto max-w-[1480px] px-4 py-4 sm:px-6">
        {/* Toolbar filters */}
        <div className="mb-4 flex flex-wrap items-center gap-2">
          <select
            aria-label="거래모드 필터"
            value={modeFilter}
            disabled={loading}
            onChange={(e) => setParam({ mode: e.target.value })}
            className="rounded-lg border border-white/10 bg-[#101a2a] px-3 py-1.5 text-xs text-gray-200 focus:border-blue-500 focus:outline-none"
          >
            {MODE_FILTERS.map(({ key, label }) => (
              <option key={key} value={key}>{label}</option>
            ))}
          </select>
          <select
            aria-label="카드 버전 필터"
            value={requestedCardVersion ?? data?.filters?.card_version ?? ""}
            disabled={loading}
            onChange={(e) => setParam({ card_version: e.target.value })}
            className="rounded-lg border border-white/10 bg-[#101a2a] px-3 py-1.5 text-xs text-gray-200 focus:border-blue-500 focus:outline-none"
          >
            {(data?.filters?.available_card_versions ?? (card?.version ? [card.version] : [])).map((version) => (
              <option key={version} value={version}>카드 v{version}</option>
            ))}
          </select>
          {viewMode === "date_range" && (
            <>
              <select
                aria-label="시장레짐 필터"
                value={marketRegime}
                disabled={loading}
                onChange={(e) => setParam({ market_regime: e.target.value })}
                className="rounded-lg border border-white/10 bg-[#101a2a] px-3 py-1.5 text-xs text-gray-200 focus:border-blue-500 focus:outline-none"
              >
                <option value="">전체 레짐</option>
                {(data?.filters?.available_market_regimes ?? []).map((regime) => (
                  <option key={regime} value={regime}>{regime}</option>
                ))}
              </select>
              <input
                type="date"
                value={dateFrom}
                aria-label="시작일"
                disabled={loading}
                onChange={(e) => setParam({ date_from: e.target.value })}
                className="rounded-lg border border-white/10 bg-[#101a2a] px-3 py-1.5 text-xs text-gray-200 focus:border-blue-500 focus:outline-none"
              />
              <span className="text-xs text-gray-600">~</span>
              <input
                type="date"
                value={dateTo}
                aria-label="종료일"
                disabled={loading}
                onChange={(e) => setParam({ date_to: e.target.value })}
                className="rounded-lg border border-white/10 bg-[#101a2a] px-3 py-1.5 text-xs text-gray-200 focus:border-blue-500 focus:outline-none"
              />
            </>
          )}
          <button
            type="button"
            className="ml-auto rounded-lg border border-white/10 bg-white/5 px-3 py-1.5 text-xs text-gray-400 transition hover:bg-white/10"
            onClick={() => {
              // Show thresholds — expand threshold section
            }}
          >
            설정 스냅샷
          </button>
        </div>

        {/* Error state */}
        {error && !data && (
          <div className="mb-4 rounded-2xl border border-red-500/20 bg-red-500/5 py-12 text-center">
            <p className="text-sm font-semibold text-red-400">데이터를 불러올 수 없습니다.</p>
            <p className="mt-1 text-xs text-gray-500">{error}</p>
            <button
              type="button"
              onClick={() => { setLoading(true); void fetchData(); }}
              className="mt-4 rounded-lg border border-red-500/30 bg-red-500/10 px-4 py-2 text-sm text-red-300 hover:bg-red-500/20"
            >
              다시 시도
            </button>
          </div>
        )}

        {/* Data reliability banner (GO100-303) */}
        {data?.data_quality && <DataReliabilityBanner dq={data.data_quality} />}

        {/* 6-stage pipeline — skeleton while loading */}
        {loading && !data && viewMode !== "lifecycle" && (
          <div
            className="mb-3 grid gap-2 animate-pulse"
            style={{ gridTemplateColumns: "repeat(6, minmax(0, 1fr))" }}
          >
            {[1, 2, 3, 4, 5, 6].map((n) => (
              <div
                key={n}
                className="h-16 rounded-2xl bg-white/5 border border-white/8"
              />
            ))}
          </div>
        )}

        {/* 6-stage pipeline */}
        {data && viewMode !== "lifecycle" && (
          <div
            className="mb-3 grid gap-2"
            style={{ gridTemplateColumns: "repeat(6, minmax(0, 1fr))" }}
            data-testid="ops-stage-flow"
          >
            {stages.map((s, idx) => (
              <StagePill
                key={s.stage_id}
                stage={s}
                idx={idx}
                active={activeStage === s.stage_id}
                onClick={() => setParam({ stage: String(s.stage_id) })}
              />
            ))}
          </div>
        )}

        {/* Risk notice */}
        {data && showMaxPositionWarning && (
          <div className="mb-3 flex items-center gap-2 rounded-xl border border-amber-500/25 bg-amber-500/8 px-4 py-2.5 text-xs text-amber-300">
            <span>⚠</span>
            <span>
              설정상 최대 보유 {maxStocks}종목이며 현재 {openCount}종목 보유 중입니다.
              신규 매수 신호는 차단하고 차단 사유를 기록합니다.
            </span>
          </div>
        )}

        {/* Diagnostics notice */}
        {diagnostics.length > 0 && (
          <div className="mb-3 rounded-xl border border-amber-400/15 bg-amber-400/5 px-4 py-2.5 text-xs text-amber-200">
            <span className="font-semibold">진단 경고: </span>
            {diagnostics.map((d, i) => (
              <span key={i}>[{d.stage}단계 {d.key}] {d.error}{i < diagnostics.length - 1 ? " · " : ""}</span>
            ))}
          </div>
        )}

        {/* View mode tabs */}
        <div className="mb-4 flex gap-0 border-b border-white/5" data-testid="ops-view-tabs">
          {VIEW_TABS.map(({ key, label }) => (
            <button
              key={key}
              type="button"
              data-testid={`ops-view-${key}`}
              disabled={loading}
              onClick={() => setParam({ view: key })}
              className={`flex items-center gap-1.5 px-4 py-2.5 text-sm transition-colors ${
                viewMode === key
                  ? "border-b-2 border-blue-400 font-semibold text-white"
                  : "text-gray-500 hover:text-gray-200"
              }`}
            >
              {key === "realtime" && (
                <span
                  className={`h-1.5 w-1.5 rounded-full ${viewMode === "realtime" ? "animate-pulse bg-green-400" : "bg-gray-700"}`}
                />
              )}
              {label}
            </button>
          ))}
        </div>

        {data && viewMode === "date_range" && (
          <DailyResultsSection
            data={data}
            dailyResults={dailyResults}
            dailyResultsLoading={dailyResultsLoading}
            recomputeLoading={recomputeLoading}
            recomputeMsg={recomputeMsg}
            onRecompute={() => { void handleRecompute(); }}
          />
        )}

        {/* Lifecycle view */}
        {data && viewMode === "lifecycle" && (
          <div data-testid="ops-lifecycle-panel">
            <h2 className="mb-3 text-sm font-semibold text-gray-300">건별 거래 추적 (매수→포지션→매도→손익)</h2>
            <LifecycleTable items={data.lifecycle_items ?? []} />
          </div>
        )}

        {/* Stage-specific content */}
        {data && viewMode !== "lifecycle" && activeStageData && (
          <>
            {/* KPI metrics */}
            <div className="mb-4 grid grid-cols-2 gap-3 sm:grid-cols-4" data-testid="ops-kpi-row">
              {kpis.map(({ label, value, sub }) => (
                <KpiCard key={label} label={label} value={value} sub={sub} />
              ))}
            </div>

            {/* Main work grid */}
            <div className="grid gap-3 xl:grid-cols-[minmax(0,1.4fr)_minmax(300px,0.6fr)]">
              {/* Left: Stage content */}
              <div className="rounded-2xl border border-white/5 bg-[#0b1421] p-4">
                <div className="mb-3 flex items-center justify-between">
                  <h2 className="text-sm font-semibold text-gray-300" data-testid="ops-stage-title">
                    {STAGE_NAMES[(activeStageData.stage_id - 1)] ?? activeStageData.label}
                    {" · "}
                    <span className="font-normal text-gray-500">
                      {VIEW_TABS.find((t) => t.key === viewMode)?.label ?? viewMode} 현황
                    </span>
                  </h2>
                  <span className="text-[10px] text-gray-600">
                    소스: {activeStageData.source}
                  </span>
                </div>

                {activeStage === 5 && (safeNum(activeStageData.summary?.unresolved_failures) ?? 0) > 0 && (
                  <div data-testid="ops-unresolved-sell-warning" className="mb-3 rounded-xl border border-red-500/30 bg-red-500/10 px-4 py-3 text-sm font-semibold text-red-300">
                    매도 실패 또는 미체결 잔여수량 {fmtCount(safeNum(activeStageData.summary?.unresolved_failures))}건 — 정상 처리 전까지 확인이 필요합니다.
                  </div>
                )}

                {activeStageData.status === "unavailable" ? (
                  <div className="rounded-2xl border border-red-500/20 bg-red-500/5 py-12 text-center">
                    <p className="text-sm font-semibold text-red-400">데이터 소스를 사용할 수 없습니다.</p>
                    <p className="mt-1 text-xs text-gray-500">진단 정보를 확인하세요.</p>
                  </div>
                ) : activeStage === 6 ? (
                  <Stage6Panel
                    stage={activeStageDataWithWindows ?? activeStageData}
                    cardId={cardId}
                    cardVersion={card?.version ?? 1}
                    proposals={proposals}
                    proposalsLoading={proposalsLoading}
                    onSaveProposal={handleSaveProposal}
                    onApprove={handleApprove}
                    onReject={handleReject}
                    onApply={handleApply}
                  />
                ) : activeStage === 1 ? (
                  <Stage1Table
                    rows={(activeStageDataWithWindows ?? activeStageData).rows}
                    stageColumns={(activeStageDataWithWindows ?? activeStageData).stage_columns}
                    summary={(activeStageDataWithWindows ?? activeStageData).summary}
                    cardId={cardId}
                  />
                ) : activeStage === 2 ? (
                  <Stage2Table rows={activeStageData.rows} />
                ) : activeStage === 3 ? (
                  <Stage3Table rows={activeStageData.rows} />
                ) : activeStage === 4 ? (
                  <Stage4Table rows={activeStageData.rows} />
                ) : (
                  <Stage5Table rows={activeStageData.rows} />
                )}
              </div>

              {/* Right sidebar */}
              <div className="space-y-3">
                {/* Funnel chart */}
                <FunnelChart stages={stages} />

                {/* 실시간 파동 상태 */}
                <WaveStatePanel />

                {/* Threshold panel */}
                {card?.thresholds && (
                  <div className="rounded-2xl border border-white/5 bg-[#0b1421] p-4">
                    <h3 className="mb-3 text-sm font-semibold text-gray-300">전략 임계값</h3>
                    <div className="grid grid-cols-2 gap-x-4 gap-y-2">
                      {[
                        ["손절", card.thresholds.stop_loss_pct != null ? `${card.thresholds.stop_loss_pct}%` : "미설정"],
                        ["익절", card.thresholds.take_profit_pct != null ? `${card.thresholds.take_profit_pct}%` : "미설정"],
                        ["추적손절", card.thresholds.trailing_stop_pct != null ? `${card.thresholds.trailing_stop_pct}%` : "미설정"],
                        ["최대종목", card.thresholds.max_position_count != null ? `${card.thresholds.max_position_count}개` : "미설정"],
                        ["최대보유일", card.thresholds.holding_days != null ? `${card.thresholds.holding_days}일` : "미설정"],
                        ["시간청산", card.thresholds.time_exit ? String(card.thresholds.time_exit) : "미설정"],
                      ].map(([label, value]) => (
                        <div key={label} className="border-b border-white/5 py-1.5 last:border-0">
                          <p className="text-[10px] text-gray-600">{label}</p>
                          <p className={`text-xs font-medium ${value === "미설정" ? "italic text-gray-700" : "text-gray-200"}`}>
                            {value}
                          </p>
                        </div>
                      ))}
                    </div>
                  </div>
                )}

                {/* Market hours note */}
                {viewMode === "realtime" && (
                  <div className="rounded-xl border border-white/5 bg-[#0b1421] px-4 py-3">
                    <p className="text-[10px] text-gray-600">
                      실시간 데이터: 장 시간(09:00–15:30 KST) 외에는 마지막 영업일 스냅샷을 표시합니다.
                    </p>
                  </div>
                )}
              </div>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────
// Page export (Suspense boundary for useSearchParams)
// ─────────────────────────────────────────────

export default function StrategyOperationsPage() {
  return (
    <Suspense
      fallback={
        <div className="flex min-h-screen items-center justify-center bg-[#070b14]">
          <div className="h-10 w-10 animate-spin rounded-full border-2 border-blue-500 border-t-transparent" />
        </div>
      }
    >
      <OperationsInner />
    </Suspense>
  );
}
