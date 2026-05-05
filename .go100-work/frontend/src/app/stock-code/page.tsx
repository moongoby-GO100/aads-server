"use client";

import { useEffect, useMemo, useState } from "react";
import { useParams, useSearchParams } from "next/navigation";
import {
  getChartDaily,
  getChartWeekly,
  getChartMinute,
  getChartInvestor,
  getChartIndicators,
  getChartPositionsOverlay,
  getChartStrategySignals,
  type ChartOhlcvBar,
  type ChartMinuteBar,
  type ChartInvestorDay,
  type ChartIndicatorPoint,
  type ChartStrategySignal,
  type ChartTradeOverlay,
} from "@/lib/api/chart";
import {
  getFundamental,
  getInvestorFlow,
  getOrderbook,
  getTradeStrength,
  type FundamentalData,
  type InvestorFlowItem,
  type TradeStrengthItem,
} from "@/lib/api/market";
import { StockChart } from "@/components/market/StockChart";
import { formatStock } from "@/go100/lib/stock-format";

type Timeframe = "daily" | "weekly" | "minute";
type IndicatorKey = "ma5" | "ma20" | "ma60" | "bollinger" | "rsi";

const INDICATORS: Array<{ key: IndicatorKey; label: string }> = [
  { key: "ma5", label: "MA5" },
  { key: "ma20", label: "MA20" },
  { key: "ma60", label: "MA60" },
  { key: "bollinger", label: "BOLL" },
  { key: "rsi", label: "RSI" },
];

const TIMEFRAMES: Array<{ key: Timeframe; label: string }> = [
  { key: "daily", label: "일" },
  { key: "weekly", label: "주" },
  { key: "minute", label: "분" },
];

interface OrderbookData {
  asks?: Array<{ price: number; qty: number }>;
  bids?: Array<{ price: number; qty: number }>;
}

function formatNumber(value: number | null | undefined, digits = 0) {
  if (value == null || Number.isNaN(Number(value))) return "-";
  return Number(value).toLocaleString("ko-KR", {
    maximumFractionDigits: digits,
    minimumFractionDigits: digits,
  });
}

function formatPct(value: number | null | undefined) {
  if (value == null || Number.isNaN(Number(value))) return "-";
  return `${value > 0 ? "+" : ""}${Number(value).toFixed(2)}%`;
}

function formatAmount(value: number | null | undefined) {
  if (value == null || Number.isNaN(Number(value))) return "-";
  const n = Number(value);
  if (Math.abs(n) >= 100000000) return `${(n / 100000000).toFixed(1)}억`;
  if (Math.abs(n) >= 10000) return `${Math.round(n / 10000).toLocaleString("ko-KR")}만`;
  return n.toLocaleString("ko-KR");
}

function changeClass(value: number | null | undefined) {
  if (value == null) return "text-muted-foreground";
  if (Number(value) > 0) return "text-red-400";
  if (Number(value) < 0) return "text-blue-400";
  return "text-muted-foreground";
}

function normalizeMinuteBars(items: ChartMinuteBar[]): ChartMinuteBar[] {
  return items
    .filter(item => item.time != null)
    .map(item => ({ ...item, time: Number(item.time) }))
    .filter(item => !Number.isNaN(item.time));
}

function lastDailyChange(data: Array<ChartOhlcvBar | ChartMinuteBar>) {
  if (data.length < 2) return null;
  const latest = data[data.length - 1];
  const prev = data[data.length - 2];
  if (!prev.close) return null;
  return ((latest.close - prev.close) / prev.close) * 100;
}

function compactIndicatorMap(
  indicators: Record<string, ChartIndicatorPoint[]>,
  enabled: Record<IndicatorKey, boolean>
) {
  return Object.fromEntries(
    Object.entries(indicators).filter(([key]) => enabled[key as IndicatorKey])
  ) as Record<string, ChartIndicatorPoint[]>;
}

