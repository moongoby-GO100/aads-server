"use client";
import { useEffect, useState, useCallback } from "react";
import Header from "@/components/Header";
import { api } from "@/lib/api";

type Tab = "dashboard" | "editor" | "preview" | "tokens";

interface Section {
  chars: number;
  est_tokens: number;
}

interface Workspace {
  id: string;
  name: string;
  system_prompt: string;
  chars: number;
  est_tokens: number;
  color: string;
  icon: string;
  updated_at: string | null;
}

interface IntentGroup {
  intents: string[];
  skip: string[];
}

export default function PromptsPage() {
  const [tab, setTab] = useState<Tab>("dashboard");
  const [sections, setSections] = useState<Record<string, Section>>({});
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [intentGroups, setIntentGroups] = useState<Record<string, IntentGroup>>({});
  const [liteIntents, setLiteIntents] = useState<string[]>([]);
  const [noToolsIntents, setNoToolsIntents] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);

  // Editor state
  const [editWs, setEditWs] = useState<Workspace | null>(null);
  const [editContent, setEditContent] = useState("");
  const [saving, setSaving] = useState(false);

  // Preview state
  const [previewWs, setPreviewWs] = useState("CEO");
  const [previewIntent, setPreviewIntent] = useState("directive");
  const [previewResult, setPreviewResult] = useState<any>(null);
  const [previewing, setPreviewing] = useState(false);

  // Token profile state
  const [tokenProfile, setTokenProfile] = useState<any>(null);

  const loadData = useCallback(async () => {
    setLoading(true);
    try {
      const [secRes, wsRes, igRes] = await Promise.all([
        api.getPromptSections(),
        api.getWorkspacePrompts(),
        api.getPromptIntentGroups(),
      ]);
      setSections(secRes.sections || {});
      setWorkspaces(wsRes.workspaces || []);
      setIntentGroups(igRes.groups || {});
      setLiteIntents(igRes.lite_intents || []);
      setNoToolsIntents(igRes.no_tools_intents || []);
    } catch (e) {
      console.error("Load failed:", e);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { loadData(); }, [loadData]);

  const handleSave = async () => {
    if (!editWs) return;
    setSaving(true);
    try {
      await api.updateWorkspacePrompt(editWs.id, editContent);
      await loadData();
      setEditWs(null);
    } catch (e) {
      alert("저장 실패: " + e);
    } finally {
      setSaving(false);
    }
  };

  const handlePreview = async () => {
    setPreviewing(true);
    try {
      const res = await api.previewPrompt(previewWs, previewIntent);
      setPreviewResult(res);
    } catch (e) {
      console.error(e);
    } finally {
      setPreviewing(false);
    }
  };

  const loadTokenProfile = async () => {
    try {
      const res = await api.getTokenProfile();
      setTokenProfile(res);
    } catch (e) {
      console.error(e);
    }
  };

  useEffect(() => {
    if (tab === "tokens" && !tokenProfile) loadTokenProfile();
  }, [tab, tokenProfile]);

  const tabs: { key: Tab; label: string; icon: string }[] = [
    { key: "dashboard", label: "대시보드", icon: "📊" },
    { key: "editor", label: "편집기", icon: "✏️" },
    { key: "preview", label: "미리보기", icon: "🔍" },
    { key: "tokens", label: "토큰 프로파일", icon: "📈" },
  ];

  const cardStyle = {
    background: "var(--bg-card)",
    border: "1px solid var(--border)",
    borderRadius: "8px",
    padding: "16px",
  };

  const btnStyle = (active?: boolean) => ({
    padding: "8px 16px",
    borderRadius: "6px",
    border: "none",
    cursor: "pointer" as const,
    fontWeight: active ? 600 : 400,
    background: active ? "var(--accent)" : "var(--bg-hover)",
    color: active ? "#fff" : "var(--text-primary)",
  });

  const allIntents = ["greeting", "casual", "search", "code_task", "strategy", "directive", "status_check", "pipeline_runner"];

  return (
    <div className="flex flex-col h-full" style={{ background: "var(--bg-primary)" }}>
      <Header title="시스템 프롬프트 관리" />
      <div className="flex-1 p-3 md:p-6 overflow-auto">
        {/* Tabs */}
        <div className="flex gap-2 mb-4 flex-wrap">
          {tabs.map((t) => (
            <button key={t.key} onClick={() => setTab(t.key)} style={btnStyle(tab === t.key)}>
              {t.icon} {t.label}
            </button>
          ))}
        </div>

        {loading ? (
          <div style={{ color: "var(--text-secondary)", textAlign: "center", padding: "40px" }}>로딩 중...</div>
        ) : (
          <>
            {/* ─── 대시보드 ─── */}
            {tab === "dashboard" && (
              <div className="grid gap-4">
                {/* 코드 섹션 */}
                <div style={cardStyle}>
                  <h3 style={{ color: "var(--text-primary)", marginBottom: "12px", fontSize: "16px", fontWeight: 600 }}>
                    코드 섹션 (system_prompt_v2.py)
                  </h3>
                  <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-3">
                    {Object.entries(sections)
                      .filter(([k]) => !k.startsWith("ROLE_") && !k.startsWith("CAP_"))
                      .sort(([, a], [, b]) => b.est_tokens - a.est_tokens)
                      .map(([name, s]) => (
                        <div key={name} style={{ ...cardStyle, padding: "12px" }}>
                          <div style={{ color: "var(--text-secondary)", fontSize: "11px" }}>{name}</div>
                          <div style={{ color: "var(--accent)", fontSize: "20px", fontWeight: 700 }}>~{s.est_tokens}tok</div>
                          <div style={{ color: "var(--text-secondary)", fontSize: "11px" }}>{s.chars.toLocaleString()} chars</div>
                        </div>
                      ))}
                  </div>
                </div>

                {/* 워크스페이스 역할 */}
                <div style={cardStyle}>
                  <h3 style={{ color: "var(--text-primary)", marginBottom: "12px", fontSize: "16px", fontWeight: 600 }}>
                    워크스페이스별 역할 (WS_ROLES)
                  </h3>
                  <table style={{ width: "100%", borderCollapse: "collapse" }}>
                    <thead>
                      <tr style={{ borderBottom: "1px solid var(--border)" }}>
                        {["워크스페이스", "ROLE 토큰", "CAP 토큰", "DB 프롬프트", "합계"].map((h) => (
                          <th key={h} style={{ padding: "8px", textAlign: "left", color: "var(--text-secondary)", fontSize: "12px" }}>{h}</th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {workspaces.map((ws) => {
                        const roleKey = `ROLE_${ws.name.replace(/\[|\]/g, "").split(" ")[0].replace(/\(.*\)/, "")}`;
                        const capKey = `CAP_${ws.name.replace(/\[|\]/g, "").split(" ")[0].replace(/\(.*\)/, "")}`;
                        const roleTok = sections[roleKey]?.est_tokens || 0;
                        const capTok = sections[capKey]?.est_tokens || 0;
                        return (
                          <tr key={ws.id} style={{ borderBottom: "1px solid var(--border)" }}>
                            <td style={{ padding: "8px", color: "var(--text-primary)", fontSize: "13px" }}>
                              {ws.icon} {ws.name}
                            </td>
                            <td style={{ padding: "8px", color: "var(--accent)", fontSize: "13px" }}>~{roleTok}</td>
                            <td style={{ padding: "8px", color: "var(--accent)", fontSize: "13px" }}>~{capTok}</td>
                            <td style={{ padding: "8px", color: "var(--text-secondary)", fontSize: "13px" }}>~{ws.est_tokens}tok ({ws.chars}자)</td>
                            <td style={{ padding: "8px", color: "var(--success)", fontSize: "13px", fontWeight: 600 }}>
                              ~{roleTok + capTok + ws.est_tokens}
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>

                {/* 인텐트 그룹 */}
                <div style={cardStyle}>
                  <h3 style={{ color: "var(--text-primary)", marginBottom: "12px", fontSize: "16px", fontWeight: 600 }}>
                    인텐트 그룹 (Phase 2 Adaptive Prompt)
                  </h3>
                  <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
                    {Object.entries(intentGroups).map(([name, g]) => (
                      <div key={name} style={{ ...cardStyle, padding: "12px" }}>
                        <div style={{ color: "var(--accent)", fontWeight: 600, marginBottom: "6px" }}>{name}</div>
                        <div style={{ color: "var(--text-secondary)", fontSize: "11px", marginBottom: "4px" }}>
                          Skip: {g.skip.length > 0 ? g.skip.join(", ") : "없음 (전체 포함)"}
                        </div>
                        <div style={{ color: "var(--text-secondary)", fontSize: "11px" }}>
                          인텐트 {g.intents.length}개: {g.intents.slice(0, 4).join(", ")}{g.intents.length > 4 ? "..." : ""}
                        </div>
                      </div>
                    ))}
                    <div style={{ ...cardStyle, padding: "12px", borderColor: "var(--warning)" }}>
                      <div style={{ color: "var(--warning)", fontWeight: 600, marginBottom: "6px" }}>lite (경량)</div>
                      <div style={{ color: "var(--text-secondary)", fontSize: "11px" }}>
                        Skip: 도구+규칙+가이드+진화 (행동원칙+역할만)
                      </div>
                      <div style={{ color: "var(--text-secondary)", fontSize: "11px" }}>
                        인텐트: {liteIntents.join(", ")}
                      </div>
                    </div>
                    <div style={{ ...cardStyle, padding: "12px", borderColor: "var(--danger)" }}>
                      <div style={{ color: "var(--danger)", fontWeight: 600, marginBottom: "6px" }}>no_tools (도구 제외)</div>
                      <div style={{ color: "var(--text-secondary)", fontSize: "11px" }}>
                        인텐트: {noToolsIntents.join(", ")}
                      </div>
                    </div>
                  </div>
                </div>
              </div>
            )}

            {/* ─── 편집기 ─── */}
            {tab === "editor" && (
              <div className="grid gap-4">
                <div style={cardStyle}>
                  <h3 style={{ color: "var(--text-primary)", marginBottom: "12px", fontSize: "16px", fontWeight: 600 }}>
                    DB 워크스페이스 프롬프트 편집
                  </h3>
                  <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                    {workspaces.map((ws) => (
                      <div
                        key={ws.id}
                        onClick={() => { setEditWs(ws); setEditContent(ws.system_prompt); }}
                        style={{
                          ...cardStyle, padding: "12px", cursor: "pointer",
                          borderColor: editWs?.id === ws.id ? "var(--accent)" : "var(--border)",
                        }}
                      >
                        <div className="flex justify-between items-center">
                          <span style={{ color: "var(--text-primary)", fontWeight: 500 }}>{ws.icon} {ws.name}</span>
                          <span style={{ color: "var(--text-secondary)", fontSize: "11px" }}>~{ws.est_tokens}tok</span>
                        </div>
                        <div style={{ color: "var(--text-secondary)", fontSize: "11px", marginTop: "4px" }}>
                          {ws.system_prompt ? ws.system_prompt.slice(0, 80) + "..." : "(비어있음)"}
                        </div>
                      </div>
                    ))}
                  </div>
                </div>

                {editWs && (
                  <div style={cardStyle}>
                    <div className="flex justify-between items-center mb-3">
                      <h3 style={{ color: "var(--text-primary)", fontSize: "16px", fontWeight: 600 }}>
                        {editWs.icon} {editWs.name} 편집
                      </h3>
                      <div className="flex gap-2 items-center">
                        <span style={{ color: "var(--text-secondary)", fontSize: "12px" }}>
                          {editContent.length}자 / ~{Math.round(editContent.length * 0.7)}tok
                        </span>
                        <button onClick={() => setEditWs(null)} style={btnStyle()}>취소</button>
                        <button onClick={handleSave} disabled={saving} style={btnStyle(true)}>
                          {saving ? "저장 중..." : "저장"}
                        </button>
                      </div>
                    </div>
                    <textarea
                      value={editContent}
                      onChange={(e) => setEditContent(e.target.value)}
                      rows={16}
                      style={{
                        width: "100%",
                        background: "var(--bg-primary)",
                        color: "var(--text-primary)",
                        border: "1px solid var(--border)",
                        borderRadius: "6px",
                        padding: "12px",
                        fontFamily: "monospace",
                        fontSize: "13px",
                        resize: "vertical",
                        lineHeight: "1.5",
                      }}
                    />
                  </div>
                )}
              </div>
            )}

            {/* ─── 미리보기 ─── */}
            {tab === "preview" && (
              <div className="grid gap-4">
                <div style={cardStyle}>
                  <div className="flex gap-3 items-end flex-wrap">
                    <div>
                      <label style={{ color: "var(--text-secondary)", fontSize: "12px", display: "block", marginBottom: "4px" }}>워크스페이스</label>
                      <select
                        value={previewWs}
                        onChange={(e) => setPreviewWs(e.target.value)}
                        style={{ background: "var(--bg-primary)", color: "var(--text-primary)", border: "1px solid var(--border)", borderRadius: "6px", padding: "8px 12px" }}
                      >
                        {["CEO", "AADS", "KIS", "GO100", "SF", "NTV2", "NAS", "KAKAOBOT"].map((w) => (
                          <option key={w} value={w}>{w}</option>
                        ))}
                      </select>
                    </div>
                    <div>
                      <label style={{ color: "var(--text-secondary)", fontSize: "12px", display: "block", marginBottom: "4px" }}>인텐트</label>
                      <select
                        value={previewIntent}
                        onChange={(e) => setPreviewIntent(e.target.value)}
                        style={{ background: "var(--bg-primary)", color: "var(--text-primary)", border: "1px solid var(--border)", borderRadius: "6px", padding: "8px 12px" }}
                      >
                        {allIntents.map((i) => (
                          <option key={i} value={i}>{i}</option>
                        ))}
                      </select>
                    </div>
                    <button onClick={handlePreview} disabled={previewing} style={btnStyle(true)}>
                      {previewing ? "생성 중..." : "미리보기"}
                    </button>
                  </div>
                </div>

                {previewResult && (
                  <div style={cardStyle}>
                    <div className="flex gap-4 mb-3 flex-wrap">
                      <div style={{ color: "var(--accent)", fontSize: "14px" }}>
                        Layer1: ~{previewResult.layer1_tokens}tok
                      </div>
                      <div style={{ color: "var(--warning)", fontSize: "14px" }}>
                        Layer4: ~{previewResult.layer4_tokens}tok
                      </div>
                      <div style={{ color: "var(--success)", fontSize: "14px", fontWeight: 600 }}>
                        총: ~{previewResult.total_tokens}tok ({previewResult.total_chars.toLocaleString()}자)
                      </div>
                    </div>
                    <pre style={{
                      background: "var(--bg-primary)",
                      color: "var(--text-primary)",
                      border: "1px solid var(--border)",
                      borderRadius: "6px",
                      padding: "16px",
                      fontSize: "12px",
                      lineHeight: "1.6",
                      overflow: "auto",
                      maxHeight: "600px",
                      whiteSpace: "pre-wrap",
                      wordBreak: "break-word",
                    }}>
                      {previewResult.prompt}
                    </pre>
                  </div>
                )}
              </div>
            )}

            {/* ─── 토큰 프로파일 ─── */}
            {tab === "tokens" && (
              <div className="grid gap-4">
                {tokenProfile ? (
                  <>
                    <div style={cardStyle}>
                      <h3 style={{ color: "var(--text-primary)", marginBottom: "12px", fontSize: "16px", fontWeight: 600 }}>
                        워크스페이스 × 인텐트 토큰 히트맵
                      </h3>
                      <div style={{ overflowX: "auto" }}>
                        <table style={{ width: "100%", borderCollapse: "collapse" }}>
                          <thead>
                            <tr>
                              <th style={{ padding: "8px", textAlign: "left", color: "var(--text-secondary)", fontSize: "12px" }}>WS</th>
                              {tokenProfile.workspaces && Object.keys(Object.values(tokenProfile.workspaces)[0] as any || {}).map((intent: string) => (
                                <th key={intent} style={{ padding: "8px", textAlign: "center", color: "var(--text-secondary)", fontSize: "11px" }}>{intent}</th>
                              ))}
                            </tr>
                          </thead>
                          <tbody>
                            {tokenProfile.workspaces && Object.entries(tokenProfile.workspaces).map(([ws, intents]: [string, any]) => (
                              <tr key={ws} style={{ borderBottom: "1px solid var(--border)" }}>
                                <td style={{ padding: "8px", color: "var(--text-primary)", fontSize: "13px", fontWeight: 500 }}>{ws}</td>
                                {Object.entries(intents).map(([intent, v]: [string, any]) => {
                                  const tok = v.est_tokens;
                                  const intensity = Math.min(1, tok / 6000);
                                  const bg = `rgba(59, 130, 246, ${0.1 + intensity * 0.5})`;
                                  return (
                                    <td key={intent} style={{ padding: "8px", textAlign: "center", fontSize: "12px", color: "var(--text-primary)", background: bg }}>
                                      {tok}
                                    </td>
                                  );
                                })}
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    </div>

                    <div style={cardStyle}>
                      <h3 style={{ color: "var(--text-primary)", marginBottom: "12px", fontSize: "16px", fontWeight: 600 }}>
                        섹션별 토큰 비중
                      </h3>
                      <div className="grid grid-cols-2 md:grid-cols-3 gap-3">
                        {tokenProfile.sections && Object.entries(tokenProfile.sections)
                          .filter(([k]: [string, any]) => !k.startsWith("ROLE_") && !k.startsWith("CAP_"))
                          .sort(([, a]: [string, any], [, b]: [string, any]) => b.est_tokens - a.est_tokens)
                          .map(([name, s]: [string, any]) => {
                            const total = Object.values(tokenProfile.sections as Record<string, Section>)
                              .filter((_, i) => !Object.keys(tokenProfile.sections)[i].startsWith("ROLE_") && !Object.keys(tokenProfile.sections)[i].startsWith("CAP_"))
                              .reduce((sum, v) => sum + v.est_tokens, 0);
                            const pct = ((s.est_tokens / total) * 100).toFixed(1);
                            return (
                              <div key={name} style={{ ...cardStyle, padding: "12px" }}>
                                <div style={{ color: "var(--text-secondary)", fontSize: "11px" }}>{name}</div>
                                <div className="flex items-end gap-2">
                                  <span style={{ color: "var(--accent)", fontSize: "18px", fontWeight: 700 }}>~{s.est_tokens}</span>
                                  <span style={{ color: "var(--text-secondary)", fontSize: "12px" }}>({pct}%)</span>
                                </div>
                                <div style={{ marginTop: "6px", height: "4px", background: "var(--bg-hover)", borderRadius: "2px" }}>
                                  <div style={{ height: "100%", width: `${pct}%`, background: "var(--accent)", borderRadius: "2px" }} />
                                </div>
                              </div>
                            );
                          })}
                      </div>
                    </div>
                  </>
                ) : (
                  <div style={{ color: "var(--text-secondary)", textAlign: "center", padding: "40px" }}>토큰 프로파일 로딩 중...</div>
                )}
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
