"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

import Header from "@/components/Header";
import { api } from "@/lib/api";

interface AgentSummary {
  role: string;
  display_name: string;
  base_model: string;
  allowed_intents: string[];
  max_tokens: number | null;
  created_at: string | null;
  recent_tasks_count: number;
  last_active_at: string | null;
}

interface AgentListResponse {
  agents: AgentSummary[];
  total: number;
}

interface AgentStatsItem {
  role: string;
  display_name: string;
  total_tasks: number;
  completed_tasks: number;
  error_tasks: number;
  completed_ratio: number;
  error_ratio: number;
  last_active_at: string | null;
}

interface AgentStatsResponse {
  agents: AgentStatsItem[];
  total: number;
}

interface AgentTask {
  job_id: string;
  project: string;
  status: string;
  phase: string;
  instruction: string;
  model: string;
  worker_model: string;
  actual_model: string;
  error_detail: string;
  started_at: string | null;
  created_at: string | null;
  updated_at: string | null;
}

interface AgentDetail {
  role: string;
  display_name: string;
  base_model: string;
  allowed_intents: string[];
  max_tokens: number | null;
  created_at: string | null;
  recent_tasks_count: number;
  last_active_at: string | null;
  total_tasks: number;
  completed_tasks: number;
  error_tasks: number;
  completed_ratio: number;
  error_ratio: number;
}

interface AgentDetailResponse {
  agent: AgentDetail;
  recent_tasks: AgentTask[];
}

type AgentHealth = "healthy" | "active" | "warning" | "idle";

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

function formatElapsed(value?: string | null): string {
  if (!value) return "-";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return "-";

  const diff = Math.max(0, Date.now() - parsed.getTime());
  const seconds = Math.floor(diff / 1000);
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return `${hours}h`;
  const days = Math.floor(hours / 24);
  return `${days}d`;
}

function ratioToPercent(value?: number): string {
  return `${Math.round((value || 0) * 100)}%`;
}

function inferHealth(agent: AgentSummary, stat?: AgentStatsItem): AgentHealth {
  if ((stat?.total_tasks || 0) > 0 && (stat?.error_ratio || 0) >= 0.3) return "warning";
  if (agent.recent_tasks_count > 0) return "active";
  if ((stat?.total_tasks || 0) > 0) return "healthy";
  return "idle";
}

function healthTone(status: AgentHealth): { label: string; style: { background: string; color: string; border: string } } {
  if (status === "healthy") {
    return {
      label: "Healthy",
      style: {
        background: "rgba(34,197,94,0.14)",
        color: "#4ade80",
        border: "1px solid rgba(34,197,94,0.24)",
      },
    };
  }
  if (status === "active") {
    return {
      label: "Active",
      style: {
        background: "rgba(59,130,246,0.14)",
        color: "#60a5fa",
        border: "1px solid rgba(59,130,246,0.24)",
      },
    };
  }
  if (status === "warning") {
    return {
      label: "At Risk",
      style: {
        background: "rgba(239,68,68,0.14)",
        color: "#f87171",
        border: "1px solid rgba(239,68,68,0.24)",
      },
    };
  }
  return {
    label: "Idle",
    style: {
      background: "rgba(148,163,184,0.14)",
      color: "#94a3b8",
      border: "1px solid rgba(148,163,184,0.24)",
    },
  };
}

function taskStatusTone(status: string): { background: string; color: string; border: string } {
  if (status === "done") {
    return {
      background: "rgba(34,197,94,0.12)",
      color: "#4ade80",
      border: "1px solid rgba(34,197,94,0.24)",
    };
  }
  if (status === "running") {
    return {
      background: "rgba(59,130,246,0.12)",
      color: "#60a5fa",
      border: "1px solid rgba(59,130,246,0.24)",
    };
  }
  if (status === "error") {
    return {
      background: "rgba(239,68,68,0.12)",
      color: "#f87171",
      border: "1px solid rgba(239,68,68,0.24)",
    };
  }
  return {
    background: "rgba(148,163,184,0.12)",
    color: "var(--text-secondary)",
    border: "1px solid rgba(148,163,184,0.24)",
  };
}

