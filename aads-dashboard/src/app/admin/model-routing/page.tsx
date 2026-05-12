"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

import Header from "@/components/Header";
import { api } from "@/lib/api";

type RouteKey = "image" | "edit_image" | "video" | "llm";

interface RoutingPreference {
  route_key: RouteKey;
  provider: string;
  model_id: string;
  display_name?: string;
  execution_model_id?: string | null;
  family?: string | null;
  category?: string | null;
  display_order: number;
  is_enabled: boolean;
  is_default: boolean;
  is_active?: boolean;
  is_selectable?: boolean;
  is_executable?: boolean;
  availability: string;
  verification_status?: string | null;
  notes?: string;
  updated_at?: string | null;
  updated_by?: string | null;
}

const ROUTES: Array<{ key: RouteKey; label: string; desc: string }> = [
  { key: "image", label: "이미지", desc: "generate_image 기본 라우팅" },
  { key: "edit_image", label: "이미지 편집", desc: "edit_image 기본 라우팅" },
  { key: "video", label: "동영상", desc: "generate_video job 라우팅" },
  { key: "llm", label: "LLM", desc: "어드민 기본 LLM 표시/선호" },
];

function availabilityStyle(value: string): { background: string; color: string; border: string } {
  if (value === "available") {
    return { background: "rgba(34,197,94,0.12)", color: "var(--success)", border: "1px solid rgba(34,197,94,0.22)" };
  }
  if (value === "disabled") {
    return { background: "rgba(148,163,184,0.12)", color: "var(--text-secondary)", border: "1px solid rgba(148,163,184,0.22)" };
  }
  if (value === "adapter_unavailable" || value === "review_required") {
    return { background: "rgba(245,158,11,0.14)", color: "#d97706", border: "1px solid rgba(245,158,11,0.26)" };
  }
  return { background: "rgba(239,68,68,0.1)", color: "var(--danger)", border: "1px solid rgba(239,68,68,0.22)" };
}

function formatDateTime(value?: string | null): string {
  if (!value) return "-";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString("ko-KR", { timeZone: "Asia/Seoul" });
}

