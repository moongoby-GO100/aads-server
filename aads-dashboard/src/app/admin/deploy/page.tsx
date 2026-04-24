"use client";

import { useCallback, useEffect, useState } from "react";

import Header from "@/components/Header";
import { api } from "@/lib/api";

type DeployProjectStatus = {
  name: string;
  status: string;
  last_commit: string | null;
  last_deploy_at: string | null;
};

type DeployServerStatus = {
  id: string;
  name: string;
  ip: string;
  projects: DeployProjectStatus[];
};

type DeployStatusResponse = {
  servers: DeployServerStatus[];
};

const SERVER_ACCENTS: Record<string, { accent: string; glow: string; muted: string }> = {
  "68": {
    accent: "#38bdf8",
    glow: "rgba(56, 189, 248, 0.18)",
    muted: "rgba(56, 189, 248, 0.08)",
  },
  "211": {
    accent: "#f59e0b",
    glow: "rgba(245, 158, 11, 0.18)",
    muted: "rgba(245, 158, 11, 0.08)",
  },
  "114": {
    accent: "#f472b6",
    glow: "rgba(244, 114, 182, 0.18)",
    muted: "rgba(244, 114, 182, 0.08)",
  },
};

function formatDeployTime(value: string | null): string {
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

function statusTone(status: string): { background: string; color: string; border: string } {
  if (status === "done") {
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

export default function AdminDeployPage() {
  const [data, setData] = useState<DeployStatusResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState("");
  const [lastRefreshedAt, setLastRefreshedAt] = useState<Date | null>(null);

  const loadStatus = useCallback(async (silent = false) => {
    silent ? setRefreshing(true) : setLoading(true);
    setError("");
    try {
      const response = await api.getAdminDeployStatus();
      setData(response as DeployStatusResponse);
      setLastRefreshedAt(new Date());
    } catch (err) {
      console.error("deploy status load failed", err);
      setError(err instanceof Error ? err.message : "배포 현황을 불러오지 못했습니다.");
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

  const servers = data?.servers ?? [];
  const totalServices = servers.reduce((sum, server) => sum + server.projects.length, 0);
  const healthyServices = servers.reduce(
    (sum, server) => sum + server.projects.filter((project) => project.status === "done").length,
    0
  );

  return (
    <div className="flex flex-col h-full" style={{ background: "var(--bg-primary)" }}>
      <Header title="배포 현황" />
      <div className="flex-1 p-3 md:p-6 overflow-auto space-y-5">
        <section
          className="rounded-2xl p-4 md:p-5"
          style={{
            background: "linear-gradient(135deg, rgba(59,130,246,0.16), rgba(30,41,59,0.92))",
            border: "1px solid rgba(148,163,184,0.18)",
            boxShadow: "0 18px 50px rgba(15,23,42,0.28)",
          }}
        >
          <div className="flex flex-col lg:flex-row lg:items-end lg:justify-between gap-4">
            <div>
              <div
                className="text-xs uppercase tracking-[0.24em] mb-2"
                style={{ color: "rgba(226,232,240,0.72)" }}
              >
                Deploy Status
              </div>
              <h1 className="text-2xl md:text-3xl font-bold" style={{ color: "var(--text-primary)" }}>
                서버별 배포 현황
              </h1>
              <p className="mt-2 text-sm max-w-2xl" style={{ color: "rgba(226,232,240,0.72)" }}>
                `pipeline_jobs`의 마지막 `done` 작업을 기준으로 각 프로젝트의 최근 배포 시각과 커밋 해시를 보여줍니다.
              </p>
            </div>

            <div className="grid grid-cols-2 gap-3 w-full lg:w-auto">
              <div
                className="rounded-xl p-3 md:p-4"
                style={{ background: "rgba(15,23,42,0.45)", border: "1px solid rgba(148,163,184,0.18)" }}
              >
                <div className="text-[11px] uppercase tracking-[0.16em]" style={{ color: "var(--text-secondary)" }}>
                  총 서비스
                </div>
                <div className="mt-1 text-2xl font-bold" style={{ color: "var(--text-primary)" }}>
                  {loading ? "..." : totalServices}
                </div>
              </div>
              <div
                className="rounded-xl p-3 md:p-4"
                style={{ background: "rgba(15,23,42,0.45)", border: "1px solid rgba(148,163,184,0.18)" }}
              >
                <div className="text-[11px] uppercase tracking-[0.16em]" style={{ color: "var(--text-secondary)" }}>
                  정상
                </div>
                <div
                  className="mt-1 text-2xl font-bold"
                  style={{ color: healthyServices === totalServices && totalServices > 0 ? "var(--success)" : "#fbbf24" }}
                >
                  {loading ? "..." : healthyServices}
                </div>
              </div>
            </div>
          </div>

          <div className="mt-4 flex flex-col sm:flex-row sm:items-center sm:justify-between gap-2 text-xs">
            <span style={{ color: "rgba(226,232,240,0.72)" }}>
              {refreshing
                ? "새로고침 중..."
                : lastRefreshedAt
                  ? `마지막 갱신: ${lastRefreshedAt.toLocaleTimeString("ko-KR", {
                    timeZone: "Asia/Seoul",
                    hour12: false,
                    hour: "2-digit",
                    minute: "2-digit",
                    second: "2-digit",
                  })}`
                  : "로딩 중..."}
            </span>
            <button
              type="button"
              onClick={() => loadStatus(true)}
              className="self-start sm:self-auto px-3 py-2 rounded-lg text-xs font-medium"
              style={{
                background: "rgba(15,23,42,0.45)",
                color: "var(--text-primary)",
                border: "1px solid rgba(148,163,184,0.18)",
              }}
            >
              새로고침
            </button>
          </div>
        </section>

        {error ? (
          <div
            className="rounded-xl p-4"
            style={{ background: "var(--bg-card)", border: "1px solid rgba(239,68,68,0.28)", color: "var(--danger)" }}
          >
            {error}
          </div>
        ) : null}

        {loading ? (
          <div
            className="rounded-xl p-6 text-center"
            style={{ background: "var(--bg-card)", border: "1px solid var(--border)", color: "var(--text-secondary)" }}
          >
            배포 현황을 불러오는 중...
          </div>
        ) : (
          <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
            {servers.map((server) => {
              const theme = SERVER_ACCENTS[server.id] ?? {
                accent: "var(--accent)",
                glow: "rgba(59,130,246,0.18)",
                muted: "rgba(59,130,246,0.08)",
              };

              return (
                <section
                  key={server.id}
                  className="rounded-2xl p-4 md:p-5"
                  style={{
                    background: "var(--bg-card)",
                    border: "1px solid var(--border)",
                    borderTop: `3px solid ${theme.accent}`,
                    boxShadow: `0 16px 38px ${theme.glow}`,
                  }}
                >
                  <div className="flex items-start justify-between gap-3">
                    <div>
                      <div className="text-lg font-bold" style={{ color: "var(--text-primary)" }}>
                        {server.name}
                      </div>
                      <div className="mt-1 text-xs" style={{ color: "var(--text-secondary)" }}>
                        {server.ip}
                      </div>
                    </div>
                    <span
                      className="px-2.5 py-1 rounded-full text-xs font-semibold"
                      style={{
                        background: theme.muted,
                        color: theme.accent,
                        border: `1px solid ${theme.glow.replace("0.18", "0.3")}`,
                      }}
                    >
                      {server.projects.length} services
                    </span>
                  </div>

                  <div className="mt-4 space-y-3">
                    {server.projects.map((project) => {
                      const healthy = project.status === "done";
                      return (
                        <div
                          key={project.name}
                          className="rounded-xl p-3"
                          style={{
                            background: "rgba(15,23,42,0.22)",
                            border: "1px solid rgba(148,163,184,0.16)",
                          }}
                        >
                          <div className="flex items-center justify-between gap-3">
                            <div className="flex items-center gap-2 min-w-0">
                              <span className="text-base" aria-hidden="true">
                                {healthy ? "🟢" : "🔴"}
                              </span>
                              <div className="min-w-0">
                                <div className="font-semibold" style={{ color: "var(--text-primary)" }}>
                                  {project.name}
                                </div>
                                <div className="text-[11px]" style={{ color: "var(--text-secondary)" }}>
                                  {healthy ? "최근 배포 완료" : "배포 기록 없음"}
                                </div>
                              </div>
                            </div>

                            <span
                              className="px-2 py-1 rounded-full text-[11px] font-semibold flex-shrink-0"
                              style={statusTone(project.status)}
                            >
                              {healthy ? "정상" : "미배포"}
                            </span>
                          </div>

                          <div className="mt-3 grid grid-cols-1 sm:grid-cols-2 gap-2">
                            <div
                              className="rounded-lg px-3 py-2"
                              style={{
                                background: "rgba(15,23,42,0.32)",
                                border: "1px solid rgba(148,163,184,0.14)",
                              }}
                            >
                              <div className="text-[11px]" style={{ color: "var(--text-secondary)" }}>
                                마지막 커밋
                              </div>
                              <div
                                className="mt-1 font-mono text-sm"
                                style={{ color: project.last_commit ? "var(--text-primary)" : "var(--text-secondary)" }}
                              >
                                {project.last_commit ? project.last_commit.slice(0, 7) : "-"}
                              </div>
                            </div>

                            <div
                              className="rounded-lg px-3 py-2"
                              style={{
                                background: "rgba(15,23,42,0.32)",
                                border: "1px solid rgba(148,163,184,0.14)",
                              }}
                            >
                              <div className="text-[11px]" style={{ color: "var(--text-secondary)" }}>
                                마지막 배포 시각
                              </div>
                              <div className="mt-1 text-sm" style={{ color: "var(--text-primary)" }}>
                                {formatDeployTime(project.last_deploy_at)}
                              </div>
                            </div>
                          </div>
                        </div>
                      );
                    })}
                  </div>
                </section>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