export default function AdminAgentsPage() {
  const [agents, setAgents] = useState<AgentSummary[]>([]);
  const [stats, setStats] = useState<AgentStatsItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState("");
  const [lastRefreshedAt, setLastRefreshedAt] = useState<Date | null>(null);

  const [selectedRole, setSelectedRole] = useState("");
  const [detail, setDetail] = useState<AgentDetailResponse | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState("");

  const statsByRole = useMemo(() => {
    const mapped: Record<string, AgentStatsItem> = {};
    for (const item of stats) {
      mapped[(item.role || "").toLowerCase()] = item;
    }
    return mapped;
  }, [stats]);

  const loadBoard = useCallback(async (silent = false) => {
    if (silent) {
      setRefreshing(true);
    } else {
      setLoading(true);
    }
    try {
      const [listRes, statsRes] = await Promise.all([
        api.getAdminAgents(),
        api.getAdminAgentStats(),
      ]);
      setAgents(((listRes as AgentListResponse)?.agents || []) as AgentSummary[]);
      setStats(((statsRes as AgentStatsResponse)?.agents || []) as AgentStatsItem[]);
      setError("");
      setLastRefreshedAt(new Date());
    } catch (err) {
      console.error("agent registry load failed", err);
      setError(err instanceof Error ? err.message : "Agent Registry 데이터를 불러오지 못했습니다.");
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  const loadDetail = useCallback(async (role: string, silent = false) => {
    if (!role) return;
    if (!silent) {
      setDetailLoading(true);
      setDetailError("");
    }
    try {
      const response = await api.getAdminAgent(role);
      setDetail((response as AgentDetailResponse) || null);
      setDetailError("");
    } catch (err) {
      console.error("agent detail load failed", err);
      setDetailError(err instanceof Error ? err.message : "에이전트 상세를 불러오지 못했습니다.");
    } finally {
      setDetailLoading(false);
    }
  }, []);

  useEffect(() => {
    loadBoard();
  }, [loadBoard]);

  useEffect(() => {
    const timer = window.setInterval(() => {
      loadBoard(true);
      if (selectedRole) {
        loadDetail(selectedRole, true);
      }
    }, 30000);
    return () => window.clearInterval(timer);
  }, [loadBoard, loadDetail, selectedRole]);

  const dashboardRows = useMemo(() => {
    return agents.map((agent) => {
      const stat = statsByRole[(agent.role || "").toLowerCase()];
      const health = inferHealth(agent, stat);
      return { agent, stat, health };
    });
  }, [agents, statsByRole]);

  const totalRecentTasks = useMemo(
    () => agents.reduce((acc, item) => acc + (item.recent_tasks_count || 0), 0),
    [agents],
  );

  const activeAgents = useMemo(
    () => dashboardRows.filter((item) => item.health === "active" || item.health === "healthy").length,
    [dashboardRows],
  );

  const avgErrorRatio = useMemo(() => {
    if (!stats.length) return 0;
    const sum = stats.reduce((acc, item) => acc + (item.error_ratio || 0), 0);
    return sum / stats.length;
  }, [stats]);

  const cardStyle = {
    background: "var(--bg-card)",
    border: "1px solid var(--border)",
    borderRadius: "10px",
    padding: "16px",
  };

  const modalAgent = detail?.agent;

  return (
    <div className="flex flex-col h-full" style={{ background: "var(--bg-primary)" }}>
      <Header title="Agent Registry" />
      <div className="flex-1 p-3 md:p-6 overflow-auto">
        <div className="grid gap-4">
          <div className="flex items-start justify-between gap-3 flex-wrap">
            <div>
              <div style={{ color: "var(--text-primary)", fontSize: "22px", fontWeight: 700 }}>AI Agent Registry</div>
              <div style={{ color: "var(--text-secondary)", fontSize: "13px", marginTop: "4px" }}>
                Meta-Agent/Worker Agent 설정과 최근 실행 흐름을 한 화면에서 확인합니다.
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
                {refreshing ? "새로고침 중..." : `최근 갱신 ${lastRefreshedAt ? formatDateTime(lastRefreshedAt.toISOString()) : "-"}`}
              </div>
              <button
                onClick={() => loadBoard(true)}
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
            {[
              ["Total Agents", agents.length, "var(--text-primary)"],
              ["Active", activeAgents, "#60a5fa"],
              ["Recent Tasks", totalRecentTasks, "#4ade80"],
              ["Avg Error", ratioToPercent(avgErrorRatio), "#f87171"],
            ].map(([label, value, color]) => (
              <div key={String(label)} style={{ ...cardStyle, padding: "14px" }}>
                <div style={{ color: "var(--text-secondary)", fontSize: "12px", marginBottom: "4px" }}>{label}</div>
                <div style={{ color: String(color), fontWeight: 700, fontSize: "28px" }}>{value}</div>
              </div>
            ))}
          </div>

          {error ? (
            <div style={{ ...cardStyle, color: "var(--danger)" }}>{error}</div>
          ) : null}

          {loading ? (
            <div style={{ ...cardStyle, color: "var(--text-secondary)", textAlign: "center" }}>로딩 중...</div>
          ) : dashboardRows.length === 0 ? (
            <div style={{ ...cardStyle, color: "var(--text-secondary)", textAlign: "center" }}>등록된 에이전트가 없습니다.</div>
          ) : (
            <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
              {dashboardRows.map(({ agent, stat, health }) => {
                const tone = healthTone(health);
                return (
                  <button
                    key={agent.role}
                    type="button"
                    onClick={() => {
                      setSelectedRole(agent.role);
                      setDetail(null);
                      setDetailLoading(true);
                      setDetailError("");
                      loadDetail(agent.role);
                    }}
                    style={{
                      ...cardStyle,
                      textAlign: "left",
                      cursor: "pointer",
                      background: "linear-gradient(180deg, rgba(15,23,42,0.4) 0%, rgba(15,23,42,0.18) 100%)",
                    }}
                  >
                    <div className="flex items-start justify-between gap-3">
                      <div>
                        <div style={{ color: "var(--accent)", fontSize: "12px", fontWeight: 700 }}>{agent.role}</div>
                        <div style={{ color: "var(--text-primary)", fontSize: "18px", fontWeight: 700, marginTop: "2px" }}>
                          {agent.display_name || agent.role}
                        </div>
                      </div>
                      <span
                        style={{
                          padding: "4px 10px",
                          borderRadius: "999px",
                          ...tone.style,
                          fontSize: "11px",
                          fontWeight: 700,
                        }}
                      >
                        {tone.label}
                      </span>
                    </div>

                    <div className="grid grid-cols-2 gap-2" style={{ marginTop: "12px" }}>
                      <div style={{ padding: "10px", borderRadius: "8px", background: "rgba(15,23,42,0.28)", border: "1px solid var(--border)" }}>
                        <div style={{ color: "var(--text-secondary)", fontSize: "11px" }}>Model</div>
                        <div style={{ color: "var(--text-primary)", fontSize: "12px", marginTop: "4px", wordBreak: "break-word" }}>
                          {agent.base_model || "-"}
                        </div>
                      </div>
                      <div style={{ padding: "10px", borderRadius: "8px", background: "rgba(15,23,42,0.28)", border: "1px solid var(--border)" }}>
                        <div style={{ color: "var(--text-secondary)", fontSize: "11px" }}>Recent Tasks</div>
                        <div style={{ color: "#93c5fd", fontSize: "18px", fontWeight: 700, marginTop: "2px" }}>
                          {(agent.recent_tasks_count || 0).toLocaleString("ko-KR")}
                        </div>
                      </div>
                    </div>

                    <div className="flex items-center justify-between gap-2" style={{ marginTop: "10px" }}>
                      <div style={{ color: "var(--text-secondary)", fontSize: "12px" }}>
                        Error {ratioToPercent(stat?.error_ratio)}
                      </div>
                      <div style={{ color: "var(--text-secondary)", fontSize: "12px" }}>
                        {formatElapsed(agent.last_active_at)}
                      </div>
                    </div>
                  </button>
                );
              })}
            </div>
          )}
        </div>
      </div>

      {selectedRole ? (
        <div
          style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,0.65)", zIndex: 1000, display: "flex", alignItems: "center", justifyContent: "center", padding: "16px" }}
          onClick={() => {
            setSelectedRole("");
            setDetail(null);
            setDetailError("");
          }}
        >
          <div
            style={{
              width: "min(1080px, 100%)",
              maxHeight: "88vh",
              overflowY: "auto",
              background: "var(--bg-card)",
              border: "1px solid var(--border)",
              borderRadius: "14px",
              padding: "20px",
            }}
            onClick={(event) => event.stopPropagation()}
          >
            <div className="flex items-start justify-between gap-3 flex-wrap" style={{ marginBottom: "18px" }}>
              <div>
                <div style={{ color: "var(--accent)", fontSize: "12px", fontWeight: 700 }}>{modalAgent?.role || selectedRole}</div>
                <div style={{ color: "var(--text-primary)", fontSize: "22px", fontWeight: 700, marginTop: "4px" }}>
                  {modalAgent?.display_name || "Agent Detail"}
                </div>
                <div className="flex items-center gap-2 flex-wrap" style={{ marginTop: "10px" }}>
                  <span
                    style={{
                      padding: "4px 10px",
                      borderRadius: "999px",
                      background: "var(--bg-hover)",
                      color: "var(--text-primary)",
                      fontSize: "12px",
                    }}
                  >
                    Model: {modalAgent?.base_model || "-"}
                  </span>
                  <span style={{ color: "var(--text-secondary)", fontSize: "12px" }}>
                    Last Active {formatDateTime(modalAgent?.last_active_at)}
                  </span>
                </div>
              </div>
              <button
                type="button"
                onClick={() => {
                  setSelectedRole("");
                  setDetail(null);
                  setDetailError("");
                }}
                style={{
                  border: "none",
                  background: "transparent",
                  color: "var(--text-secondary)",
                  fontSize: "24px",
                  cursor: "pointer",
                  lineHeight: 1,
                }}
              >
                ✕
              </button>
            </div>

            {detailLoading && !detail ? (
              <div style={{ ...cardStyle, color: "var(--text-secondary)", textAlign: "center" }}>상세를 불러오는 중...</div>
            ) : null}

            {detailError ? (
              <div style={{ ...cardStyle, color: "var(--danger)", marginBottom: "16px" }}>{detailError}</div>
            ) : null}

            {detail ? (
              <div className="grid gap-4">
                <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                  {[
                    ["Max Tokens", modalAgent?.max_tokens != null ? modalAgent.max_tokens.toLocaleString("ko-KR") : "-"],
                    ["Total Tasks", (modalAgent?.total_tasks || 0).toLocaleString("ko-KR")],
                    ["Completed", ratioToPercent(modalAgent?.completed_ratio)],
                    ["Error", ratioToPercent(modalAgent?.error_ratio)],
                  ].map(([label, value]) => (
                    <div key={String(label)} style={{ ...cardStyle, padding: "14px" }}>
                      <div style={{ color: "var(--text-secondary)", fontSize: "12px", marginBottom: "4px" }}>{label}</div>
                      <div style={{ color: "var(--text-primary)", fontSize: "16px", fontWeight: 700 }}>{value}</div>
                    </div>
                  ))}
                </div>

                <div style={{ ...cardStyle }}>
                  <div className="flex items-center justify-between gap-2" style={{ marginBottom: "10px" }}>
                    <div style={{ color: "var(--text-primary)", fontWeight: 700 }}>Allowed Intents</div>
                    <div style={{ color: "var(--text-secondary)", fontSize: "12px" }}>
                      {(modalAgent?.allowed_intents || []).length}개
                    </div>
                  </div>
                  {(modalAgent?.allowed_intents || []).length > 0 ? (
                    <div className="flex flex-wrap gap-2">
                      {(modalAgent?.allowed_intents || []).map((intent) => (
                        <span
                          key={intent}
                          style={{
                            padding: "4px 10px",
                            borderRadius: "999px",
                            border: "1px solid var(--border)",
                            background: "var(--bg-hover)",
                            color: "var(--text-primary)",
                            fontSize: "12px",
                          }}
                        >
                          {intent}
                        </span>
                      ))}
                    </div>
                  ) : (
                    <div style={{ color: "var(--text-secondary)", fontSize: "13px" }}>설정된 allowed intents가 없습니다.</div>
                  )}
                </div>

                <div style={{ ...cardStyle }}>
                  <div className="flex items-center justify-between gap-2" style={{ marginBottom: "12px" }}>
                    <div style={{ color: "var(--text-primary)", fontWeight: 700 }}>최근 작업 10건</div>
                    <div style={{ color: "var(--text-secondary)", fontSize: "12px" }}>
                      created {formatDateTime(modalAgent?.created_at)}
                    </div>
                  </div>

                  {detail.recent_tasks.length === 0 ? (
                    <div style={{ color: "var(--text-secondary)", fontSize: "13px" }}>최근 작업 기록이 없습니다.</div>
                  ) : (
                    <div className="grid gap-2">
                      {detail.recent_tasks.map((task) => (
                        <div
                          key={task.job_id}
                          style={{
                            border: "1px solid var(--border)",
                            borderRadius: "10px",
                            padding: "12px",
                            background: "rgba(15,23,42,0.28)",
                          }}
                        >
                          <div className="flex items-center justify-between gap-2 flex-wrap">
                            <div style={{ color: "var(--accent)", fontSize: "12px", fontWeight: 700 }}>{task.job_id}</div>
                            <span
                              style={{
                                padding: "3px 8px",
                                borderRadius: "999px",
                                ...taskStatusTone(task.status),
                                fontSize: "11px",
                                fontWeight: 600,
                              }}
                            >
                              {task.status || "-"}
                            </span>
                          </div>
                          <div style={{ color: "var(--text-primary)", fontSize: "13px", lineHeight: "1.5", marginTop: "8px" }}>
                            {task.instruction || "(instruction 없음)"}
                          </div>
                          <div className="flex items-center justify-between gap-2 flex-wrap" style={{ marginTop: "10px" }}>
                            <div style={{ color: "var(--text-secondary)", fontSize: "12px" }}>
                              {task.actual_model || task.worker_model || task.model || "-"}
                            </div>
                            <div style={{ color: "var(--text-secondary)", fontSize: "12px" }}>
                              {formatDateTime(task.updated_at || task.created_at)}
                            </div>
                          </div>
                          {task.error_detail ? (
                            <div style={{ color: "#fca5a5", fontSize: "11px", marginTop: "8px", lineHeight: "1.5" }}>
                              {task.error_detail}
                            </div>
                          ) : null}
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              </div>
            ) : null}
          </div>
        </div>
      ) : null}
    </div>
  );
}
