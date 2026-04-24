"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

import Header from "@/components/Header";
import {
  api,
  type GovernanceAuditLogItem,
  type GovernanceFeatureFlag,
  type GovernanceIntentPolicy,
  type GovernanceRoleProfile,
} from "@/lib/api";

function formatDateTime(value?: string | null): string {
  if (!value) return "-";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString("ko-KR", {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function normalizeText(value: unknown, fallback = "-"): string {
  if (value == null) return fallback;
  const text = String(value).trim();
  return text || fallback;
}

function toStringArray(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.map((item) => String(item).trim()).filter(Boolean);
}

function getFlagKey(flag: GovernanceFeatureFlag): string {
  return String(flag.flag_key || flag.key || flag.name || "").trim();
}

function extractRoleProfiles(payload: unknown): GovernanceRoleProfile[] {
  if (!payload || typeof payload !== "object") return [];
  const data = payload as Record<string, unknown>;
  const candidates = [data.profiles, data.roles, data.items];
  for (const item of candidates) {
    if (Array.isArray(item)) {
      return item as GovernanceRoleProfile[];
    }
  }
  return [];
}

export default function AdminGovernancePage() {
  const [intentPolicies, setIntentPolicies] = useState<GovernanceIntentPolicy[]>([]);
  const [featureFlags, setFeatureFlags] = useState<GovernanceFeatureFlag[]>([]);
  const [roleProfiles, setRoleProfiles] = useState<GovernanceRoleProfile[]>([]);
  const [auditLog, setAuditLog] = useState<GovernanceAuditLogItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState("");
  const [lastRefreshedAt, setLastRefreshedAt] = useState<Date | null>(null);
  const [togglePending, setTogglePending] = useState<Record<string, boolean>>({});

  const loadData = useCallback(async (silent = false) => {
    if (silent) {
      setRefreshing(true);
    } else {
      setLoading(true);
    }

    try {
      const [policyRes, flagRes, roleRes, auditRes] = await Promise.allSettled([
        api.getGovernanceIntentPolicies(),
        api.getGovernanceFeatureFlags(),
        api.getGovernanceRoleProfiles(),
        api.getGovernanceAuditLog(30),
      ]);

      if (policyRes.status === "fulfilled") {
        setIntentPolicies(policyRes.value?.policies || []);
      } else {
        setIntentPolicies([]);
      }

      if (flagRes.status === "fulfilled") {
        setFeatureFlags(flagRes.value?.flags || []);
      } else {
        setFeatureFlags([]);
      }

      if (roleRes.status === "fulfilled") {
        setRoleProfiles(extractRoleProfiles(roleRes.value));
      } else {
        setRoleProfiles([]);
      }

      if (auditRes.status === "fulfilled") {
        setAuditLog(auditRes.value?.items || []);
      } else {
        setAuditLog([]);
      }

      const failed: string[] = [];
      if (policyRes.status === "rejected") failed.push("intent-policies");
      if (flagRes.status === "rejected") failed.push("feature-flags");
      if (roleRes.status === "rejected") failed.push("role-profiles");
      if (auditRes.status === "rejected") failed.push("audit-log");

      setError(failed.length > 0 ? `${failed.join(", ")} 조회에 실패했습니다.` : "");
      setLastRefreshedAt(new Date());
    } catch (err) {
      console.error("governance page load failed", err);
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

  const toggleFeatureFlag = useCallback(async (flag: GovernanceFeatureFlag) => {
    const flagKey = getFlagKey(flag);
    if (!flagKey) return;

    const nextEnabled = !Boolean(flag.enabled);
    setTogglePending((prev) => ({ ...prev, [flagKey]: true }));

    try {
      const updated = await api.updateGovernanceFeatureFlag(flagKey, nextEnabled);
      setFeatureFlags((prev) => prev.map((item) => {
        if (getFlagKey(item) !== flagKey) return item;
        return {
          ...item,
          ...updated,
          flag_key: getFlagKey(updated) || flagKey,
          enabled: Boolean(updated?.enabled),
        };
      }));
      setError("");
    } catch (err) {
      console.error("feature flag update failed", err);
      setError(err instanceof Error ? err.message : `${flagKey} 플래그를 변경하지 못했습니다.`);
    } finally {
      setTogglePending((prev) => ({ ...prev, [flagKey]: false }));
    }
  }, []);

  const summary = useMemo(() => {
    const enabledFlags = featureFlags.filter((flag) => Boolean(flag.enabled)).length;
    return {
      intentPolicies: intentPolicies.length,
      enabledFlags,
      roleProfiles: roleProfiles.length,
      auditLog: auditLog.length,
    };
  }, [auditLog.length, featureFlags, intentPolicies.length, roleProfiles.length]);

  const cardStyle = {
    background: "var(--bg-card)",
    border: "1px solid var(--border)",
    borderRadius: "10px",
    padding: "16px",
  };

  return (
    <div className="flex flex-col h-full" style={{ background: "var(--bg-primary)" }}>
      <Header title="Governance v2.1" />
      <div className="flex-1 p-3 md:p-6 overflow-auto">
        <div className="grid gap-4">
          <div className="flex items-start justify-between gap-3 flex-wrap">
            <div>
              <div style={{ color: "var(--text-primary)", fontSize: "22px", fontWeight: 700 }}>
                Governance v2.1 Console
              </div>
              <div style={{ color: "var(--text-secondary)", fontSize: "13px", marginTop: "4px" }}>
                intent 정책, feature flag, role profile, audit log를 한 화면에서 관리합니다.
              </div>
            </div>
            <div className="flex items-center gap-2 flex-wrap">
              <div
                style={{
                  padding: "8px 12px",
                  borderRadius: "999px",
                  background: "var(--bg-card)",
                  border: "1px solid var(--border)",
                  color: "var(--text-secondary)",
                  fontSize: "12px",
                }}
              >
                {refreshing
                  ? "새로고침 중..."
                  : `최근 갱신 ${lastRefreshedAt ? formatDateTime(lastRefreshedAt.toISOString()) : "-"}`}
              </div>
              <button
                type="button"
                onClick={() => loadData(true)}
                style={{
                  padding: "8px 14px",
                  borderRadius: "8px",
                  border: "none",
                  background: "var(--accent)",
                  color: "#fff",
                  cursor: "pointer",
                  fontWeight: 600,
                }}
              >
                새로고침
              </button>
            </div>
          </div>

          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            {[{
              label: "Intent Policies",
              value: summary.intentPolicies,
            }, {
              label: "Enabled Flags",
              value: summary.enabledFlags,
            }, {
              label: "Role Profiles",
              value: summary.roleProfiles,
            }, {
              label: "Audit Logs",
              value: summary.auditLog,
            }].map((item) => (
              <div key={item.label} style={{ ...cardStyle, padding: "14px" }}>
                <div style={{ color: "var(--text-secondary)", fontSize: "12px", marginBottom: "4px" }}>
                  {item.label}
                </div>
                <div style={{ color: "var(--text-primary)", fontSize: "26px", fontWeight: 700 }}>
                  {loading ? "..." : item.value}
                </div>
              </div>
            ))}
          </div>

          {error ? (
            <div style={{ ...cardStyle, color: "var(--danger)" }}>{error}</div>
          ) : null}

          <section style={cardStyle}>
            <div className="flex items-center justify-between gap-3 flex-wrap" style={{ marginBottom: "12px" }}>
              <h2 style={{ color: "var(--text-primary)", fontWeight: 700, fontSize: "16px" }}>Intent Policies</h2>
              <span style={{ color: "var(--text-secondary)", fontSize: "12px" }}>
                {intentPolicies.length} rows
              </span>
            </div>
            <div style={{ overflowX: "auto" }}>
              <table style={{ width: "100%", borderCollapse: "collapse", minWidth: "780px" }}>
                <thead>
                  <tr style={{ borderBottom: "1px solid var(--border)", color: "var(--text-secondary)" }}>
                    <th style={thStyle}>Intent</th>
                    <th style={thStyle}>Default Model</th>
                    <th style={thStyle}>Allowed Models</th>
                    <th style={thStyle}>Temperature</th>
                    <th style={thStyle}>Cascade Downgrade</th>
                  </tr>
                </thead>
                <tbody>
                  {intentPolicies.length === 0 ? (
                    <tr>
                      <td colSpan={5} style={{ ...tdStyle, textAlign: "center", color: "var(--text-secondary)" }}>
                        {loading ? "로딩 중..." : "등록된 intent policy가 없습니다."}
                      </td>
                    </tr>
                  ) : (
                    intentPolicies.map((policy) => (
                      <tr key={policy.intent} style={{ borderBottom: "1px solid var(--border)" }}>
                        <td style={tdStyle}>{normalizeText(policy.intent)}</td>
                        <td style={tdStyle}>{normalizeText(policy.default_model)}</td>
                        <td style={tdStyle}>{toStringArray(policy.allowed_models).join(", ") || "-"}</td>
                        <td style={tdStyle}>
                          {typeof policy.temperature === "number" ? policy.temperature.toFixed(2) : "-"}
                        </td>
                        <td style={tdStyle}>
                          <span
                            style={{
                              padding: "4px 10px",
                              borderRadius: "999px",
                              background: policy.cascade_downgrade ? "rgba(59,130,246,0.14)" : "rgba(148,163,184,0.14)",
                              color: policy.cascade_downgrade ? "var(--accent)" : "var(--text-secondary)",
                              border: "1px solid var(--border)",
                              fontSize: "11px",
                              fontWeight: 600,
                            }}
                          >
                            {policy.cascade_downgrade ? "ON" : "OFF"}
                          </span>
                        </td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </div>
          </section>

          <section style={cardStyle}>
            <div className="flex items-center justify-between gap-3 flex-wrap" style={{ marginBottom: "12px" }}>
              <h2 style={{ color: "var(--text-primary)", fontWeight: 700, fontSize: "16px" }}>Feature Flags</h2>
              <span style={{ color: "var(--text-secondary)", fontSize: "12px" }}>
                {featureFlags.length} flags
              </span>
            </div>

            {featureFlags.length === 0 ? (
              <div style={{ color: "var(--text-secondary)", fontSize: "13px" }}>
                {loading ? "로딩 중..." : "표시할 feature flag가 없습니다."}
              </div>
            ) : (
              <div className="grid gap-2">
                {featureFlags.map((flag) => {
                  const flagKey = getFlagKey(flag) || "(unknown)";
                  const pending = Boolean(togglePending[flagKey]);
                  return (
                    <div
                      key={flagKey}
                      className="flex items-center justify-between gap-3"
                      style={{
                        border: "1px solid var(--border)",
                        borderRadius: "10px",
                        padding: "10px 12px",
                        background: "rgba(15,23,42,0.28)",
                      }}
                    >
                      <div>
                        <div style={{ color: "var(--text-primary)", fontWeight: 600 }}>{flagKey}</div>
                        <div style={{ color: "var(--text-secondary)", fontSize: "12px", marginTop: "2px" }}>
                          changed by {normalizeText(flag.changed_by, "-")} · {formatDateTime(flag.changed_at || flag.updated_at || null)}
                        </div>
                      </div>

                      <button
                        type="button"
                        role="switch"
                        aria-checked={Boolean(flag.enabled)}
                        disabled={pending || !getFlagKey(flag)}
                        onClick={() => toggleFeatureFlag(flag)}
                        style={{
                          width: "52px",
                          height: "30px",
                          borderRadius: "999px",
                          border: "1px solid var(--border)",
                          background: flag.enabled ? "var(--accent)" : "rgba(148,163,184,0.35)",
                          cursor: pending ? "wait" : "pointer",
                          position: "relative",
                          transition: "all 0.15s ease",
                          opacity: pending ? 0.65 : 1,
                        }}
                      >
                        <span
                          style={{
                            position: "absolute",
                            top: "3px",
                            left: flag.enabled ? "25px" : "3px",
                            width: "22px",
                            height: "22px",
                            borderRadius: "50%",
                            background: "#fff",
                            transition: "all 0.15s ease",
                          }}
                        />
                      </button>
                    </div>
                  );
                })}
              </div>
            )}
          </section>

          <section style={cardStyle}>
            <div className="flex items-center justify-between gap-3 flex-wrap" style={{ marginBottom: "12px" }}>
              <h2 style={{ color: "var(--text-primary)", fontWeight: 700, fontSize: "16px" }}>Role Profiles</h2>
              <span style={{ color: "var(--text-secondary)", fontSize: "12px" }}>
                {roleProfiles.length} rows
              </span>
            </div>
            <div style={{ overflowX: "auto" }}>
              <table style={{ width: "100%", borderCollapse: "collapse", minWidth: "760px" }}>
                <thead>
                  <tr style={{ borderBottom: "1px solid var(--border)", color: "var(--text-secondary)" }}>
                    <th style={thStyle}>Role</th>
                    <th style={thStyle}>Default/Base Model</th>
                    <th style={thStyle}>Allowed Models / Intents</th>
                    <th style={thStyle}>Updated At</th>
                  </tr>
                </thead>
                <tbody>
                  {roleProfiles.length === 0 ? (
                    <tr>
                      <td colSpan={4} style={{ ...tdStyle, textAlign: "center", color: "var(--text-secondary)" }}>
                        {loading ? "로딩 중..." : "등록된 role profile이 없습니다."}
                      </td>
                    </tr>
                  ) : (
                    roleProfiles.map((profile, index) => {
                      const extras = profile as Record<string, unknown>;
                      const roleLabel = normalizeText(profile.role || profile.name || extras.workspace || extras.workspace_id);
                      const baseModel = normalizeText(profile.default_model || profile.base_model);
                      const allowed = [
                        ...toStringArray(profile.allowed_models),
                        ...toStringArray(profile.allowed_intents),
                        ...toStringArray(extras.intents),
                      ];
                      return (
                        <tr key={`${roleLabel}-${index}`} style={{ borderBottom: "1px solid var(--border)" }}>
                          <td style={tdStyle}>{roleLabel}</td>
                          <td style={tdStyle}>{baseModel}</td>
                          <td style={tdStyle}>{allowed.join(", ") || "-"}</td>
                          <td style={tdStyle}>{formatDateTime((profile.updated_at as string | null) || null)}</td>
                        </tr>
                      );
                    })
                  )}
                </tbody>
              </table>
            </div>
          </section>

          <section style={cardStyle}>
            <div className="flex items-center justify-between gap-3 flex-wrap" style={{ marginBottom: "12px" }}>
              <h2 style={{ color: "var(--text-primary)", fontWeight: 700, fontSize: "16px" }}>Recent Audit Log</h2>
              <span style={{ color: "var(--text-secondary)", fontSize: "12px" }}>
                {auditLog.length} rows
              </span>
            </div>
            <div style={{ overflowX: "auto" }}>
              <table style={{ width: "100%", borderCollapse: "collapse", minWidth: "780px" }}>
                <thead>
                  <tr style={{ borderBottom: "1px solid var(--border)", color: "var(--text-secondary)" }}>
                    <th style={thStyle}>Time</th>
                    <th style={thStyle}>Event</th>
                    <th style={thStyle}>Mode</th>
                    <th style={thStyle}>Diff Summary</th>
                    <th style={thStyle}>Trace ID</th>
                  </tr>
                </thead>
                <tbody>
                  {auditLog.length === 0 ? (
                    <tr>
                      <td colSpan={5} style={{ ...tdStyle, textAlign: "center", color: "var(--text-secondary)" }}>
                        {loading ? "로딩 중..." : "표시할 audit log가 없습니다."}
                      </td>
                    </tr>
                  ) : (
                    auditLog.map((item) => (
                      <tr key={String(item.id)} style={{ borderBottom: "1px solid var(--border)" }}>
                        <td style={tdStyle}>{formatDateTime(item.at)}</td>
                        <td style={tdStyle}>{normalizeText(item.event)}</td>
                        <td style={tdStyle}>{normalizeText(item.mode)}</td>
                        <td style={tdStyle}>{normalizeText(item.diff_summary)}</td>
                        <td style={tdStyle}>{normalizeText(item.trace_id)}</td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </div>
          </section>
        </div>
      </div>
    </div>
  );
}

const thStyle = {
  textAlign: "left" as const,
  padding: "10px 8px",
  fontSize: "12px",
  fontWeight: 600,
};

const tdStyle = {
  padding: "10px 8px",
  color: "var(--text-primary)",
  fontSize: "13px",
  verticalAlign: "top" as const,
};
