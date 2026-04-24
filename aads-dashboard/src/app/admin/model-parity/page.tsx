"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

import Header from "@/components/Header";
import { api } from "@/lib/api";

type Tab = "models" | "routing" | "daily";

interface SummaryData {
  window_days: number;
  window_start: string;
  window_end: string;
  tracked_models: number;
  tracked_intents: number;
  tracked_messages: number;
  total_calls: number;
  total_input_tokens: number;
  total_output_tokens: number;
  total_tokens: number;
}

interface RoutingModelBucket {
  model: string;
  count: number;
  tool_enabled: number;
  thinking_enabled: number;
  gemini_direct_enabled: number;
  intents: string[];
}

interface RoutingBucket {
  model: string;
  tools: boolean;
  group: string;
  thinking: boolean;
  gemini_direct: string;
  count: number;
  intents: string[];
}

interface ModelMetric {
  model: string;
  calls: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  avg_tokens_per_call: number;
  distinct_intents: number;
  configured_intents: number;
}

interface DailyMetric {
  date: string;
  calls: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  models: Array<{
    model: string;
    calls: number;
    total_tokens: number;
  }>;
}

interface ModelParityResponse {
  summary: SummaryData;
  routing: {
    source: string;
    total_intents: number;
    tool_enabled_intents: number;
    thinking_enabled_intents: number;
    gemini_direct_intents: number;
    by_model: RoutingModelBucket[];
    by_route: RoutingBucket[];
  };
  models: ModelMetric[];
  daily: DailyMetric[];
}

interface IntentMapItem {
  model: string;
  tools?: boolean;
  group?: string;
  thinking?: boolean;
  gemini_direct?: string;
}

interface IntentMapResponse {
  source: string;
  count: number;
  intent_map: Record<string, IntentMapItem>;
}

function formatNumber(value?: number): string {
  return new Intl.NumberFormat("ko-KR").format(value || 0);
}

