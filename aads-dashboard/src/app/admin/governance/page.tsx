"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

import Header from "@/components/Header";
import { api } from "@/lib/api";

type Tab = "overview" | "policies" | "audit";

interface IntentPolicy {
  id: number;
  intent: string;
  allowed_models: string[] | string | null;
  default_model: string;
  cascade_downgrade: boolean;
  tool_allowlist?: string[] | string | null;
  description?: string | null;
  updated_by?: string | null;
  updated_at?: string | null;
  temperature?: number | null;
}

interface IntentPolicyResponse {
  policies: IntentPolicy[];
  total: number;
}

interface FeatureFlag {
  flag_key: string;
  enabled: boolean;
  scope?: string | null;
  last_changed_by?: string | null;
  last_changed_at?: string | null;
  notes?: string | null;
}

interface FeatureFlagResponse {
  flags: FeatureFlag[];
  total: number;
}

interface AuditLogItem {
  id: number;
  at: string | null;
  event: string;
  mode: string;
  legacy_result?: unknown;
  db_result?: unknown;
  diff_summary?: string | null;
  trace_id?: string | null;
}

interface AuditLogResponse {
  items: AuditLogItem[];
  total: number;
}

function formatDateTime(value?: string | null): string {
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

function formatNumber(value?: number): string {
  return new Intl.NumberFormat("ko-KR").format(value || 0);
}

function asStringList(value: string[] | string | null | undefined): string[] {
  if (Array.isArray(value)) {
    return value.map((item) => String(item || "").trim()).filter(Boolean);
  }
  if (!value) return [];
  return String(value)
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

function stringifyValue(value: unknown): string {
  if (value == null || value === "") return "-";
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function flagTone(enabled: boolean): { background: string; color: string; border: string } {
  if (enabled) {
    return {
      background: "rgba(34,197,94,0.14)",
      color: "#4ade80",
      border: "1px solid rgba(34,197,94,0.24)",
    };
  }
  return {
    background: "rgba(239,68,68,0.14)",
    color: "#f87171",
    border: "1px solid rgba(239,68,68,0.24)",
  };
}

export default function AdminGovernancePage() {
  const [tab, setTab] = useState<Tab>("overview");
  const [policies, setPolicies] = useState<IntentPolicy[]>([]);
  const [flags, setFlags] = useState<FeatureFlag[]>([]);
  const [auditLog, setAuditLog] = useState<AuditLogItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState("");
  const [busyFlagKey, setBusyFlagKey] = useState("");
  const [lastRefreshedAt, setLastRefreshedAt] = useState<Date | null>(null);

  const loadData = useCallback(async (silent = false) => {
    if (silent) {
      setRefreshing(true);
    } else {
      setLoading(true);
    }
    setError("");
    try {
      const [policyRes, flagRes, auditRes] = await Promise.all([
        api.getGovernanceIntentPolicies(),
        api.getGovernanceFeatureFlags(),
        api.getGovernanceAuditLog(100),
      ]);
      setPolicies(((policyRes as IntentPolicyResponse)?.policies || []) as IntentPolicy[]);
      setFlags(((flagRes as FeatureFlagResponse)?.flags || []) as FeatureFlag[]);
      setAuditLog(((auditRes as AuditLogResponse)?.items || []) as AuditLogItem[]);
      setLastRefreshedAt(new Date());
    } catch (err) {
      console.error("governance dashboard load failed", err);
      setError(err instanceof Error ? err.message : "Governance 데이터를 불러오지 못했습니다.");
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    loadData();
  }, [loadData]);

  useEffect(() => {
    const timer = window.setInterval(() => {
      loadData(true);
    }, 30000);
    return () => window.clearInterval(timer);
  }, [loadData]);

  const governanceFlag = useMemo(
    () => flags.find((item) => item.flag_key === "governance_enabled"),
    [flags],
  );

  const enabledFlagsCount = useMemo(
    () => flags.filter((item) => item.enabled).length,
    [flags],
  );

  const defaultModelBuckets = useMemo(() => {
    const buckets: Record<string, number> = {};
    for (const policy of policies) {
      const key = (policy.default_model || "unknown").trim() || "unknown";
      buckets[key] = (buckets[key] || 0) + 1;
    }
    return Object.entries(buckets).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
  }, [policies]);

  const handleToggleFlag = useCallback(
    async (flagKey: string, nextEnabled: boolean) => {
      setBusyFlagKey(flagKey);
      setError("");
      try {
        await api.updateGovernanceFeatureFlag(flagKey, nextEnabled);
        await loadData(true);
      } catch (err) {
        console.error("governance flag toggle failed", err);
        setError(err instanceof Error ? err.message : "Feature flag 변경에 실패했습니다.");
      } finally {
        setBusyFlagKey("");
      }
    },
    [loadData],
  );

  const tabs: Array<{ key: Tab; label: string }> = [
    { key: "overview", label: "현황" },
    { key: "policies", label: "정책 테이블" },
    { key: "audit", label: "감사로그" },
  ];

  const cardStyle = {
    background: "var(--bg-card)",
    border: "1px solid var(--border)",
    borderRadius: "12px",
    padding: "16px",
  };

  return (
    <div className="flex flex-col h-full" style={{ background: "var(--bg-primary)" }}>
      <Header title="Governance" />
      <div className="flex-1 p-3 md:p-6 overflow-auto">
        <div className="grid gap-4">
          <section
            className="rounded-2xl p-4 md:p-5"
            style={{
              background: "linear-gradient(135deg, rgba(14,165,233,0.18), rgba(15,23,42,0.96))",
              border: "1px solid rgba(148,163,184,0.18)",
              boxShadow: "0 18px 48px rgba(14,165,233,0.18)",
            }}
          >
            <div className="flex flex-col lg:flex-row lg:items-end lg:justify-between gap-4">
              <div>
                <div className="text-xs uppercase tracking-[0.24em]" style={{ color: "rgba(226,232,240,0.72)" }}>
                  Governance Dashboard
                </div>
                <h1 className="mt-2 text-2xl md:text-3xl font-bold" style={{ color: "var(--text-primary)" }}>
                  정책, kill-switch, 감사 로그를 한 화면에서 관리
                </h1>
                <p className="mt-2 text-sm max-w-3xl" style={{ color: "rgba(226,232,240,0.72)" }}>
                  `intent_policies`, `feature_flags`, `governance_audit_log`를 묶어 운영 상태를 빠르게 점검합니다.
                </p>
              </div>
              <div className="flex items-center gap-2 flex-wrap">
                <div
                  className="px-3 py-2 rounded-lg text-xs"
                  style={{
                    background: "rgba(15,23,42,0.44)",
                    color: "rgba(226,232,240,0.72)",
                    border: "1px solid rgba(148,163,184,0.18)",
                  }}
                >
                  {refreshing
                    ? "새로고침 중..."
                    : `최근 갱신 ${lastRefreshedAt ? formatDateTime(lastRefreshedAt.toISOString()) : "-"}`}
                </div>
                <button
                  type="button"
                  onClick={() => loadData(true)}
                  className="px-3 py-2 rounded-lg text-sm font-semibold"
                  style={{
                    background: "rgba(15,23,42,0.44)",
                    color: "var(--text-primary)",
                    border: "1px solid rgba(148,163,184,0.18)",
                  }}
                >
                  새로고침
                </button>
              </div>
            </div>
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
              Governance 데이터를 불러오는 중...
            </div>
          ) : null}

          {!loading && tab === "overview" ? (
            <div className="grid gap-4">
              <div className="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-5 gap-3">
                {[
                  ["정책 수", formatNumber(policies.length), "var(--text-primary)"],
                  ["활성 플래그", `${formatNumber(enabledFlagsCount)}/${formatNumber(flags.length)}`, "#4ade80"],
                  [
                    "governance_enabled",
                    governanceFlag?.enabled === false ? "OFF" : "ON",
                    governanceFlag?.enabled === false ? "#f87171" : "#38bdf8",
                  ],
                  ["감사 로그", formatNumber(auditLog.length), "var(--accent)"],
                  ["기본 모델 종류", formatNumber(defaultModelBuckets.length), "#fbbf24"],
                ].map(([label, value, color]) => (
                  <div key={String(label)} style={{ ...cardStyle, padding: "14px" }}>
                    <div style={{ color: "var(--text-secondary)", fontSize: "12px", marginBottom: "4px" }}>{label}</div>
                    <div style={{ color: String(color), fontSize: "26px", fontWeight: 700 }}>{value}</div>
                  </div>
                ))}
              </div>

              <section style={cardStyle}>
                <div className="flex items-center justify-between gap-3 flex-wrap" style={{ marginBottom: "14px" }}>
                  <div>
                    <div style={{ color: "var(--text-primary)", fontSize: "16px", fontWeight: 700 }}>
                      Kill-switch / Feature Flags
                    </div>
                    <div style={{ color: "var(--text-secondary)", fontSize: "12px", marginTop: "4px" }}>
                      플래그 변경은 즉시 반영되며 `governance_enabled`를 포함한 전체 상태를 여기서 확인할 수 있습니다.
                    </div>
                  </div>
                </div>

                {flags.length === 0 ? (
                  <div style={{ color: "var(--text-secondary)", textAlign: "center", padding: "24px 0" }}>
                    등록된 feature flag가 없습니다.
                  </div>
                ) : (
                  <div className="grid grid-cols-1 xl:grid-cols-2 gap-3">
                    {flags.map((flag) => {
                      const tone = flagTone(flag.enabled);
                      return (
                        <div
                          key={flag.flag_key}
                          className="rounded-xl p-4"
                          style={{
                            background: "rgba(15,23,42,0.28)",
                            border: "1px solid var(--border)",
                          }}
                        >
                          <div className="flex items-start justify-between gap-3 flex-wrap">
                            <div>
                              <div style={{ color: "var(--text-primary)", fontWeight: 700 }}>{flag.flag_key}</div>
                              <div style={{ color: "var(--text-secondary)", fontSize: "12px", marginTop: "4px" }}>
                                scope {flag.scope || "global"} · 변경 {formatDateTime(flag.last_changed_at)}
                              </div>
                              {flag.notes ? (
                                <div style={{ color: "var(--text-secondary)", fontSize: "12px", marginTop: "8px", lineHeight: 1.5 }}>
                                  {flag.notes}
                                </div>
                              ) : null}
                            </div>

                            <div className="flex items-center gap-2 flex-wrap">
                              <span
                                className="px-2.5 py-1 rounded-full text-xs font-semibold"
                                style={tone}
                              >
                                {flag.enabled ? "enabled" : "disabled"}
                              </span>
                              <button
                                type="button"
                                onClick={() => handleToggleFlag(flag.flag_key, !flag.enabled)}
                                disabled={busyFlagKey === flag.flag_key}
                                className="px-3 py-2 rounded-lg text-xs font-semibold"
                                style={{
                                  background: flag.enabled ? "rgba(127,29,29,0.88)" : "rgba(15,118,110,0.88)",
                                  color: "#fff",
                                  border: "1px solid rgba(255,255,255,0.12)",
                                  opacity: busyFlagKey === flag.flag_key ? 0.7 : 1,
                                }}
                              >
                                {busyFlagKey === flag.flag_key ? "변경 중..." : flag.enabled ? "비활성화" : "활성화"}
                              </button>
                            </div>
                          </div>

                          <div style={{ color: "var(--text-secondary)", fontSize: "11px", marginTop: "10px" }}>
                            changed by {flag.last_changed_by || "-"}
                          </div>
                        </div>
                      );
                    })}
                  </div>
                )}
              </section>

              <div className="grid grid-cols-1 xl:grid-cols-[minmax(0,0.9fr)_minmax(0,1.1fr)] gap-4">
                <section style={cardStyle}>
                  <div style={{ color: "var(--text-primary)", fontSize: "16px", fontWeight: 700, marginBottom: "12px" }}>
                    기본 모델 분포
                  </div>
                  <div className="grid gap-2">
                    {defaultModelBuckets.length === 0 ? (
                      <div style={{ color: "var(--text-secondary)" }}>등록된 정책이 없습니다.</div>
                    ) : (
                      defaultModelBuckets.map(([model, count]) => (
                        <div
                          key={model}
                          className="flex items-center justify-between gap-3 rounded-xl px-3 py-3"
                          style={{ background: "rgba(15,23,42,0.28)", border: "1px solid var(--border)" }}
                        >
                          <span style={{ color: "var(--text-primary)", fontWeight: 600 }}>{model}</span>
                          <span style={{ color: "var(--accent)", fontWeight: 700 }}>{formatNumber(count)}</span>
                        </div>
                      ))
                    )}
                  </div>
                </section>

                <section style={cardStyle}>
                  <div className="flex items-center justify-between gap-3" style={{ marginBottom: "12px" }}>
                    <div style={{ color: "var(--text-primary)", fontSize: "16px", fontWeight: 700 }}>
                      최근 감사 이벤트
                    </div>
                    <div style={{ color: "var(--text-secondary)", fontSize: "12px" }}>
                      최신 {Math.min(auditLog.length, 6)}건
                    </div>
                  </div>
                  <div className="grid gap-3">
                    {auditLog.length === 0 ? (
                      <div style={{ color: "var(--text-secondary)" }}>감사 로그가 없습니다.</div>
                    ) : (
                      auditLog.slice(0, 6).map((item) => (
                        <div
                          key={item.id}
                          className="rounded-xl p-3"
                          style={{ background: "rgba(15,23,42,0.28)", border: "1px solid var(--border)" }}
                        >
                          <div className="flex items-center justify-between gap-3 flex-wrap">
                            <div style={{ color: "var(--text-primary)", fontWeight: 600 }}>
                              {item.event} · {item.mode}
                            </div>
                            <div style={{ color: "var(--text-secondary)", fontSize: "11px" }}>
                              {formatDateTime(item.at)}
                            </div>
                          </div>
                          <div style={{ color: "var(--text-secondary)", fontSize: "12px", marginTop: "8px", lineHeight: 1.5 }}>
                            {item.diff_summary || "diff summary 없음"}
                          </div>
                        </div>
                      ))
                    )}
                  </div>
                </section>
              </div>
            </div>
          ) : null}

          {!loading && tab === "policies" ? (
            <section style={cardStyle}>
              <div className="flex items-center justify-between gap-3 flex-wrap" style={{ marginBottom: "12px" }}>
                <div>
                  <div style={{ color: "var(--text-primary)", fontSize: "16px", fontWeight: 700 }}>
                    Intent Policy Table
                  </div>
                  <div style={{ color: "var(--text-secondary)", fontSize: "12px", marginTop: "4px" }}>
                    정책 수 {formatNumber(policies.length)}건
                  </div>
                </div>
              </div>

              {policies.length === 0 ? (
                <div style={{ color: "var(--text-secondary)", textAlign: "center", padding: "24px 0" }}>
                  등록된 intent policy가 없습니다.
                </div>
              ) : (
                <div style={{ overflowX: "auto" }}>
                  <table style={{ width: "100%", borderCollapse: "collapse", minWidth: "1120px" }}>
                    <thead>
                      <tr style={{ borderBottom: "1px solid var(--border)" }}>
                        {["Intent", "Default", "Allowed Models", "Downgrade", "Tools", "Updated", "Description"].map((header) => (
                          <th
                            key={header}
                            style={{
                              padding: "12px 10px",
                              textAlign: "left",
                              color: "var(--text-secondary)",
                              fontSize: "12px",
                              fontWeight: 600,
                            }}
                          >
                            {header}
                          </th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {policies.map((policy) => {
                        const allowedModels = asStringList(policy.allowed_models);
                        const toolAllowlist = asStringList(policy.tool_allowlist);
                        return (
                          <tr key={`${policy.id}-${policy.intent}`} style={{ borderBottom: "1px solid rgba(148,163,184,0.12)" }}>
                            <td style={{ padding: "14px 10px", verticalAlign: "top" }}>
                              <div style={{ color: "var(--text-primary)", fontWeight: 700 }}>{policy.intent}</div>
                              {policy.temperature != null ? (
                                <div style={{ color: "var(--text-secondary)", fontSize: "11px", marginTop: "4px" }}>
                                  temperature {policy.temperature}
                                </div>
                              ) : null}
                            </td>
                            <td style={{ padding: "14px 10px", verticalAlign: "top", color: "var(--accent)", fontWeight: 600 }}>
                              {policy.default_model || "-"}
                            </td>
                            <td style={{ padding: "14px 10px", verticalAlign: "top" }}>
                              <div className="flex flex-wrap gap-2">
                                {allowedModels.length === 0 ? (
                                  <span style={{ color: "var(--text-secondary)", fontSize: "12px" }}>-</span>
                                ) : (
                                  allowedModels.map((model) => (
                                    <span
                                      key={`${policy.intent}-${model}`}
                                      className="px-2 py-1 rounded-full text-xs"
                                      style={{
                                        background: "var(--bg-hover)",
                                        color: "var(--text-primary)",
                                        border: "1px solid var(--border)",
                                      }}
                                    >
                                      {model}
                                    </span>
                                  ))
                                )}
                              </div>
                            </td>
                            <td style={{ padding: "14px 10px", verticalAlign: "top" }}>
                              <span
                                className="px-2 py-1 rounded-full text-xs font-semibold"
                                style={policy.cascade_downgrade ? flagTone(true) : flagTone(false)}
                              >
                                {policy.cascade_downgrade ? "enabled" : "disabled"}
                              </span>
                            </td>
                            <td style={{ padding: "14px 10px", verticalAlign: "top" }}>
                              <div className="flex flex-wrap gap-2">
                                {toolAllowlist.length === 0 ? (
                                  <span style={{ color: "var(--text-secondary)", fontSize: "12px" }}>-</span>
                                ) : (
                                  toolAllowlist.map((tool) => (
                                    <span
                                      key={`${policy.intent}-${tool}`}
                                      className="px-2 py-1 rounded-full text-xs"
                                      style={{
                                        background: "rgba(56,189,248,0.12)",
                                        color: "#7dd3fc",
                                        border: "1px solid rgba(56,189,248,0.2)",
                                      }}
                                    >
                                      {tool}
                                    </span>
                                  ))
                                )}
                              </div>
                            </td>
                            <td style={{ padding: "14px 10px", verticalAlign: "top" }}>
                              <div style={{ color: "var(--text-primary)", fontSize: "12px" }}>{formatDateTime(policy.updated_at)}</div>
                              <div style={{ color: "var(--text-secondary)", fontSize: "11px", marginTop: "4px" }}>
                                {policy.updated_by || "-"}
                              </div>
                            </td>
                            <td style={{ padding: "14px 10px", verticalAlign: "top", color: "var(--text-secondary)", fontSize: "12px", lineHeight: 1.5 }}>
                              {policy.description || "-"}
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              )}
            </section>
          ) : null}

          {!loading && tab === "audit" ? (
            <section style={cardStyle}>
              <div className="flex items-center justify-between gap-3 flex-wrap" style={{ marginBottom: "12px" }}>
                <div>
                  <div style={{ color: "var(--text-primary)", fontSize: "16px", fontWeight: 700 }}>
                    Governance Audit Log
                  </div>
                  <div style={{ color: "var(--text-secondary)", fontSize: "12px", marginTop: "4px" }}>
                    총 {formatNumber(auditLog.length)}건
                  </div>
                </div>
              </div>

              {auditLog.length === 0 ? (
                <div style={{ color: "var(--text-secondary)", textAlign: "center", padding: "24px 0" }}>
                  감사 로그가 없습니다.
                </div>
              ) : (
                <div className="grid gap-3">
                  {auditLog.map((item) => (
                    <div
                      key={item.id}
                      className="rounded-xl p-4"
                      style={{ background: "rgba(15,23,42,0.28)", border: "1px solid var(--border)" }}
                    >
                      <div className="flex items-start justify-between gap-3 flex-wrap">
                        <div>
                          <div style={{ color: "var(--text-primary)", fontWeight: 700 }}>
                            {item.event} · {item.mode}
                          </div>
                          <div style={{ color: "var(--text-secondary)", fontSize: "12px", marginTop: "4px" }}>
                            {formatDateTime(item.at)}
                            {item.trace_id ? ` · ${item.trace_id}` : ""}
                          </div>
                        </div>
                        <span
                          className="px-2.5 py-1 rounded-full text-xs font-semibold"
                          style={{
                            background: "rgba(56,189,248,0.12)",
                            color: "#7dd3fc",
                            border: "1px solid rgba(56,189,248,0.2)",
                          }}
                        >
                          #{item.id}
                        </span>
                      </div>

                      <div className="grid grid-cols-1 md:grid-cols-2 gap-3" style={{ marginTop: "12px" }}>
                        <div
                          className="rounded-lg p-3"
                          style={{ background: "rgba(15,23,42,0.28)", border: "1px solid rgba(148,163,184,0.14)" }}
                        >
                          <div style={{ color: "var(--text-secondary)", fontSize: "11px", marginBottom: "6px" }}>
                            Diff Summary
                          </div>
                          <div style={{ color: "var(--text-primary)", fontSize: "13px", lineHeight: 1.6 }}>
                            {item.diff_summary || "diff 없음"}
                          </div>
                        </div>
                        <div
                          className="rounded-lg p-3"
                          style={{ background: "rgba(15,23,42,0.28)", border: "1px solid rgba(148,163,184,0.14)" }}
                        >
                          <div style={{ color: "var(--text-secondary)", fontSize: "11px", marginBottom: "6px" }}>
                            Legacy / DB Result
                          </div>
                          <div style={{ color: "var(--text-primary)", fontSize: "13px", lineHeight: 1.6 }}>
                            legacy {stringifyValue(item.legacy_result)} · db {stringifyValue(item.db_result)}
                          </div>
                        </div>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </section>
          ) : null}
        </div>
      </div>
    </div>
  );
}
