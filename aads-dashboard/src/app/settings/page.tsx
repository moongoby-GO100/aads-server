"use client";
import { useEffect, useState, useCallback } from "react";
import Header from "@/components/Header";
import { api } from "@/lib/api";
import type { HealthResponse } from "@/types";

const QUICK_LINKS = [
  { label: "HANDOVER", url: "https://aads.newtalk.kr/api/v1/context/handover", desc: "현재 핸드오버 문서" },
  { label: "CEO DIRECTIVES", url: "https://aads.newtalk.kr/api/v1/context/system/ceo_directives", desc: "CEO 지시사항" },
  { label: "Public Summary", url: "https://aads.newtalk.kr/api/v1/context/public-summary", desc: "공개 요약 정보" },
  { label: "Watchdog Summary", url: "https://aads.newtalk.kr/api/v1/watchdog/summary", desc: "에러 자동감시 현황" },
  { label: "API Docs", url: "https://aads.newtalk.kr/api/v1/docs", desc: "FastAPI Swagger" },
  { label: "API Health", url: "https://aads.newtalk.kr/api/v1/health", desc: "서버 헬스체크" },
];

const SIZE_ORDER = ["XS", "S", "M", "L", "XL", "AI_REVIEW"];
const SIZE_LABELS: Record<string, string> = {
  XS: "XS (초소형)",
  S: "S (소형)",
  M: "M (중형)",
  L: "L (대형)",
  XL: "XL (초대형)",
  AI_REVIEW: "AI Review (코드 리뷰)",
};



const AVAILABLE_MODELS = [
  { group: "Claude (월정액)", models: ["claude-opus-4-6", "claude-sonnet-4-6", "claude-haiku-4-5-20251001"] },
  { group: "Codex/GPT (월정액)", models: ["codex:gpt-5.4", "codex:gpt-5.4-mini", "codex:gpt-5.3-codex"] },
  { group: "MiniMax (월정액)", models: ["litellm:minimax-m2.7", "litellm:minimax-m2.5"] },
  { group: "Groq (무료)", models: ["litellm:groq-llama-70b", "litellm:groq-qwen3-32b", "litellm:groq-kimi-k2", "litellm:groq-llama4-scout"] },
  { group: "Gemini", models: ["litellm:gemini-2.5-flash", "litellm:gemini-2.5-pro", "litellm:gemini-3-flash-preview", "litellm:gemini-3-pro-preview", "litellm:gemini-3.1-flash-lite-preview", "litellm:gemini-3.1-pro-preview"] },
  { group: "Qwen", models: ["qwen-turbo", "litellm:qwen3-coder-plus", "litellm:qwen3-235b", "litellm:qwen3-max", "litellm:qwen3-coder-flash"] },
  { group: "DeepSeek", models: ["litellm:deepseek-chat", "litellm:deepseek-reasoner"] },
  { group: "Kimi", models: ["litellm:kimi-k2", "litellm:kimi-k2.5"] },
  { group: "OpenRouter", models: ["litellm:openrouter-grok-4-fast", "litellm:openrouter-deepseek-v3"] },
];

interface ModelConfig {
  size: string;
  models: string[];
  updated_at: string | null;
  updated_by: string;
}

interface LlmKey {
  id: number;
  provider: string;
  key_name: string;
  masked_value: string;
  label: string;
  priority: number;
  is_active: boolean;
  rate_limited_until: string | null;
  last_used_at: string | null;
  notes: string;
}

type UsageScope = "exact" | "provider_observed" | "key_state_only";

interface UsageWindow {
  scope: UsageScope;
  calls: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cost_usd: number;
  error_runs: number;
  models: string[];
}

interface AccountUsageProvider {
  provider: string;
  display_name: string;
  account_count: number;
  active_account_count: number;
  rate_limited_account_count: number;
  inactive_account_count: number;
  observed_usage_5h: UsageWindow;
  observed_usage_7d: UsageWindow;
  recent_errors_24h: number;
  last_observed_at: string | null;
  last_used_at: string | null;
  last_verified_at: string | null;
  usage_scope: "exact_per_account" | "provider_observed" | "key_state_only";
  status: string;
  pressure: string;
  pressure_reason: string;
}

interface AccountUsageLatestRateLimit {
  requests_limit: number | null;
  requests_remaining: number | null;
  requests_reset: string | null;
  tokens_limit: number | null;
  tokens_remaining: number | null;
  tokens_reset: string | null;
  input_tokens_limit: number | null;
  input_tokens_remaining: number | null;
  input_tokens_reset: string | null;
  output_tokens_limit: number | null;
  output_tokens_remaining: number | null;
  output_tokens_reset: string | null;
  captured_at: string | null;
}

interface AccountUsageItem {
  id: number;
  provider: string;
  provider_display_name: string;
  key_name: string;
  label: string;
  slot: string | null;
  priority: number;
  status: string;
  pressure: string;
  pressure_reason: string;
  usage_scope: UsageScope;
  measurement_note: string;
  rate_limited_until: string | null;
  last_used_at: string | null;
  last_verified_at: string | null;
  usage_5h: UsageWindow;
  usage_7d: UsageWindow;
  recent_errors_24h: number;
  recent_errors_scope: "exact" | "provider_observed" | "none";
  latest_ratelimit: AccountUsageLatestRateLimit | null;
}