function formatDateTime(value?: string): string {
  if (!value) return "-";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString("ko-KR", {
    timeZone: "Asia/Seoul",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function barWidth(value: number, total: number): string {
  if (!total) return "0%";
  return `${Math.max(6, Math.round((value / total) * 100))}%`;
}

export default function ModelParityPage() {
  const [tab, setTab] = useState<Tab>("models");
  const [data, setData] = useState<ModelParityResponse | null>(null);
  const [intentMap, setIntentMap] = useState<IntentMapResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const loadData = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [parityRes, intentRes] = await Promise.all([
        api.getModelParity(),
        api.getModelParityIntentMap(),
      ]);
      setData(parityRes as ModelParityResponse);
      setIntentMap(intentRes as IntentMapResponse);
    } catch (err) {
      console.error("model parity load failed", err);
      setError(err instanceof Error ? err.message : "모델 패리티 데이터를 불러오지 못했습니다.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadData();
  }, [loadData]);

  const topCallCount = useMemo(() => {
    return Math.max(...(data?.models.map((item) => item.calls) || [0]));
  }, [data]);

  const topDailyCalls = useMemo(() => {
    return Math.max(...(data?.daily.map((item) => item.calls) || [0]));
  }, [data]);

  const cardStyle = {
    background: "var(--bg-card)",
    border: "1px solid var(--border)",
    borderRadius: "10px",
    padding: "16px",
  };

  const tabs: Array<{ key: Tab; label: string }> = [
    { key: "models", label: "모델 현황" },
    { key: "routing", label: "인텐트 라우팅" },
    { key: "daily", label: "일별 추이" },
  ];

  const summaryCards = data ? [
    { label: "Tracked Models", value: data.summary.tracked_models },
    { label: "Tracked Intents", value: data.summary.tracked_intents },
    { label: "Tracked Messages", value: data.summary.tracked_messages },
    { label: "Total Calls", value: data.summary.total_calls },
    { label: "Input Tokens", value: data.summary.total_input_tokens },
    { label: "Output Tokens", value: data.summary.total_output_tokens },
  ] : [];

  return (
    <div className="flex flex-col h-full" style={{ background: "var(--bg-primary)" }}>
      <Header title="Model Parity" />
      <div className="flex-1 p-3 md:p-6 overflow-auto">
        <div className="grid gap-4">
          <section
            className="rounded-2xl p-4 md:p-5"
            style={{
              background: "linear-gradient(135deg, rgba(14,165,233,0.18), rgba(15,23,42,0.96))",
              border: "1px solid rgba(148,163,184,0.16)",
              boxShadow: "0 18px 48px rgba(2,132,199,0.18)",
            }}
          >
            <div className="flex flex-col lg:flex-row lg:items-end lg:justify-between gap-4">
              <div>
                <div className="text-xs uppercase tracking-[0.24em]" style={{ color: "rgba(226,232,240,0.72)" }}>
                  Model Parity
                </div>
                <h1 className="mt-2 text-2xl md:text-3xl font-bold" style={{ color: "var(--text-primary)" }}>
                  모델 라우팅과 실제 사용량 비교
                </h1>
                <p className="mt-2 text-sm max-w-3xl" style={{ color: "rgba(226,232,240,0.72)" }}>
                  최근 {data?.summary.window_days || 7}일 기준 `chat_messages` 사용량과 `INTENT_MAP` 라우팅 설정을 한 화면에서 비교합니다.
                </p>
              </div>
              <button
                type="button"
                onClick={loadData}
                className="self-start px-3 py-2 rounded-lg text-sm font-semibold"
                style={{
                  background: "rgba(15,23,42,0.44)",
                  color: "var(--text-primary)",
                  border: "1px solid rgba(148,163,184,0.18)",
                }}
              >
                새로고침
              </button>
            </div>

            {data ? (
              <div className="mt-4 flex flex-wrap gap-2 text-xs">
                <span style={{ color: "rgba(226,232,240,0.72)" }}>
                  기간: {formatDateTime(data.summary.window_start)} ~ {formatDateTime(data.summary.window_end)}
                </span>
                <span style={{ color: "rgba(226,232,240,0.44)" }}>•</span>
                <span style={{ color: "rgba(226,232,240,0.72)" }}>
                  routing source: {data.routing.source}
                </span>
                {intentMap?.source ? (
                  <>
                    <span style={{ color: "rgba(226,232,240,0.44)" }}>•</span>
                    <span style={{ color: "rgba(226,232,240,0.72)" }}>
                      intent map: {intentMap.source}
                    </span>
                  </>
                ) : null}
              </div>
            ) : null}
          </section>

          <div className="flex gap-2 flex-wrap">
            {tabs.map((item) => (
              <button
                key={item.key}
                type="button"
                onClick={() => setTab(item.key)}
                className="px-3 py-2 rounded-lg text-sm font-semibold"
                style={{
                  background: tab === item.key ? "var(--accent)" : "var(--bg-card)",
                  color: tab === item.key ? "#fff" : "var(--text-primary)",
                  border: tab === item.key ? "1px solid var(--accent)" : "1px solid var(--border)",
                }}
              >
                {item.label}
              </button>
            ))}
          </div>

          {error ? (
            <div style={{ ...cardStyle, color: "var(--danger)" }}>{error}</div>
          ) : null}

          {loading ? (
            <div style={{ ...cardStyle, color: "var(--text-secondary)", textAlign: "center" }}>
              모델 패리티 데이터를 불러오는 중...
            </div>
          ) : !data ? (
            <div style={{ ...cardStyle, color: "var(--text-secondary)", textAlign: "center" }}>
              표시할 데이터가 없습니다.
            </div>
          ) : (
            <>
              <div className="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-6 gap-3">
                {summaryCards.map((item) => (
                  <div key={item.label} style={{ ...cardStyle, padding: "14px" }}>
                    <div style={{ color: "var(--text-secondary)", fontSize: "11px", marginBottom: "4px" }}>
                      {item.label}
                    </div>
                    <div style={{ color: "var(--text-primary)", fontSize: "24px", fontWeight: 700 }}>
                      {formatNumber(item.value)}
                    </div>
                  </div>
                ))}
              </div>

              {tab === "models" ? (
                <div className="grid gap-4 xl:grid-cols-[minmax(0,1.4fr)_minmax(320px,0.9fr)]">
                  <section style={cardStyle}>
                    <div className="flex items-center justify-between gap-2 mb-4">
                      <div style={{ color: "var(--text-primary)", fontSize: "16px", fontWeight: 700 }}>
                        실제 모델 사용량
                      </div>
                      <div style={{ color: "var(--text-secondary)", fontSize: "12px" }}>
                        calls / tokens / intents
                      </div>
                    </div>
                    <div className="grid gap-3">
                      {data.models.length === 0 ? (
                        <div style={{ color: "var(--text-secondary)", textAlign: "center", padding: "20px 0" }}>
                          최근 사용량 데이터가 없습니다.
                        </div>
                      ) : data.models.map((item) => (
                        <div
                          key={item.model}
                          className="rounded-xl p-3"
                          style={{ background: "var(--bg-primary)", border: "1px solid var(--border)" }}
                        >
                          <div className="flex items-center justify-between gap-3">
                            <div>
                              <div style={{ color: "var(--text-primary)", fontWeight: 700 }}>{item.model}</div>
                              <div style={{ color: "var(--text-secondary)", fontSize: "12px", marginTop: "2px" }}>
                                configured intents {formatNumber(item.configured_intents)} / observed intents {formatNumber(item.distinct_intents)}
                              </div>
                            </div>
                            <div style={{ color: "var(--text-primary)", fontWeight: 700, fontSize: "18px" }}>
                              {formatNumber(item.calls)}
                            </div>
                          </div>
                          <div
                            className="mt-3 h-2 rounded-full"
                            style={{ background: "rgba(148,163,184,0.16)", overflow: "hidden" }}
                          >
                            <div
                              style={{
                                width: barWidth(item.calls, topCallCount),
                                height: "100%",
                                background: "linear-gradient(90deg, #0ea5e9, #38bdf8)",
                              }}
                            />
                          </div>
                          <div className="grid grid-cols-2 md:grid-cols-4 gap-2 mt-3">
                            {[
                              ["Input", formatNumber(item.input_tokens)],
                              ["Output", formatNumber(item.output_tokens)],
                              ["Total", formatNumber(item.total_tokens)],
                              ["Avg/Call", formatNumber(item.avg_tokens_per_call)],
                            ].map(([label, value]) => (
                              <div key={String(label)} style={{ background: "var(--bg-card)", border: "1px solid var(--border)", borderRadius: "8px", padding: "8px 10px" }}>
                                <div style={{ color: "var(--text-secondary)", fontSize: "11px" }}>{label}</div>
                                <div style={{ color: "var(--text-primary)", fontSize: "13px", marginTop: "2px" }}>{value}</div>
                              </div>
                            ))}
                          </div>
                        </div>
                      ))}
                    </div>
                  </section>

                  <section style={cardStyle}>
                    <div style={{ color: "var(--text-primary)", fontSize: "16px", fontWeight: 700, marginBottom: "14px" }}>
                      설정 라우팅 요약
                    </div>
                    <div className="grid gap-3">
                      {[
                        ["Intent Total", data.routing.total_intents],
                        ["Tools Enabled", data.routing.tool_enabled_intents],
                        ["Thinking Enabled", data.routing.thinking_enabled_intents],
                        ["Gemini Direct", data.routing.gemini_direct_intents],
                      ].map(([label, value]) => (
                        <div key={String(label)} style={{ background: "var(--bg-primary)", border: "1px solid var(--border)", borderRadius: "10px", padding: "12px" }}>
                          <div style={{ color: "var(--text-secondary)", fontSize: "12px" }}>{label}</div>
                          <div style={{ color: "var(--text-primary)", fontWeight: 700, fontSize: "22px", marginTop: "4px" }}>
                            {formatNumber(Number(value))}
                          </div>
                        </div>
                      ))}
                    </div>
                    <div className="mt-4">
                      <div style={{ color: "var(--text-primary)", fontSize: "14px", fontWeight: 700, marginBottom: "10px" }}>
                        configured by model
                      </div>
                      <div className="grid gap-2">
                        {data.routing.by_model.map((item) => (
                          <div key={item.model} style={{ background: "var(--bg-primary)", border: "1px solid var(--border)", borderRadius: "8px", padding: "10px 12px" }}>
                            <div className="flex items-center justify-between gap-2">
                              <span style={{ color: "var(--text-primary)", fontWeight: 600 }}>{item.model}</span>
                              <span style={{ color: "var(--text-secondary)", fontSize: "12px" }}>
                                {formatNumber(item.count)} intents
                              </span>
                            </div>
                          </div>
                        ))}
                      </div>
                    </div>
                  </section>
                </div>
              ) : null}

              {tab === "routing" ? (
                <div className="grid gap-4 xl:grid-cols-[minmax(0,1.2fr)_minmax(0,1fr)]">
                  <section style={cardStyle}>
                    <div style={{ color: "var(--text-primary)", fontSize: "16px", fontWeight: 700, marginBottom: "14px" }}>
                      라우팅 버킷
                    </div>
                    <div className="grid gap-3">
                      {data.routing.by_route.map((item, idx) => (
                        <div key={`${item.model}-${item.group}-${idx}`} style={{ background: "var(--bg-primary)", border: "1px solid var(--border)", borderRadius: "10px", padding: "12px" }}>
                          <div className="flex items-start justify-between gap-3">
                            <div>
                              <div style={{ color: "var(--text-primary)", fontWeight: 700 }}>
                                {item.model}
                              </div>
                              <div style={{ color: "var(--text-secondary)", fontSize: "12px", marginTop: "2px" }}>
                                group {item.group || "-"} · tools {item.tools ? "on" : "off"} · thinking {item.thinking ? "on" : "off"}
                              </div>
                              {item.gemini_direct ? (
                                <div style={{ color: "var(--accent)", fontSize: "12px", marginTop: "4px" }}>
                                  gemini_direct: {item.gemini_direct}
                                </div>
                              ) : null}
                            </div>
                            <span
                              className="px-2 py-1 rounded-full text-xs font-semibold"
                              style={{ background: "rgba(14,165,233,0.12)", color: "#38bdf8", border: "1px solid rgba(14,165,233,0.24)" }}
                            >
                              {formatNumber(item.count)} intents
                            </span>
                          </div>
                          <div className="mt-3 flex flex-wrap gap-2">
                            {item.intents.map((intent) => (
                              <span
                                key={intent}
                                style={{
                                  padding: "5px 9px",
                                  borderRadius: "999px",
                                  background: "var(--bg-card)",
                                  border: "1px solid var(--border)",
                                  color: "var(--text-secondary)",
                                  fontSize: "11px",
                                }}
                              >
                                {intent}
                              </span>
                            ))}
                          </div>
                        </div>
                      ))}
                    </div>
                  </section>

                  <section style={cardStyle}>
                    <div style={{ color: "var(--text-primary)", fontSize: "16px", fontWeight: 700, marginBottom: "14px" }}>
                      intent map 샘플
                    </div>
                    <div className="grid gap-2 max-h-[72vh] overflow-auto pr-1">
                      {Object.entries(intentMap?.intent_map || {}).map(([intent, config]) => (
                        <div key={intent} style={{ background: "var(--bg-primary)", border: "1px solid var(--border)", borderRadius: "10px", padding: "12px" }}>
                          <div style={{ color: "var(--text-primary)", fontWeight: 700 }}>{intent}</div>
                          <div className="mt-2 grid grid-cols-2 gap-2 text-xs">
                            <div style={{ color: "var(--text-secondary)" }}>model</div>
                            <div style={{ color: "var(--text-primary)" }}>{config.model || "-"}</div>
                            <div style={{ color: "var(--text-secondary)" }}>group</div>
                            <div style={{ color: "var(--text-primary)" }}>{config.group || "-"}</div>
                            <div style={{ color: "var(--text-secondary)" }}>tools</div>
                            <div style={{ color: "var(--text-primary)" }}>{config.tools ? "true" : "false"}</div>
                            <div style={{ color: "var(--text-secondary)" }}>thinking</div>
                            <div style={{ color: "var(--text-primary)" }}>{config.thinking ? "true" : "false"}</div>
                          </div>
                        </div>
                      ))}
                    </div>
                  </section>
                </div>
              ) : null}

              {tab === "daily" ? (
                <section style={cardStyle}>
                  <div className="flex items-center justify-between gap-2 mb-4">
                    <div style={{ color: "var(--text-primary)", fontSize: "16px", fontWeight: 700 }}>
                      일별 추이
                    </div>
                    <div style={{ color: "var(--text-secondary)", fontSize: "12px" }}>
                      최근 {data.summary.window_days}일
                    </div>
                  </div>
                  <div className="grid gap-3">
                    {data.daily.map((day) => (
                      <div key={day.date} style={{ background: "var(--bg-primary)", border: "1px solid var(--border)", borderRadius: "10px", padding: "12px" }}>
                        <div className="flex items-center justify-between gap-3">
                          <div>
                            <div style={{ color: "var(--text-primary)", fontWeight: 700 }}>{day.date}</div>
                            <div style={{ color: "var(--text-secondary)", fontSize: "12px", marginTop: "2px" }}>
                              calls {formatNumber(day.calls)} · total tokens {formatNumber(day.total_tokens)}
                            </div>
                          </div>
                          <div style={{ color: "var(--text-primary)", fontWeight: 700 }}>
                            {formatNumber(day.calls)}
                          </div>
                        </div>
                        <div className="mt-3 h-2 rounded-full" style={{ background: "rgba(148,163,184,0.16)", overflow: "hidden" }}>
                          <div
                            style={{
                              width: barWidth(day.calls, topDailyCalls),
                              height: "100%",
                              background: "linear-gradient(90deg, #22c55e, #38bdf8)",
                            }}
                          />
                        </div>
                        <div className="mt-3 flex flex-wrap gap-2">
                          {day.models.length === 0 ? (
                            <span style={{ color: "var(--text-secondary)", fontSize: "12px" }}>모델 사용 없음</span>
                          ) : day.models.map((item) => (
                            <span
                              key={`${day.date}-${item.model}`}
                              style={{
                                padding: "5px 9px",
                                borderRadius: "999px",
                                background: "var(--bg-card)",
                                border: "1px solid var(--border)",
                                color: "var(--text-secondary)",
                                fontSize: "11px",
                              }}
                            >
                              {item.model} · {formatNumber(item.calls)}
                            </span>
                          ))}
                        </div>
                      </div>
                    ))}
                  </div>
                </section>
              ) : null}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
