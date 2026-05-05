"use client";

/**
 * Phase 2: TradingView Lightweight Charts — 캔들 + 거래량 + 지표(MA/RSI/볼린저) + 매매/시그널 마커
 * V4 chart API 데이터 형식 사용.
 * DESK2-BT-CHART-VISUAL-001: markers, highlightRanges, indicatorLines optional props 추가 (기존 동작 유지)
 */
import { useEffect, useRef, useCallback, useMemo, useState } from "react";
import type { ChartOhlcvBar, ChartMinuteBar, ChartIndicatorPoint, ChartTradeOverlay, ChartStrategySignal } from "@/lib/api/chart";

export type ChartTimeFormat = "daily" | "minute";
export type MarkerStyleMode = "kr" | "us";

/** 백테스트 차트용 진입/청산 마커 (optional) */
export interface TradeMarker {
  time: string | number;
  position: "aboveBar" | "belowBar";
  color: string;
  shape: "arrowUp" | "arrowDown" | "circle";
  text: string;
  size?: number;
}

/** 보유 구간 하이라이트 */
export interface HighlightRange {
  startTime: string | number;
  endTime: string | number;
  color: string;
  title?: string;
}

/** 지표 라인 (이름·데이터·색상) */
export interface IndicatorLine {
  name: string;
  data: { time: string | number; value: number }[];
  color: string;
  lineWidth?: number;
  lineStyle?: number;
}

export interface StockChartProps {
  /** 일봉 또는 분봉 데이터 */
  candleData: ChartOhlcvBar[] | ChartMinuteBar[];
  /** time 포맷: daily = "YYYY-MM-DD", minute = Unix 초 */
  timeFormat: ChartTimeFormat;
  /** 거래량 (candleData와 동일한 time 순서) */
  volumeData?: { time: string | number; value: number }[];
  /** 지표: ma5, ma20, ma60, rsi, bollinger (value 또는 upper/middle/lower) */
  indicators?: Record<string, ChartIndicatorPoint[]>;
  /** 매매 오버레이 (매수/매도 마커) */
  trades?: ChartTradeOverlay[];
  /** 전략 시그널 (BUY/SELL 마커) */
  signals?: ChartStrategySignal[];
  /** [optional] 백테스트용 마커. 전달 시 이 목록으로 setMarkers 호출 (trades/signals 무시) */
  markers?: TradeMarker[];
  /** [optional] 보유 구간 등 하이라이트 영역 */
  highlightRanges?: HighlightRange[];
  /** [optional] 지표 라인 배열 (이름·데이터·색상) */
  indicatorLines?: IndicatorLine[];
  /** 자동 생성 마커 스타일. 기본값은 한국식(BUY=빨강, SELL=파랑). */
  markerStyle?: MarkerStyleMode;
  /** 자동 생성 마커 범례 표시 여부 */
  showTradeLegend?: boolean;
  className?: string;
  height?: number;
}

type AutoMarker = {
  time: string | number;
  position: "aboveBar" | "belowBar";
  shape: "arrowUp" | "arrowDown" | "circle";
  color: string;
  text?: string;
};

type MarkerFilterMode = "all" | "executed" | "signal";

type ChartMarkerRecord = AutoMarker & {
  timeKey: string;
  label: string;
  price?: number;
  executed: boolean;
};

const AUTO_MARKER_COLORS: Record<
  MarkerStyleMode,
  {
    executedBuy: string;
    executedSell: string;
    pendingBuy: string;
    pendingSell: string;
  }
> = {
  kr: {
    executedBuy: "#ef4444",
    executedSell: "#3b82f6",
    pendingBuy: "rgba(239,68,68,0.35)",
    pendingSell: "rgba(59,130,246,0.35)",
  },
  us: {
    executedBuy: "#22c55e",
    executedSell: "#ef4444",
    pendingBuy: "rgba(34,197,94,0.35)",
    pendingSell: "rgba(239,68,68,0.35)",
  },
};

function formatMarkerPrice(price: number | null | undefined) {
  if (price == null || Number.isNaN(price)) return undefined;
  return Math.round(price).toLocaleString("ko-KR");
}