interface AccountUsageResponse {
  generated_at: string;
  summary: {
    total_accounts: number;
    active_accounts: number;
    rate_limited_accounts: number;
    inactive_accounts: number;
    provider_count: number;
  };
  providers: AccountUsageProvider[];
  accounts: AccountUsageItem[];
}

const PROVIDER_COLORS: Record<string, string> = {
  anthropic: "#d4a017",
  codex: "#0f172a",
  deepseek: "#00bcd4",
  gemini: "#4285f4",
  groq: "#ff6b6b",
  kimi: "#2563eb",
  litellm: "#334155",
  minimax: "#ef4444",
  openai: "#10a37f",
  openrouter: "#f97316",
  qwen: "#7c3aed",
};

const USAGE_SCOPE_LABELS: Record<string, string> = {
  exact: "Exact",
  exact_per_account: "Exact per-account",
  provider_observed: "Observed only",
  key_state_only: "Key state only",
};

function formatDateTime(value: string | null | undefined): string {
  if (!value) return "—";
  return new Date(value).toLocaleString("ko-KR", { timeZone: "Asia/Seoul" });
}

function formatCompactNumber(value: number): string {
  return new Intl.NumberFormat("ko-KR", { notation: "compact", maximumFractionDigits: 1 }).format(value);
}

function formatUsageHeadline(usage: UsageWindow): string {
  if (usage.scope === "exact") {
    return `${usage.calls} calls · ${formatCompactNumber(usage.total_tokens)} tok`;
  }
  if (usage.scope === "provider_observed") {
    return `${usage.calls} observed runs`;
  }
  return "Key state only";
}

function formatUsageDetail(usage: UsageWindow): string {
  if (usage.scope === "exact") {
    return `$${usage.cost_usd.toFixed(3)} · ${formatCompactNumber(usage.input_tokens)} in / ${formatCompactNumber(usage.output_tokens)} out`;
  }
  if (usage.models.length > 0) {
    return usage.models.slice(0, 2).join(", ");
  }
  return "측정치 없음";
}

function statusStyle(status: string): { background: string; color: string; border: string } {
  if (status === "rate_limited") {
    return { background: "rgba(239,68,68,0.12)", color: "var(--danger)", border: "1px solid rgba(239,68,68,0.25)" };
  }
  if (status === "inactive") {
    return { background: "rgba(148,163,184,0.12)", color: "var(--text-secondary)", border: "1px solid rgba(148,163,184,0.2)" };
  }
  if (status === "observed_only") {
    return { background: "rgba(59,130,246,0.12)", color: "var(--accent)", border: "1px solid rgba(59,130,246,0.22)" };
  }
  return { background: "rgba(34,197,94,0.12)", color: "var(--success)", border: "1px solid rgba(34,197,94,0.22)" };
}

function pressureStyle(pressure: string): { background: string; color: string; border: string } {
  if (pressure === "exhausted") {
    return { background: "rgba(220,38,38,0.16)", color: "var(--danger)", border: "1px solid rgba(220,38,38,0.28)" };
  }
  if (pressure === "critical") {
    return { background: "rgba(249,115,22,0.16)", color: "#ea580c", border: "1px solid rgba(249,115,22,0.28)" };
  }
  if (pressure === "elevated") {
    return { background: "rgba(245,158,11,0.14)", color: "#d97706", border: "1px solid rgba(245,158,11,0.24)" };
  }
  if (pressure === "observed") {
    return { background: "rgba(59,130,246,0.12)", color: "var(--accent)", border: "1px solid rgba(59,130,246,0.22)" };
  }
  if (pressure === "inactive") {
    return { background: "rgba(148,163,184,0.12)", color: "var(--text-secondary)", border: "1px solid rgba(148,163,184,0.2)" };
  }
  return { background: "rgba(34,197,94,0.12)", color: "var(--success)", border: "1px solid rgba(34,197,94,0.22)" };
}

function scopeStyle(scope: string): { background: string; color: string; border: string } {
  if (scope === "exact" || scope === "exact_per_account") {
    return { background: "rgba(34,197,94,0.12)", color: "var(--success)", border: "1px solid rgba(34,197,94,0.22)" };
  }
  if (scope === "provider_observed") {
    return { background: "rgba(59,130,246,0.12)", color: "var(--accent)", border: "1px solid rgba(59,130,246,0.22)" };
  }
  return { background: "rgba(148,163,184,0.12)", color: "var(--text-secondary)", border: "1px solid rgba(148,163,184,0.2)" };
}

function itemTone(status: string, pressure: string): { background: string; border: string } {
  if (status === "rate_limited" || pressure === "exhausted") {
    return { background: "rgba(239,68,68,0.05)", border: "1px solid rgba(239,68,68,0.22)" };
  }
  if (pressure === "critical") {
    return { background: "rgba(249,115,22,0.05)", border: "1px solid rgba(249,115,22,0.22)" };
  }
  return { background: "var(--bg-primary)", border: "1px solid var(--border)" };
}

