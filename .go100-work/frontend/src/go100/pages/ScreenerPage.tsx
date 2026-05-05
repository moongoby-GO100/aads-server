"use client";

import { Suspense, useEffect, useMemo, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import {
  getAdvancedScreenerMeta,
  searchAdvancedStocks,
  type ScreenerCondition,
} from "@/go100/api/screenerApi";
import { getStrategyCard } from "@/go100/api/go100Api";
import { formatStock } from "@/go100/lib/stock-format";
import type { FilterExpression, Go100StrategyCard } from "@/go100/types/strategy";

type SortOrder = "asc" | "desc";
type DateMode = "single" | "range";
type SavedScreenState = {
  conditions: ScreenerCondition[];
  activePresetIds: string[];
  exclude: Record<string, boolean>;
  dateMode: DateMode;
  baseDate: string | null;
  dateFrom: string | null;
  dateTo: string | null;
  sortBy: string;
  sortOrder: SortOrder;
  rankLimit: number | null;
  rankFilters: Array<{ sort_by: string; sort_order: SortOrder; limit: number }> | null;
  limit: number;
};

interface SavedScreen {
  id: string;
  name: string;
  savedAt: string;
  state: SavedScreenState;
}

interface ScreenedStock {
  code: string;
  name: string;
  market_cap: number | null;
  current_price: number | null;
  change_rate: number | null;
  volume: number | null;
  signal_hit?: boolean | null;
}

interface StrategyScreenResponse {
  ok: number;
  error?: string;
  message?: string;
  card_id?: number;
  strategy_name?: string;
  count?: number;
  stocks?: ScreenedStock[];
  screened_at?: string;
  source?: "universe+kiwoom" | "universe_only";
  condition_code?: string;
}

interface ScreenerField {
  key: string;
  label: string;
  group: string;
  type: "number" | "select" | "text" | "date";
  operators: string[];
  options?: string[];
  requires_date_range?: boolean;
}

interface ScreenerPreset {
  id: string;
  name: string;
  icon?: string;
  conditions: ScreenerCondition[];
  sort_by?: string;
  sort_order?: SortOrder;
  rank_limit?: number;
  date_range?: "this_week" | "this_month";
}

interface ExcludeOption {
  key: string;
  label: string;
  default: boolean;
}

interface ScreenerMeta {
  fields: ScreenerField[];
  presets: ScreenerPreset[];
  latest_date: string | null;
  available_dates: string[];
  min_date: string | null;
  exclude_options: ExcludeOption[];
}

interface ScreenerRow {
  stock_code: string;
  stock_name: string;
  market?: string | null;
  sector?: string | null;
  close?: number | null;
  change_pct?: number | null;
  volume?: number | null;
  trade_amount?: number | null;
  volume_ratio?: number | null;
  market_cap?: number | null;
  per?: number | null;
  pbr?: number | null;
  roe?: number | null;
  dividend_yield?: number | null;
  rsi_14?: number | null;
  foreign_net_5d?: number | null;
  institution_net_5d?: number | null;
  theme_tags?: string[];
  change_pct_5d?: number | null;
  change_pct_20d?: number | null;
  change_pct_60d?: number | null;
  high_52w_pct?: number | null;
  low_52w_pct?: number | null;
  consecutive_up?: number | null;
  range_change_pct?: number | null;
  range_volume_sum?: number | null;
  range_trade_amount_sum?: number | null;
  range_up_days?: number | null;
  range_trading_days?: number | null;
  range_foreign_net?: number | null;
  range_inst_net?: number | null;
}

interface TableColumn {
  key: keyof ScreenerRow | "stock";
  label: string;
  align?: "left" | "right" | "center";
  period?: boolean;
  range?: boolean;
  sortable?: string;
  render: (row: ScreenerRow) => string;
}

const PRESET_GROUPS: Array<{ key: string; label: string; ids: string[] }> = [
  { key: "value", label: "가치/밸류", ids: ["value", "dividend", "foreign_inst"] },
  { key: "momentum", label: "모멘텀/기술", ids: ["vol_surge", "small_growth", "rsi_oversold", "ma_golden", "macd_buy"] },
  {
    key: "rank",
    label: "순위 조회",
    ids: [
      "rank_change_up",
      "rank_change_down",
      "rank_volume",
      "rank_trade_amount",
      "rank_market_cap",
      "rank_foreign_buy",
      "rank_inst_buy",
      "rank_per_low",
      "rank_dividend",
    ],
  },
  { key: "period", label: "N일 분석", ids: ["weekly_winner", "monthly_winner", "new_high_20d", "near_52w_low", "consecutive_rise"] },
  { key: "range", label: "기간 분석", ids: ["range_week_winner", "range_month_winner", "range_foreign_buy", "range_vol_surge"] },
];

const COLUMNS: TableColumn[] = [
  {
    key: "stock",
    label: "종목",
    align: "left",
    render: row => formatStock(row.stock_name, row.stock_code),
  },
  { key: "close", label: "현재가", align: "right", sortable: "close", render: row => fmtPrice(row.close) },
  { key: "change_pct", label: "등락률", align: "right", sortable: "change_pct", render: row => fmtPct(row.change_pct) },
  { key: "volume", label: "거래량", align: "right", sortable: "volume", render: row => fmtVol(row.volume) },
  { key: "trade_amount", label: "거래대금", align: "right", sortable: "trade_amount", render: row => fmtAmt(row.trade_amount) },
  { key: "volume_ratio", label: "거래량비", align: "right", sortable: "volume_ratio", render: row => fmtRatio(row.volume_ratio) },
  { key: "market_cap", label: "시총", align: "right", sortable: "market_cap", render: row => fmtCap(row.market_cap) },
  { key: "per", label: "PER", align: "right", sortable: "per", render: row => fmtNum(row.per, 1) },
  { key: "pbr", label: "PBR", align: "right", sortable: "pbr", render: row => fmtNum(row.pbr, 2) },
  { key: "rsi_14", label: "RSI", align: "right", sortable: "rsi_14", render: row => fmtNum(row.rsi_14, 0) },
  { key: "foreign_net_5d", label: "외인5일", align: "right", sortable: "foreign_net_5d", render: row => fmtEok(row.foreign_net_5d) },
  { key: "institution_net_5d", label: "기관5일", align: "right", sortable: "institution_net_5d", render: row => fmtEok(row.institution_net_5d) },
  { key: "roe", label: "ROE", align: "right", sortable: "roe", render: row => fmtPctPlain(row.roe) },
  { key: "dividend_yield", label: "배당률", align: "right", sortable: "dividend_yield", render: row => fmtPctPlain(row.dividend_yield) },
  { key: "change_pct_5d", label: "5일%", align: "right", period: true, sortable: "change_pct_5d", render: row => fmtPct(row.change_pct_5d) },
  { key: "change_pct_20d", label: "20일%", align: "right", period: true, sortable: "change_pct_20d", render: row => fmtPct(row.change_pct_20d) },
  { key: "change_pct_60d", label: "60일%", align: "right", period: true, sortable: "change_pct_60d", render: row => fmtPct(row.change_pct_60d) },
  { key: "high_52w_pct", label: "52주고", align: "right", period: true, sortable: "high_52w", render: row => fmtPct(row.high_52w_pct) },
  { key: "low_52w_pct", label: "52주저", align: "right", period: true, sortable: "low_52w", render: row => fmtPctPlain(row.low_52w_pct) },
  { key: "consecutive_up", label: "연속상승", align: "right", period: true, sortable: "consecutive_up", render: row => row.consecutive_up ? `${row.consecutive_up}일` : "-" },
  { key: "range_change_pct", label: "기간%", align: "right", range: true, sortable: "range_change_pct", render: row => fmtPct(row.range_change_pct) },
  { key: "range_volume_sum", label: "기간거래량", align: "right", range: true, sortable: "range_volume_sum", render: row => fmtVol(row.range_volume_sum) },
  { key: "range_trade_amount_sum", label: "기간거래대금", align: "right", range: true, sortable: "range_trade_amount_sum", render: row => fmtAmt(row.range_trade_amount_sum) },
  { key: "range_up_days", label: "양봉일", align: "right", range: true, sortable: "range_up_days", render: row => row.range_up_days != null ? `${row.range_up_days}일` : "-" },
  { key: "range_trading_days", label: "거래일", align: "right", range: true, sortable: "range_trading_days", render: row => row.range_trading_days != null ? `${row.range_trading_days}일` : "-" },
  { key: "range_foreign_net", label: "기간외인", align: "right", range: true, sortable: "range_foreign_net", render: row => fmtEok(row.range_foreign_net) },
  { key: "range_inst_net", label: "기간기관", align: "right", range: true, sortable: "range_inst_net", render: row => fmtEok(row.range_inst_net) },
];

const SAVED_SCREENS_KEY = "go100-screener-saved-v1";
const LAST_SCREEN_KEY = "go100-screener-last-v1";

function getStockChartHref(code: string, name?: string | null) {
  const qs = name ? `?name=${encodeURIComponent(name)}` : "";
  return `/stock/${encodeURIComponent(code)}${qs}`;
}

function extractFilterFields(expr: FilterExpression | null | undefined): string[] {
  if (!expr) return [];
  const fields: string[] = [];
  if (expr.field) fields.push(expr.field);
  if (expr.conditions) {
    for (const c of expr.conditions) fields.push(...extractFilterFields(c as FilterExpression));
  }
  if (expr.condition) fields.push(...extractFilterFields(expr.condition as FilterExpression));
  return [...new Set(fields)];
}

function formatFilterSummary(universeFilter: FilterExpression | null | undefined): string {
  if (!universeFilter || Object.keys(universeFilter).length === 0) return "필터 없음";
  const fields = extractFilterFields(universeFilter);
  if (fields.length === 0) return universeFilter.logic ? `${universeFilter.logic} 조건식` : "조건 적용됨";
  const shown = fields.slice(0, 3).join(", ");
  return fields.length > 3 ? `${shown} 외 ${fields.length - 3}개` : shown;
}

function fmtNum(value: number | null | undefined, digits = 0) {
  if (value == null || Number.isNaN(Number(value))) return "-";
  return Number(value).toLocaleString("ko-KR", { maximumFractionDigits: digits, minimumFractionDigits: digits });
}

function fmtPrice(value: number | null | undefined) {
  return value == null ? "-" : Number(value).toLocaleString("ko-KR");
}

function fmtVol(value: number | null | undefined) {
  if (value == null) return "-";
  const n = Number(value);
  if (Math.abs(n) >= 100000000) return `${(n / 100000000).toFixed(1)}억`;
  if (Math.abs(n) >= 10000) return `${Math.round(n / 10000).toLocaleString("ko-KR")}만`;
  return n.toLocaleString("ko-KR");
}

function fmtAmt(value: number | null | undefined) {
  if (value == null) return "-";
  const eok = Number(value) / 100;
  if (Math.abs(eok) >= 10000) return `${(eok / 10000).toFixed(1)}조`;
  if (Math.abs(eok) >= 1) return `${Math.round(eok).toLocaleString("ko-KR")}억`;
  return `${Number(value).toFixed(0)}백만`;
}

function fmtCap(value: number | null | undefined) {
  if (value == null) return "-";
  const n = Number(value);
  return Math.abs(n) >= 10000 ? `${(n / 10000).toFixed(1)}조` : `${n.toLocaleString("ko-KR")}억`;
}

function fmtPct(value: number | null | undefined) {
  if (value == null) return "-";
  const n = Number(value);
  return `${n > 0 ? "+" : ""}${n.toFixed(2)}%`;
}

function fmtPctPlain(value: number | null | undefined) {
  return value == null ? "-" : `${Number(value).toFixed(1)}%`;
}

function fmtRatio(value: number | null | undefined) {
  return value == null ? "-" : `${Number(value).toFixed(1)}x`;
}

function fmtEok(value: number | null | undefined) {
  if (value == null) return "-";
  const n = Number(value);
  const sign = n > 0 ? "+" : "";
  return Math.abs(n) >= 10000 ? `${sign}${(n / 10000).toFixed(1)}조` : `${sign}${n.toFixed(1)}억`;
}

function fmtDate(value: string | null | undefined) {
  if (!value) return "";
  const d = value.replaceAll("-", "");
  return d.length === 8 ? `${d.slice(0, 4)}-${d.slice(4, 6)}-${d.slice(6, 8)}` : value;
}

function toDate8(value: string | null | undefined) {
  return value ? value.replaceAll("-", "") : null;
}

function readJson<T>(key: string, fallback: T): T {
  if (typeof window === "undefined") return fallback;
  try {
    const raw = window.localStorage.getItem(key);
    return raw ? (JSON.parse(raw) as T) : fallback;
  } catch {
    return fallback;
  }
}

function writeJson<T>(key: string, value: T) {
  if (typeof window === "undefined") return;
  window.localStorage.setItem(key, JSON.stringify(value));
}

function changeClass(value: number | null | undefined) {
  if (value == null) return "";
  if (Number(value) > 0) return "text-red-500";
  if (Number(value) < 0) return "text-blue-500";
  return "text-muted-foreground";
}

function parseInputValue(field: ScreenerField | undefined, op: string, value: string, valueTo: string) {
  if (op === "between") {
    const from = field?.type === "number" ? Number(value) : value;
    const to = field?.type === "number" ? Number(valueTo) : valueTo;
    return [from, to];
  }
  if (field?.type === "number") return Number(value);
  return value;
}

function conditionKey(condition: ScreenerCondition) {
  return `${condition.field}|${condition.op}|${JSON.stringify(condition.value)}`;
}

function ScreenerContent() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const strategyId = searchParams?.get("strategy_id") ?? null;

  const [meta, setMeta] = useState<ScreenerMeta | null>(null);
  const [metaError, setMetaError] = useState<string | null>(null);
  const [conditions, setConditions] = useState<ScreenerCondition[]>([]);
  const [activePresetIds, setActivePresetIds] = useState<string[]>([]);
  const [exclude, setExclude] = useState<Record<string, boolean>>({});
  const [fieldKey, setFieldKey] = useState("");
  const [operator, setOperator] = useState("");
  const [value, setValue] = useState("");
  const [valueTo, setValueTo] = useState("");
  const [dateMode, setDateMode] = useState<DateMode>("single");
  const [baseDate, setBaseDate] = useState<string | null>(null);
  const [dateFrom, setDateFrom] = useState<string | null>(null);
  const [dateTo, setDateTo] = useState<string | null>(null);
  const [sortBy, setSortBy] = useState("market_cap");
  const [sortOrder, setSortOrder] = useState<SortOrder>("desc");
  const [rankLimit, setRankLimit] = useState<number | null>(null);
  const [rankFilters, setRankFilters] = useState<Array<{ sort_by: string; sort_order: SortOrder; limit: number }> | null>(null);
  const [page, setPage] = useState(1);
  const [limit, setLimit] = useState(50);
  const [rows, setRows] = useState<ScreenerRow[]>([]);
  const [total, setTotal] = useState(0);
  const [resultBaseDate, setResultBaseDate] = useState<string | null>(null);
  const [resultDateFrom, setResultDateFrom] = useState<string | null>(null);
  const [resultDateTo, setResultDateTo] = useState<string | null>(null);
  const [isRealtime, setIsRealtime] = useState(false);
  const [loading, setLoading] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);
  const [savedScreens, setSavedScreens] = useState<SavedScreen[]>([]);
  const [saveName, setSaveName] = useState("");
  const [selectedRow, setSelectedRow] = useState<ScreenerRow | null>(null);

  const [card, setCard] = useState<Go100StrategyCard | null>(null);
  const [screenResult, setScreenResult] = useState<StrategyScreenResponse | null>(null);
  const [screenError, setScreenError] = useState<string | null>(null);
  const [cardLoading, setCardLoading] = useState(false);

  const selectedField = useMemo(
    () => meta?.fields.find(field => field.key === fieldKey),
    [fieldKey, meta?.fields],
  );

  const visibleFields = useMemo(
    () => (meta?.fields || []).filter(field => !field.requires_date_range || dateMode === "range"),
    [dateMode, meta?.fields],
  );

  const visibleColumns = useMemo(() => {
    const hasPeriod = rows.some(row => row.change_pct_5d != null || row.change_pct_20d != null || row.change_pct_60d != null);
    const hasRange = rows.some(row => row.range_change_pct != null);
    return COLUMNS.filter(column => {
      if (column.period && !hasPeriod) return false;
      if (column.range && !hasRange) return false;
      return true;
    });
  }, [rows]);

  const totalPages = Math.max(1, Math.ceil(total / limit));

  const captureState = (): SavedScreenState => ({
    conditions,
    activePresetIds,
    exclude,
    dateMode,
    baseDate,
    dateFrom,
    dateTo,
    sortBy,
    sortOrder,
    rankLimit,
    rankFilters,
    limit,
  });

  const applyState = (state: SavedScreenState) => {
    setConditions(state.conditions || []);
    setActivePresetIds(state.activePresetIds || []);
    setExclude(state.exclude || {});
    setDateMode(state.dateMode || "single");
    setBaseDate(state.baseDate ?? null);
    setDateFrom(state.dateFrom ?? null);
    setDateTo(state.dateTo ?? meta?.latest_date ?? null);
    setSortBy(state.sortBy || "market_cap");
    setSortOrder(state.sortOrder || "desc");
    setRankLimit(state.rankLimit ?? null);
    setRankFilters(state.rankFilters ?? null);
    setLimit(state.limit || 50);
    setPage(1);
  };

  const saveCurrentScreen = () => {
    const name = saveName.trim() || `조건 ${savedScreens.length + 1}`;
    const next: SavedScreen = {
      id: `${Date.now()}`,
      name,
      savedAt: new Date().toISOString(),
      state: captureState(),
    };
    const screens = [next, ...savedScreens.filter(item => item.name !== name)].slice(0, 12);
    setSavedScreens(screens);
    writeJson(SAVED_SCREENS_KEY, screens);
    setSaveName("");
  };

  const deleteSavedScreen = (id: string) => {
    const screens = savedScreens.filter(item => item.id !== id);
    setSavedScreens(screens);
    writeJson(SAVED_SCREENS_KEY, screens);
  };

  useEffect(() => {
    getAdvancedScreenerMeta()
      .then((data: ScreenerMeta) => {
        setMeta(data);
        setMetaError(null);
        setDateTo(data.latest_date);
        const defaults: Record<string, boolean> = {};
        data.exclude_options.forEach(option => {
          defaults[option.key] = !!option.default;
        });
        setExclude(defaults);
      })
      .catch(err => {
        setMetaError(err instanceof Error ? err.message : "필터 정보를 불러오지 못했습니다.");
      });
  }, []);

  useEffect(() => {
    setSavedScreens(readJson<SavedScreen[]>(SAVED_SCREENS_KEY, []));
    const lastState = readJson<SavedScreenState | null>(LAST_SCREEN_KEY, null);
    if (lastState) applyState(lastState);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!strategyId) return;
    const id = parseInt(strategyId, 10);
    if (Number.isNaN(id)) {
      setScreenError("잘못된 전략카드 ID입니다.");
      return;
    }

    setCardLoading(true);
    setScreenError(null);
    setCard(null);
    setScreenResult(null);

    Promise.all([
      getStrategyCard(id),
      fetch(`/api/go100/strategy-cards/${id}/screen?with_signals=true`, { credentials: "include" }).then(r => r.json()),
    ])
      .then(([cardData, screenData]) => {
        setCard(cardData as Go100StrategyCard);
        setScreenResult(screenData as StrategyScreenResponse);
        if (!screenData || screenData.ok !== 1) {
          setScreenError(screenData?.message || "조건검색 결과를 불러오지 못했습니다.");
        }
      })
      .catch(err => {
        setScreenError(err instanceof Error ? err.message : "데이터를 불러오지 못했습니다.");
      })
      .finally(() => setCardLoading(false));
  }, [strategyId]);

  const runSearch = (
    targetPage = page,
    overrides: Partial<{ sortBy: string; sortOrder: SortOrder; limit: number }> = {},
  ) => {
    setLoading(true);
    setSearchError(null);
    const effectiveSortBy = overrides.sortBy || sortBy;
    const effectiveSortOrder = overrides.sortOrder || sortOrder;
    const effectiveLimit = overrides.limit || limit;
    const excludeList = Object.entries(exclude)
      .filter(([, enabled]) => enabled)
      .map(([key]) => key);

    searchAdvancedStocks({
      conditions,
      sort_by: effectiveSortBy,
      sort_order: effectiveSortOrder,
      page: targetPage,
      limit: effectiveLimit,
      base_date: dateMode === "single" ? baseDate : null,
      date_from: dateMode === "range" ? dateFrom : null,
      date_to: dateMode === "range" ? dateTo : null,
      rank_limit: rankFilters ? null : rankLimit,
      rank_filters: rankFilters,
      exclude: excludeList,
    })
      .then(data => {
        writeJson(LAST_SCREEN_KEY, captureState());
        setRows(data.items || []);
        setTotal(data.total || 0);
        setPage(data.page || targetPage);
        setResultBaseDate(data.base_date || null);
        setResultDateFrom(data.date_from || null);
        setResultDateTo(data.date_to || null);
        setIsRealtime(!!data.is_realtime);
      })
      .catch(err => {
        setRows([]);
        setTotal(0);
        setSearchError(err instanceof Error ? err.message : "검색에 실패했습니다.");
      })
      .finally(() => setLoading(false));
  };

  const addCondition = () => {
    if (!selectedField || !operator || !value || (operator === "between" && !valueTo)) return;
    const nextCondition: ScreenerCondition = {
      field: selectedField.key,
      op: operator,
      value: parseInputValue(selectedField, operator, value, valueTo),
    };
    setConditions(prev => {
      const nextKey = conditionKey(nextCondition);
      if (prev.some(item => conditionKey(item) === nextKey)) return prev;
      return [...prev, nextCondition];
    });
    setValue("");
    setValueTo("");
  };

  const removeCondition = (index: number) => {
    setConditions(prev => prev.filter((_, i) => i !== index));
    setActivePresetIds([]);
    setRankLimit(null);
    setRankFilters(null);
  };

  const resetSearch = () => {
    setConditions([]);
    setActivePresetIds([]);
    setRankLimit(null);
    setRankFilters(null);
    setSortBy("market_cap");
    setSortOrder("desc");
    setDateMode("single");
    setBaseDate(null);
    setDateFrom(null);
    setDateTo(meta?.latest_date || null);
    setRows([]);
    setTotal(0);
    setPage(1);
    setSelectedRow(null);
  };

  const applyPreset = (preset: ScreenerPreset) => {
    const isActive = activePresetIds.includes(preset.id);
    const presetConditionKeys = new Set((preset.conditions || []).map(conditionKey));
    let nextActive = isActive
      ? activePresetIds.filter(id => id !== preset.id)
      : [...activePresetIds, preset.id];

    setConditions(prev => {
      if (isActive) return prev.filter(condition => !presetConditionKeys.has(conditionKey(condition)));
      const existing = new Set(prev.map(conditionKey));
      const additions = (preset.conditions || []).filter(condition => !existing.has(conditionKey(condition)));
      return [...prev, ...additions];
    });

    if (preset.date_range && !isActive) {
      setDateMode("range");
      setQuickRange(preset.date_range === "this_week" ? "week" : "month");
    }

    setActivePresetIds(nextActive);
    const activeRankPresets = nextActive
      .map(id => meta?.presets.find(p => p.id === id))
      .filter((p): p is ScreenerPreset => !!p && !!p.rank_limit);

    if (activeRankPresets.length >= 2) {
      const filters = activeRankPresets.map(p => ({
        sort_by: p.sort_by || "market_cap",
        sort_order: p.sort_order || "desc",
        limit: p.rank_limit || 100,
      }));
      setRankFilters(filters);
      setRankLimit(null);
      setSortBy(filters[0].sort_by);
      setSortOrder(filters[0].sort_order);
    } else if (activeRankPresets.length === 1) {
      const p = activeRankPresets[0];
      setRankFilters(null);
      setRankLimit(p.rank_limit || 100);
      setSortBy(p.sort_by || "market_cap");
      setSortOrder(p.sort_order || "desc");
    } else {
      setRankFilters(null);
      setRankLimit(null);
      if (!isActive && preset.sort_by) {
        setSortBy(preset.sort_by);
        setSortOrder(preset.sort_order || "desc");
      }
    }
    setPage(1);
  };

  const setQuickRange = (period: "week" | "month" | "3month") => {
    const latest = meta?.latest_date || null;
    const dates = meta?.available_dates || [];
    setDateTo(latest);
    if (period === "week") setDateFrom(dates[4] || dates[dates.length - 1] || latest);
    if (period === "month") setDateFrom(dates[19] || dates[dates.length - 1] || latest);
    if (period === "3month" && latest?.length === 8) {
      const y = Number(latest.slice(0, 4));
      const m = Number(latest.slice(4, 6));
      const d = latest.slice(6, 8);
      const shifted = new Date(y, m - 4, Number(d));
      setDateFrom(`${shifted.getFullYear()}${String(shifted.getMonth() + 1).padStart(2, "0")}${String(shifted.getDate()).padStart(2, "0")}`);
    }
  };

  const changeSort = (key: string) => {
    const nextOrder = sortBy === key && sortOrder === "desc" ? "asc" : "desc";
    setSortBy(key);
    setSortOrder(nextOrder);
    setPage(1);
    runSearch(1, { sortBy: key, sortOrder: nextOrder });
  };

  const goPage = (nextPage: number) => {
    const bounded = Math.min(Math.max(nextPage, 1), totalPages);
    setPage(bounded);
    runSearch(bounded);
  };

  const exportCsv = () => {
    if (rows.length === 0) return;
    const header = ["종목", "시장", "업종", "현재가", "등락률", "거래량", "거래대금", "시총", "PER", "PBR", "ROE"];
    const body = rows.map(row => [
      formatStock(row.stock_name, row.stock_code),
      row.market || "",
      row.sector || "",
      row.close ?? "",
      row.change_pct ?? "",
      row.volume ?? "",
      row.trade_amount ?? "",
      row.market_cap ?? "",
      row.per ?? "",
      row.pbr ?? "",
      row.roe ?? "",
    ]);
    const csv = [header, ...body].map(line => line.map(cell => `"${String(cell).replaceAll("\"", "\"\"")}"`).join(",")).join("\n");
    const blob = new Blob([`\uFEFF${csv}`], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `go100-screener-${new Date().toISOString().slice(0, 10)}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  };

  const selectedMetrics = selectedRow
    ? [
        { label: "현재가", value: fmtPrice(selectedRow.close) },
        { label: "등락률", value: fmtPct(selectedRow.change_pct), tone: changeClass(selectedRow.change_pct) },
        { label: "거래대금", value: fmtAmt(selectedRow.trade_amount) },
        { label: "시총", value: fmtCap(selectedRow.market_cap) },
        { label: "PER/PBR", value: `${fmtNum(selectedRow.per, 1)} / ${fmtNum(selectedRow.pbr, 2)}` },
        { label: "RSI", value: fmtNum(selectedRow.rsi_14, 0) },
        { label: "외인5일", value: fmtEok(selectedRow.foreign_net_5d), tone: changeClass(selectedRow.foreign_net_5d) },
        { label: "기관5일", value: fmtEok(selectedRow.institution_net_5d), tone: changeClass(selectedRow.institution_net_5d) },
      ]
    : [];

  if (strategyId) {
    return (
      <div className="mx-auto max-w-6xl p-6">
        <div className="mb-4 flex items-center gap-3">
          <button onClick={() => router.back()} className="text-sm text-muted-foreground transition-colors hover:text-foreground">
            뒤로
          </button>
          <h1 className="text-2xl font-bold">전략카드 조건검색</h1>
        </div>

        {cardLoading && <div className="py-16 text-center text-muted-foreground">불러오는 중...</div>}

        {!cardLoading && screenError && (
          <div className="mb-4 rounded border border-red-500/30 bg-red-500/10 p-4 text-sm text-red-400">{screenError}</div>
        )}

        {!cardLoading && card && (
          <div className="mb-6 flex flex-wrap gap-6 rounded bg-muted/50 p-4">
            <div>
              <div className="mb-0.5 text-xs text-muted-foreground">전략명</div>
              <div className="font-semibold">{card.strategy_name}</div>
            </div>
            <div>
              <div className="mb-0.5 text-xs text-muted-foreground">적용 필터</div>
              <div className="text-sm">{formatFilterSummary(card.universe_filter)}</div>
            </div>
            {screenResult?.ok === 1 && (
              <>
                <div>
                  <div className="mb-0.5 text-xs text-muted-foreground">결과 종목 수</div>
                  <div className="text-sm font-medium">{screenResult.stocks?.length ?? 0}개</div>
                </div>
                {screenResult.screened_at && (
                  <div>
                    <div className="mb-0.5 text-xs text-muted-foreground">조회 시각</div>
                    <div className="text-sm">{screenResult.screened_at.replace("T", " ").slice(0, 19)}</div>
                  </div>
                )}
                {screenResult.source && (
                  <div>
                    <div className="mb-0.5 text-xs text-muted-foreground">소스</div>
                    <div className="text-sm">{screenResult.source === "universe+kiwoom" ? "유니버스+키움" : "유니버스"}</div>
                  </div>
                )}
              </>
            )}
          </div>
        )}

        {!cardLoading && !screenError && screenResult?.ok === 1 && (
          (screenResult.stocks?.length ?? 0) === 0 ? (
            <div className="py-16 text-center text-muted-foreground">조건에 맞는 종목이 없습니다</div>
          ) : (
            <div className="overflow-x-auto">
              <div className="mb-2 text-sm text-muted-foreground">{screenResult.stocks?.length ?? 0}개 종목</div>
              <table className="w-full border-collapse text-sm">
                <thead>
                  <tr className="bg-muted">
                    <th className="min-w-[160px] p-2 text-left">종목</th>
                    <th className="p-2 text-right">현재가</th>
                    <th className="p-2 text-right">등락률</th>
                    <th className="p-2 text-right">시총(억)</th>
                    <th className="p-2 text-right">거래량</th>
                    <th className="p-2 text-center">신호</th>
                    <th className="p-2"></th>
                  </tr>
                </thead>
                <tbody>
                  {screenResult.stocks?.map((stock, i) => (
                    <tr
                      key={`${stock.code}-${i}`}
                      className={`cursor-pointer border-b border-border hover:bg-muted/50${stock.signal_hit ? " border-l-4 border-l-amber-400" : ""}`}
                      onClick={() => router.push(getStockChartHref(stock.code, stock.name))}
                    >
                      <td className="p-2">{formatStock(stock.name, stock.code)}</td>
                      <td className="p-2 text-right">{stock.current_price?.toLocaleString() ?? "-"}</td>
                      <td className={`p-2 text-right ${changeClass(stock.change_rate)}`}>
                        {stock.change_rate != null ? `${stock.change_rate >= 0 ? "+" : ""}${stock.change_rate.toFixed(2)}%` : "-"}
                      </td>
                      <td className="p-2 text-right">{stock.market_cap != null ? (stock.market_cap / 100000000).toFixed(0) : "-"}</td>
                      <td className="p-2 text-right">{stock.volume?.toLocaleString() ?? "-"}</td>
                      <td className="p-2 text-center">{stock.signal_hit ? "적중" : ""}</td>
                      <td className="p-2"><button className="text-xs text-primary">분석</button></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )
        )}
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-7xl p-6">
      <div className="mb-5 flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="mb-1 text-2xl font-bold">종목 스크리너</h1>
          <p className="text-sm text-muted-foreground">프리셋, 기술지표, 기간, 제외조건을 조합해 종목을 검색합니다.</p>
        </div>
        <div className="flex flex-wrap justify-end gap-2">
          <input
            value={saveName}
            onChange={event => setSaveName(event.target.value)}
            placeholder="저장명"
            className="w-32 rounded border border-border bg-background px-3 py-2 text-sm"
          />
          <button onClick={saveCurrentScreen} className="rounded border border-border px-3 py-2 text-sm hover:bg-muted">조건 저장</button>
          <button onClick={resetSearch} className="rounded border border-border px-3 py-2 text-sm hover:bg-muted">초기화</button>
          <button onClick={exportCsv} disabled={rows.length === 0} className="rounded border border-border px-3 py-2 text-sm hover:bg-muted disabled:opacity-40">CSV</button>
          <button onClick={() => runSearch(1)} disabled={loading || !!metaError} className="rounded bg-primary px-4 py-2 text-sm text-primary-foreground hover:bg-primary/90 disabled:opacity-50">
            {loading ? "검색 중..." : "검색"}
          </button>
        </div>
      </div>

      {metaError && <div className="mb-4 rounded border border-red-500/30 bg-red-500/10 p-4 text-sm text-red-400">{metaError}</div>}

      {savedScreens.length > 0 && (
        <section className="mb-4 flex flex-wrap items-center gap-2 rounded border border-border bg-muted/20 p-3">
          <span className="mr-1 text-xs font-medium text-muted-foreground">내 저장 조건</span>
          {savedScreens.map(screen => (
            <span key={screen.id} className="inline-flex items-center overflow-hidden rounded border border-border text-xs">
              <button
                onClick={() => applyState(screen.state)}
                className="px-3 py-1.5 hover:bg-muted"
                title={new Date(screen.savedAt).toLocaleString("ko-KR")}
              >
                {screen.name}
              </button>
              <button onClick={() => deleteSavedScreen(screen.id)} className="border-l border-border px-2 py-1.5 text-muted-foreground hover:bg-muted">x</button>
            </span>
          ))}
        </section>
      )}

      <section className="mb-4 rounded border border-border bg-muted/30 p-4">
        <div className="mb-3 flex flex-wrap items-center gap-2">
          <select
            value={dateMode}
            onChange={event => {
              const mode = event.target.value as DateMode;
              setDateMode(mode);
              if (mode === "range" && !dateFrom) setQuickRange("week");
            }}
            className="rounded border border-border bg-background px-2 py-2 text-sm"
          >
            <option value="single">기준일</option>
            <option value="range">기간</option>
          </select>
          {dateMode === "single" ? (
            <select value={baseDate || ""} onChange={event => setBaseDate(event.target.value || null)} className="rounded border border-border bg-background px-2 py-2 text-sm">
              <option value="">최신 ({fmtDate(meta?.latest_date) || "-"})</option>
              {(meta?.available_dates || []).map(date => (
                <option key={date} value={date}>{fmtDate(date)}</option>
              ))}
            </select>
          ) : (
            <>
              <input type="date" value={fmtDate(dateFrom)} onChange={event => setDateFrom(toDate8(event.target.value))} className="rounded border border-border bg-background px-2 py-2 text-sm" />
              <span className="text-muted-foreground">~</span>
              <input type="date" value={fmtDate(dateTo)} onChange={event => setDateTo(toDate8(event.target.value))} className="rounded border border-border bg-background px-2 py-2 text-sm" />
              <button onClick={() => setQuickRange("week")} className="rounded border border-border px-2 py-2 text-xs hover:bg-muted">1주</button>
              <button onClick={() => setQuickRange("month")} className="rounded border border-border px-2 py-2 text-xs hover:bg-muted">1달</button>
              <button onClick={() => setQuickRange("3month")} className="rounded border border-border px-2 py-2 text-xs hover:bg-muted">3달</button>
            </>
          )}
          <select value={limit} onChange={event => setLimit(Number(event.target.value))} className="rounded border border-border bg-background px-2 py-2 text-sm">
            {[20, 50, 100, 200].map(n => <option key={n} value={n}>{n}개</option>)}
          </select>
        </div>

        <div className="mb-3 grid gap-2 md:grid-cols-[minmax(160px,1.4fr)_120px_minmax(120px,1fr)_minmax(120px,1fr)_auto]">
          <select
            value={fieldKey}
            onChange={event => {
              const next = event.target.value;
              const field = meta?.fields.find(item => item.key === next);
              setFieldKey(next);
              setOperator(field?.operators[0] || "");
              setValue("");
              setValueTo("");
            }}
            className="rounded border border-border bg-background px-2 py-2 text-sm"
          >
            <option value="">필드 선택</option>
            {Object.entries(
              visibleFields.reduce<Record<string, ScreenerField[]>>((groups, field) => {
                groups[field.group] = [...(groups[field.group] || []), field];
                return groups;
              }, {}),
            ).map(([group, fields]) => (
              <optgroup key={group} label={group}>
                {fields.map(field => <option key={field.key} value={field.key}>{field.label}</option>)}
              </optgroup>
            ))}
          </select>
          <select value={operator} onChange={event => setOperator(event.target.value)} className="rounded border border-border bg-background px-2 py-2 text-sm">
            {(selectedField?.operators || []).map(op => <option key={op} value={op}>{op === "eq" ? "=" : op === "like" ? "포함" : op}</option>)}
          </select>
          {selectedField?.type === "select" ? (
            <select value={value} onChange={event => setValue(event.target.value)} className="rounded border border-border bg-background px-2 py-2 text-sm">
              <option value="">값 선택</option>
              {(selectedField.options || []).map(option => <option key={option} value={option}>{option}</option>)}
            </select>
          ) : (
            <input
              type={selectedField?.type === "number" ? "number" : selectedField?.type === "date" ? "date" : "text"}
              value={value}
              onChange={event => setValue(event.target.value)}
              placeholder={selectedField?.label || "값"}
              className="rounded border border-border bg-background px-2 py-2 text-sm"
            />
          )}
          {operator === "between" ? (
            <input
              type={selectedField?.type === "number" ? "number" : "text"}
              value={valueTo}
              onChange={event => setValueTo(event.target.value)}
              placeholder="끝값"
              className="rounded border border-border bg-background px-2 py-2 text-sm"
            />
          ) : <div />}
          <button onClick={addCondition} className="rounded border border-border px-3 py-2 text-sm hover:bg-muted">조건 추가</button>
        </div>

        <div className="mb-3 flex flex-wrap gap-2">
          {conditions.length === 0 ? (
            <span className="text-sm text-muted-foreground">조건 없음</span>
          ) : conditions.map((condition, index) => {
            const field = meta?.fields.find(item => item.key === condition.field);
            return (
              <button key={`${conditionKey(condition)}-${index}`} onClick={() => removeCondition(index)} className="rounded-full border border-primary/30 bg-primary/10 px-3 py-1 text-xs text-primary">
                {field?.label || condition.field} {condition.op === "eq" ? "=" : condition.op} {Array.isArray(condition.value) ? condition.value.join("~") : condition.value} x
              </button>
            );
          })}
        </div>

        <div className="flex flex-wrap gap-3 text-xs text-muted-foreground">
          {(meta?.exclude_options || []).map(option => (
            <label key={option.key} className="flex items-center gap-1">
              <input type="checkbox" checked={!!exclude[option.key]} onChange={event => setExclude(prev => ({ ...prev, [option.key]: event.target.checked }))} />
              {option.label}
            </label>
          ))}
        </div>
      </section>

      <section className="mb-4 space-y-3">
        {PRESET_GROUPS.map(group => {
          const presets = group.ids.map(id => meta?.presets.find(p => p.id === id)).filter((p): p is ScreenerPreset => !!p);
          if (presets.length === 0) return null;
          return (
            <div key={group.key} className="flex flex-wrap items-center gap-2">
              <span className="w-20 shrink-0 text-xs font-medium text-muted-foreground">{group.label}</span>
              {presets.map(preset => (
                <button
                  key={preset.id}
                  onClick={() => applyPreset(preset)}
                  className={`rounded border px-3 py-1.5 text-xs ${activePresetIds.includes(preset.id) ? "border-primary bg-primary text-primary-foreground" : "border-border hover:bg-muted"}`}
                  title={(preset.conditions || []).map(c => `${c.field} ${c.op} ${c.value}`).join(", ")}
                >
                  {preset.name}
                </button>
              ))}
            </div>
          );
        })}
      </section>

      <section className="mb-3 flex flex-wrap items-center justify-between gap-2 text-sm text-muted-foreground">
        <div>
          {loading ? "검색 중..." : `${total.toLocaleString("ko-KR")}개 결과`}
          {resultBaseDate ? ` | 기준일 ${resultBaseDate}` : ""}
          {resultDateFrom && resultDateTo ? ` | 기간 ${resultDateFrom} ~ ${resultDateTo}` : ""}
          {isRealtime ? " | 장중 스냅샷" : ""}
          {rankFilters ? ` | 순위 교집합 ${rankFilters.length}개` : rankLimit ? ` | TOP ${rankLimit}` : ""}
        </div>
        {searchError && <div className="text-red-400">검색 오류: {searchError}</div>}
      </section>

      <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_320px]">
        <div className="overflow-x-auto rounded border border-border">
          <table className="w-full min-w-[1180px] border-collapse text-sm">
            <thead>
              <tr className="bg-muted">
                {visibleColumns.map(column => (
                  <th key={column.key} className={`p-2 ${column.align === "right" ? "text-right" : column.align === "center" ? "text-center" : "text-left"}`}>
                    {column.sortable ? (
                      <button onClick={() => changeSort(column.sortable!)} className="inline-flex items-center gap-1 hover:text-primary">
                        {column.label}
                        {sortBy === column.sortable ? (sortOrder === "desc" ? "↓" : "↑") : ""}
                      </button>
                    ) : column.label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.length === 0 ? (
                <tr>
                  <td colSpan={visibleColumns.length} className="p-10 text-center text-muted-foreground">
                    {loading ? "검색 중..." : "프리셋이나 조건을 선택한 뒤 검색하세요"}
                  </td>
                </tr>
              ) : rows.map(row => (
                <tr
                  key={row.stock_code}
                  className={`cursor-pointer border-t border-border hover:bg-muted/50 ${selectedRow?.stock_code === row.stock_code ? "bg-primary/10" : ""}`}
                  onClick={() => setSelectedRow(row)}
                  onDoubleClick={() => router.push(getStockChartHref(row.stock_code, row.stock_name))}
                >
                  {visibleColumns.map(column => (
                    <td key={`${row.stock_code}-${column.key}`} className={`p-2 ${column.align === "right" ? `text-right ${column.key === "change_pct" || column.key === "change_pct_5d" || column.key === "change_pct_20d" || column.key === "change_pct_60d" || column.key === "range_change_pct" ? changeClass(row[column.key as keyof ScreenerRow] as number | null | undefined) : ""}` : column.align === "center" ? "text-center" : "text-left"}`}>
                      {column.render(row)}
                      {column.key === "stock" && row.theme_tags && row.theme_tags.length > 0 ? (
                        <div className="mt-1 truncate text-xs text-muted-foreground">{row.theme_tags.slice(0, 2).join(", ")}</div>
                      ) : null}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <aside className="rounded border border-border bg-muted/20 p-4">
          {selectedRow ? (
            <div className="space-y-4">
              <div>
                <div className="text-lg font-semibold">{formatStock(selectedRow.stock_name, selectedRow.stock_code)}</div>
                <div className="mt-1 text-xs text-muted-foreground">{[selectedRow.market, selectedRow.sector].filter(Boolean).join(" · ") || "시장 정보 없음"}</div>
              </div>
              <div className="grid grid-cols-2 gap-2">
                {selectedMetrics.map(metric => (
                  <div key={metric.label} className="rounded border border-border bg-background/60 p-2">
                    <div className="text-xs text-muted-foreground">{metric.label}</div>
                    <div className={`mt-1 text-sm font-medium tabular-nums ${metric.tone || ""}`}>{metric.value}</div>
                  </div>
                ))}
              </div>
              {selectedRow.theme_tags && selectedRow.theme_tags.length > 0 && (
                <div className="flex flex-wrap gap-1">
                  {selectedRow.theme_tags.slice(0, 6).map(tag => (
                    <span key={tag} className="rounded border border-border px-2 py-1 text-xs text-muted-foreground">{tag}</span>
                  ))}
                </div>
              )}
              <div className="grid grid-cols-2 gap-2">
                <button onClick={() => router.push(getStockChartHref(selectedRow.stock_code, selectedRow.stock_name))} className="rounded bg-primary px-3 py-2 text-sm text-primary-foreground hover:bg-primary/90">차트</button>
                <button onClick={() => router.push(`/go100/company?code=${encodeURIComponent(selectedRow.stock_code)}&tab=analysis`)} className="rounded border border-border px-3 py-2 text-sm hover:bg-muted">분석</button>
              </div>
            </div>
          ) : (
            <div className="py-12 text-center text-sm text-muted-foreground">종목을 선택하면 핵심 지표와 차트 이동 버튼이 표시됩니다.</div>
          )}
        </aside>
      </div>

      <div className="mt-4 flex flex-wrap items-center justify-center gap-2">
        <button onClick={() => goPage(1)} disabled={page <= 1 || loading} className="rounded border border-border px-3 py-1.5 text-sm disabled:opacity-40">처음</button>
        <button onClick={() => goPage(page - 1)} disabled={page <= 1 || loading} className="rounded border border-border px-3 py-1.5 text-sm disabled:opacity-40">이전</button>
        <span className="px-3 py-1.5 text-sm text-muted-foreground">{page} / {totalPages}</span>
        <button onClick={() => goPage(page + 1)} disabled={page >= totalPages || loading} className="rounded border border-border px-3 py-1.5 text-sm disabled:opacity-40">다음</button>
        <button onClick={() => goPage(totalPages)} disabled={page >= totalPages || loading} className="rounded border border-border px-3 py-1.5 text-sm disabled:opacity-40">마지막</button>
      </div>
    </div>
  );
}

export default function ScreenerPage() {
  return (
    <Suspense fallback={<div className="p-6 text-center text-muted-foreground">불러오는 중...</div>}>
      <ScreenerContent />
    </Suspense>
  );
}