function getSignalDirection(type: string) {
  const normalized = String(type).toUpperCase();
  if (normalized.includes("BUY")) return "BUY";
  if (normalized.includes("SELL")) return "SELL";
  return null;
}

function toChartTime(t: string | number, format: ChartTimeFormat): string | number {
  if (format === "minute") return typeof t === "number" ? t : Number(t);
  return typeof t === "string" ? t : "";
}

function normalizeTimeKey(time: unknown): string {
  if (typeof time === "number") return String(time);
  if (typeof time === "string") return time;
  if (
    time &&
    typeof time === "object" &&
    "year" in time &&
    "month" in time &&
    "day" in time
  ) {
    const businessDay = time as { year: number; month: number; day: number };
    return `${businessDay.year}-${String(businessDay.month).padStart(2, "0")}-${String(businessDay.day).padStart(2, "0")}`;
  }
  return "";
}

function formatMarkerTimestampKst(time: string | number, format: ChartTimeFormat): string {
  const date =
    format === "minute"
      ? new Date(Number(time) * 1000)
      : new Date(`${String(time)}T00:00:00+09:00`);
  if (Number.isNaN(date.getTime())) return "-";

  const formatter = new Intl.DateTimeFormat("sv-SE", {
    timeZone: "Asia/Seoul",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });

  return formatter.format(date);
}

