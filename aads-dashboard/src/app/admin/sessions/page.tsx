"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

import Header from "@/components/Header";
import { api, type AdminSessionItem } from "@/lib/api";

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

function toNumber(value: unknown): number {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim()) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : 0;
  }
  return 0;
}

function normalizeSession(item: unknown): AdminSessionItem {
  const row = (item || {}) as Record<string, unknown>;
  const sessionId = String(row.session_id || row.id || row.chat_session_id || "").trim();
  return {
    ...row,
    session_id: sessionId || "-",
    workspace: String(row.workspace || row.workspace_name || row.project || "-").trim() || "-",
    created_at: (row.created_at || row.started_at || row.updated_at || null) as string | null,
    message_count: toNumber(row.message_count || row.total_messages || row.turns),
  };
}

function extractSessions(payload: unknown): AdminSessionItem[] {
  if (Array.isArray(payload)) {
    return payload.map((item) => normalizeSession(item));
  }

  if (!payload || typeof payload !== "object") return [];

  const data = payload as Record<string, unknown>;
  const candidates = [data.sessions, data.items, data.data];
  for (const item of candidates) {
    if (Array.isArray(item)) {
      return item.map((entry) => normalizeSession(entry));
    }
  }

  return [];
}

export default function AdminSessionsPage() {
  const [sessions, setSessions] = useState<AdminSessionItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState("");
  const [lastRefreshedAt, setLastRefreshedAt] = useState<Date | null>(null);

  const loadSessions = useCallback(async (silent = false) => {
    if (silent) {
      setRefreshing(true);
    } else {
      setLoading(true);
    }

    try {
      const response = await api.getAdminSessions();
      setSessions(extractSessions(response));
      setError("");
      setLastRefreshedAt(new Date());
    } catch (err) {
      console.error("admin sessions load failed", err);
      setError(err instanceof Error ? err.message : "세션 목록을 불러오지 못했습니다.");
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    loadSessions();
  }, [loadSessions]);

  useEffect(() => {
    const timer = window.setInterval(() => {
      loadSessions(true);
    }, 30000);
    return () => window.clearInterval(timer);
  }, [loadSessions]);

  const stats = useMemo(() => {
    const totalSessions = sessions.length;
    const totalMessages = sessions.reduce((sum, session) => sum + toNumber(session.message_count), 0);
    return { totalSessions, totalMessages };
  }, [sessions]);

  const cardStyle = {
    background: "var(--bg-card)",
    border: "1px solid var(--border)",
    borderRadius: "10px",
    padding: "16px",
  };

  return (
    <div className="flex flex-col h-full" style={{ background: "var(--bg-primary)" }}>
      <Header title="Sessions" />
      <div className="flex-1 p-3 md:p-6 overflow-auto">
        <div className="grid gap-4">
          <div className="flex items-start justify-between gap-3 flex-wrap">
            <div>
              <div style={{ color: "var(--text-primary)", fontSize: "22px", fontWeight: 700 }}>
                세션 모니터링
              </div>
              <div style={{ color: "var(--text-secondary)", fontSize: "13px", marginTop: "4px" }}>
                Admin 세션 목록과 메시지 수를 확인합니다.
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

          <div className="grid grid-cols-2 gap-3">
            <div style={{ ...cardStyle, padding: "14px" }}>
              <div style={{ color: "var(--text-secondary)", fontSize: "12px", marginBottom: "4px" }}>Sessions</div>
              <div style={{ color: "var(--text-primary)", fontSize: "26px", fontWeight: 700 }}>
                {loading ? "..." : stats.totalSessions}
              </div>
            </div>
            <div style={{ ...cardStyle, padding: "14px" }}>
              <div style={{ color: "var(--text-secondary)", fontSize: "12px", marginBottom: "4px" }}>Messages</div>
              <div style={{ color: "var(--accent)", fontSize: "26px", fontWeight: 700 }}>
                {loading ? "..." : stats.totalMessages}
              </div>
            </div>
          </div>

          {error ? <div style={{ ...cardStyle, color: "var(--danger)" }}>{error}</div> : null}

          <section style={cardStyle}>
            <div style={{ overflowX: "auto" }}>
              <table style={{ width: "100%", borderCollapse: "collapse", minWidth: "760px" }}>
                <thead>
                  <tr style={{ borderBottom: "1px solid var(--border)", color: "var(--text-secondary)" }}>
                    <th style={thStyle}>Session ID</th>
                    <th style={thStyle}>Workspace</th>
                    <th style={thStyle}>Created At</th>
                    <th style={thStyle}>Message Count</th>
                  </tr>
                </thead>
                <tbody>
                  {sessions.length === 0 ? (
                    <tr>
                      <td colSpan={4} style={{ ...tdStyle, textAlign: "center", color: "var(--text-secondary)" }}>
                        {loading ? "로딩 중..." : "표시할 세션이 없습니다."}
                      </td>
                    </tr>
                  ) : (
                    sessions.map((session, index) => (
                      <tr key={`${session.session_id}-${index}`} style={{ borderBottom: "1px solid var(--border)" }}>
                        <td style={{ ...tdStyle, fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace" }}>
                          {session.session_id}
                        </td>
                        <td style={tdStyle}>{session.workspace || "-"}</td>
                        <td style={tdStyle}>{formatDateTime(session.created_at)}</td>
                        <td style={tdStyle}>{toNumber(session.message_count)}</td>
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
