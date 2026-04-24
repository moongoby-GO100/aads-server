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

export default function AdminEmergencyPage() {
  const [flags, setFlags] = useState<FeatureFlag[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [lastRefreshedAt, setLastRefreshedAt] = useState<Date | null>(null);

  const loadData = useCallback(async (silent = false) => {
    if (silent) {
      setRefreshing(true);
    } else {
      setLoading(true);
    }
    setError("");
    try {
      const response = await api.getGovernanceFeatureFlags();
      setFlags(((response as FeatureFlagResponse)?.flags || []) as FeatureFlag[]);
      setLastRefreshedAt(new Date());
    } catch (err) {
      console.error("emergency page load failed", err);
      setError(err instanceof Error ? err.message : "Emergency control 데이터를 불러오지 못했습니다.");
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    loadData();
  }, [loadData]);

  const governanceFlag = useMemo(
    () => flags.find((item) => item.flag_key === "governance_enabled"),
    [flags],
  );

  const governanceEnabled = governanceFlag?.enabled !== false;

  const updateGovernanceFlag = useCallback(
    async (nextEnabled: boolean) => {
      setBusy(true);
      setError("");
      try {
        await api.updateGovernanceFeatureFlag("governance_enabled", nextEnabled);
        setConfirmOpen(false);
        await loadData(true);
      } catch (err) {
        console.error("governance kill switch update failed", err);
        setError(err instanceof Error ? err.message : "Kill-switch 변경에 실패했습니다.");
      } finally {
        setBusy(false);
      }
    },
    [loadData],
  );

  const cardStyle = {
    background: "var(--bg-card)",
    border: "1px solid var(--border)",
    borderRadius: "12px",
    padding: "16px",
  };

  return (
    <div className="flex flex-col h-full" style={{ background: "var(--bg-primary)" }}>
      <Header title="Emergency" />
      <div className="flex-1 p-3 md:p-6 overflow-auto">
        <div className="grid gap-4">
          <section
            className="rounded-2xl p-4 md:p-5"
            style={{
              background: governanceEnabled
                ? "linear-gradient(135deg, rgba(15,118,110,0.24), rgba(15,23,42,0.96))"
                : "linear-gradient(135deg, rgba(185,28,28,0.26), rgba(15,23,42,0.96))",
              border: governanceEnabled
                ? "1px solid rgba(45,212,191,0.22)"
                : "1px solid rgba(248,113,113,0.24)",
              boxShadow: governanceEnabled
                ? "0 18px 48px rgba(13,148,136,0.18)"
                : "0 18px 48px rgba(220,38,38,0.18)",
            }}
          >
            <div className="flex flex-col lg:flex-row lg:items-end lg:justify-between gap-4">
              <div>
                <div className="text-xs uppercase tracking-[0.24em]" style={{ color: "rgba(226,232,240,0.72)" }}>
                  Emergency Kill Switch
                </div>
                <h1 className="mt-2 text-2xl md:text-3xl font-bold" style={{ color: "var(--text-primary)" }}>
                  {governanceEnabled ? "거버넌스 경로 활성" : "거버넌스 경로 비활성"}
                </h1>
                <p className="mt-2 text-sm max-w-3xl" style={{ color: "rgba(226,232,240,0.72)" }}>
                  `governance_enabled` 플래그 하나로 DB 거버넌스 경로와 레거시 폴백 경로를 즉시 전환합니다.
                </p>
              </div>

              <div className="flex items-center gap-2 flex-wrap">
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
                  {refreshing ? "새로고침 중..." : "새로고침"}
                </button>
                <button
                  type="button"
                  onClick={() => {
                    if (governanceEnabled) {
                      setConfirmOpen(true);
                      return;
                    }
                    updateGovernanceFlag(true);
                  }}
                  disabled={busy}
                  className="px-4 py-2 rounded-lg text-sm font-semibold"
                  style={{
                    background: governanceEnabled ? "rgba(127,29,29,0.9)" : "rgba(15,118,110,0.9)",
                    color: "#fff",
                    border: "1px solid rgba(255,255,255,0.12)",
                    opacity: busy ? 0.7 : 1,
                  }}
                >
                  {busy
                    ? "변경 중..."
                    : governanceEnabled
                      ? "거버넌스 비활성화"
                      : "거버넌스 다시 활성화"}
                </button>
              </div>
            </div>

            <div className="mt-4 grid grid-cols-1 sm:grid-cols-3 gap-3">
              <div
                className="rounded-xl p-3"
                style={{ background: "rgba(15,23,42,0.42)", border: "1px solid rgba(148,163,184,0.18)" }}
              >
                <div className="text-[11px] uppercase tracking-[0.16em]" style={{ color: "var(--text-secondary)" }}>
                  Current State
                </div>
                <div
                  className="mt-1 text-2xl font-bold"
                  style={{ color: governanceEnabled ? "#4ade80" : "#f87171" }}
                >
                  {governanceEnabled ? "ON" : "OFF"}
                </div>
              </div>
              <div
                className="rounded-xl p-3"
                style={{ background: "rgba(15,23,42,0.42)", border: "1px solid rgba(148,163,184,0.18)" }}
              >
                <div className="text-[11px] uppercase tracking-[0.16em]" style={{ color: "var(--text-secondary)" }}>
                  Last Changed
                </div>
                <div className="mt-1 text-sm font-semibold" style={{ color: "var(--text-primary)" }}>
                  {formatDateTime(governanceFlag?.last_changed_at)}
                </div>
              </div>
              <div
                className="rounded-xl p-3"
                style={{ background: "rgba(15,23,42,0.42)", border: "1px solid rgba(148,163,184,0.18)" }}
              >
                <div className="text-[11px] uppercase tracking-[0.16em]" style={{ color: "var(--text-secondary)" }}>
                  Changed By
                </div>
                <div className="mt-1 text-sm font-semibold" style={{ color: "var(--text-primary)" }}>
                  {governanceFlag?.last_changed_by || "-"}
                </div>
              </div>
            </div>
          </section>

          {error ? (
            <div style={{ ...cardStyle, color: "var(--danger)" }}>{error}</div>
          ) : null}

          {loading ? (
            <div style={{ ...cardStyle, color: "var(--text-secondary)", textAlign: "center" }}>
              Emergency control 데이터를 불러오는 중...
            </div>
          ) : (
            <div className="grid grid-cols-1 xl:grid-cols-[minmax(0,0.8fr)_minmax(0,1.2fr)] gap-4">
              <section style={cardStyle}>
                <div style={{ color: "var(--text-primary)", fontSize: "16px", fontWeight: 700, marginBottom: "12px" }}>
                  현재 상태
                </div>
                <div
                  className="rounded-xl p-4"
                  style={{
                    background: governanceEnabled ? "rgba(34,197,94,0.08)" : "rgba(239,68,68,0.08)",
                    border: governanceEnabled
                      ? "1px solid rgba(34,197,94,0.2)"
                      : "1px solid rgba(239,68,68,0.2)",
                  }}
                >
                  <div style={{ color: "var(--text-primary)", fontWeight: 700 }}>
                    {governanceEnabled ? "정책 기반 라우팅 활성" : "레거시 폴백 경로 사용 중"}
                  </div>
                  <div style={{ color: "var(--text-secondary)", fontSize: "13px", marginTop: "8px", lineHeight: 1.6 }}>
                    {governanceEnabled
                      ? "intent policy, feature flag, audit log 기반 거버넌스 경로가 기본값으로 동작합니다."
                      : "DB 거버넌스 경로를 우회하고 기존 코드 경로를 사용합니다. 비상 시에만 유지하는 상태입니다."}
                  </div>
                </div>

                <div style={{ marginTop: "14px", color: "var(--text-secondary)", fontSize: "12px", lineHeight: 1.7 }}>
                  최근 갱신 {lastRefreshedAt ? formatDateTime(lastRefreshedAt.toISOString()) : "-"}
                  {governanceFlag ? "" : " · DB row 없음, 기본 동작은 활성"}
                </div>
              </section>

              <section style={cardStyle}>
                <div style={{ color: "var(--text-primary)", fontSize: "16px", fontWeight: 700, marginBottom: "12px" }}>
                  전체 Feature Flags
                </div>
                <div className="grid gap-3">
                  {flags.length === 0 ? (
                    <div style={{ color: "var(--text-secondary)" }}>등록된 flag가 없습니다.</div>
                  ) : (
                    flags.map((flag) => (
                      <div
                        key={flag.flag_key}
                        className="rounded-xl p-3"
                        style={{ background: "rgba(15,23,42,0.28)", border: "1px solid var(--border)" }}
                      >
                        <div className="flex items-start justify-between gap-3 flex-wrap">
                          <div>
                            <div style={{ color: "var(--text-primary)", fontWeight: 700 }}>{flag.flag_key}</div>
                            <div style={{ color: "var(--text-secondary)", fontSize: "12px", marginTop: "4px" }}>
                              scope {flag.scope || "global"} · {formatDateTime(flag.last_changed_at)}
                            </div>
                            {flag.notes ? (
                              <div style={{ color: "var(--text-secondary)", fontSize: "12px", marginTop: "6px" }}>
                                {flag.notes}
                              </div>
                            ) : null}
                          </div>
                          <span
                            className="px-2.5 py-1 rounded-full text-xs font-semibold"
                            style={{
                              background: flag.enabled ? "rgba(34,197,94,0.14)" : "rgba(239,68,68,0.14)",
                              color: flag.enabled ? "#4ade80" : "#f87171",
                              border: flag.enabled
                                ? "1px solid rgba(34,197,94,0.24)"
                                : "1px solid rgba(239,68,68,0.24)",
                            }}
                          >
                            {flag.enabled ? "enabled" : "disabled"}
                          </span>
                        </div>
                      </div>
                    ))
                  )}
                </div>
              </section>
            </div>
          )}
        </div>
      </div>

      {confirmOpen ? (
        <div
          style={{
            position: "fixed",
            inset: 0,
            background: "rgba(0,0,0,0.68)",
            zIndex: 1000,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            padding: "16px",
          }}
          onClick={() => {
            if (!busy) setConfirmOpen(false);
          }}
        >
          <div
            style={{
              width: "min(520px, 100%)",
              background: "var(--bg-card)",
              border: "1px solid rgba(239,68,68,0.26)",
              borderRadius: "16px",
              padding: "20px",
              boxShadow: "0 24px 64px rgba(0,0,0,0.38)",
            }}
            onClick={(event) => event.stopPropagation()}
          >
            <div style={{ color: "#f87171", fontSize: "12px", fontWeight: 700, letterSpacing: "0.08em", textTransform: "uppercase" }}>
              Confirm Disable
            </div>
            <h2 style={{ color: "var(--text-primary)", fontSize: "22px", fontWeight: 700, marginTop: "8px" }}>
              거버넌스 경로를 비활성화할까요?
            </h2>
            <p style={{ color: "var(--text-secondary)", fontSize: "14px", lineHeight: 1.7, marginTop: "10px" }}>
              이 작업은 `governance_enabled=false`를 즉시 기록합니다. 이후 요청은 DB 정책 경로 대신 레거시 폴백 경로를 사용합니다.
            </p>

            <div
              className="rounded-xl p-3"
              style={{
                marginTop: "16px",
                background: "rgba(127,29,29,0.12)",
                border: "1px solid rgba(248,113,113,0.18)",
                color: "#fecaca",
                fontSize: "13px",
                lineHeight: 1.6,
              }}
            >
              장애 대응 목적이 아니면 비활성화를 유지하지 않는 편이 안전합니다.
            </div>

            <div className="flex items-center justify-end gap-2" style={{ marginTop: "18px" }}>
              <button
                type="button"
                onClick={() => setConfirmOpen(false)}
                disabled={busy}
                className="px-4 py-2 rounded-lg text-sm font-semibold"
                style={{
                  background: "var(--bg-hover)",
                  color: "var(--text-primary)",
                  border: "1px solid var(--border)",
                  opacity: busy ? 0.7 : 1,
                }}
              >
                취소
              </button>
              <button
                type="button"
                onClick={() => updateGovernanceFlag(false)}
                disabled={busy}
                className="px-4 py-2 rounded-lg text-sm font-semibold"
                style={{
                  background: "rgba(127,29,29,0.92)",
                  color: "#fff",
                  border: "1px solid rgba(255,255,255,0.12)",
                  opacity: busy ? 0.7 : 1,
                }}
              >
                {busy ? "변경 중..." : "비활성화 확인"}
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}