export function StockChart({
  candleData,
  timeFormat,
  volumeData,
  indicators = {},
  trades = [],
  signals = [],
  markers: markersProp,
  highlightRanges = [],
  indicatorLines = [],
  markerStyle = "kr",
  showTradeLegend = false,
  className = "",
  height = 320,
}: StockChartProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<ReturnType<typeof import("lightweight-charts").createChart> | null>(null);
  const markersRef = useRef<{ setMarkers?: (markers: AutoMarker[]) => void } | null>(null);
  const activeMarkersRef = useRef<ChartMarkerRecord[]>([]);
  const legendColors = AUTO_MARKER_COLORS[markerStyle];
  const [markerFilter, setMarkerFilter] = useState<MarkerFilterMode>("all");
  const [hoveredMarker, setHoveredMarker] = useState<ChartMarkerRecord | null>(null);

  const generatedMarkers = useMemo<ChartMarkerRecord[]>(() => {
    if (markersProp != null && markersProp.length > 0) {
      return markersProp.map((marker) => {
        const chartTime = toChartTime(marker.time, timeFormat);
        return {
          time: chartTime,
          timeKey: normalizeTimeKey(chartTime),
          position: marker.position,
          shape: marker.shape,
          color: marker.color,
          text: marker.text,
          label: marker.text?.trim()?.slice(0, 1) || "",
          executed: true,
        };
      });
    }

    const closePriceByTime = new Map(
      candleData.map((bar) => {
        const chartTime = toChartTime(bar.time, timeFormat);
        return [String(chartTime), bar.close] as const;
      })
    );
    const colorSet = AUTO_MARKER_COLORS[markerStyle];
    const resolvePrice = (time: string | number, explicitPrice?: number | null) => {
      if (explicitPrice != null && !Number.isNaN(explicitPrice)) return explicitPrice;
      return closePriceByTime.get(String(toChartTime(time, timeFormat)));
    };
    const nextMarkers: ChartMarkerRecord[] = [];

    trades.forEach((trade) => {
      const isBuy = trade.type === "BUY";
      const executed = trade.executed !== false;
      const label = executed ? (isBuy ? "B" : "S") : isBuy ? "b" : "s";
      const chartTime = toChartTime(trade.date, timeFormat);
      const price = resolvePrice(trade.date, trade.price);
      const priceText = formatMarkerPrice(price);
      nextMarkers.push({
        time: chartTime,
        timeKey: normalizeTimeKey(chartTime),
        position: isBuy ? "belowBar" : "aboveBar",
        shape: isBuy ? "arrowUp" : "arrowDown",
        color: executed
          ? isBuy
            ? colorSet.executedBuy
            : colorSet.executedSell
          : isBuy
            ? colorSet.pendingBuy
            : colorSet.pendingSell,
        text: priceText ? `${label} ${priceText}` : label,
        label,
        price,
        executed,
      });
    });

    signals.forEach((signal) => {
      const direction = getSignalDirection(signal.type);
      if (!direction) return;
      const isBuy = direction === "BUY";
      const executed = Boolean(signal.executed);
      const label = executed ? (isBuy ? "B" : "S") : isBuy ? "b" : "s";
      const chartTime = toChartTime(signal.date, timeFormat);
      const price = resolvePrice(signal.date, signal.price ?? signal.close);
      const priceText = formatMarkerPrice(price);
      nextMarkers.push({
        time: chartTime,
        timeKey: normalizeTimeKey(chartTime),
        position: isBuy ? "belowBar" : "aboveBar",
        shape: isBuy ? "arrowUp" : "arrowDown",
        color: executed
          ? isBuy
            ? colorSet.executedBuy
            : colorSet.executedSell
          : isBuy
            ? colorSet.pendingBuy
            : colorSet.pendingSell,
        text: priceText ? `${label} ${priceText}` : label,
        label,
        price,
        executed,
      });
    });

    return nextMarkers;
  }, [candleData, markerStyle, markersProp, signals, timeFormat, trades]);

  const filteredMarkers = useMemo(
    () =>
      generatedMarkers.filter((marker) => {
        if (markerFilter === "all") return true;
        if (markerFilter === "executed") return marker.executed;
        return !marker.executed;
      }),
    [generatedMarkers, markerFilter]
  );

  activeMarkersRef.current = filteredMarkers;

  const buildChart = useCallback(async () => {
    if (typeof window === "undefined" || !containerRef.current || candleData.length === 0) return;

    const {
      createChart,
      ColorType,
      CandlestickSeries,
      HistogramSeries,
      LineSeries,
      AreaSeries,
      createSeriesMarkers,
    } = await import("lightweight-charts");

    if (chartRef.current) {
      chartRef.current.remove();
      chartRef.current = null;
      markersRef.current = null;
    }

    const chart = createChart(containerRef.current, {
      layout: {
        background: { type: ColorType.Solid, color: "transparent" },
        textColor: "rgba(255,255,255,0.7)",
      },
      grid: { vertLines: { color: "rgba(255,255,255,0.06)" }, horzLines: { color: "rgba(255,255,255,0.06)" } },
      width: containerRef.current.clientWidth,
      height,
      rightPriceScale: { borderColor: "rgba(255,255,255,0.1)", scaleMargins: { top: 0.1, bottom: 0.2 } },
      timeScale: { borderColor: "rgba(255,255,255,0.1)", timeVisible: true, secondsVisible: timeFormat === "minute" },
    });

    chartRef.current = chart;

    const candlestickSeries = chart.addSeries(CandlestickSeries, {
      upColor: "#22c55e",
      downColor: "#ef4444",
      borderUpColor: "#22c55e",
      borderDownColor: "#ef4444",
    });

    const candleSet = candleData.map((b) => {
      const t = "time" in b ? (b as ChartOhlcvBar).time : (b as ChartMinuteBar).time;
      return {
        time: toChartTime(t, timeFormat),
        open: b.open,
        high: b.high,
        low: b.low,
        close: b.close,
      };
    });
    candlestickSeries.setData(candleSet as Parameters<typeof candlestickSeries.setData>[0]);

    if (volumeData && volumeData.length > 0) {
      const volSeries = chart.addSeries(HistogramSeries, {
        color: "#26a69a",
        priceFormat: { type: "volume" },
        priceScaleId: "",
      });
      volSeries.priceScale().applyOptions({ scaleMargins: { top: 0.8, bottom: 0 } });
      const volSet = volumeData.map((v) => ({
        time: toChartTime(v.time, timeFormat),
        value: v.value,
      }));
      volSeries.setData(volSet as Parameters<typeof volSeries.setData>[0]);
    }

    const lineColors: Record<string, string> = {
      ma5: "#a78bfa",
      ma20: "#38bdf8",
      ma60: "#f472b6",
      ma120: "#fbbf24",
    };
    ["ma5", "ma20", "ma60", "ma120"].forEach((key) => {
      const arr = indicators[key];
      if (!arr || arr.length === 0) return;
      const lineSeries = chart.addSeries(LineSeries, { color: lineColors[key] ?? "#94a3b8", lineWidth: 2 });
      const data = arr.filter((p) => p.value != null).map((p) => ({ time: toChartTime(p.time, timeFormat), value: p.value! }));
      lineSeries.setData(data as Parameters<typeof lineSeries.setData>[0]);
    });

    if (indicators.bollinger && indicators.bollinger.length > 0) {
      const upper = chart.addSeries(LineSeries, { color: "rgba(148,163,184,0.6)", lineWidth: 1 });
      const middle = chart.addSeries(LineSeries, { color: "rgba(148,163,184,0.9)", lineWidth: 1 });
      const lower = chart.addSeries(LineSeries, { color: "rgba(148,163,184,0.6)", lineWidth: 1 });
      const u = indicators.bollinger.filter((p) => p.upper != null).map((p) => ({ time: toChartTime(p.time, timeFormat), value: p.upper! }));
      const m = indicators.bollinger.filter((p) => p.middle != null).map((p) => ({ time: toChartTime(p.time, timeFormat), value: p.middle! }));
      const l = indicators.bollinger.filter((p) => p.lower != null).map((p) => ({ time: toChartTime(p.time, timeFormat), value: p.lower! }));
      upper.setData(u as Parameters<typeof upper.setData>[0]);
      middle.setData(m as Parameters<typeof middle.setData>[0]);
      lower.setData(l as Parameters<typeof lower.setData>[0]);
    }

    if (indicators.rsi && indicators.rsi.length > 0) {
      const rsiSeries = chart.addSeries(LineSeries, { color: "#c084fc", lineWidth: 2 }, 1);
      const rsiData = indicators.rsi.filter((p) => p.value != null).map((p) => ({ time: toChartTime(p.time, timeFormat), value: p.value! }));
      rsiSeries.setData(rsiData as Parameters<typeof rsiSeries.setData>[0]);
      rsiSeries.priceScale().applyOptions({ scaleMargins: { top: 0.9, bottom: 0 }, borderColor: "rgba(255,255,255,0.1)" });
    }

    try {
      const seriesMarkers = createSeriesMarkers(
        candlestickSeries as Parameters<typeof createSeriesMarkers>[0],
        activeMarkersRef.current as Parameters<typeof createSeriesMarkers>[1]
      );
      markersRef.current = seriesMarkers as typeof markersRef.current;
    } catch {
      // v5 marker API may differ
    }

    chart.subscribeCrosshairMove((param) => {
      if (!param.point || !param.time) {
        setHoveredMarker(null);
        return;
      }

      const timeKey = normalizeTimeKey(param.time);
      const marker = activeMarkersRef.current.find((item) => item.timeKey === timeKey) ?? null;
      setHoveredMarker(marker);
    });

    if (highlightRanges.length > 0 && candleSet.length > 0) {
      const toNum = (t: string | number): number => {
        if (typeof t === "number") return t;
        const n = Number(t);
        if (!Number.isNaN(n)) return n;
        return 0;
      };
      highlightRanges.forEach((r) => {
        const start = toNum(r.startTime);
        const end = toNum(r.endTime);
        const inRange = candleSet.filter(
          (c) => toNum(c.time) >= start && toNum(c.time) <= end
        );
        if (inRange.length === 0) return;
        const minLow = Math.min(...inRange.map((c) => c.low));
        const maxHigh = Math.max(...inRange.map((c) => c.high));
        const areaSeries = chart.addSeries(AreaSeries, {
          topColor: r.color,
          bottomColor: r.color,
          lineColor: "transparent",
        });
        areaSeries.setData([
          { time: inRange[0].time, value: minLow },
          { time: inRange[inRange.length - 1].time, value: maxHigh },
        ] as Parameters<typeof areaSeries.setData>[0]);
      });
    }

    if (indicatorLines.length > 0) {
      indicatorLines.forEach((line) => {
        if (!line.data || line.data.length === 0) return;
        const lineSeries = chart.addSeries(LineSeries, {
          color: line.color,
        });
        const data = line.data.map((d) => ({
          time: toChartTime(d.time, timeFormat),
          value: d.value,
        }));
        lineSeries.setData(data as Parameters<typeof lineSeries.setData>[0]);
      });
    }

    chart.timeScale().fitContent();
  }, [candleData, timeFormat, volumeData, indicators, highlightRanges, indicatorLines, height]);

  useEffect(() => {
    buildChart();
    const container = containerRef.current;
    if (!container) return;
    const ro = new ResizeObserver(() => {
      if (chartRef.current && container) chartRef.current.resize(container.clientWidth, height);
    });
    ro.observe(container);
    return () => {
      ro.disconnect();
      if (chartRef.current) {
        chartRef.current.remove();
        chartRef.current = null;
      }
      markersRef.current = null;
      setHoveredMarker(null);
    };
  }, [buildChart, height]);

  useEffect(() => {
    markersRef.current?.setMarkers?.(filteredMarkers);
    const hoveredTimeKey = hoveredMarker?.timeKey;
    if (hoveredTimeKey && !filteredMarkers.some((marker) => marker.timeKey === hoveredTimeKey)) {
      setHoveredMarker(null);
    }
  }, [filteredMarkers, hoveredMarker?.timeKey]);

  if (candleData.length === 0) {
    return (
      <div className={`flex items-center justify-center rounded-lg border border-white/10 bg-white/5 ${className}`} style={{ height }}>
        <span className="text-sm text-muted-foreground">차트 데이터 없음</span>
      </div>
    );
  }

  return (
    <div className={className}>
      {(showTradeLegend || markersProp == null) && (
        <div className="mb-3 flex flex-wrap items-start justify-between gap-3">
          {showTradeLegend && markersProp == null ? (
            <div className="flex flex-wrap items-center gap-3 text-xs text-slate-300">
              <span className="inline-flex items-center gap-1">
                <span className="h-2.5 w-2.5 rounded-full" style={{ backgroundColor: legendColors.executedBuy }} />
                B 매수 체결
              </span>
              <span className="inline-flex items-center gap-1">
                <span className="h-2.5 w-2.5 rounded-full" style={{ backgroundColor: legendColors.executedSell }} />
                S 매도 체결
              </span>
              <span className="inline-flex items-center gap-1">
                <span className="h-2.5 w-2.5 rounded-full border" style={{ borderColor: legendColors.executedBuy, backgroundColor: legendColors.pendingBuy }} />
                b 매수시그널
              </span>
              <span className="inline-flex items-center gap-1">
                <span className="h-2.5 w-2.5 rounded-full border" style={{ borderColor: legendColors.executedSell, backgroundColor: legendColors.pendingSell }} />
                s 매도시그널
              </span>
            </div>
          ) : (
            <div />
          )}
          {markersProp == null && (
            <div className="flex items-center justify-end gap-2 text-xs text-slate-200">
              <button
                type="button"
                onClick={() => setMarkerFilter("all")}
                className={`rounded border px-2 py-1 transition-colors ${
                  markerFilter === "all" ? "bg-white/10 border-white/30" : "border-white/10 hover:bg-white/5"
                }`}
              >
                전체
              </button>
              <button
                type="button"
                onClick={() => setMarkerFilter("executed")}
                className={`rounded border px-2 py-1 transition-colors ${
                  markerFilter === "executed" ? "bg-white/10 border-white/30" : "border-white/10 hover:bg-white/5"
                }`}
              >
                체결만
              </button>
              <button
                type="button"
                onClick={() => setMarkerFilter("signal")}
                className={`rounded border px-2 py-1 transition-colors ${
                  markerFilter === "signal" ? "bg-white/10 border-white/30" : "border-white/10 hover:bg-white/5"
                }`}
              >
                시그널만
              </button>
            </div>
          )}
        </div>
      )}
      <div className="relative">
        {hoveredMarker && (
          <div className="pointer-events-none absolute right-2 top-2 z-10 rounded bg-black/70 px-2 py-1 text-xs text-white">
            {(hoveredMarker.executed ? "체결" : "시그널") + `(${hoveredMarker.label})`}{" "}
            {hoveredMarker.price != null ? hoveredMarker.price.toLocaleString("ko-KR") : "-"} ·{" "}
            {formatMarkerTimestampKst(hoveredMarker.time, timeFormat)}
          </div>
        )}
        <div ref={containerRef} style={{ height }} />
      </div>
    </div>
  );
}
