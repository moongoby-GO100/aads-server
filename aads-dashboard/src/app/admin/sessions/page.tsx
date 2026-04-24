"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

import Header from "@/components/Header";
import { api } from "@/lib/api";

type SessionStatus = "done" | "error" | string;
type TimelineEventType = "tool_call" | "llm_response" | "error" | string;

interface SessionSummary {
  job_id: string;
  project: string;
  instruction: string;
  status: SessionStatus;
  created_at: string | null;
  updated_at: string | null;
  error_detail: string;
}

interface SessionListResponse {
  sessions: SessionSummary[];
  total: number;
}

interface ReplayEvent {
  timestamp: string | null;
  type: TimelineEventType;
  summary: string;
  error_category?: string;
}

interface SessionReplayDetail {
  job_id: string;
  project: string;
  status: SessionStatus;
  raw_status: string;
  phase: string;
  instruction: string;
  error_detail: string;
  result_preview: string;
  duration_seconds: number;
  started_at: string | null;
  created_at: string | null;
  updated_at: string | null;
}

interface SessionReplayResponse {
  session: SessionReplayDetail;
  timeline: ReplayEvent[];
  timeline_count: number;
}

function toMs(value?: string | null): number | null {
  if (!value) return null;
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return null;
  return parsed.getTime();
}

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