export default function ModelRoutingPage() {
  const [items, setItems] = useState<RoutingPreference[]>([]);
  const [activeRoute, setActiveRoute] = useState<RouteKey>("image");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState("");

  const load = useCallback(() => {
    setLoading(true);
    setMessage("");
    api.getModelRoutingPreferences()
      .then((res: any) => setItems(Array.isArray(res.preferences) ? res.preferences : []))
      .catch((err) => setMessage(err instanceof Error ? err.message : "모델 라우팅 설정을 불러오지 못했습니다."))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => { load(); }, [load]);

  const grouped = useMemo(() => {
    return ROUTES.reduce((acc, route) => {
      acc[route.key] = items.filter((item) => item.route_key === route.key);
      return acc;
    }, {} as Record<RouteKey, RoutingPreference[]>);
  }, [items]);

  const visibleItems = grouped[activeRoute] || [];
  const defaultItem = visibleItems.find((item) => item.is_default);

  const patchItem = (target: RoutingPreference, patch: Partial<RoutingPreference>) => {
    setItems((prev) => prev.map((item) => (
      item.route_key === target.route_key && item.provider === target.provider && item.model_id === target.model_id
        ? { ...item, ...patch }
        : item
    )));
  };

  const setDefault = (target: RoutingPreference) => {
    setItems((prev) => prev.map((item) => (
      item.route_key === target.route_key
        ? { ...item, is_default: item.provider === target.provider && item.model_id === target.model_id }
        : item
    )));
  };

  const save = async () => {
    setSaving(true);
    setMessage("");
    try {
      const preferences = items.map((item) => ({
        route_key: item.route_key,
        provider: item.provider,
        model_id: item.model_id,
        display_order: item.display_order,
        is_enabled: item.is_enabled,
        is_default: item.is_default,
        notes: item.notes || "",
      }));
      const res: any = await api.updateModelRoutingPreferences(preferences);
      setItems(Array.isArray(res.preferences) ? res.preferences : items);
      setMessage("저장 완료");
    } catch (err) {
      setMessage(err instanceof Error ? err.message : "저장 실패");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="flex flex-col h-full" style={{ background: "var(--bg-primary)" }}>
      <Header title="Model Routing" />
      <div className="flex-1 p-3 md:p-6 overflow-auto space-y-4">
        <section className="rounded-xl p-5" style={{ background: "var(--bg-card)", border: "1px solid var(--border)" }}>
          <div className="flex flex-col lg:flex-row lg:items-center lg:justify-between gap-3">
            <div>
              <h2 className="text-sm font-semibold mb-1" style={{ color: "var(--text-primary)" }}>DB 모델 라우팅 설정</h2>
              <p className="text-xs" style={{ color: "var(--text-secondary)" }}>
                explicit 요청값 다음으로 적용되는 이미지/동영상/LLM 기본 모델입니다.
              </p>
            </div>
            <div className="flex gap-2 flex-wrap">
              <button
                onClick={load}
                className="px-3 py-2 rounded-lg text-xs"
                style={{ background: "var(--bg-hover)", color: "var(--text-primary)", border: "1px solid var(--border)" }}
              >
                새로고침
              </button>
              <button
                onClick={save}
                disabled={saving || loading}
                className="px-4 py-2 rounded-lg text-xs font-bold"
                style={{ background: "var(--accent)", color: "#fff", opacity: saving || loading ? 0.6 : 1 }}
              >
                {saving ? "저장 중..." : "설정 저장"}
              </button>
            </div>
          </div>
          {message && (
            <p className="text-xs mt-3 px-3 py-2 rounded" style={{
              background: message.includes("실패") || message.includes("API error") ? "rgba(239,68,68,0.1)" : "rgba(34,197,94,0.1)",
              color: message.includes("실패") || message.includes("API error") ? "var(--danger)" : "var(--success)",
            }}>
              {message}
            </p>
          )}
        </section>

        <section className="grid grid-cols-2 xl:grid-cols-4 gap-3">
          {ROUTES.map((route) => {
            const routeItems = grouped[route.key] || [];
            const active = activeRoute === route.key;
            const routeDefault = routeItems.find((item) => item.is_default);
            return (
              <button
                key={route.key}
                onClick={() => setActiveRoute(route.key)}
                className="text-left rounded-xl p-4"
                style={{
                  background: active ? "rgba(59,130,246,0.12)" : "var(--bg-card)",
                  border: active ? "1px solid rgba(59,130,246,0.35)" : "1px solid var(--border)",
                }}
              >
                <div className="flex items-center justify-between gap-2 mb-2">
                  <span className="text-sm font-semibold" style={{ color: "var(--text-primary)" }}>{route.label}</span>
                  <span className="text-xs" style={{ color: "var(--text-secondary)" }}>{routeItems.length}개</span>
                </div>
                <p className="text-[11px] mb-2" style={{ color: "var(--text-secondary)" }}>{route.desc}</p>
                <p className="text-xs font-mono truncate" style={{ color: "var(--accent)" }}>
                  {routeDefault ? `${routeDefault.provider}:${routeDefault.model_id}` : "default 없음"}
                </p>
              </button>
            );
          })}
        </section>

        <section className="rounded-xl overflow-hidden" style={{ background: "var(--bg-card)", border: "1px solid var(--border)" }}>
          <div className="px-4 py-3 flex items-center justify-between gap-3 flex-wrap" style={{ borderBottom: "1px solid var(--border)" }}>
            <div>
              <h3 className="text-sm font-semibold" style={{ color: "var(--text-primary)" }}>
                {ROUTES.find((route) => route.key === activeRoute)?.label} 모델
              </h3>
              <p className="text-xs mt-1" style={{ color: "var(--text-secondary)" }}>
                현재 기본값: {defaultItem ? `${defaultItem.provider}:${defaultItem.model_id}` : "-"}
              </p>
            </div>
            <span className="text-xs" style={{ color: "var(--text-secondary)" }}>
              Updated by DB seed/API
            </span>
          </div>

          {loading ? (
            <p className="text-sm p-5" style={{ color: "var(--text-secondary)" }}>로딩 중...</p>
          ) : visibleItems.length === 0 ? (
            <p className="text-sm p-5" style={{ color: "var(--text-secondary)" }}>등록된 모델이 없습니다.</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead style={{ background: "var(--bg-hover)", color: "var(--text-secondary)" }}>
                  <tr>
                    {["기본", "상태", "Provider", "Model", "Availability", "비고", "Updated"].map((header) => (
                      <th key={header} className="text-left px-4 py-3 text-xs font-semibold whitespace-nowrap">{header}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {visibleItems.map((item) => {
                    const tone = availabilityStyle(item.availability);
                    return (
                      <tr key={`${item.route_key}:${item.provider}:${item.model_id}`} style={{ borderTop: "1px solid var(--border)" }}>
                        <td className="px-4 py-3">
                          <input
                            type="radio"
                            checked={item.is_default}
                            onChange={() => setDefault(item)}
                            aria-label={`${item.model_id} 기본 모델`}
                          />
                        </td>
                        <td className="px-4 py-3">
                          <label className="inline-flex items-center gap-2 text-xs" style={{ color: "var(--text-primary)" }}>
                            <input
                              type="checkbox"
                              checked={item.is_enabled}
                              onChange={(event) => patchItem(item, { is_enabled: event.target.checked })}
                            />
                            enabled
                          </label>
                        </td>
                        <td className="px-4 py-3">
                          <span className="text-xs font-bold px-2 py-0.5 rounded" style={{ background: "var(--bg-hover)", color: "var(--text-primary)", border: "1px solid var(--border)" }}>
                            {item.provider}
                          </span>
                        </td>
                        <td className="px-4 py-3 min-w-[260px]">
                          <p className="font-mono text-xs break-all" style={{ color: "var(--text-primary)" }}>{item.model_id}</p>
                          <p className="text-[11px] mt-1" style={{ color: "var(--text-secondary)" }}>
                            {item.display_name || item.model_id}
                            {item.execution_model_id && item.execution_model_id !== item.model_id ? ` -> ${item.execution_model_id}` : ""}
                          </p>
                        </td>
                        <td className="px-4 py-3">
                          <span className="text-[11px] px-2 py-1 rounded whitespace-nowrap" style={tone}>
                            {item.availability}
                          </span>
                          {item.verification_status && (
                            <p className="text-[11px] mt-1" style={{ color: "var(--text-secondary)" }}>{item.verification_status}</p>
                          )}
                        </td>
                        <td className="px-4 py-3 min-w-[320px]">
                          <textarea
                            value={item.notes || ""}
                            onChange={(event) => patchItem(item, { notes: event.target.value })}
                            rows={2}
                            className="w-full rounded px-2 py-1 text-xs"
                            style={{ background: "var(--bg-primary)", border: "1px solid var(--border)", color: "var(--text-primary)" }}
                          />
                        </td>
                        <td className="px-4 py-3 whitespace-nowrap text-xs" style={{ color: "var(--text-secondary)" }}>
                          <p>{formatDateTime(item.updated_at)}</p>
                          <p>{item.updated_by || "-"}</p>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </section>
      </div>
    </div>
  );
}
