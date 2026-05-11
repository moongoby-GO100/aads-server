"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";

import Header from "@/components/Header";
import { api, type DesignModificationRequestSummary } from "@/lib/api";

const PAGE_SIZE = 50;

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

function statusTone(status: string): { background: string; color: string; border: string } {
  if (status === "approved") {
    return { background: "rgba(34,197,94,0.12)", color: "var(--success)", border: "1px solid rgba(34,197,94,0.24)" };
  }
  if (status === "rejected") {
    return { background: "rgba(239,68,68,0.12)", color: "var(--danger)", border: "1px solid rgba(239,68,68,0.24)" };
  }
  if (status === "review" || status === "running") {
    return { background: "rgba(59,130,246,0.12)", color: "var(--accent)", border: "1px solid rgba(59,130,246,0.24)" };
  }
  if (status === "ready") {
    return { background: "rgba(245,158,11,0.12)", color: "#d97706", border: "1px solid rgba(245,158,11,0.24)" };
  }
  return { background: "rgba(148,163,184,0.12)", color: "var(--text-secondary)", border: "1px solid rgba(148,163,184,0.2)" };
}

export default function DesignModificationsPage() {
  const [requests, setRequests] = useState<DesignModificationRequestSummary[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState("");
  const [statusFilter, setStatusFilter] = useState("all");
  const [lastRefreshedAt, setLastRefreshedAt] = useState<Date | null>(null);

  const loadRequests = useCallback(async (silent = false) => {
    silent ? setRefreshing(true) : setLoading(true);
    setError("");
    try {
      const response = await api.getDesignModificationRequests({
        status: statusFilter === "all" ? undefined : statusFilter,
        limit: PAGE_SIZE,
        offset: 0,
      });
      setRequests(Array.isArray(response.requests) ? response.requests : []);
      setTotal(Number(response.total || 0));
      setLastRefreshedAt(new Date());
    } catch (err) {
      console.error("design modification requests load failed", err);
      setError(err instanceof Error ? err.message : "디자인 수정 요청을 불러오지 못했습니다.");
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, [statusFilter]);

  useEffect(() => {
    loadRequests();
  }, [loadRequests]);

  const statusCounts = useMemo(() => {
    return requests.reduce<Record<string, number>>((acc, item) => {
      const key = item.status || "draft";
      acc[key] = (acc[key] || 0) + 1;
      return acc;
    }, {});
  }, [requests]);

  return (
    <div className="flex flex-col h-full" style={{ background: "var(--bg-primary)" }}>
      <Header title="Design Modifications" />
      <main className="flex-1 overflow-auto p-3 md:p-6">
        <div className="mx-auto grid max-w-7xl gap-4">
          <section className="flex flex-col gap-3 md:flex-row md:items-end md:justify-between">
            <div>
              <h1 className="text-xl font-semibold" style={{ color: "var(--text-primary)" }}>
                Design Modification Requests
              </h1>
              <p className="mt-1 text-sm" style={{ color: "var(--text-secondary)" }}>
                요청별 context pack, snapshot, 관련 결정을 검토하는 운영 목록입니다.
              </p>
            </div>
            <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
              <select
                value={statusFilter}
                onChange={(event) => setStatusFilter(event.target.value)}
                className="h-9 rounded-lg px-3 text-sm outline-none"
                style={{ background: "var(--bg-card)", border: "1px solid var(--border)", color: "var(--text-primary)" }}
                aria-label="상태 필터"
              >
                <option value="all">All status</option>
                <option value="draft">Draft</option>
                <option value="ready">Ready</option>
                <option value="running">Running</option>
                <option value="review">Review</option>
                <option value="approved">Approved</option>
                <option value="rejected">Rejected</option>
              </select>
              <button
                type="button"
                onClick={() => loadRequests(true)}
                disabled={refreshing}
                className="h-9 rounded-lg px-3 text-sm font-medium disabled:opacity-60"
                style={{ background: "var(--accent)", color: "#fff", border: "1px solid var(--accent)" }}
              >
                {refreshing ? "Refreshing" : "Refresh"}
              </button>
            </div>
          </section>

          <section
            className="rounded-lg"
            style={{ background: "var(--bg-card)", border: "1px solid var(--border)", overflow: "hidden" }}
          >
            <div
              className="grid gap-3 px-4 py-3 text-xs md:grid-cols-[1.1fr_1.8fr_0.8fr_0.7fr_0.9fr_0.8fr]"
              style={{ color: "var(--text-secondary)", borderBottom: "1px solid var(--border)" }}
            >
              <div>Project / Route</div>
              <div>Request</div>
              <div>Type</div>
              <div>Status</div>
              <div>Updated</div>
              <div className="md:text-right">Context</div>
            </div>

            {loading ? (
              <div className="px-4 py-10 text-sm" style={{ color: "var(--text-secondary)" }}>
                디자인 수정 요청을 불러오는 중입니다.
              </div>
            ) : error ? (
              <div className="px-4 py-10">
                <div className="text-sm font-medium" style={{ color: "var(--danger)" }}>
                  요청 목록 로드 실패
                </div>
                <div className="mt-2 text-xs break-words" style={{ color: "var(--text-secondary)" }}>
                  {error}
                </div>
              </div>
            ) : requests.length === 0 ? (
              <div className="px-4 py-10 text-sm" style={{ color: "var(--text-secondary)" }}>
                표시할 디자인 수정 요청이 없습니다.
              </div>
            ) : (
              <div>
                {requests.map((request) => {
                  const tone = statusTone(request.status);
                  return (
                    <Link
                      key={request.id}
                      href={`/design/modifications/${encodeURIComponent(request.id)}`}
                      className="grid gap-3 px-4 py-3 text-sm transition-colors md:grid-cols-[1.1fr_1.8fr_0.8fr_0.7fr_0.9fr_0.8fr]"
                      style={{ color: "var(--text-primary)", borderBottom: "1px solid var(--border)" }}
                    >
                      <div className="min-w-0">
                        <div className="font-medium">{request.project_key || "-"}</div>
                        <div className="mt-1 truncate text-xs" style={{ color: "var(--text-secondary)" }}>
                          {request.screen_route || request.screen_name || "No route"}
                        </div>
                      </div>
                      <div className="min-w-0">
                        <div className="overflow-hidden" style={{ maxHeight: "44px" }}>
                          {request.prompt_excerpt || "요청 설명 없음"}
                        </div>
                        <div className="mt-1 text-xs" style={{ color: "var(--text-secondary)" }}>
                          AC {request.acceptance_criteria_count || 0}
                        </div>
                      </div>
                      <div className="break-words text-xs md:text-sm">{request.request_type || "other"}</div>
                      <div>
                        <span className="inline-flex rounded-md px-2 py-1 text-xs font-medium" style={tone}>
                          {request.status || "draft"}
                        </span>
                      </div>
                      <div className="text-xs md:text-sm" style={{ color: "var(--text-secondary)" }}>
                        {formatDateTime(request.updated_at || request.created_at)}
                      </div>
                      <div className="md:text-right">
                        <span className="text-xs" style={{ color: "var(--text-secondary)" }}>
                          {request.context_pack_count || 0} packs
                        </span>
                      </div>
                    </Link>
                  );
                })}
              </div>
            )}
          </section>

          <footer className="flex flex-col gap-1 text-xs sm:flex-row sm:items-center sm:justify-between" style={{ color: "var(--text-secondary)" }}>
            <span>Total {total || requests.length} requests</span>
            <span>
              {lastRefreshedAt ? `Last refreshed ${lastRefreshedAt.toLocaleTimeString("ko-KR", { timeZone: "Asia/Seoul", hour12: false })}` : "Not refreshed"}
              {Object.keys(statusCounts).length > 0 ? ` · ${Object.entries(statusCounts).map(([key, value]) => `${key} ${value}`).join(" / ")}` : ""}
            </span>
          </footer>
        </div>
      </main>
    </div>
  );
}
