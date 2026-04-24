"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

import Header from "@/components/Header";
import { api } from "@/lib/api";

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
  diff_summary?: string | null;
  trace_id?: string | null;
}

interface AuditLogResponse {
  items: AuditLogItem[];
  total: number;
}

interface RoleProfile {
  role: string;
  system_prompt_ref?: string | null;
  tool_allowlist?: string[] | null;
  max_turns?: number | null;
  budget_usd?: number | null;
  escalation_rules?: Record<string, unknown> | null;
  project_scope?: string[] | null;
  updated_at?: string | null;
}

interface RoleProfileResponse {
  profiles: RoleProfile[];
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

function formatCurrency(value?: number | null): string {
  if (value == null) return "-";
  return new Intl.NumberFormat("ko-KR", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 2,
  }).format(value);
}

export default function AdminEmergencyPage() {
  const [flags, setFlags] = useState<FeatureFlag[]>([]);
  const [auditLog, setAuditLog] = useState<AuditLogItem[]>([]);
  const [profiles, setProfiles] = useState<RoleProfile[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [busyFlagKey, setBusyFlagKey] = useState("");

  const loadData = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [flagsRes, auditRes, profilesRes] = await Promise.all([
        api.getGovernanceFeatureFlags(),
        api.getGovernanceAuditLog(40),
        api.getGovernanceRoleProfiles(),
      ]);
      setFlags((flagsRes as FeatureFlagResponse).flags || []);
      setAuditLog((auditRes as AuditLogResponse).items || []);
      setProfiles((profilesRes as RoleProfileResponse).profiles || []);
    } catch (err) {
      console.error("emergency governance load failed", err);
      setError(err instanceof Error ? err.message : "운영 데이터를 불러오지 못했습니다.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadData();
  }, [loadData]);

  const governanceEnabled = useMemo(
    () => flags.find((item) => item.flag_key === "governance_enabled"),
    [flags],
  );

  const handleToggle = useCallback(
    async (flagKey: string, nextEnabled: boolean) => {
      setBusyFlagKey(flagKey);
      setError("");
      try {
        await api.updateGovernanceFeatureFlag(flagKey, nextEnabled);
        await loadData();
      } catch (err) {
        console.error("feature flag toggle failed", err);
        setError(err instanceof Error ? err.message : "플래그 변경에 실패했습니다.");
      } finally {
        setBusyFlagKey("");
      }
    },
    [loadData],
  );

  const cardStyle = {
    background: "var(--bg-card)",
    border: "1px solid var(--border)",
    borderRadius: "10px",
    padding: "16px",
  };

  const governanceOn = governanceEnabled?.enabled !== false;

  return (
    <div className="flex flex-col h-full" style={{ background: "var(--bg-primary)" }}>
      <Header title="Emergency Control" />
      <div className="flex-1 p-3 md:p-6 overflow-auto">
        <div className="grid gap-4">
          <section
            className="rounded-2xl p-4 md:p-5"
            style={{
              background: governanceOn
                ? "linear-gradient(135deg, rgba(15,118,110,0.24), rgba(15,23,42,0.96))"
                : "linear-gradient(135deg, rgba(185,28,28,0.24), rgba(15,23,42,0.96))",
              border: governanceOn
                ? "1px solid rgba(45,212,191,0.22)"
                : "1px solid rgba(248,113,113,0.24)",
              boxShadow: governanceOn
                ? "0 18px 48px rgba(13,148,136,0.18)"
                : "0 18px 48px rgba(220,38,38,0.18)",
            }}
          >
            <div className="flex flex-col lg:flex-row lg:items-end lg:justify-between gap-4">
              <div>
                <div className="text-xs uppercase tracking-[0.24em]" style={{ color: "rgba(226,232,240,0.72)" }}>
                  Governance Kill Switch
                </div>
                <h1 className="mt-2 text-2xl md:text-3xl font-bold" style={{ color: "var(--text-primary)" }}>
                  {governanceOn ? "거버넌스 경로 활성" : "레거시 폴백 모드"}
                </h1>
                <p className="mt-2 text-sm max-w-3xl" style={{ color: "rgba(226,232,240,0.72)" }}>
                  `governance_enabled` 플래그를 직접 제어해 DB 정책 경로와 레거시 코드 경로를 즉시 전환합니다.
                </p>
                <div className="mt-3 text-xs" style={{ color: "rgba(226,232,240,0.72)" }}>
                  마지막 변경: {formatDateTime(governanceEnabled?.last_changed_at)} / {governanceEnabled?.last_changed_by || "-"}
                </div>
              </div>
              <button
                type="button"
                onClick={() => handleToggle("governance_enabled", !governanceOn)}
                disabled={busyFlagKey === "governance_enabled"}
                className="self-start px-4 py-2 rounded-lg text-sm font-semibold"
                style={{
                  background: governanceOn ? "rgba(127,29,29,0.88)" : "rgba(15,118,110,0.88)",
                  color: "#fff",
                  border: "1px solid rgba(255,255,255,0.14)",
                  opacity: busyFlagKey === "governance_enabled" ? 0.7 : 1,
                }}
              >
                {busyFlagKey === "governance_enabled"
                  ? "변경 중..."
                  : governanceOn
                    ? "레거시 폴백으로 전환"
                    : "거버넌스 다시 활성화"}
              </button>
            </div>
          </section>

          {error ? (
            <div style={{ ...cardStyle, color: "var(--danger)" }}>{error}</div>
          ) : null}

          {loading ? (
            <div style={{ ...cardStyle, color: "var(--text-secondary)", textAlign: "center" }}>
              운영 데이터를 불러오는 중...
            </div>
          ) : (
            <>
              <section className="grid grid-cols-1 xl:grid-cols-2 gap-4">
                <div style={cardStyle}>
                  <div className="flex items-center justify-between gap-3 mb-3">
                    <h2 style={{ color: "var(--text-primary)", fontWeight: 700 }}>Feature Flags</h2>
                    <button
                      type="button"
                      onClick={loadData}
                      className="px-3 py-2 rounded-lg text-sm font-semibold"
                      style={{
                        background: "var(--bg-hover)",
                        color: "var(--text-primary)",
                        border: "1px solid var(--border)",
                      }}
                    >
                      새로고침
                    </button>
                  </div>
                  <div className="grid gap-3">
                    {flags.map((flag) => (
                      <div
                        key={flag.flag_key}
                        className="rounded-xl p-3"
                        style={{
                          border: "1px solid var(--border)",
                          background: flag.enabled ? "rgba(34,197,94,0.08)" : "rgba(239,68,68,0.08)",
                        }}
                      >
                        <div className="flex items-start justify-between gap-3">
                          <div>
                            <div style={{ color: "var(--text-primary)", fontWeight: 600 }}>{flag.flag_key}</div>
                            <div className="mt-1 text-xs" style={{ color: "var(--text-secondary)" }}>
                              scope: {flag.scope || "global"} · 변경: {formatDateTime(flag.last_changed_at)}
                            </div>
                            {flag.notes ? (
                              <div className="mt-1 text-xs" style={{ color: "var(--text-secondary)" }}>
                                {flag.notes}
                              </div>
                            ) : null}
                          </div>
                          <button
                            type="button"
                            disabled={busyFlagKey === flag.flag_key}
                            onClick={() => handleToggle(flag.flag_key, !flag.enabled)}
                            className="px-3 py-2 rounded-lg text-xs font-semibold"
                            style={{
                              background: flag.enabled ? "rgba(127,29,29,0.9)" : "rgba(15,118,110,0.9)",
                              color: "#fff",
                              border: "1px solid rgba(255,255,255,0.12)",
                              opacity: busyFlagKey === flag.flag_key ? 0.7 : 1,
                            }}
                          >
                            {flag.enabled ? "비활성화" : "활성화"}
                          </button>
                        </div>
                      </div>
                    ))}
                  </div>
                </div>

                <div style={cardStyle}>
                  <h2 style={{ color: "var(--text-primary)", fontWeight: 700, marginBottom: "12px" }}>
                    Role Profiles
                  </h2>
                  <div className="grid gap-3">
                    {profiles.map((profile) => (
                      <div
                        key={profile.role}
                        className="rounded-xl p-3"
                        style={{ border: "1px solid var(--border)", background: "rgba(15,23,42,0.28)" }}
                      >
                        <div className="flex items-center justify-between gap-3">
                          <div style={{ color: "var(--text-primary)", fontWeight: 600 }}>{profile.role}</div>
                          <div className="text-xs" style={{ color: "var(--text-secondary)" }}>
                            {formatCurrency(profile.budget_usd)} / {profile.max_turns ?? "-"} turns
                          </div>
                        </div>
                        <div className="mt-2 text-xs" style={{ color: "var(--text-secondary)" }}>
                          scope: {profile.project_scope && profile.project_scope.length > 0
                            ? profile.project_scope.join(", ")
                            : "ALL"}
                        </div>
                        <div className="mt-1 text-xs" style={{ color: "var(--text-secondary)" }}>
                          tools: {profile.tool_allowlist && profile.tool_allowlist.length > 0
                            ? profile.tool_allowlist.join(", ")
                            : "제한 없음"}
                        </div>
                        <div className="mt-1 text-xs" style={{ color: "var(--text-secondary)" }}>
                          prompt: {profile.system_prompt_ref || "-"}
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              </section>

              <section style={cardStyle}>
                <h2 style={{ color: "var(--text-primary)", fontWeight: 700, marginBottom: "12px" }}>
                  Governance Audit Log
                </h2>
                <div className="grid gap-3">
                  {auditLog.length === 0 ? (
                    <div style={{ color: "var(--text-secondary)" }}>감사 로그가 없습니다.</div>
                  ) : (
                    auditLog.map((item) => (
                      <div
                        key={item.id}
                        className="rounded-xl p-3"
                        style={{ border: "1px solid var(--border)", background: "rgba(15,23,42,0.28)" }}
                      >
                        <div className="flex flex-col md:flex-row md:items-center md:justify-between gap-2">
                          <div style={{ color: "var(--text-primary)", fontWeight: 600 }}>
                            {item.event} · {item.mode}
                          </div>
                          <div className="text-xs" style={{ color: "var(--text-secondary)" }}>
                            {formatDateTime(item.at)} {item.trace_id ? `· ${item.trace_id}` : ""}
                          </div>
                        </div>
                        <div className="mt-2 text-sm" style={{ color: "var(--text-secondary)" }}>
                          {item.diff_summary || "diff 없음"}
                        </div>
                      </div>
                    ))
                  )}
                </div>
              </section>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