export default function StockChartPage() {
  const params = useParams();
  const searchParams = useSearchParams();
  const stockCode = typeof params?.code === "string" ? params.code : Array.isArray(params?.code) ? params.code[0] : "";
  const stockName = searchParams?.get("name") || "";

  const [timeframe, setTimeframe] = useState<Timeframe>("daily");
  const [indicatorEnabled, setIndicatorEnabled] = useState<Record<IndicatorKey, boolean>>({
    ma5: true,
    ma20: true,
    ma60: true,
    bollinger: false,
    rsi: true,
  });
  const [showSignals, setShowSignals] = useState(true);
  const [showTrades, setShowTrades] = useState(true);
  const [showInvestor, setShowInvestor] = useState(true);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [candles, setCandles] = useState<Array<ChartOhlcvBar | ChartMinuteBar>>([]);
  const [indicators, setIndicators] = useState<Record<string, ChartIndicatorPoint[]>>({});
  const [investors, setInvestors] = useState<ChartInvestorDay[]>([]);
  const [flow, setFlow] = useState<InvestorFlowItem[]>([]);
  const [trades, setTrades] = useState<ChartTradeOverlay[]>([]);
  const [signals, setSignals] = useState<ChartStrategySignal[]>([]);
  const [fundamental, setFundamental] = useState<FundamentalData | null>(null);
  const [strength, setStrength] = useState<TradeStrengthItem[]>([]);
  const [orderbook, setOrderbook] = useState<OrderbookData | null>(null);

  useEffect(() => {
    if (!stockCode) return;
    setLoading(true);
    setError(null);

    const chartPromise =
      timeframe === "weekly"
        ? getChartWeekly(stockCode, 260)
        : timeframe === "minute"
          ? getChartMinute(stockCode, { interval: 1, limit: 240 })
          : getChartDaily(stockCode, { limit: 360 });

    Promise.all([
      chartPromise,
      getChartIndicators(stockCode, { indicators: "ma5,ma20,ma60,bollinger,rsi", period: 20 }),
      getChartInvestor(stockCode, 80),
      getInvestorFlow(stockCode, 80),
      getChartPositionsOverlay(stockCode),
      getChartStrategySignals(stockCode, { days: 180 }),
      getFundamental(stockCode),
      getTradeStrength(stockCode, "daily"),
      getOrderbook(stockCode).catch(() => null),
    ])
      .then(([chartData, indicatorData, investorData, flowData, tradeData, signalData, fundamentalData, strengthData, orderbookData]) => {
        const nextCandles =
          timeframe === "minute"
            ? normalizeMinuteBars((chartData.data || []) as ChartMinuteBar[])
            : ((chartData.data || []) as ChartOhlcvBar[]);
        setCandles(nextCandles);
        setIndicators(indicatorData.indicators || {});
        setInvestors(investorData.data || []);
        setFlow(flowData.data || []);
        setTrades(tradeData.trades || []);
        setSignals(signalData.signals || []);
        setFundamental(fundamentalData);
        setStrength(strengthData || []);
        setOrderbook(orderbookData as OrderbookData | null);
      })
      .catch(err => {
        setCandles([]);
        setError(err instanceof Error ? err.message : "차트 데이터를 불러오지 못했습니다.");
      })
      .finally(() => setLoading(false));
  }, [stockCode, timeframe]);

  const latest = candles[candles.length - 1];
  const changePct = lastDailyChange(candles);
  const volumeData = useMemo(
    () => candles.map(item => ({ time: item.time, value: item.volume || 0 })),
    [candles]
  );
  const visibleIndicators = useMemo(
    () => compactIndicatorMap(indicators, indicatorEnabled),
    [indicatorEnabled, indicators]
  );
  const latestFlow = flow[flow.length - 1];
  const latestInvestor = investors[investors.length - 1];
  const latestStrength = strength[strength.length - 1];

  if (!stockCode) {
    return <div className="flex min-h-[240px] items-center justify-center text-muted-foreground">종목 코드가 없습니다.</div>;
  }

  return (
    <div className="mx-auto max-w-7xl space-y-5 p-6">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold">{formatStock(stockName, stockCode)}</h1>
          <div className="mt-1 flex flex-wrap gap-3 text-sm text-muted-foreground">
            <span>현재가 {formatNumber(latest?.close)}</span>
            <span className={changeClass(changePct)}>{formatPct(changePct)}</span>
            <span>거래량 {formatAmount(latest?.volume)}</span>
          </div>
        </div>
        <div className="flex flex-wrap gap-2">
          {TIMEFRAMES.map(item => (
            <button
              key={item.key}
              onClick={() => setTimeframe(item.key)}
              className={`h-9 w-10 rounded border text-sm ${timeframe === item.key ? "border-primary bg-primary text-primary-foreground" : "border-border hover:bg-muted"}`}
            >
              {item.label}
            </button>
          ))}
        </div>
      </header>

      {error && <div className="rounded border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-400">{error}</div>}

      <section className="flex flex-wrap items-center justify-between gap-3 rounded border border-border bg-muted/20 p-3">
        <div className="flex flex-wrap gap-2">
          {INDICATORS.map(item => (
            <button
              key={item.key}
              onClick={() => setIndicatorEnabled(prev => ({ ...prev, [item.key]: !prev[item.key] }))}
              className={`rounded border px-3 py-1.5 text-xs ${indicatorEnabled[item.key] ? "border-primary bg-primary/15 text-primary" : "border-border text-muted-foreground hover:bg-muted"}`}
            >
              {item.label}
            </button>
          ))}
        </div>
        <div className="flex flex-wrap gap-2">
          <label className="inline-flex items-center gap-1.5 text-xs text-muted-foreground">
            <input type="checkbox" checked={showTrades} onChange={event => setShowTrades(event.target.checked)} />
            체결
          </label>
          <label className="inline-flex items-center gap-1.5 text-xs text-muted-foreground">
            <input type="checkbox" checked={showSignals} onChange={event => setShowSignals(event.target.checked)} />
            시그널
          </label>
          <label className="inline-flex items-center gap-1.5 text-xs text-muted-foreground">
            <input type="checkbox" checked={showInvestor} onChange={event => setShowInvestor(event.target.checked)} />
            수급
          </label>
        </div>
      </section>

      <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_340px]">
        <main className="rounded border border-border bg-card p-4">
          {loading ? (
            <div className="flex h-[560px] items-center justify-center text-sm text-muted-foreground">차트 로딩 중...</div>
          ) : (
            <StockChart
              candleData={timeframe === "minute" ? (candles as ChartMinuteBar[]) : (candles as ChartOhlcvBar[])}
              timeFormat={timeframe === "minute" ? "minute" : "daily"}
              volumeData={volumeData}
              indicators={visibleIndicators}
              trades={showTrades ? trades : []}
              signals={showSignals ? signals : []}
              showTradeLegend
              height={560}
            />
          )}
        </main>

        <aside className="space-y-4">
          <MetricPanel
            title="핵심 지표"
            items={[
              ["시가", formatNumber(latest?.open)],
              ["고가", formatNumber(latest?.high)],
              ["저가", formatNumber(latest?.low)],
              ["종가", formatNumber(latest?.close)],
              ["PER", formatNumber(fundamental?.per, 2)],
              ["PBR", formatNumber(fundamental?.pbr, 2)],
              ["배당률", fundamental?.dividend_yield != null ? `${formatNumber(fundamental.dividend_yield, 2)}%` : "-"],
              ["체결강도", formatNumber(Number(latestStrength?.strength), 1)],
            ]}
          />

          {showInvestor && (
            <MetricPanel
              title="수급"
              items={[
                ["외국인", formatAmount(latestFlow?.foreign_net ?? latestInvestor?.foreign_net)],
                ["기관", formatAmount(latestFlow?.institution_net ?? latestInvestor?.inst_net)],
                ["개인", formatAmount(latestFlow?.individual_net)],
                ["연기금", formatAmount(latestInvestor?.pension_net)],
              ]}
            />
          )}

          <OrderbookPanel orderbook={orderbook} />
          <SignalPanel title="최근 시그널" signals={signals.slice(-8).reverse()} />
          <TradePanel trades={trades.slice(-8).reverse()} />
        </aside>
      </div>
    </div>
  );
}

