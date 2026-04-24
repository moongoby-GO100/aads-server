"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

import Header from "@/components/Header";
import { api, type AdminDeployStatusResponse } from "@/lib/api";

type DeployStatus = "ok" | "error" | "unknown";

function formatDateTime(value?: string | null): string {
  if (!value) return "-";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString("ko-KR", {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function normalizeStatus(value: string): DeployStatus {
  if (value === "ok" || value === "error" || value === "unknown") {
    return value;
  }
  return "unknown";
}

function statusTone(status: DeployStatus): { label: string; background: string; color: string; border: string } {
  if (status === "ok") {
    return {
      label: "정상",
      background: "rgba(34,197,94,0.14)",
      color: "#4ade80",
      border: "1px solid rgba(34,197,94,0.24)",
    };
  }
  if (status === "error") {
    return {
      label: "이상",
      background: "rgba(239,68,68,0.14)",
      color: "#f87171",
      border: "1px solid rgba(239,68,68,0.24)",
    };
  }
  return {
    label: "미확인",
    background: "rgba(148,163,184,0.14)",
    color: "var(--text-secondary)",
    border: "1px solid rgba(148,163,184,0.24)",
  };
}

export default function AdminDeployPage() {
  const [data, setData] = useState<AdminDeployStatusResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState("");
  const [lastRefreshedAt, setLastRefreshedAt] = useState<Date | null>(null);

  const loadStatus = useCallback(async (silent = false) => {
    if (silent) {
      setRefreshing(true);
    } else {
      setLoading(true);
    }

    try {
      const response = await api.getAdminDeployStatus();
      setData(response);
      setError("");
      setLastRefreshedAt(new Date());
    } catch (err) {
      console.error("deploy status load failed", err);
      setError(err instanceof Error ? err.message : "배포 상태를 불러오지 못했습니다.");
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    loadStatus();
  }, [loadStatus]);

  useEffect(() => {
    const timer = window.setInterval(() => {
      loadStatus(true);
    }, 30000);
    return () => window.clearInterval(timer);
  }, [loadStatus]);

  const rows = useMemo(() => {
    if (!data?.servers) return [];
    return data.servers.flatMap((server) =>
      (server.projects || []).map((project) => ({
        serverId: server.id,
        serverName: server.name,
        serverIp: server.ip,
        projectName: project.name,
        status: normalizeStatus(project.status),
        lastCommit: project.last_commit || "-",
        lastDeployAt: project.last_deploy_at || null,
      }))
    );
  }, [data]);

  const stats = useMemo(() => {
    const total = rows.length;
    const ok = rows.filter((row) => row.status === "ok").length;
    const errorCount = rows.filter((row) => row.status === "error").length;
    const unknown = rows.filter((row) => row.status === "unknown").length;
    return { total, ok, errorCount, unknown };
  }, [rows]);

  const cardStyle = {
    background: "var(--bg-card)",
    border: "1px solid var(--border)",
    borderRadius: "10px",
    padding: "16px",
  };

  return (
    <div className="flex flex-col h-full" style={{ background: "var(--bg-primary)" }}>
      <Header title="Deploy Status" />
      <div className="flex-1 p-3 md:p-6 overflow-auto">
        <div className="grid gap-4">
          <div className="flex items-start justify-between gap-3 flex-wrap">
            <div>
              <div style={{ color: "var(--text-primary)", fontSize: "22px", fontWeight: 700 }}>
                배포 상태 대시보드
              </div>
              <div style={{ color: "var(--text-secondary)", fontSize: "13px", marginTop: "4px" }}>
                프로젝트별 최신 배포 결과를 확인합니다.
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
                onClick={() => loadStatus(true)}
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
              label: "Total",
              value: stats.total,
              color: "var(--text-primary)",
            }, {
              label: "OK",
              value: stats.ok,
              color: "var(--success)",
            }, {
              label: "Error",
              value: stats.errorCount,
              color: "var(--danger)",
            }, {
              label: "Unknown",
              value: stats.unknown,
              color: "var(--text-secondary)",
            }].map((item) => (
              <div key={item.label} style={{ ...cardStyle, padding: "14px" }}>
                <div style={{ color: "var(--text-secondary)", fontSize: "12px", marginBottom: "4px" }}>
                  {item.label}
                </div>
                <div style={{ color: item.color, fontSize: "26px", fontWeight: 700 }}>
                  {loading ? "..." : item.value}
                </div>
              </div>
            ))}
          </div>

          {error ? <div style={{ ...cardStyle, color: "var(--danger)" }}>{error}</div> : null}

          <section style={cardStyle}>
            <div style={{ overflowX: "auto" }}>
              <table style={{ width: "100%", borderCollapse: "collapse", minWidth: "900px" }}>
                <thead>
                  <tr style={{ borderBottom: "1px solid var(--border)", color: "var(--text-secondary)" }}>
                    <th style={thStyle}>Server</th>
                    <th style={thStyle}>IP</th>
                    <th style={thStyle}>Project</th>
                    <th style={thStyle}>Status</th>
                    <th style={thStyle}>Last Commit</th>
                    <th style={thStyle}>Last Deploy At</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.length === 0 ? (
                    <tr>
                      <td colSpan={6} style={{ ...tdStyle, textAlign: "center", color: "var(--text-secondary)" }}>
                        {loading ? "로딩 중..." : "표시할 배포 데이터가 없습니다."}
                      </td>
                    </tr>
                  ) : (
                    rows.map((row) => {
                      const tone = statusTone(row.status);
                      return (
                        <tr key={`${row.serverId}-${row.projectName}`} style={{ borderBottom: "1px solid var(--border)" }}>
                          <td style={tdStyle}>{row.serverName}</td>
                          <td style={tdStyle}>{row.serverIp}</td>
                          <td style={tdStyle}>{row.projectName}</td>
                          <td style={tdStyle}>
                            <span
                              style={{
                                padding: "4px 10px",
                                borderRadius: "999px",
                                background: tone.background,
                                color: tone.color,
                                border: tone.border,
                                fontSize: "11px",
                                fontWeight: 700,
                              }}
                            >
                              {tone.label}
                            </span>
                          </td>
                          <td style={tdStyle}>{row.lastCommit}</td>
                          <td style={tdStyle}>{formatDateTime(row.lastDeployAt)}</td>
                        </tr>
                      );
                    })
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
