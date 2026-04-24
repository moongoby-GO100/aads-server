"use client";

import type { ReactNode } from "react";
import { useCallback, useEffect, useMemo, useState } from "react";

import Header from "@/components/Header";
import { api } from "@/lib/api";

type TaskStatus = "queued" | "running" | "awaiting_approval" | "done" | "error";

interface TaskSummary {
  job_id: string;
  project: string;
  status: TaskStatus;
  phase: string;
  instruction: string;
  model: string;
  worker_model: string;
  created_at: string | null;
  updated_at: string | null;
  error_detail: string;
}

interface TaskLog {
  id: number;
  log_type: string;
  content: string;
  phase: string;
  metadata: Record<string, unknown>;
  created_at: string | null;
}

interface TaskDetail {
  job_id: string;
  project: string;
  status: TaskStatus;
  raw_status: string;
  phase: string;
  cycle: number;
  max_cycles: number;
  instruction: string;
  model: string;
  worker_model: string;
  actual_model: string;
  size: string;
  logs: TaskLog[];
  log_snapshot: unknown;
  result_output: string;
  git_diff: string;
  review_feedback: string;
  error_detail: string;
  started_at: string | null;
  created_at: string | null;
  updated_at: string | null;
}

interface TaskListResponse {
  tasks: TaskSummary[];
  total: number;
  page: number;
}

interface TaskStats {
  queued: number;
  running: number;
  awaiting_approval: number;
  done: number;
  error: number;
  total: number;
}

const PAGE_SIZE = 100;
const BOARD_COLUMNS: { key: TaskStatus; label: string; accent: string; empty: string }[] = [
  { key: "queued", label: "Queued", accent: "#64748b", empty: "대기 중인 작업이 없습니다." },
  { key: "running", label: "Running", accent: "#3b82f6", empty: "실행 중인 작업이 없습니다." },
  { key: "awaiting_approval", label: "Awaiting Approval", accent: "#f59e0b", empty: "승인 대기 작업이 없습니다." },
  { key: "done", label: "Done", accent: "#22c55e", empty: "완료된 작업이 없습니다." },
  { key: "error", label: "Error", accent: "#ef4444", empty: "에러 작업이 없습니다." },
];

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

