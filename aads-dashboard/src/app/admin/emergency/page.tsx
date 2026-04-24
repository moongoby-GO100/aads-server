"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

import Header from "@/components/Header";
import { api, type GovernanceFeatureFlag } from "@/lib/api";

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

function flagKey(flag: GovernanceFeatureFlag): string {
  return String(flag.flag_key || flag.key || flag.name || "").trim();
}

function findGovernanceFlag(flags: GovernanceFeatureFlag[]): GovernanceFeatureFlag | null {
  return flags.find((item) => flagKey(item) === "governance_enabled") || null;
}

export default function AdminEmergencyPage() {
  const [governanceFlag, setGovernanceFlag] = useState<GovernanceFeatureFlag | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [updating, setUpdating] = useState(false);
  const [error, setError] = useState("");
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [lastRefreshedAt, setLastRefreshedAt] = useState<Date | null>(null);

  const loadFlag = useCallback(async (silent = false) => {
    if (silent) {
      setRefreshing(true);
    } else {
      setLoading(true);
    }

    try {
      const response = await api.getGovernanceFeatureFlags();
      const current = findGovernanceFlag(response?.flags || []);
      setGovernanceFlag(current);
      setError("");
      setLastRefreshedAt(new Date());
    } catch (err) {
      console.error("emergency flag load failed", err);
      setError(err instanceof Error ? err.message : "governance_enabled 상태를 불러오지 못했습니다.");
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    loadFlag();
  }, [loadFlag]);

  const setGovernanceEnabled = useCallback(async (enabled: boolean) => {
    setUpdating(true);
    try {
      const updated = await api.updateGovernanceFeatureFlag("governance_enabled", enabled);
      setGovernanceFlag({
        ...(governanceFlag || {}),
        ...updated,
        flag_key: "governance_enabled",
        enabled: Boolean(updated.enabled),
      });
      setError("");
    } catch (err) {
      console.error("governance flag update failed", err);
      setError(err instanceof Error ? err.message : "kill-switch 상태를 변경하지 못했습니다.");
    } finally {
      setUpdating(false);
      setConfirmOpen(false);
    }
  }, [governanceFlag]);

  const isEnabled = Boolean(governanceFlag?.enabled);

  const statusTone = useMemo(() => {
    if (isEnabled) {
      return {
        label: "ENABLED",
        background: "rgba(34,197,94,0.14)",
        color: "#4ade80",
        border: "1px solid rgba(34,197,94,0.24)",
      };
    }
    return {
      label: "DISABLED",
      background: "rgba(239,68,68,0.14)",
      color: "#f87171",
      border: "1px solid rgba(239,68,68,0.24)",
    };
  }, [isEnabled]);

  const cardStyle = {
    background: "var(--bg-card)",
    border: "1px solid var(--border)",
    borderRadius: "10px",
    padding: "16px",
  };

  return (
    <div className="flex flex-col h-full" style={{ background: "var(--bg-primary)" }}>
      <Header title="Emergency" />
      <div className="flex-1 p-3 md:p-6 overflow-auto">
        <div className="grid gap-4 max-w-4xl">
          <div className="flex items-start justify-between gap-3 flex-wrap">
            <div>
              <div style={{ color: "var(--text-primary)", fontSize: "22px", fontWeight: 700 }}>
                Governance Emergency Kill-Switch
              </div>
              <div style={{ color: "var(--text-secondary)", fontSize: "13px", marginTop: "4px" }}>
                `governance_enabled` 플래그를 즉시 제어합니다.
              </div>
            </div>

            <button
              type="button"
              onClick={() => loadFlag(true)}
              disabled={refreshing}
              style={{
                padding: "8px 14px",
                borderRadius: "8px",
                border: "none",
                background: "var(--accent)",
                color: "#fff",
                cursor: refreshing ? "wait" : "pointer",
                fontWeight: 600,
                opacity: refreshing ? 0.75 : 1,
              }}
            >
              {refreshing ? "새로고침 중..." : "새로고침"}
            </button>
          </div>

          {error ? <div style={{ ...cardStyle, color: "var(--danger)" }}>{error}</div> : null}

          <section style={cardStyle}>
            <div className="flex items-center justify-between gap-3 flex-wrap">
              <div>
                <div style={{ color: "var(--text-secondary)", fontSize: "12px" }}>Current Status</div>
                <div style={{ marginTop: "8px" }}>
                  <span
                    style={{
                      padding: "6px 12px",
                      borderRadius: "999px",
                      background: statusTone.background,
                      color: statusTone.color,
                      border: statusTone.border,
                      fontSize: "12px",
                      fontWeight: 700,
                    }}
                  >
                    {loading ? "LOADING" : statusTone.label}
                  </span>
                </div>
                <div style={{ color: "var(--text-secondary)", fontSize: "12px", marginTop: "10px" }}>
                  changed by {governanceFlag?.changed_by || "-"} · {formatDateTime(governanceFlag?.changed_at || governanceFlag?.updated_at || null)}
                </div>
                <div style={{ color: "var(--text-secondary)", fontSize: "12px", marginTop: "4px" }}>
                  last refreshed {lastRefreshedAt ? formatDateTime(lastRefreshedAt.toISOString()) : "-"}
                </div>
              </div>

              <div className="flex items-center gap-2">
                {isEnabled ? (
                  <button
                    type="button"
                    onClick={() => setConfirmOpen(true)}
                    disabled={loading || updating || !governanceFlag}
                    style={{
                      padding: "10px 14px",
                      borderRadius: "8px",
                      border: "1px solid rgba(239,68,68,0.32)",
                      background: "rgba(239,68,68,0.18)",
                      color: "#fca5a5",
                      fontWeight: 700,
                      cursor: loading || updating || !governanceFlag ? "not-allowed" : "pointer",
                      opacity: loading || updating || !governanceFlag ? 0.6 : 1,
                    }}
                  >
                    Governance 비활성화
                  </button>
                ) : (
                  <button
                    type="button"
                    onClick={() => setGovernanceEnabled(true)}
                    disabled={loading || updating}
                    style={{
                      padding: "10px 14px",
                      borderRadius: "8px",
                      border: "1px solid rgba(34,197,94,0.32)",
                      background: "rgba(34,197,94,0.18)",
                      color: "#86efac",
                      fontWeight: 700,
                      cursor: loading || updating ? "not-allowed" : "pointer",
                      opacity: loading || updating ? 0.6 : 1,
                    }}
                  >
                    Governance 활성화
                  </button>
                )}
              </div>
            </div>
          </section>

          <section style={{ ...cardStyle, border: "1px solid rgba(239,68,68,0.32)", background: "rgba(127,29,29,0.18)" }}>
            <h2 style={{ color: "#fca5a5", fontSize: "16px", fontWeight: 700, marginBottom: "8px" }}>주의</h2>
            <p style={{ color: "#fecaca", fontSize: "13px", lineHeight: "1.6", margin: 0 }}>
              비활성화 시 intent policy 기반 라우팅/제약이 우회되어 시스템 동작이 기본 폴백 정책으로 전환됩니다.
            </p>
          </section>
        </div>
      </div>

      {confirmOpen ? (
        <div
          style={{
            position: "fixed",
            inset: 0,
            background: "rgba(0,0,0,0.6)",
            zIndex: 1000,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            padding: "16px",
          }}
          onClick={() => setConfirmOpen(false)}
        >
          <div
            style={{
              width: "min(460px, 100%)",
              background: "var(--bg-card)",
              border: "1px solid var(--border)",
              borderRadius: "12px",
              padding: "20px",
            }}
            onClick={(event) => event.stopPropagation()}
          >
            <div style={{ color: "var(--text-primary)", fontSize: "18px", fontWeight: 700 }}>
              Governance 비활성화 확인
            </div>
            <p style={{ color: "var(--text-secondary)", fontSize: "13px", marginTop: "10px", lineHeight: "1.6" }}>
              `governance_enabled`를 `false`로 변경합니다. 계속 진행하시겠습니까?
            </p>

            <div className="flex items-center justify-end gap-2" style={{ marginTop: "18px" }}>
              <button
                type="button"
                onClick={() => setConfirmOpen(false)}
                disabled={updating}
                style={{
                  padding: "8px 12px",
                  borderRadius: "8px",
                  border: "1px solid var(--border)",
                  background: "transparent",
                  color: "var(--text-primary)",
                  cursor: updating ? "not-allowed" : "pointer",
                }}
              >
                취소
              </button>
              <button
                type="button"
                onClick={() => setGovernanceEnabled(false)}
                disabled={updating}
                style={{
                  padding: "8px 12px",
                  borderRadius: "8px",
                  border: "none",
                  background: "#ef4444",
                  color: "#fff",
                  fontWeight: 700,
                  cursor: updating ? "wait" : "pointer",
                  opacity: updating ? 0.7 : 1,
                }}
              >
                {updating ? "처리 중..." : "비활성화"}
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}