function LlmKeyManager() {
  const [keys, setKeys] = useState<LlmKey[]>([]);
  const [loading, setLoading] = useState(true);
  const [msg, setMsg] = useState("");
  const [editId, setEditId] = useState<number | null>(null);
  const [editVal, setEditVal] = useState("");
  const [showAdd, setShowAdd] = useState(false);
  const [newKey, setNewKey] = useState({ provider: "anthropic", key_name: "", value: "", label: "", priority: 1, notes: "" });

  const load = useCallback(() => {
    setLoading(true);
    (api as any).getLlmKeys()
      .then((r: LlmKey[]) => setKeys(r))
      .catch(() => setMsg("로드 실패"))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => { load(); }, [load]);

  const flash = (m: string) => { setMsg(m); setTimeout(() => setMsg(""), 3000); };

  const toggle = async (k: LlmKey) => {
    await (api as any).updateLlmKey(k.id, { is_active: !k.is_active });
    load(); flash(k.is_active ? "비활성화됨" : "활성화됨");
  };

  const saveEdit = async (id: number) => {
    if (!editVal.trim()) return;
    await (api as any).updateLlmKey(id, { value: editVal.trim() });
    setEditId(null); setEditVal(""); load(); flash("키 값 업데이트 완료");
  };

  const addKey = async () => {
    if (!newKey.key_name || !newKey.value) { flash("key_name과 value는 필수입니다"); return; }
    await (api as any).createLlmKey(newKey);
    setShowAdd(false); setNewKey({ provider: "anthropic", key_name: "", value: "", label: "", priority: 1, notes: "" });
    load(); flash("키 추가 완료");
  };

  const byProvider = keys.reduce((acc, k) => {
    if (!acc[k.provider]) acc[k.provider] = [];
    acc[k.provider].push(k);
    return acc;
  }, {} as Record<string, LlmKey[]>);

  if (loading) return <p className="text-sm p-4" style={{ color: "var(--text-secondary)" }}>로딩 중...</p>;

  return (
    <div className="space-y-4">
      {msg && <p className="text-sm px-3 py-2 rounded" style={{ background: msg.includes("실패") ? "rgba(239,68,68,0.1)" : "rgba(34,197,94,0.1)", color: msg.includes("실패") ? "var(--danger)" : "var(--success)" }}>{msg}</p>}

      {Object.entries(byProvider).map(([provider, pKeys]) => (
        <div key={provider} className="rounded-lg overflow-hidden" style={{ border: "1px solid var(--border)" }}>
          <div className="px-4 py-2 flex items-center gap-2" style={{ background: "var(--bg-hover)" }}>
            <span className="text-xs font-bold px-2 py-0.5 rounded" style={{ background: PROVIDER_COLORS[provider] ?? "var(--accent)", color: "#fff" }}>{provider.toUpperCase()}</span>
            <span className="text-xs" style={{ color: "var(--text-secondary)" }}>{pKeys.length}개 키</span>
          </div>
          <div className="divide-y" style={{ borderColor: "var(--border)" }}>
            {pKeys.map((k) => (
              <div key={k.id} className="px-4 py-3 flex items-center gap-3 flex-wrap" style={{ background: k.is_active ? "var(--bg-primary)" : "rgba(0,0,0,0.15)", opacity: k.is_active ? 1 : 0.6 }}>
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="text-xs font-mono font-bold" style={{ color: "var(--text-primary)" }}>{k.key_name}</span>
                    {k.label && <span className="text-xs px-1.5 py-0.5 rounded" style={{ background: "var(--bg-hover)", color: "var(--text-secondary)" }}>{k.label}</span>}
                    <span className="text-xs" style={{ color: "var(--text-secondary)" }}>P{k.priority}</span>
                    {k.rate_limited_until && new Date(k.rate_limited_until) > new Date() && (
                      <span className="text-xs px-1.5 py-0.5 rounded" style={{ background: "rgba(239,68,68,0.15)", color: "var(--danger)" }}>Rate-limited</span>
                    )}
                  </div>
                  <div className="flex items-center gap-2 mt-1">
                    {editId === k.id ? (
                      <>
                        <input value={editVal} onChange={(e) => setEditVal(e.target.value)}
                          placeholder="새 키 값 입력..."
                          className="text-xs font-mono rounded px-2 py-1 flex-1"
                          style={{ background: "var(--bg-hover)", border: "1px solid var(--border)", color: "var(--text-primary)" }} />
                        <button onClick={() => saveEdit(k.id)} className="text-xs px-2 py-1 rounded" style={{ background: "var(--accent)", color: "#fff" }}>저장</button>
                        <button onClick={() => { setEditId(null); setEditVal(""); }} className="text-xs px-2 py-1 rounded" style={{ background: "var(--bg-hover)", color: "var(--text-secondary)" }}>취소</button>
                      </>
                    ) : (
                      <span className="text-xs font-mono" style={{ color: "var(--text-secondary)" }}>{k.masked_value}</span>
                    )}
                  </div>
                  {k.last_used_at && (
                    <p className="text-xs mt-0.5" style={{ color: "var(--text-secondary)", opacity: 0.7 }}>
                      최근 사용: {new Date(k.last_used_at).toLocaleString("ko-KR", { timeZone: "Asia/Seoul" })}
                    </p>
                  )}
                </div>
                <div className="flex items-center gap-2 shrink-0">
                  {editId !== k.id && (
                    <button onClick={() => { setEditId(k.id); setEditVal(""); }} className="text-xs px-2 py-1 rounded" style={{ background: "var(--bg-hover)", border: "1px solid var(--border)", color: "var(--text-primary)" }}>키 변경</button>
                  )}
                  <button onClick={() => toggle(k)} className="text-xs px-2 py-1 rounded" style={{ background: k.is_active ? "rgba(239,68,68,0.1)" : "rgba(34,197,94,0.1)", color: k.is_active ? "var(--danger)" : "var(--success)" }}>
                    {k.is_active ? "비활성화" : "활성화"}
                  </button>
                </div>
              </div>
            ))}
          </div>
        </div>
      ))}

      {showAdd ? (
        <div className="rounded-lg p-4 space-y-3" style={{ background: "var(--bg-hover)", border: "1px solid var(--border)" }}>
          <h4 className="text-sm font-bold" style={{ color: "var(--text-primary)" }}>새 키 추가</h4>
          <div className="grid grid-cols-2 gap-3">
            {[["provider", "Provider (예: anthropic)"], ["key_name", "Key Name (예: ANTHROPIC_AUTH_TOKEN_3)"], ["label", "Label (예: moong3@gmail)"], ["value", "API Key 값"]].map(([field, ph]) => (
              <input key={field} value={(newKey as any)[field]} onChange={(e) => setNewKey((p) => ({ ...p, [field]: e.target.value }))}
                placeholder={ph} type={field === "value" ? "password" : "text"}
                className="text-sm rounded px-3 py-2"
                style={{ background: "var(--bg-primary)", border: "1px solid var(--border)", color: "var(--text-primary)", gridColumn: field === "value" ? "1 / -1" : undefined }} />
            ))}
            <input type="number" value={newKey.priority} onChange={(e) => setNewKey((p) => ({ ...p, priority: +e.target.value }))}
              placeholder="우선순위 (낮을수록 먼저)"
              className="text-sm rounded px-3 py-2"
              style={{ background: "var(--bg-primary)", border: "1px solid var(--border)", color: "var(--text-primary)" }} />
          </div>
          <div className="flex gap-2">
            <button onClick={addKey} className="px-4 py-2 rounded text-sm font-bold" style={{ background: "var(--accent)", color: "#fff" }}>추가</button>
            <button onClick={() => setShowAdd(false)} className="px-4 py-2 rounded text-sm" style={{ background: "var(--bg-primary)", border: "1px solid var(--border)", color: "var(--text-secondary)" }}>취소</button>
          </div>
        </div>
      ) : (
        <button onClick={() => setShowAdd(true)} className="w-full py-2 rounded-lg text-sm font-bold border-dashed"
          style={{ border: "1px dashed var(--border)", color: "var(--accent)", background: "transparent" }}>
          + 새 LLM API 키 추가
        </button>
      )}
    </div>
  );
}

function LlmAccountUsagePanel() {
  const [data, setData] = useState<AccountUsageResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const load = useCallback(() => {
    setLoading(true);
    setError("");
    (api as any).getOpsAccountUsage()
      .then((response: AccountUsageResponse) => setData(response))
      .catch(() => setError("계정 사용량 현황을 불러오지 못했습니다."))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => { load(); }, [load]);

  if (loading && !data) {
    return <p className="text-sm p-4" style={{ color: "var(--text-secondary)" }}>로딩 중...</p>;
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <p className="text-xs" style={{ color: "var(--text-secondary)" }}>
          Anthropic은 exact per-account, 나머지 provider는 observed usage 또는 key state 기준으로 표시됩니다.
        </p>
        <button
          onClick={load}
          className="px-3 py-1.5 rounded text-xs"
          style={{ background: "var(--bg-hover)", color: "var(--text-primary)", border: "1px solid var(--border)" }}
        >
          {loading ? "새로고침 중..." : "새로고침"}
        </button>
      </div>

      {error && (
        <p className="text-sm px-3 py-2 rounded" style={{ background: "rgba(239,68,68,0.1)", color: "var(--danger)" }}>
          {error}
        </p>
      )}

      {data && (
        <>
          <div className="grid grid-cols-2 xl:grid-cols-4 gap-3">
            {[
              ["전체 계정 수", String(data.summary.total_accounts)],
              ["활성 계정 수", String(data.summary.active_accounts)],
              ["Rate-limited 계정 수", String(data.summary.rate_limited_accounts)],
              ["생성 시각", formatDateTime(data.generated_at)],
            ].map(([label, value], idx) => (
              <div
                key={label}
                className="rounded-lg p-3"
                style={{
                  background: idx === 2 ? "rgba(239,68,68,0.06)" : "var(--bg-hover)",
                  border: idx === 2 ? "1px solid rgba(239,68,68,0.18)" : "1px solid var(--border)",
                }}
              >
                <p className="text-[11px] mb-1" style={{ color: "var(--text-secondary)" }}>{label}</p>
                <p className="text-sm font-semibold break-all" style={{ color: idx === 2 ? "var(--danger)" : "var(--text-primary)" }}>
                  {value}
                </p>
              </div>
            ))}
          </div>

          <div className="space-y-3">
            <div className="flex items-center justify-between gap-3 flex-wrap">
              <h3 className="text-sm font-semibold" style={{ color: "var(--text-primary)" }}>Provider Summary</h3>
              <span className="text-xs" style={{ color: "var(--text-secondary)" }}>{data.summary.provider_count}개 provider</span>
            </div>
            <div className="grid grid-cols-1 xl:grid-cols-2 gap-3">
              {data.providers.map((provider) => {
                const tone = itemTone(provider.status, provider.pressure);
                const providerStatus = statusStyle(provider.status);
                const providerPressure = pressureStyle(provider.pressure);
                const scopeTone = scopeStyle(provider.usage_scope);
                return (
                  <div key={provider.provider} className="rounded-xl p-4 space-y-3" style={tone}>
                    <div className="flex items-start justify-between gap-3 flex-wrap">
                      <div className="min-w-0">
                        <div className="flex items-center gap-2 flex-wrap mb-2">
                          <span
                            className="text-[11px] font-bold px-2 py-0.5 rounded"
                            style={{ background: PROVIDER_COLORS[provider.provider] ?? "var(--accent)", color: "#fff" }}
                          >
                            {provider.display_name}
                          </span>
                          <span className="text-[11px] px-2 py-0.5 rounded" style={scopeTone}>
                            {USAGE_SCOPE_LABELS[provider.usage_scope] || provider.usage_scope}
                          </span>
                          <span className="text-[11px] px-2 py-0.5 rounded" style={providerStatus}>
                            {provider.status}
                          </span>
                          <span className="text-[11px] px-2 py-0.5 rounded" style={providerPressure}>
                            {provider.pressure}
                          </span>
                        </div>
                        <p className="text-xs" style={{ color: "var(--text-secondary)" }}>{provider.pressure_reason}</p>
                      </div>
                      <div className="text-right text-xs" style={{ color: "var(--text-secondary)" }}>
                        <p>Observed {formatDateTime(provider.last_observed_at)}</p>
                      </div>
                    </div>

                    <div className="grid grid-cols-3 gap-2">
                      {[
                        ["계정", provider.account_count, "var(--text-primary)"],
                        ["활성", provider.active_account_count, "var(--success)"],
                        ["제한", provider.rate_limited_account_count, provider.rate_limited_account_count > 0 ? "var(--danger)" : "var(--text-primary)"],
                      ].map(([label, value, color]) => (
                        <div key={String(label)} className="rounded-lg p-3" style={{ background: "var(--bg-hover)" }}>
                          <p className="text-[11px] mb-1" style={{ color: "var(--text-secondary)" }}>{label}</p>
                          <p className="text-sm font-semibold" style={{ color: String(color) }}>{value}</p>
                        </div>
                      ))}
                    </div>

                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
                      {[
                        ["Observed 5h", provider.observed_usage_5h],
                        ["Observed 7d", provider.observed_usage_7d],
                      ].map(([label, usage]) => (
                        <div key={String(label)} className="rounded-lg p-3" style={{ background: "var(--bg-hover)" }}>
                          <p className="text-[11px] mb-1" style={{ color: "var(--text-secondary)" }}>{label}</p>
                          <p className="text-sm font-semibold" style={{ color: "var(--text-primary)" }}>
                            {formatUsageHeadline(usage as UsageWindow)}
                          </p>
                          <p className="text-[11px] mt-1 break-all" style={{ color: "var(--text-secondary)" }}>
                            {formatUsageDetail(usage as UsageWindow)}
                          </p>
                        </div>
                      ))}
                    </div>

                    <div className="flex items-center justify-between gap-3 flex-wrap text-xs" style={{ color: "var(--text-secondary)" }}>
                      <span>최근 에러 24h: {provider.recent_errors_24h}</span>
                      <span>최근 검증: {formatDateTime(provider.last_verified_at)}</span>
                    </div>
                  </div>
                );
              })}
            </div>
          </div>

          <div className="space-y-3">
            <div className="flex items-center justify-between gap-3 flex-wrap">
              <h3 className="text-sm font-semibold" style={{ color: "var(--text-primary)" }}>계정 리스트</h3>
              <span className="text-xs" style={{ color: "var(--text-secondary)" }}>{data.accounts.length}개 계정</span>
            </div>

            {data.accounts.map((account) => {
              const tone = itemTone(account.status, account.pressure);
              const accountStatus = statusStyle(account.status);
              const accountPressure = pressureStyle(account.pressure);
              const accountScope = scopeStyle(account.usage_scope);
              return (
                <div key={account.id} className="rounded-xl p-4 space-y-3" style={tone}>
                  <div className="flex items-start justify-between gap-3 flex-wrap">
                    <div className="min-w-0">
                      <div className="flex items-center gap-2 flex-wrap mb-2">
                        <span
                          className="text-[11px] font-bold px-2 py-0.5 rounded"
                          style={{ background: PROVIDER_COLORS[account.provider] ?? "var(--accent)", color: "#fff" }}
                        >
                          {account.provider_display_name}
                        </span>
                        <span className="text-[11px] px-2 py-0.5 rounded" style={accountScope}>
                          {USAGE_SCOPE_LABELS[account.usage_scope] || account.usage_scope}
                        </span>
                        <span className="text-[11px] px-2 py-0.5 rounded" style={accountStatus}>
                          {account.status}
                        </span>
                        <span className="text-[11px] px-2 py-0.5 rounded" style={accountPressure}>
                          {account.pressure}
                        </span>
                        {account.slot && (
                          <span className="text-[11px] px-2 py-0.5 rounded" style={{ background: "var(--bg-hover)", color: "var(--text-secondary)", border: "1px solid var(--border)" }}>
                            slot {account.slot}
                          </span>
                        )}
                      </div>
                      <p className="text-sm font-semibold break-all" style={{ color: "var(--text-primary)" }}>
                        {account.label || account.key_name}
                      </p>
                      {account.label && (
                        <p className="text-xs font-mono break-all mt-1" style={{ color: "var(--text-secondary)" }}>
                          {account.key_name}
                        </p>
                      )}
                    </div>
                    <div className="text-right text-xs" style={{ color: "var(--text-secondary)" }}>
                      <p>P{account.priority}</p>
                      <p>{account.measurement_note}</p>
                    </div>
                  </div>

                  <p className="text-xs" style={{ color: "var(--text-secondary)" }}>{account.pressure_reason}</p>

                  <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-5 gap-2">
                    {[
                      ["Usage 5h", formatUsageHeadline(account.usage_5h), formatUsageDetail(account.usage_5h)],
                      ["Usage 7d", formatUsageHeadline(account.usage_7d), formatUsageDetail(account.usage_7d)],
                      ["Recent Errors 24h", String(account.recent_errors_24h), account.recent_errors_scope === "exact" ? "Exact per-account" : account.recent_errors_scope === "provider_observed" ? "Provider observed" : "No observed errors"],
                      ["Last Used", formatDateTime(account.last_used_at), account.rate_limited_until ? `Rate limit until ${formatDateTime(account.rate_limited_until)}` : "—"],
                      ["Last Verified", formatDateTime(account.last_verified_at), account.latest_ratelimit?.captured_at ? `RL sample ${formatDateTime(account.latest_ratelimit.captured_at)}` : "—"],
                    ].map(([label, value, detail]) => (
                      <div key={String(label)} className="rounded-lg p-3" style={{ background: "var(--bg-hover)" }}>
                        <p className="text-[11px] mb-1" style={{ color: "var(--text-secondary)" }}>{label}</p>
                        <p className="text-sm font-semibold break-all" style={{ color: "var(--text-primary)" }}>{value}</p>
                        <p className="text-[11px] mt-1 break-all" style={{ color: "var(--text-secondary)" }}>{detail}</p>
                      </div>
                    ))}
                  </div>

                  {account.latest_ratelimit && (
                    <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 gap-2">
                      {[
                        ["Requests Remaining", account.latest_ratelimit.requests_remaining, account.latest_ratelimit.requests_limit],
                        ["Tokens Remaining", account.latest_ratelimit.tokens_remaining, account.latest_ratelimit.tokens_limit],
                        ["Input Remaining", account.latest_ratelimit.input_tokens_remaining, account.latest_ratelimit.input_tokens_limit],
                        ["Output Remaining", account.latest_ratelimit.output_tokens_remaining, account.latest_ratelimit.output_tokens_limit],
                      ].map(([label, remaining, limit]) => (
                        <div key={String(label)} className="rounded-lg p-3" style={{ background: "rgba(34,197,94,0.05)", border: "1px solid rgba(34,197,94,0.12)" }}>
                          <p className="text-[11px] mb-1" style={{ color: "var(--text-secondary)" }}>{label}</p>
                          <p className="text-sm font-semibold" style={{ color: "var(--text-primary)" }}>
                            {remaining ?? "—"}{limit ? ` / ${limit}` : ""}
                          </p>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </>
      )}
    </div>
  );
}

function RunnerModelConfig() {
  const [configs, setConfigs] = useState<ModelConfig[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState("");
  const [newModel, setNewModel] = useState<Record<string, string>>({});

  const load = useCallback(() => {
    setLoading(true);
    api.getRunnerModels()
      .then((r: any) => {
        const sorted = (r.configs || []).sort(
          (a: ModelConfig, b: ModelConfig) => SIZE_ORDER.indexOf(a.size) - SIZE_ORDER.indexOf(b.size)
        );
        setConfigs(sorted);
      })
      .catch(() => setMsg("로드 실패"))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => { load(); }, [load]);

  const moveModel = (sizeIdx: number, modelIdx: number, dir: -1 | 1) => {
    const next = [...configs];
    const arr = [...next[sizeIdx].models];
    const target = modelIdx + dir;
    if (target < 0 || target >= arr.length) return;
    [arr[modelIdx], arr[target]] = [arr[target], arr[modelIdx]];
    next[sizeIdx] = { ...next[sizeIdx], models: arr };
    setConfigs(next);
  };

  const removeModel = (sizeIdx: number, modelIdx: number) => {
    const next = [...configs];
    const arr = next[sizeIdx].models.filter((_, i) => i !== modelIdx);
    if (arr.length === 0) return;
    next[sizeIdx] = { ...next[sizeIdx], models: arr };
    setConfigs(next);
  };

  const addModel = (sizeIdx: number) => {
    const size = configs[sizeIdx].size;
    const val = (newModel[size] || "").trim();
    if (!val) return;
    const next = [...configs];
    next[sizeIdx] = { ...next[sizeIdx], models: [...next[sizeIdx].models, val] };
    setConfigs(next);
    setNewModel((p) => ({ ...p, [size]: "" }));
  };

  const save = async () => {
    setSaving(true);
    setMsg("");
    try {
      const payload = configs.map((c) => ({ size: c.size, models: c.models }));
      const res: any = await api.updateRunnerModels(payload);
      setMsg(res.message || "저장 완료");
      load();
    } catch {
      setMsg("저장 실패");
    } finally {
      setSaving(false);
    }
  };

  if (loading) return <p className="text-sm p-4" style={{ color: "var(--text-secondary)" }}>로딩 중...</p>;

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
      {configs.map((cfg, si) => (
        <div key={cfg.size} className="rounded-lg p-4" style={{ background: "var(--bg-hover)", border: "1px solid var(--border)" }}>
          <div className="flex items-center justify-between mb-3">
            <h3 className="text-sm font-bold" style={{ color: cfg.size === "AI_REVIEW" ? "var(--accent)" : "var(--text-primary)" }}>
              {SIZE_LABELS[cfg.size] || cfg.size}
            </h3>
            {cfg.updated_at && (
              <span className="text-xs" style={{ color: "var(--text-secondary)" }}>
                {new Date(cfg.updated_at).toLocaleString("ko-KR", { timeZone: "Asia/Seoul" })}
              </span>
            )}
          </div>
          <div className="space-y-2">
            {cfg.models.map((model, mi) => (
              <div key={mi} className="flex items-center gap-2 rounded px-3 py-2" style={{ background: "var(--bg-primary)" }}>
                <span className="text-xs font-bold w-5 text-center" style={{ color: mi === 0 ? "var(--success)" : "var(--text-secondary)" }}>
                  {mi + 1}
                </span>
                <span className="flex-1 text-sm font-mono truncate" style={{ color: "var(--text-primary)" }}>{model}</span>
                <button onClick={() => moveModel(si, mi, -1)} disabled={mi === 0}
                  className="px-1.5 py-0.5 rounded text-xs disabled:opacity-30" style={{ background: "var(--bg-hover)" }}>▲</button>
                <button onClick={() => moveModel(si, mi, 1)} disabled={mi === cfg.models.length - 1}
                  className="px-1.5 py-0.5 rounded text-xs disabled:opacity-30" style={{ background: "var(--bg-hover)" }}>▼</button>
                <button onClick={() => removeModel(si, mi)} disabled={cfg.models.length <= 1}
                  className="px-1.5 py-0.5 rounded text-xs disabled:opacity-30" style={{ color: "var(--danger)" }}>✕</button>
              </div>
            ))}
          </div>
          <div className="flex gap-2 mt-2">
            <select
              value={newModel[cfg.size] || ""}
              onChange={(e) => setNewModel((p) => ({ ...p, [cfg.size]: e.target.value }))}
              className="flex-1 rounded px-3 py-1.5 text-sm font-mono"
              style={{ background: "var(--bg-primary)", border: "1px solid var(--border)", color: "var(--text-primary)" }}
            >
              <option value="">모델 선택...</option>
              {AVAILABLE_MODELS.map((g) => (
                <optgroup key={g.group} label={g.group}>
                  {g.models.filter((m) => !cfg.models.includes(m)).map((m) => (
                    <option key={m} value={m}>{m}</option>
                  ))}
                </optgroup>
              ))}
            </select>
            <button onClick={() => addModel(si)} className="px-3 py-1.5 rounded text-xs font-bold"
              style={{ background: "var(--accent)", color: "#fff" }}>추가</button>
          </div>
        </div>
      ))}
      </div>
      <div className="flex items-center gap-3 pt-2">
        <button onClick={save} disabled={saving}
          className="px-5 py-2 rounded-lg text-sm font-bold"
          style={{ background: "var(--accent)", color: "#fff", opacity: saving ? 0.6 : 1 }}>
          {saving ? "저장 중..." : "설정 저장"}
        </button>
        <button onClick={load} className="px-4 py-2 rounded-lg text-sm"
          style={{ background: "var(--bg-hover)", color: "var(--text-secondary)", border: "1px solid var(--border)" }}>초기화</button>
        {msg && <span className="text-sm" style={{ color: msg.includes("실패") ? "var(--danger)" : "var(--success)" }}>{msg}</span>}
      </div>
    </div>
  );
}

export default function SettingsPage() {
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    api.getHealth()
      .then(setHealth)
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  const ok = (v: boolean | undefined) =>
    v === undefined ? "var(--text-secondary)" : v ? "var(--success)" : "var(--danger)";

  return (
    <div className="flex flex-col h-full" style={{ background: "var(--bg-primary)" }}>
      <Header title="Settings" />
      <div className="flex-1 p-3 md:p-6 overflow-auto space-y-5">

        {/* 러너 모델 설정 */}
        <section className="rounded-xl p-5" style={{ background: "var(--bg-card)", border: "1px solid var(--border)" }}>
          <h2 className="text-sm font-semibold mb-1" style={{ color: "var(--text-primary)" }}>러너 모델 우선순위</h2>
          <p className="text-xs mb-4" style={{ color: "var(--text-secondary)" }}>
            Size별 모델 실행 순서를 설정합니다. 1순위 실패 시 다음 순위로 자동 폴백됩니다.
          </p>
          <RunnerModelConfig />
        </section>
        {/* LLM API 키 관리 (AADS-188) */}
        <section className="rounded-xl p-5" style={{ background: "var(--bg-card)", border: "1px solid var(--border)" }}>
          <h2 className="text-sm font-semibold mb-1" style={{ color: "var(--text-primary)" }}>LLM API 키 관리</h2>
          <p className="text-xs mb-4" style={{ color: "var(--text-secondary)" }}>
            Provider별 API 키 조회/수정/활성화 관리. 키 값은 마스킹 표시됩니다.
          </p>
          <LlmKeyManager />
        </section>

        <section className="rounded-xl p-5" style={{ background: "var(--bg-card)", border: "1px solid var(--border)" }}>
          <h2 className="text-sm font-semibold mb-1" style={{ color: "var(--text-primary)" }}>계정별 사용량 현황</h2>
          <p className="text-xs mb-4" style={{ color: "var(--text-secondary)" }}>
            계정 상태, provider별 observed usage, Anthropic exact per-account usage를 한 화면에서 확인합니다.
          </p>
          <LlmAccountUsagePanel />
        </section>



        {/* API 상태 요약 */}
        <section className="rounded-xl p-5" style={{ background: "var(--bg-card)", border: "1px solid var(--border)" }}>
          <h2 className="text-sm font-semibold mb-4" style={{ color: "var(--text-primary)" }}>시스템 상태</h2>
          {loading ? (
            <p className="text-sm" style={{ color: "var(--text-secondary)" }}>로딩 중...</p>
          ) : health ? (
            <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
              <div className="rounded-lg p-3" style={{ background: "var(--bg-hover)" }}>
                <p className="text-xs mb-1" style={{ color: "var(--text-secondary)" }}>서버 상태</p>
                <p className="font-bold" style={{ color: ok(health.status === "ok") }}>
                  {health.status?.toUpperCase() ?? "UNKNOWN"}
                </p>
              </div>
              <div className="rounded-lg p-3" style={{ background: "var(--bg-hover)" }}>
                <p className="text-xs mb-1" style={{ color: "var(--text-secondary)" }}>Graph DB</p>
                <p className="font-bold" style={{ color: ok(health.graph_ready) }}>
                  {health.graph_ready ? "READY" : "LOADING"}
                </p>
              </div>
              <div className="rounded-lg p-3" style={{ background: "var(--bg-hover)" }}>
                <p className="text-xs mb-1" style={{ color: "var(--text-secondary)" }}>API 버전</p>
                <p className="font-bold" style={{ color: "var(--text-primary)" }}>
                  {health.version ?? "—"}
                </p>
              </div>
            </div>
          ) : (
            <p className="text-sm font-bold" style={{ color: "var(--danger)" }}>서버 응답 없음</p>
          )}
        </section>

        {/* 버전 / 인프라 정보 */}
        <section className="rounded-xl p-5" style={{ background: "var(--bg-card)", border: "1px solid var(--border)" }}>
          <h2 className="text-sm font-semibold mb-4" style={{ color: "var(--text-primary)" }}>버전 정보</h2>
          <div className="space-y-2 text-sm">
            {[
              ["Dashboard", "v0.5.3 (Runner Model + AI Review Config)"],
              ["HANDOVER", "v5.22 (T-038 Watchdog)"],
              ["서버", "68 (aads.newtalk.kr)"],
              ["API Base", "https://aads.newtalk.kr/api/v1"],
              ["API Version", health?.version ?? "—"],
              ["DB", "PostgreSQL 15 (aads-postgres:5433)"],
              ["Watchdog", "aads-watchdog.service · 30초 주기"],
            ].map(([label, value]) => (
              <div key={label} className="flex justify-between py-1"
                style={{ borderBottom: "1px solid var(--border)" }}>
                <span style={{ color: "var(--text-secondary)" }}>{label}</span>
                <span style={{ color: "var(--text-primary)", fontFamily: "monospace", fontSize: 12 }}>{value}</span>
              </div>
            ))}
          </div>
        </section>

        {/* 빠른 링크 */}
        <section className="rounded-xl p-5" style={{ background: "var(--bg-card)", border: "1px solid var(--border)" }}>
          <h2 className="text-sm font-semibold mb-4" style={{ color: "var(--text-primary)" }}>빠른 링크</h2>
          <div className="grid grid-cols-2 sm:grid-cols-3 gap-3">
            {QUICK_LINKS.map((link) => (
              <a key={link.label}
                href={link.url}
                target="_blank"
                rel="noopener noreferrer"
                className="rounded-lg p-3 block transition-colors"
                style={{ background: "var(--bg-hover)", border: "1px solid var(--border)" }}
              >
                <p className="text-sm font-semibold mb-0.5" style={{ color: "var(--accent)" }}>{link.label}</p>
                <p className="text-xs" style={{ color: "var(--text-secondary)" }}>{link.desc}</p>
              </a>
            ))}
          </div>
        </section>

      </div>
    </div>
  );
}
