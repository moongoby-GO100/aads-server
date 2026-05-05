export interface ScreenerCondition {
  field: string;
  op: string;
  value: string | number | Array<string | number>;
}

export interface ScreenerSearchPayload {
  conditions: ScreenerCondition[];
  sort_by?: string;
  sort_order?: "asc" | "desc";
  page?: number;
  limit?: number;
  base_date?: string | null;
  date_from?: string | null;
  date_to?: string | null;
  rank_limit?: number | null;
  rank_filters?: Array<{ sort_by: string; sort_order: "asc" | "desc"; limit: number }> | null;
  exclude?: string[];
}

function getToken() {
  if (typeof window === "undefined") return "";
  return localStorage.getItem("token") || localStorage.getItem("access_token") || "";
}

async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const token = getToken();
  const res = await fetch(path, {
    ...init,
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(init?.headers || {}),
    },
  });
  if (res.status === 401) {
    if (typeof window !== "undefined") window.location.href = "/auth/login";
    throw new Error("인증이 필요합니다.");
  }
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    throw new Error(body?.detail || body?.message || `HTTP ${res.status}`);
  }
  return res.json();
}

export const getFilters = () => apiFetch<any>("/api/go100/screener/filters");

export const getSectors = () => apiFetch<any>("/api/go100/screener/sectors");

export const searchStocks = (filters: any[], sortBy = "market_cap", limit = 50) =>
  apiFetch<any>("/api/go100/screener/search", {
    method: "POST",
    body: JSON.stringify({ filters, sort_by: sortBy, limit }),
  });

export const getAdvancedScreenerMeta = () => apiFetch<any>("/api/v4/stock-screener/meta");

export const searchAdvancedStocks = (payload: ScreenerSearchPayload) =>
  apiFetch<any>("/api/v4/stock-screener/search", {
    method: "POST",
    body: JSON.stringify(payload),
  });