function MetricPanel({ title, items }: { title: string; items: Array<[string, string]> }) {
  return (
    <section className="rounded border border-border bg-card p-4">
      <h2 className="mb-3 text-sm font-semibold">{title}</h2>
      <div className="grid grid-cols-2 gap-2">
        {items.map(([label, value]) => (
          <div key={label} className="rounded border border-border bg-muted/20 p-2">
            <div className="text-xs text-muted-foreground">{label}</div>
            <div className="mt-1 text-sm font-medium tabular-nums">{value}</div>
          </div>
        ))}
      </div>
    </section>
  );
}

function OrderbookPanel({ orderbook }: { orderbook: OrderbookData | null }) {
  const asks = (orderbook?.asks || []).slice(0, 5).reverse();
  const bids = (orderbook?.bids || []).slice(0, 5);
  return (
    <section className="rounded border border-border bg-card p-4">
      <h2 className="mb-3 text-sm font-semibold">호가</h2>
      {asks.length === 0 && bids.length === 0 ? (
        <div className="py-4 text-sm text-muted-foreground">호가 데이터 없음</div>
      ) : (
        <div className="grid grid-cols-2 gap-3 text-xs">
          <div className="space-y-1">
            {asks.map((item, index) => (
              <div key={`ask-${index}`} className="flex justify-between gap-2 text-red-400">
                <span>{formatNumber(item.price)}</span>
                <span className="text-muted-foreground">{formatAmount(item.qty)}</span>
              </div>
            ))}
          </div>
          <div className="space-y-1">
            {bids.map((item, index) => (
              <div key={`bid-${index}`} className="flex justify-between gap-2 text-blue-400">
                <span>{formatNumber(item.price)}</span>
                <span className="text-muted-foreground">{formatAmount(item.qty)}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </section>
  );
}

function SignalPanel({ title, signals }: { title: string; signals: ChartStrategySignal[] }) {
  return (
    <section className="rounded border border-border bg-card p-4">
      <h2 className="mb-3 text-sm font-semibold">{title}</h2>
      {signals.length === 0 ? (
        <div className="py-3 text-sm text-muted-foreground">시그널 없음</div>
      ) : (
        <div className="space-y-2">
          {signals.map((signal, index) => (
            <div key={`${signal.date}-${index}`} className="rounded border border-border bg-muted/20 p-2 text-xs">
              <div className="flex justify-between gap-2">
                <span className={signal.type.toUpperCase().includes("BUY") ? "text-red-400" : "text-blue-400"}>{signal.type}</span>
                <span className="text-muted-foreground">{signal.date}</span>
              </div>
              <div className="mt-1 truncate text-muted-foreground">{signal.strategy} · 신뢰도 {formatNumber(signal.confidence, 2)}</div>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

function TradePanel({ trades }: { trades: ChartTradeOverlay[] }) {
  return (
    <section className="rounded border border-border bg-card p-4">
      <h2 className="mb-3 text-sm font-semibold">최근 체결</h2>
      {trades.length === 0 ? (
        <div className="py-3 text-sm text-muted-foreground">체결 없음</div>
      ) : (
        <div className="space-y-2">
          {trades.map((trade, index) => (
            <div key={`${trade.date}-${index}`} className="rounded border border-border bg-muted/20 p-2 text-xs">
              <div className="flex justify-between gap-2">
                <span className={trade.type === "BUY" ? "text-red-400" : "text-blue-400"}>{trade.type}</span>
                <span className="text-muted-foreground">{trade.date}</span>
              </div>
              <div className="mt-1 text-muted-foreground">
                {formatNumber(trade.price)} · {formatNumber(trade.quantity)}주 · {trade.pnl_pct == null ? "-" : formatPct(trade.pnl_pct)}
              </div>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}
