"""AADS-188: LLM 키 관리 UI — api.ts + settings/page.tsx 패치."""
import shutil
from pathlib import Path

DASH = Path("/root/aads/aads-dashboard/src")
API_TS = DASH / "lib/api.ts"
SETTINGS_TSX = DASH / "app/settings/page.tsx"

# ── 1. api.ts — LLM 키 API 함수 추가 ──────────────────────────────────────
api_old = '  getTokenProfile: () => request<any>("/admin/prompts/token-profile"),\n};'
api_new = '''  getTokenProfile: () => request<any>("/admin/prompts/token-profile"),

  // LLM API 키 관리 (AADS-188)
  getLlmKeys: () => request<any[]>("/llm-keys"),
  createLlmKey: (data: { provider: string; key_name: string; value: string; label?: string; priority?: number; notes?: string }) =>
    request<any>("/llm-keys", { method: "POST", body: JSON.stringify(data) }),
  updateLlmKey: (id: number, data: Partial<{ value: string; label: string; priority: number; is_active: boolean; notes: string }>) =>
    request<any>(`/llm-keys/${id}`, { method: "PUT", body: JSON.stringify(data) }),
  deleteLlmKey: (id: number) => request<any>(`/llm-keys/${id}`, { method: "DELETE" }),
};'''

txt = API_TS.read_text()
assert api_old in txt, "api.ts 패턴 미발견"
shutil.copy(API_TS, str(API_TS) + ".bak_aads")
API_TS.write_text(txt.replace(api_old, api_new))
print("✅ api.ts 패치 완료")

# ── 2. settings/page.tsx — LlmKeyManager 컴포넌트 + 섹션 추가 ───────────────
LLM_COMPONENT = '''
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

const PROVIDER_COLORS: Record<string, string> = {
  anthropic: "#d4a017",
  gemini: "#4285f4",
  deepseek: "#00bcd4",
  groq: "#ff6b6b",
  openai: "#10a37f",
};

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
'''

LLM_SECTION = '''
        {/* LLM API 키 관리 (AADS-188) */}
        <section className="rounded-xl p-5" style={{ background: "var(--bg-card)", border: "1px solid var(--border)" }}>
          <h2 className="text-sm font-semibold mb-1" style={{ color: "var(--text-primary)" }}>LLM API 키 관리</h2>
          <p className="text-xs mb-4" style={{ color: "var(--text-secondary)" }}>
            Provider별 API 키 조회/수정/활성화 관리. 키 값은 마스킹 표시됩니다.
          </p>
          <LlmKeyManager />
        </section>

'''

page_txt = SETTINGS_TSX.read_text()

# LlmKeyManager 컴포넌트를 RunnerModelConfig 함수 앞에 삽입
RUNNER_FUNC_START = "\nfunction RunnerModelConfig()"
assert RUNNER_FUNC_START in page_txt, "RunnerModelConfig 함수 미발견"
if "function LlmKeyManager()" in page_txt:
    print("⚠️ LlmKeyManager 이미 존재 — 스킵")
else:
    shutil.copy(SETTINGS_TSX, str(SETTINGS_TSX) + ".bak_aads")
    page_txt = page_txt.replace(RUNNER_FUNC_START, LLM_COMPONENT + RUNNER_FUNC_START)

    # LLM 섹션을 러너 모델 섹션 바로 아래에 삽입
    RUNNER_SECTION_END = "          <RunnerModelConfig />\n        </section>"
    assert RUNNER_SECTION_END in page_txt, "RunnerModelConfig 섹션 종료 미발견"
    page_txt = page_txt.replace(RUNNER_SECTION_END, RUNNER_SECTION_END + LLM_SECTION)

    SETTINGS_TSX.write_text(page_txt)
    print("✅ settings/page.tsx 패치 완료")