function formatAgo(value?: string | null): string {
  const ms = toMs(value);
  if (ms == null) return "-";
  const diff = Math.max(0, Math.floor((Date.now() - ms) / 1000));
  if (diff < 60) return `${diff}s`;
  const minutes = Math.floor(diff / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return `${hours}h`;
  const days = Math.floor(hours / 24);
  return `${days}d`;
}

function formatDurationSeconds(value?: number): string {
  const sec = Math.max(0, Math.floor(value || 0));
  if (sec < 60) return `${sec}s`;
  const minutes = Math.floor(sec / 60);
  if (minutes < 60) return `${minutes}m ${sec % 60}s`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ${minutes % 60}m`;
  const days = Math.floor(hours / 24);
  return `${days}d ${hours % 24}h`;
}

function formatDelta(value?: string | null, baselineMs?: number | null): string {
  if (!baselineMs) return "-";
  const eventMs = toMs(value);
  if (eventMs == null) return "-";
  const diff = Math.max(0, Math.floor((eventMs - baselineMs) / 1000));
  if (diff < 60) return `+${diff}s`;
  const minutes = Math.floor(diff / 60);
  if (minutes < 60) return `+${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return `+${hours}h`;
  const days = Math.floor(hours / 24);
  return `+${days}d`;
}

function statusTone(status: SessionStatus): { background: string; color: string; border: string } {
  if (status === "done") {
    return {
      background: "rgba(34,197,94,0.14)",
      color: "#4ade80",
      border: "1px solid rgba(34,197,94,0.24)",
    };
  }
  if (status === "error") {
    return {
      background: "rgba(239,68,68,0.14)",
      color: "#f87171",
      border: "1px solid rgba(239,68,68,0.24)",
    };
  }
  return {
    background: "rgba(148,163,184,0.14)",
    color: "var(--text-secondary)",
    border: "1px solid rgba(148,163,184,0.24)",
  };
}

function eventStyle(type: TimelineEventType): {
  icon: string;
  iconBg: string;
  iconColor: string;
  borderColor: string;
} {
  if (type === "tool_call") {
    return {
      icon: "🔧",
      iconBg: "rgba(59,130,246,0.14)",
      iconColor: "#60a5fa",
      borderColor: "rgba(59,130,246,0.24)",
    };
  }
  if (type === "error") {
    return {
      icon: "❌",
      iconBg: "rgba(239,68,68,0.14)",
      iconColor: "#f87171",
      borderColor: "rgba(239,68,68,0.24)",
    };
  }
  return {
    icon: "✅",
    iconBg: "rgba(34,197,94,0.14)",
    iconColor: "#4ade80",
    borderColor: "rgba(34,197,94,0.24)",
  };
}

export default function AdminSessionsPage() {
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState("");
  const [selectedProject, setSelectedProject] = useState("all");
  const [selectedJobId, setSelectedJobId] = useState("");
  const [lastRefreshedAt, setLastRefreshedAt] = useState<Date | null>(null);

  const [replay, setReplay] = useState<SessionReplayResponse | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState("");

  const loadSessions = useCallback(async (silent = false) => {
    if (silent) {
      setRefreshing(true);
    } else {
      setLoading(true);
    }
    try {
      const response = (await api.getAdminSessions()) as SessionListResponse;
      const list = response.sessions || [];
      setSessions(list);
      setTotal(response.total || list.length);
      setError("");
      setLastRefreshedAt(new Date());

      if (!selectedJobId && list.length > 0) {
        setSelectedJobId(list[0].job_id);
      } else if (selectedJobId && !list.some((item) => item.job_id === selectedJobId)) {
        setSelectedJobId(list[0]?.job_id || "");
      }
    } catch (err) {
      console.error("session list load failed", err);
      setError(err instanceof Error ? err.message : "세션 목록을 불러오지 못했습니다.");
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, [selectedJobId]);

  const loadReplay = useCallback(async (jobId: string, silent = false) => {
    if (!jobId) return;
    if (!silent) {
      setDetailLoading(true);
      setDetailError("");
    }
    try {
      const response = (await api.getAdminSessionReplay(jobId)) as SessionReplayResponse;
      setReplay(response);
      setDetailError("");
    } catch (err) {
      console.error("session replay load failed", err);
      setDetailError(err instanceof Error ? err.message : "세션 리플레이를 불러오지 못했습니다.");
      setReplay(null);
    } finally {
      setDetailLoading(false);
    }
  }, []);

  useEffect(() => {
    loadSessions();
  }, [loadSessions]);

  useEffect(() => {
    if (!selectedJobId) return;
    loadReplay(selectedJobId);
  }, [loadReplay, selectedJobId]);

  useEffect(() => {
    const timer = window.setInterval(() => {
      loadSessions(true);
      if (selectedJobId) {
        loadReplay(selectedJobId, true);
      }
    }, 30000);
    return () => window.clearInterval(timer);
  }, [loadReplay, loadSessions, selectedJobId]);

  const projectOptions = useMemo(() => {
    const set = new Set<string>();
    for (const item of sessions) {
      const project = (item.project || "").trim();
      if (project) set.add(project);
    }
    return ["all", ...Array.from(set).sort()];
  }, [sessions]);

  const filteredSessions = useMemo(() => {
    if (selectedProject === "all") return sessions;
    return sessions.filter((item) => item.project === selectedProject);
  }, [selectedProject, sessions]);

  const selectedSummary = useMemo(
    () => sessions.find((item) => item.job_id === selectedJobId) || null,
    [selectedJobId, sessions],
  );

  const timelineBaselineMs = useMemo(() => {
    if (!replay?.session) return null;
    const sessionBase = toMs(replay.session.started_at) ?? toMs(replay.session.created_at);
    if (sessionBase != null) return sessionBase;
    return toMs(replay.timeline?.[0]?.timestamp) ?? null;
  }, [replay]);

  const computedDuration = useMemo(() => {
    if (!replay?.session) return 0;
    if (replay.session.duration_seconds > 0) return replay.session.duration_seconds;
    const started = toMs(replay.session.started_at) ?? toMs(replay.session.created_at);
    const ended = toMs(replay.session.updated_at);
    if (started == null || ended == null) return 0;
    return Math.max(0, Math.floor((ended - started) / 1000));
  }, [replay]);

  const resultSummary = useMemo(() => {
    if (!replay?.session) return "-";
    if (replay.session.status === "error") {
      return replay.session.error_detail || "오류로 종료되었습니다.";
    }
    return replay.session.result_preview || "완료되었습니다.";
  }, [replay]);

  const cardStyle = {
    background: "var(--bg-card)",
    border: "1px solid var(--border)",
    borderRadius: "10px",
    padding: "14px",
  };

  return (
    <div className="flex flex-col h-full" style={{ background: "var(--bg-primary)" }}>
      <Header title="Sessions Replay" />
      <div className="flex-1 p-3 md:p-6 overflow-auto">
        <div className="grid gap-4">
          <div className="flex items-start justify-between gap-3 flex-wrap">
            <div>
              <div style={{ color: "var(--text-primary)", fontSize: "22px", fontWeight: 700 }}>Session Replay</div>
              <div style={{ color: "var(--text-secondary)", fontSize: "13px", marginTop: "4px" }}>
                에이전트 작업의 도구 호출과 응답 흐름을 타임라인으로 확인합니다.
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
                onClick={() => loadSessions(true)}
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

          {error ? (
            <div style={{ ...cardStyle, color: "var(--danger)" }}>{error}</div>
          ) : null}

          <div className="grid gap-4 md:grid-cols-[340px_minmax(0,1fr)]">
            <div style={{ ...cardStyle, padding: "12px", maxHeight: "calc(100vh - 220px)", overflow: "auto" }}>
              <div className="flex items-center justify-between gap-2 mb-3">
                <div style={{ color: "var(--text-primary)", fontWeight: 700, fontSize: "14px" }}>
                  Sessions ({total.toLocaleString("ko-KR")})
                </div>
                <select
                  value={selectedProject}
                  onChange={(e) => setSelectedProject(e.target.value)}
                  style={{
                    background: "var(--bg-primary)",
                    color: "var(--text-primary)",
                    border: "1px solid var(--border)",
                    borderRadius: "8px",
                    padding: "6px 8px",
                    fontSize: "12px",
                  }}
                >
                  {projectOptions.map((project) => (
                    <option key={project} value={project}>
                      {project === "all" ? "전체 프로젝트" : project}
                    </option>
                  ))}
                </select>
              </div>

              {loading ? (
                <div style={{ color: "var(--text-secondary)", textAlign: "center", padding: "22px 0" }}>로딩 중...</div>
              ) : filteredSessions.length === 0 ? (
                <div style={{ color: "var(--text-secondary)", textAlign: "center", padding: "22px 0" }}>
                  표시할 세션이 없습니다.
                </div>
              ) : (
                <div className="grid gap-2">
                  {filteredSessions.map((item) => {
                    const active = selectedJobId === item.job_id;
                    const tone = statusTone(item.status);
                    return (
                      <button
                        key={item.job_id}
                        type="button"
                        onClick={() => setSelectedJobId(item.job_id)}
                        style={{
                          textAlign: "left",
                          borderRadius: "8px",
                          border: active ? "1px solid var(--accent)" : "1px solid var(--border)",
                          background: active ? "rgba(59,130,246,0.10)" : "var(--bg-primary)",
                          padding: "10px",
                          cursor: "pointer",
                        }}
                      >
                        <div className="flex items-center justify-between gap-2 mb-1">
                          <div style={{ color: "var(--text-primary)", fontWeight: 600, fontSize: "12px" }}>
                            {item.project || "UNKNOWN"} · {item.job_id}
                          </div>
                          <span
                            style={{
                              ...tone,
                              borderRadius: "999px",
                              fontSize: "11px",
                              padding: "3px 8px",
                              fontWeight: 600,
                            }}
                          >
                            {item.status}
                          </span>
                        </div>
                        <div style={{ color: "var(--text-secondary)", fontSize: "12px", lineHeight: 1.45 }}>
                          {item.instruction || "(instruction 없음)"}
                        </div>
                        <div className="flex items-center justify-between mt-2">
                          <span style={{ color: "var(--text-secondary)", fontSize: "11px" }}>
                            {formatDateTime(item.updated_at || item.created_at)}
                          </span>
                          <span style={{ color: "var(--text-secondary)", fontSize: "11px" }}>
                            {formatAgo(item.updated_at || item.created_at)} 전
                          </span>
                        </div>
                      </button>
                    );
                  })}
                </div>
              )}
            </div>

            <div className="grid gap-3 min-w-0">
              {!selectedJobId ? (
                <div style={{ ...cardStyle, color: "var(--text-secondary)", textAlign: "center" }}>세션을 선택해주세요.</div>
              ) : detailLoading ? (
                <div style={{ ...cardStyle, color: "var(--text-secondary)", textAlign: "center" }}>세션 상세 로딩 중...</div>
              ) : detailError ? (
                <div style={{ ...cardStyle, color: "var(--danger)" }}>{detailError}</div>
              ) : !replay?.session ? (
                <div style={{ ...cardStyle, color: "var(--text-secondary)", textAlign: "center" }}>세션 정보를 불러오지 못했습니다.</div>
              ) : (
                <>
                  <div style={cardStyle}>
                    <div className="flex items-start justify-between gap-3 flex-wrap">
                      <div className="flex items-center gap-2 flex-wrap">
                        <span
                          style={{
                            ...statusTone(replay.session.status),
                            borderRadius: "999px",
                            fontSize: "12px",
                            padding: "4px 10px",
                            fontWeight: 700,
                          }}
                        >
                          {replay.session.status}
                        </span>
                        <span style={{ color: "var(--text-secondary)", fontSize: "12px" }}>
                          {replay.session.project || selectedSummary?.project || "UNKNOWN"} · {replay.session.job_id}
                        </span>
                      </div>
                      <div style={{ color: "var(--text-secondary)", fontSize: "12px" }}>
                        총 소요 {formatDurationSeconds(computedDuration)}
                      </div>
                    </div>

                    <div style={{ marginTop: "12px" }}>
                      <div style={{ color: "var(--text-secondary)", fontSize: "12px", marginBottom: "4px" }}>Instruction</div>
                      <div style={{ color: "var(--text-primary)", fontSize: "14px", lineHeight: 1.55 }}>
                        {replay.session.instruction || "(instruction 없음)"}
                      </div>
                    </div>

                    <div style={{ marginTop: "12px" }}>
                      <div style={{ color: "var(--text-secondary)", fontSize: "12px", marginBottom: "4px" }}>Result</div>
                      <div
                        style={{
                          color: replay.session.status === "error" ? "#f87171" : "var(--text-primary)",
                          fontSize: "13px",
                          lineHeight: 1.55,
                        }}
                      >
                        {resultSummary}
                      </div>
                    </div>

                    <div className="grid grid-cols-1 md:grid-cols-3 gap-2 mt-3">
                      {[
                        ["시작", formatDateTime(replay.session.started_at || replay.session.created_at)],
                        ["종료", formatDateTime(replay.session.updated_at)],
                        ["이벤트", `${replay.timeline_count || replay.timeline?.length || 0}개`],
                      ].map(([label, value]) => (
                        <div key={String(label)} style={{ background: "var(--bg-primary)", border: "1px solid var(--border)", borderRadius: "8px", padding: "8px 10px" }}>
                          <div style={{ color: "var(--text-secondary)", fontSize: "11px" }}>{label}</div>
                          <div style={{ color: "var(--text-primary)", fontSize: "12px", marginTop: "2px" }}>{value}</div>
                        </div>
                      ))}
                    </div>
                  </div>

                  <div style={{ ...cardStyle, padding: "12px" }}>
                    <div className="flex items-center justify-between gap-2 mb-3">
                      <div style={{ color: "var(--text-primary)", fontWeight: 700, fontSize: "14px" }}>Timeline</div>
                      <div style={{ color: "var(--text-secondary)", fontSize: "12px" }}>
                        base {formatDateTime(replay.session.started_at || replay.session.created_at)}
                      </div>
                    </div>

                    {(replay.timeline || []).length === 0 ? (
                      <div style={{ color: "var(--text-secondary)", textAlign: "center", padding: "24px 0" }}>
                        표시할 이벤트가 없습니다.
                      </div>
                    ) : (
                      <div style={{ position: "relative", paddingLeft: "4px" }}>
                        <div
                          style={{
                            position: "absolute",
                            left: "18px",
                            top: "4px",
                            bottom: "4px",
                            width: "1px",
                            background: "var(--border)",
                          }}
                        />
                        <div className="grid gap-2">
                          {replay.timeline.map((event, idx) => {
                            const style = eventStyle(event.type);
                            return (
                              <div key={`${event.timestamp || "none"}-${event.type}-${idx}`} className="flex items-start gap-2">
                                <div
                                  style={{
                                    width: "30px",
                                    height: "30px",
                                    borderRadius: "999px",
                                    background: style.iconBg,
                                    color: style.iconColor,
                                    border: `1px solid ${style.borderColor}`,
                                    display: "flex",
                                    alignItems: "center",
                                    justifyContent: "center",
                                    zIndex: 1,
                                    fontSize: "13px",
                                  }}
                                >
                                  {style.icon}
                                </div>
                                <div
                                  style={{
                                    flex: 1,
                                    background: "var(--bg-primary)",
                                    border: `1px solid ${style.borderColor}`,
                                    borderRadius: "8px",
                                    padding: "10px 12px",
                                  }}
                                >
                                  <div className="flex items-center justify-between gap-2 flex-wrap">
                                    <div style={{ color: "var(--text-primary)", fontWeight: 600, fontSize: "12px" }}>
                                      {event.type}
                                    </div>
                                    <div className="flex items-center gap-2 flex-wrap">
                                      {event.type === "error" && event.error_category ? (
                                        <span
                                          style={{
                                            borderRadius: "999px",
                                            padding: "2px 8px",
                                            background: "rgba(239,68,68,0.14)",
                                            color: "#f87171",
                                            border: "1px solid rgba(239,68,68,0.24)",
                                            fontSize: "11px",
                                          }}
                                        >
                                          {event.error_category}
                                        </span>
                                      ) : null}
                                      <span style={{ color: "var(--text-secondary)", fontSize: "11px" }}>
                                        {formatDateTime(event.timestamp)}
                                      </span>
                                      <span style={{ color: "var(--text-secondary)", fontSize: "11px" }}>
                                        {formatDelta(event.timestamp, timelineBaselineMs)}
                                      </span>
                                    </div>
                                  </div>
                                  <div style={{ color: "var(--text-primary)", fontSize: "13px", lineHeight: 1.5, marginTop: "6px", whiteSpace: "pre-wrap", wordBreak: "break-word" }}>
                                    {event.summary || "(요약 없음)"}
                                  </div>
                                </div>
                              </div>
                            );
                          })}
                        </div>
                      </div>
                    )}
                  </div>
                </>
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