function toneForStatus(status: TaskStatus): { background: string; color: string; border: string } {
  if (status === "running") {
    return {
      background: "rgba(59,130,246,0.12)",
      color: "#60a5fa",
      border: "1px solid rgba(59,130,246,0.26)",
    };
  }
  if (status === "awaiting_approval") {
    return {
      background: "rgba(245,158,11,0.12)",
      color: "#fbbf24",
      border: "1px solid rgba(245,158,11,0.26)",
    };
  }
  if (status === "done") {
    return {
      background: "rgba(34,197,94,0.12)",
      color: "#4ade80",
      border: "1px solid rgba(34,197,94,0.24)",
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
    background: "rgba(100,116,139,0.12)",
    color: "var(--text-secondary)",
    border: "1px solid rgba(148,163,184,0.18)",
  };
}

function modelLabel(task: Pick<TaskSummary, "worker_model" | "model">): string {
  return task.worker_model || task.model || "-";
}

function stringifyValue(value: unknown): string {
  if (value == null) return "";
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

export default function AdminTasksPage() {
  const [tasks, setTasks] = useState<TaskSummary[]>([]);
  const [stats, setStats] = useState<TaskStats | null>(null);
  const [page, setPage] = useState(1);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState("");
  const [selectedJobId, setSelectedJobId] = useState("");
  const [detail, setDetail] = useState<TaskDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState("");
  const [lastRefreshedAt, setLastRefreshedAt] = useState<Date | null>(null);

  const loadDetail = useCallback(async (jobId: string, silent = false) => {
    if (!jobId) return;
    if (!silent) {
      setDetailLoading(true);
      setDetailError("");
    }
    try {
      const response = await api.getAdminTask(jobId);
      setDetail(response as TaskDetail);
      setDetailError("");
    } catch (err) {
      console.error("task detail load failed", err);
      setDetailError(err instanceof Error ? err.message : "작업 상세를 불러오지 못했습니다.");
    } finally {
      setDetailLoading(false);
    }
  }, []);

  const loadBoard = useCallback(async (silent = false) => {
    if (silent) {
      setRefreshing(true);
    } else {
      setLoading(true);
    }
    try {
      const [tasksRes, statsRes] = await Promise.all([
        api.getAdminTasks({ page, page_size: PAGE_SIZE }),
        api.getAdminTaskStats(),
      ]);
      const listRes = tasksRes as TaskListResponse;
      setTasks(listRes.tasks || []);
      setTotal(listRes.total || 0);
      setStats(statsRes as TaskStats);
      setError("");
      setLastRefreshedAt(new Date());
    } catch (err) {
      console.error("task board load failed", err);
      setError(err instanceof Error ? err.message : "Task Board 데이터를 불러오지 못했습니다.");
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, [page]);

  useEffect(() => {
    loadBoard();
  }, [loadBoard]);

  useEffect(() => {
    const timer = window.setInterval(() => {
      loadBoard(true);
      if (selectedJobId) {
        loadDetail(selectedJobId, true);
      }
    }, 30000);
    return () => window.clearInterval(timer);
  }, [loadBoard, loadDetail, selectedJobId]);

  const groupedTasks = useMemo(() => {
    return BOARD_COLUMNS.reduce<Record<TaskStatus, TaskSummary[]>>((acc, column) => {
      acc[column.key] = tasks.filter((task) => task.status === column.key);
      return acc;
    }, {
      queued: [],
      running: [],
      awaiting_approval: [],
      done: [],
      error: [],
    });
  }, [tasks]);

  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const cardStyle = {
    background: "var(--bg-card)",
    border: "1px solid var(--border)",
    borderRadius: "10px",
    padding: "16px",
  };

  return (
    <div className="flex flex-col h-full" style={{ background: "var(--bg-primary)" }}>
      <Header title="Task Board" />
      <div className="flex-1 p-3 md:p-6 overflow-auto">
        <div className="grid gap-4">
          <div className="flex items-start justify-between gap-3 flex-wrap">
            <div>
              <div style={{ color: "var(--text-primary)", fontSize: "22px", fontWeight: 700 }}>Pipeline Runner Task Board</div>
              <div style={{ color: "var(--text-secondary)", fontSize: "13px", marginTop: "4px" }}>
                최근 작업을 상태별 칸반으로 확인하고, 카드 클릭 시 instruction, logs, diff를 바로 확인합니다.
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

          {stats ? (
            <div className="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-6 gap-3">
              {[
                ["Queued", stats.queued, "#94a3b8"],
                ["Running", stats.running, "#60a5fa"],
                ["Awaiting", stats.awaiting_approval, "#fbbf24"],
                ["Done", stats.done, "#4ade80"],
                ["Error", stats.error, "#f87171"],
                ["Total", stats.total, "var(--text-primary)"],
              ].map(([label, value, color]) => (
                <div key={String(label)} style={{ ...cardStyle, padding: "14px" }}>
                  <div style={{ color: "var(--text-secondary)", fontSize: "12px", marginBottom: "4px" }}>{label}</div>
                  <div style={{ color: String(color), fontWeight: 700, fontSize: "28px" }}>{value}</div>
                </div>
              ))}
            </div>
          ) : null}

          <div className="flex items-center justify-between gap-3 flex-wrap" style={{ ...cardStyle, padding: "12px 16px" }}>
            <div style={{ color: "var(--text-secondary)", fontSize: "13px" }}>
              총 {total.toLocaleString("ko-KR")}건 · 페이지 {page}/{totalPages}
            </div>
            <div className="flex items-center gap-2">
              <button
                onClick={() => setPage((prev) => Math.max(1, prev - 1))}
                disabled={page <= 1}
                style={{
                  padding: "8px 12px",
                  borderRadius: "8px",
                  border: "1px solid var(--border)",
                  background: page <= 1 ? "var(--bg-hover)" : "var(--bg-card)",
                  color: page <= 1 ? "var(--text-secondary)" : "var(--text-primary)",
                  cursor: page <= 1 ? "not-allowed" : "pointer",
                }}
              >
                이전
              </button>
              <button
                onClick={() => setPage((prev) => Math.min(totalPages, prev + 1))}
                disabled={page >= totalPages}
                style={{
                  padding: "8px 12px",
                  borderRadius: "8px",
                  border: "1px solid var(--border)",
                  background: page >= totalPages ? "var(--bg-hover)" : "var(--bg-card)",
                  color: page >= totalPages ? "var(--text-secondary)" : "var(--text-primary)",
                  cursor: page >= totalPages ? "not-allowed" : "pointer",
                }}
              >
                다음
              </button>
            </div>
          </div>

          {error ? (
            <div style={{ ...cardStyle, color: "var(--danger)" }}>{error}</div>
          ) : null}

          {loading ? (
            <div style={{ ...cardStyle, color: "var(--text-secondary)", textAlign: "center" }}>로딩 중...</div>
          ) : (
            <div style={{ overflowX: "auto", paddingBottom: "4px" }}>
              <div style={{ display: "grid", gridTemplateColumns: "repeat(5, minmax(250px, 1fr))", gap: "14px", minWidth: "1280px" }}>
                {BOARD_COLUMNS.map((column) => (
                  <div
                    key={column.key}
                    style={{
                      ...cardStyle,
                      padding: "0",
                      borderTop: `3px solid ${column.accent}`,
                      minHeight: "620px",
                    }}
                  >
                    <div
                      style={{
                        padding: "14px 14px 12px",
                        borderBottom: "1px solid var(--border)",
                        display: "flex",
                        justifyContent: "space-between",
                        alignItems: "center",
                        gap: "8px",
                      }}
                    >
                      <div>
                        <div style={{ color: "var(--text-primary)", fontWeight: 700, fontSize: "15px" }}>{column.label}</div>
                        <div style={{ color: "var(--text-secondary)", fontSize: "11px", marginTop: "2px" }}>{column.key}</div>
                      </div>
                      <span
                        style={{
                          padding: "4px 10px",
                          borderRadius: "999px",
                          background: `${column.accent}22`,
                          color: column.accent,
                          fontSize: "12px",
                          fontWeight: 700,
                        }}
                      >
                        {groupedTasks[column.key].length}
                      </span>
                    </div>
                    <div style={{ padding: "12px", display: "grid", gap: "10px" }}>
                      {groupedTasks[column.key].length === 0 ? (
                        <div style={{ color: "var(--text-secondary)", fontSize: "13px", textAlign: "center", padding: "36px 12px" }}>
                          {column.empty}
                        </div>
                      ) : (
                        groupedTasks[column.key].map((task) => (
                          <button
                            key={task.job_id}
                            type="button"
                            onClick={() => {
                              setSelectedJobId(task.job_id);
                              setDetail(null);
                              setDetailLoading(true);
                              setDetailError("");
                              loadDetail(task.job_id);
                            }}
                            style={{
                              background: "rgba(15,23,42,0.28)",
                              border: "1px solid var(--border)",
                              borderRadius: "10px",
                              padding: "12px",
                              textAlign: "left",
                              cursor: "pointer",
                            }}
                          >
                            <div className="flex items-start justify-between gap-2">
                              <div style={{ color: "var(--accent)", fontSize: "12px", fontWeight: 700 }}>{task.job_id}</div>
                              <span
                                style={{
                                  padding: "3px 8px",
                                  borderRadius: "999px",
                                  background: "var(--bg-hover)",
                                  color: "var(--text-primary)",
                                  fontSize: "11px",
                                  fontWeight: 600,
                                }}
                              >
                                {task.project || "-"}
                              </span>
                            </div>

                            <div style={{ color: "var(--text-primary)", fontSize: "13px", lineHeight: "1.5", marginTop: "10px", minHeight: "58px" }}>
                              {task.instruction || "(instruction 없음)"}
                            </div>

                            <div className="flex items-center justify-between gap-2 flex-wrap" style={{ marginTop: "10px" }}>
                              <span
                                style={{
                                  padding: "3px 8px",
                                  borderRadius: "999px",
                                  background: "rgba(59,130,246,0.12)",
                                  color: "#93c5fd",
                                  fontSize: "11px",
                                }}
                              >
                                {modelLabel(task)}
                              </span>
                              <span style={{ color: "var(--text-secondary)", fontSize: "11px" }}>
                                {formatElapsed(task.created_at)}
                              </span>
                            </div>

                            <div className="flex items-center justify-between gap-2 flex-wrap" style={{ marginTop: "10px" }}>
                              <span
                                style={{
                                  padding: "3px 8px",
                                  borderRadius: "999px",
                                  ...(toneForStatus(task.status)),
                                  fontSize: "11px",
                                  fontWeight: 600,
                                }}
                              >
                                {task.status}
                              </span>
                              <span style={{ color: "var(--text-secondary)", fontSize: "11px" }}>
                                {task.phase || "-"}
                              </span>
                            </div>

                            {task.error_detail ? (
                              <div style={{ color: "#fca5a5", fontSize: "11px", lineHeight: "1.45", marginTop: "10px" }}>
                                {task.error_detail}
                              </div>
                            ) : null}
                          </button>
                        ))
                      )}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      </div>

      {selectedJobId ? (
        <div
          style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,0.65)", zIndex: 1000, display: "flex", alignItems: "center", justifyContent: "center", padding: "16px" }}
          onClick={() => {
            setSelectedJobId("");
            setDetail(null);
            setDetailError("");
          }}
        >
          <div
            style={{
              width: "min(1200px, 100%)",
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
                <div style={{ color: "var(--accent)", fontSize: "12px", fontWeight: 700 }}>{selectedJobId}</div>
                <div style={{ color: "var(--text-primary)", fontSize: "22px", fontWeight: 700, marginTop: "4px" }}>
                  {detail?.project || "Task Detail"}
                </div>
                <div className="flex items-center gap-2 flex-wrap" style={{ marginTop: "10px" }}>
                  <span
                    style={{
                      padding: "4px 10px",
                      borderRadius: "999px",
                      ...(detail ? toneForStatus(detail.status) : toneForStatus("queued")),
                      fontSize: "12px",
                      fontWeight: 700,
                    }}
                  >
                    {detail?.status || "loading"}
                  </span>
                  <span
                    style={{
                      padding: "4px 10px",
                      borderRadius: "999px",
                      background: "var(--bg-hover)",
                      color: "var(--text-secondary)",
                      fontSize: "12px",
                    }}
                  >
                    raw: {detail?.raw_status || "-"}
                  </span>
                  <span style={{ color: "var(--text-secondary)", fontSize: "12px" }}>
                    phase {detail?.phase || "-"}
                  </span>
                </div>
              </div>
              <button
                type="button"
                onClick={() => {
                  setSelectedJobId("");
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
                <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-3">
                  {[
                    ["Model", detail.actual_model || detail.worker_model || detail.model || "-"],
                    ["Cycle", `${detail.cycle}/${detail.max_cycles || "-"}`],
                    ["Started", formatDateTime(detail.started_at)],
                    ["Updated", formatDateTime(detail.updated_at)],
                  ].map(([label, value]) => (
                    <div key={String(label)} style={{ ...cardStyle, padding: "14px" }}>
                      <div style={{ color: "var(--text-secondary)", fontSize: "12px", marginBottom: "4px" }}>{label}</div>
                      <div style={{ color: "var(--text-primary)", fontSize: "14px", lineHeight: "1.5", wordBreak: "break-word" }}>
                        {value}
                      </div>
                    </div>
                  ))}
                </div>

                <DetailSection title="Instruction" subtitle={`created ${formatDateTime(detail.created_at)}`}>
                  <pre style={contentStyle}>{detail.instruction || "(instruction 없음)"}</pre>
                </DetailSection>

                {detail.error_detail ? (
                  <DetailSection title="Error Detail">
                    <pre style={{ ...contentStyle, color: "#fca5a5" }}>{detail.error_detail}</pre>
                  </DetailSection>
                ) : null}

                <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
                  <DetailSection title="Logs" subtitle={`${detail.logs.length} entries`}>
                    {detail.logs.length > 0 ? (
                      <div style={{ display: "grid", gap: "10px" }}>
                        {detail.logs.map((log) => (
                          <div key={`${log.id}-${log.created_at || "none"}`} style={{ border: "1px solid var(--border)", borderRadius: "10px", padding: "12px", background: "rgba(15,23,42,0.28)" }}>
                            <div className="flex items-center justify-between gap-2 flex-wrap" style={{ marginBottom: "8px" }}>
                              <div style={{ color: "var(--text-primary)", fontSize: "12px", fontWeight: 600 }}>
                                {log.log_type || "log"} · {log.phase || "-"}
                              </div>
                              <div style={{ color: "var(--text-secondary)", fontSize: "11px" }}>
                                {formatDateTime(log.created_at)}
                              </div>
                            </div>
                            <pre style={contentStyle}>{log.content || "(content 없음)"}</pre>
                          </div>
                        ))}
                      </div>
                    ) : (
                      <pre style={contentStyle}>{stringifyValue(detail.log_snapshot) || "(task_logs 없음)"}</pre>
                    )}
                  </DetailSection>

                  <DetailSection title="Review Feedback">
                    <pre style={contentStyle}>{detail.review_feedback || "(review_feedback 없음)"}</pre>
                  </DetailSection>
                </div>

                <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
                  <DetailSection title="Git Diff">
                    <pre style={contentStyle}>{detail.git_diff || "(git_diff 없음)"}</pre>
                  </DetailSection>

                  <DetailSection title="Result Output">
                    <pre style={contentStyle}>{detail.result_output || "(result_output 없음)"}</pre>
                  </DetailSection>
                </div>
              </div>
            ) : null}
          </div>
        </div>
      ) : null}
    </div>
  );
}

function DetailSection({
  title,
  subtitle,
  children,
}: {
  title: string;
  subtitle?: string;
  children: ReactNode;
}) {
  return (
    <div
      style={{
        background: "var(--bg-card)",
        border: "1px solid var(--border)",
        borderRadius: "10px",
        padding: "16px",
      }}
    >
      <div className="flex items-start justify-between gap-3 flex-wrap" style={{ marginBottom: "12px" }}>
        <div>
          <div style={{ fontSize: "12px", color: "var(--text-secondary)", textTransform: "uppercase", letterSpacing: "0.06em" }}>{title}</div>
          {subtitle ? (
            <div style={{ color: "var(--text-secondary)", fontSize: "11px", marginTop: "3px" }}>{subtitle}</div>
          ) : null}
        </div>
      </div>
      {children}
    </div>
  );
}

const contentStyle = {
  margin: 0,
  whiteSpace: "pre-wrap" as const,
  wordBreak: "break-word" as const,
  color: "var(--text-primary)",
  fontSize: "12px",
  lineHeight: "1.6",
  fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
};
