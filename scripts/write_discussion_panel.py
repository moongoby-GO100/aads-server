#!/usr/bin/env python3
"""Write updated DiscussionPanel.tsx to dashboard directory."""
import pathlib

TARGET = pathlib.Path("/root/aads/aads-dashboard/src/components/chat/DiscussionPanel.tsx")

CONTENT = r'''"use client";

import React, { useState, useRef, useCallback, useEffect } from "react";

/* ── Types ── */
interface Participant {
  name: string;
  role: string;
  model: string;
  color: string;
  avatar: string;
  system_prompt?: string;
}

interface Preset {
  name: string;
  label: string;
  synthesizer_model: string;
  participants: Participant[];
}

interface DiscussionEntry {
  id: string;
  type: "participant" | "ceo" | "system" | "synthesis";
  name: string;
  model?: string;
  content: string;
  color: string;
  avatar: string;
  round?: number;
  timestamp: Date;
}

type Phase = "setup" | "streaming" | "wait_ceo" | "done";

interface DiscussionPanelProps {
  sessionId: string;
  onClose: () => void;
}

interface ModelOption {
  value: string;
  label: string;
}

const BASE_URL =
  typeof window !== "undefined"
    ? process.env.NEXT_PUBLIC_API_URL || "https://aads.newtalk.kr/api/v1"
    : "";

function authHdrs(): Record<string, string> {
  if (typeof window === "undefined") return {};
  const t = localStorage.getItem("aads_token");
  return t ? { Authorization: `Bearer ${t}` } : {};
}

/* ── Preset defaults ── */
const DEFAULT_PRESETS: Record<string, Preset> = {
  standard: {
    name: "standard",
    label: "기본 3인",
    synthesizer_model: "claude-opus-4-6",
    participants: [
      { name: "기획 A", role: "strategic_planner", model: "claude-opus-4-6", color: "#6c5ce7", avatar: "A" },
      { name: "기획 B", role: "market_analyst", model: "gemini-2.5-pro", color: "#00cec9", avatar: "B" },
      { name: "검증 C", role: "critical_reviewer", model: "claude-sonnet-4-6", color: "#fdcb6e", avatar: "C" },
    ],
  },
  deep: {
    name: "deep",
    label: "심층 4인",
    synthesizer_model: "claude-opus-4-6",
    participants: [
      { name: "기획 A", role: "strategic_planner", model: "claude-opus-4-6", color: "#6c5ce7", avatar: "A" },
      { name: "시장 B", role: "market_analyst", model: "gemini-2.5-pro", color: "#00cec9", avatar: "B" },
      { name: "검증 C", role: "critical_reviewer", model: "claude-sonnet-4-6", color: "#fdcb6e", avatar: "C" },
      { name: "속도 D", role: "rapid_ideator", model: "gemini-2.5-flash", color: "#55efc4", avatar: "D" },
    ],
  },
  light: {
    name: "light",
    label: "경량 2인",
    synthesizer_model: "claude-sonnet-4-6",
    participants: [
      { name: "기획 A", role: "planner", model: "claude-sonnet-4-6", color: "#6c5ce7", avatar: "A" },
      { name: "속도 B", role: "rapid_ideator", model: "gemini-2.5-flash", color: "#00cec9", avatar: "B" },
    ],
  },
};

const FALLBACK_MODEL_OPTIONS: ModelOption[] = [
  { value: "claude-opus-4-6", label: "Claude Opus 4.6" },
  { value: "claude-sonnet-4-6", label: "Claude Sonnet 4.6" },
  { value: "claude-haiku-4-5-20251001", label: "Claude Haiku 4.5" },
  { value: "gemini-2.5-pro", label: "Gemini 2.5 Pro" },
  { value: "gemini-2.5-flash", label: "Gemini 2.5 Flash" },
  { value: "deepseek-v4-pro", label: "DeepSeek V4 Pro" },
  { value: "deepseek-v4-flash", label: "DeepSeek V4 Flash" },
];

/* ── Component ── */
export default function DiscussionPanel({ sessionId, onClose }: DiscussionPanelProps) {
  const [phase, setPhase] = useState<Phase>("setup");
  const [entries, setEntries] = useState<DiscussionEntry[]>([]);
  const [ceoInput, setCeoInput] = useState("");
  const [currentRound, setCurrentRound] = useState(0);
  const [totalCost, setTotalCost] = useState(0);
  const [, setBudgetRemaining] = useState(0);
  const [synthesis, setSynthesis] = useState("");
  const [error, setError] = useState("");
  const [discussionId, setDiscussionId] = useState("");

  /* setup state */
  const [topic, setTopic] = useState("");
  const [preset, setPreset] = useState("standard");
  const [mode, setMode] = useState<"manual" | "auto">("manual");
  const [budget, setBudget] = useState(10);
  const [participants, setParticipants] = useState<Participant[]>(
    DEFAULT_PRESETS.standard.participants
  );
  const [useCustom, setUseCustom] = useState(false);
  const [modelOptions, setModelOptions] = useState<ModelOption[]>(FALLBACK_MODEL_OPTIONS);

  const scrollRef = useRef<HTMLDivElement>(null);
  const abortRef = useRef<AbortController | null>(null);

  /* ── Fetch AADS model list on mount ── */
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch(`${BASE_URL}/llm-models?active_only=true`, {
          headers: authHdrs(),
        });
        if (!res.ok) return;
        const data = await res.json();
        if (cancelled) return;
        const models: ModelOption[] = (data.models || [])
          .filter((m: Record<string, unknown>) => m.is_active)
          .map((m: Record<string, unknown>) => ({
            value: m.model_id as string,
            label: (m.display_name as string) || (m.model_id as string),
          }))
          .sort((a: ModelOption, b: ModelOption) => a.label.localeCompare(b.label));
        if (models.length > 0) {
          setModelOptions(models);
        }
      } catch {
        /* fallback to hardcoded list */
      }
    })();
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    if (!useCustom) {
      setParticipants([...(DEFAULT_PRESETS[preset]?.participants || [])]);
    }
  }, [preset, useCustom]);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [entries, synthesis]);

  const addEntry = useCallback((e: DiscussionEntry) => {
    setEntries((prev) => [...prev, e]);
  }, []);

  /* ── SSE event handler (use ref to avoid stale closure) ── */
  const handleSSEEventRef = useRef<(ev: Record<string, unknown>) => void>(() => {});

  useEffect(() => {
    handleSSEEventRef.current = (ev: Record<string, unknown>) => {
      const event = ev.event as string;

      if (event === "discussion_start") {
        setDiscussionId(ev.discussion_id as string);
        addEntry({
          id: `sys-start-${Date.now()}`,
          type: "system",
          name: "시스템",
          content: `토론 시작: ${ev.topic} (${(ev.participants as Participant[])?.length || 0}명)`,
          color: "#666",
          avatar: "⚡",
          timestamp: new Date(),
        });
      } else if (event === "round_start") {
        setCurrentRound(ev.round as number);
        addEntry({
          id: `sys-r${ev.round}-${Date.now()}`,
          type: "system",
          name: "시스템",
          content: `── 라운드 ${ev.round} 시작 ──`,
          color: "#666",
          avatar: "🔄",
          round: ev.round as number,
          timestamp: new Date(),
        });
      } else if (event === "participant_reply") {
        const pList = DEFAULT_PRESETS[preset]?.participants || participants;
        const pMatch = pList.find((p) => p.name === ev.participant);
        addEntry({
          id: `p-${ev.round}-${ev.participant}-${Date.now()}`,
          type: "participant",
          name: (ev.participant as string) || "?",
          model: ev.model as string,
          content: ev.content as string,
          color: pMatch?.color || "#888",
          avatar: pMatch?.avatar || "?",
          round: ev.round as number,
          timestamp: new Date(),
        });
      } else if (event === "round_complete") {
        setTotalCost(ev.total_cost_usd as number);
        setBudgetRemaining(ev.budget_remaining as number);
      } else if (event === "wait_ceo") {
        setPhase("wait_ceo");
      } else if (event === "ceo_directive") {
        addEntry({
          id: `ceo-dir-${Date.now()}`,
          type: "ceo",
          name: "CEO",
          content: ev.directive as string,
          color: "#e74c3c",
          avatar: "👤",
          timestamp: new Date(),
        });
      } else if (event === "ceo_stop") {
        addEntry({
          id: `sys-stop-${Date.now()}`,
          type: "system",
          name: "시스템",
          content: "CEO가 토론 종료를 요청했습니다.",
          color: "#e74c3c",
          avatar: "🛑",
          timestamp: new Date(),
        });
      } else if (event === "synthesis_start") {
        setPhase("streaming");
        addEntry({
          id: `sys-synth-${Date.now()}`,
          type: "system",
          name: "시스템",
          content: `종합 분석 중... (${ev.model})`,
          color: "#6c5ce7",
          avatar: "🧠",
          timestamp: new Date(),
        });
      } else if (event === "synthesis_complete") {
        setSynthesis(ev.synthesis as string);
        setPhase("done");
        setTotalCost(ev.total_cost_usd as number);
      } else if (event === "budget_exceeded") {
        addEntry({
          id: `sys-budget-${Date.now()}`,
          type: "system",
          name: "시스템",
          content: `예산 초과 ($${ev.total_cost_usd}/$${ev.budget_usd})`,
          color: "#e17055",
          avatar: "💰",
          timestamp: new Date(),
        });
      } else if (event === "error") {
        setError(ev.message as string);
        setPhase("setup");
      }
    };
  }, [addEntry, preset, participants]);

  /* ── SSE reader (uses ref to always get latest handler) ── */
  const readSSE = useCallback(
    async (res: Response) => {
      const reader = res.body?.getReader();
      if (!reader) {
        setError("SSE 스트림을 읽을 수 없습니다.");
        setPhase("setup");
        return;
      }
      const decoder = new TextDecoder();
      let buf = "";

      try {
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buf += decoder.decode(value, { stream: true });
          const lines = buf.split("\n");
          buf = lines.pop() ?? "";

          for (const line of lines) {
            if (!line.startsWith("data: ")) continue;
            const raw = line.slice(6).trim();
            if (!raw || raw === "[DONE]") continue;
            try {
              const ev = JSON.parse(raw);
              handleSSEEventRef.current(ev);
            } catch {
              /* ignore malformed */
            }
          }
        }
      } catch (e: unknown) {
        if ((e as Error).name !== "AbortError") {
          setError(`스트림 읽기 오류: ${(e as Error).message}`);
        }
      }
    },
    []
  );

  /* ── Start discussion ── */
  const handleStart = useCallback(async () => {
    if (!topic.trim()) return;
    setPhase("streaming");
    setError("");
    setEntries([]);
    setSynthesis("");
    setCurrentRound(0);
    setTotalCost(0);

    const ctrl = new AbortController();
    abortRef.current = ctrl;

    try {
      const body: Record<string, unknown> = {
        topic: topic.trim(),
        mode,
        preset,
        budget_usd: budget,
      };
      if (useCustom) {
        body.custom_participants = participants;
      }

      const res = await fetch(
        `${BASE_URL}/chat/sessions/${sessionId}/discussion/start`,
        {
          method: "POST",
          headers: { ...authHdrs(), "Content-Type": "application/json" },
          body: JSON.stringify(body),
          signal: ctrl.signal,
        }
      );

      if (!res.ok) {
        const msg = await res.text();
        setError(`API 오류 (${res.status}): ${msg}`);
        setPhase("setup");
        return;
      }

      await readSSE(res);
    } catch (e: unknown) {
      if ((e as Error).name !== "AbortError") {
        setError(`연결 오류: ${(e as Error).message}`);
        setPhase("setup");
      }
    }
  }, [topic, mode, preset, budget, useCustom, participants, sessionId, readSSE]);

  /* ── CEO send (continue) ── */
  const handleCeoSend = useCallback(async () => {
    const msg = ceoInput.trim();
    if (!msg) return;

    addEntry({
      id: `ceo-${Date.now()}`,
      type: "ceo",
      name: "CEO",
      content: msg,
      color: "#e74c3c",
      avatar: "👤",
      timestamp: new Date(),
    });
    setCeoInput("");
    setPhase("streaming");

    try {
      const res = await fetch(
        `${BASE_URL}/chat/sessions/${sessionId}/discussion/continue`,
        {
          method: "POST",
          headers: { ...authHdrs(), "Content-Type": "application/json" },
          body: JSON.stringify({ message: msg }),
        }
      );
      if (!res.ok) {
        const errText = await res.text();
        setError(`API 오류 (${res.status}): ${errText}`);
        setPhase("wait_ceo");
        return;
      }
      await readSSE(res);
    } catch (e: unknown) {
      setError(`연결 오류: ${(e as Error).message}`);
      setPhase("wait_ceo");
    }
  }, [ceoInput, sessionId, addEntry, readSSE]);

  /* ── Cancel ── */
  const handleCancel = useCallback(async () => {
    abortRef.current?.abort();
    try {
      await fetch(`${BASE_URL}/chat/sessions/${sessionId}/discussion/stop`, {
        method: "POST",
        headers: authHdrs(),
      });
    } catch { /* best-effort */ }
    setPhase("done");
  }, [sessionId]);

  /* ── Participant editor ── */
  const updateParticipant = (idx: number, field: keyof Participant, val: string) => {
    setParticipants((prev) => {
      const next = [...prev];
      next[idx] = { ...next[idx], [field]: val };
      return next;
    });
  };

  const addParticipant = () => {
    setParticipants((prev) => [
      ...prev,
      { name: `참가자 ${prev.length + 1}`, role: "analyst", model: "claude-sonnet-4-6", color: "#a29bfe", avatar: String.fromCharCode(65 + prev.length) },
    ]);
  };

  const removeParticipant = (idx: number) => {
    setParticipants((prev) => prev.filter((_, i) => i !== idx));
  };

  /* ── Render ── */
  return (
    <div style={{
      position: "fixed", inset: 0, zIndex: 9999,
      background: "rgba(0,0,0,0.6)", display: "flex", justifyContent: "center", alignItems: "center",
    }}>
      <div style={{
        width: "min(95vw, 900px)", height: "min(90vh, 800px)",
        background: "var(--ct-bg, #1a1a2e)", borderRadius: 16,
        display: "flex", flexDirection: "column", overflow: "hidden",
        border: "1px solid var(--ct-border, #333)",
        boxShadow: "0 8px 40px rgba(0,0,0,0.5)",
      }}>
        {/* Header */}
        <div style={{
          padding: "14px 20px", display: "flex", justifyContent: "space-between", alignItems: "center",
          borderBottom: "1px solid var(--ct-border, #333)",
          background: "var(--ct-card, #1e1e2e)",
        }}>
          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <span style={{ fontSize: 20 }}>💬</span>
            <span style={{ fontWeight: 700, fontSize: 16, color: "var(--ct-text, #e0e0e0)" }}>
              멀티 LLM 토론
            </span>
            {phase !== "setup" && (
              <span style={{
                fontSize: 12, padding: "2px 8px", borderRadius: 8,
                background: phase === "done" ? "#27ae60" : phase === "wait_ceo" ? "#e74c3c" : "#6c5ce7",
                color: "#fff",
              }}>
                {phase === "streaming" ? "진행 중" : phase === "wait_ceo" ? "CEO 대기" : phase === "done" ? "완료" : ""}
              </span>
            )}
          </div>
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            {currentRound > 0 && (
              <span style={{ fontSize: 12, color: "#888" }}>
                R{currentRound} · ${totalCost.toFixed(2)}
              </span>
            )}
            <button onClick={onClose} style={{
              background: "none", border: "none", color: "var(--ct-text-secondary, #888)",
              fontSize: 20, cursor: "pointer", lineHeight: 1,
            }}>✕</button>
          </div>
        </div>

        {/* Body */}
        <div ref={scrollRef} style={{ flex: 1, overflowY: "auto", padding: 20 }}>
          {phase === "setup" && (
            <div style={{ maxWidth: 600, margin: "0 auto" }}>
              {/* Topic */}
              <label style={{ display: "block", marginBottom: 12, color: "var(--ct-text, #e0e0e0)" }}>
                <span style={{ fontWeight: 600, fontSize: 14 }}>토론 주제</span>
                <textarea
                  value={topic}
                  onChange={(e) => setTopic(e.target.value)}
                  placeholder="예: AADS 모바일앱 MVP 기획 — 핵심 기능과 우선순위를 논의해주세요"
                  rows={3}
                  style={{
                    width: "100%", marginTop: 6, padding: 12, borderRadius: 10,
                    background: "var(--ct-input-bg, #16213e)", border: "1px solid var(--ct-border, #333)",
                    color: "var(--ct-text, #e0e0e0)", fontSize: 14, resize: "vertical",
                    boxSizing: "border-box",
                  }}
                />
              </label>

              {/* Preset + Mode row */}
              <div style={{ display: "flex", gap: 12, marginBottom: 12, flexWrap: "wrap" }}>
                <label style={{ flex: 1, minWidth: 140, color: "var(--ct-text, #e0e0e0)" }}>
                  <span style={{ fontWeight: 600, fontSize: 14 }}>프리셋</span>
                  <select value={preset} onChange={(e) => setPreset(e.target.value)}
                    style={{
                      width: "100%", marginTop: 6, padding: 10, borderRadius: 8,
                      background: "var(--ct-input-bg, #16213e)", border: "1px solid var(--ct-border, #333)",
                      color: "var(--ct-text, #e0e0e0)", fontSize: 14,
                    }}>
                    <option value="standard">기본 3인 (Opus + Gemini + Sonnet)</option>
                    <option value="deep">심층 4인 (+Flash)</option>
                    <option value="light">경량 2인 (Sonnet + Flash)</option>
                  </select>
                </label>
                <label style={{ flex: 1, minWidth: 140, color: "var(--ct-text, #e0e0e0)" }}>
                  <span style={{ fontWeight: 600, fontSize: 14 }}>진행 모드</span>
                  <select value={mode} onChange={(e) => setMode(e.target.value as "manual" | "auto")}
                    style={{
                      width: "100%", marginTop: 6, padding: 10, borderRadius: 8,
                      background: "var(--ct-input-bg, #16213e)", border: "1px solid var(--ct-border, #333)",
                      color: "var(--ct-text, #e0e0e0)", fontSize: 14,
                    }}>
                    <option value="manual">수동 (라운드별 CEO 승인)</option>
                    <option value="auto">자동 (CEO &quot;그만&quot; 시 종료)</option>
                  </select>
                </label>
              </div>

              {/* Budget */}
              <label style={{ display: "block", marginBottom: 12, color: "var(--ct-text, #e0e0e0)" }}>
                <span style={{ fontWeight: 600, fontSize: 14 }}>예산 한도 (USD)</span>
                <input type="number" value={budget} min={0.1} max={100} step={0.5}
                  onChange={(e) => setBudget(Number(e.target.value))}
                  style={{
                    width: "100%", marginTop: 6, padding: 10, borderRadius: 8,
                    background: "var(--ct-input-bg, #16213e)", border: "1px solid var(--ct-border, #333)",
                    color: "var(--ct-text, #e0e0e0)", fontSize: 14, boxSizing: "border-box",
                  }}
                />
              </label>

              {/* Custom participants toggle */}
              <div style={{ marginBottom: 12 }}>
                <label style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer", color: "var(--ct-text, #e0e0e0)" }}>
                  <input type="checkbox" checked={useCustom}
                    onChange={(e) => setUseCustom(e.target.checked)}
                    style={{ accentColor: "#6c5ce7" }} />
                  <span style={{ fontWeight: 600, fontSize: 14 }}>참가자 직접 설정</span>
                </label>
              </div>

              {/* Participants list */}
              <div style={{
                background: "var(--ct-card, #1e1e2e)", borderRadius: 10, padding: 12,
                border: "1px solid var(--ct-border, #333)", marginBottom: 16,
              }}>
                <div style={{ fontSize: 13, fontWeight: 600, color: "#888", marginBottom: 8 }}>
                  참가자 ({participants.length}명) · 모델 {modelOptions.length}종 사용 가능
                </div>
                {participants.map((p, i) => (
                  <div key={i} style={{
                    display: "flex", gap: 8, alignItems: "center", marginBottom: 8,
                    padding: 8, borderRadius: 8,
                    background: useCustom ? "var(--ct-input-bg, #16213e)" : "transparent",
                    opacity: useCustom ? 1 : 0.7,
                    flexWrap: "wrap",
                  }}>
                    <span style={{
                      width: 28, height: 28, borderRadius: "50%", display: "flex",
                      alignItems: "center", justifyContent: "center",
                      background: p.color, color: "#fff", fontWeight: 700, fontSize: 13, flexShrink: 0,
                    }}>{p.avatar}</span>
                    {useCustom ? (
                      <>
                        <input value={p.name} onChange={(e) => updateParticipant(i, "name", e.target.value)}
                          style={{ flex: 1, minWidth: 80, padding: 6, borderRadius: 6, background: "var(--ct-bg, #1a1a2e)", border: "1px solid #333", color: "#e0e0e0", fontSize: 13 }} />
                        <select value={p.model} onChange={(e) => updateParticipant(i, "model", e.target.value)}
                          style={{ flex: 2, minWidth: 120, padding: 6, borderRadius: 6, background: "var(--ct-bg, #1a1a2e)", border: "1px solid #333", color: "#e0e0e0", fontSize: 12 }}>
                          {modelOptions.map((m) => (
                            <option key={m.value} value={m.value}>{m.label}</option>
                          ))}
                        </select>
                        <button onClick={() => removeParticipant(i)} style={{
                          background: "none", border: "none", color: "#e17055", cursor: "pointer", fontSize: 16,
                        }}>✕</button>
                      </>
                    ) : (
                      <>
                        <span style={{ flex: 1, fontSize: 13, color: "#e0e0e0" }}>{p.name}</span>
                        <span style={{ fontSize: 12, color: "#888" }}>{p.model}</span>
                      </>
                    )}
                  </div>
                ))}
                {useCustom && (
                  <button onClick={addParticipant} style={{
                    width: "100%", padding: 8, borderRadius: 8, border: "1px dashed #555",
                    background: "transparent", color: "#888", cursor: "pointer", fontSize: 13,
                  }}>+ 참가자 추가</button>
                )}
              </div>

              {error && (
                <div style={{ color: "#e74c3c", fontSize: 13, marginBottom: 12, padding: 10, background: "rgba(231,76,60,0.1)", borderRadius: 8, border: "1px solid rgba(231,76,60,0.3)" }}>
                  ⚠️ {error}
                </div>
              )}

              <button onClick={handleStart} disabled={!topic.trim()}
                style={{
                  width: "100%", padding: 14, borderRadius: 10, border: "none",
                  background: topic.trim() ? "linear-gradient(135deg, #6c5ce7, #a29bfe)" : "#333",
                  color: "#fff", fontWeight: 700, fontSize: 15, cursor: topic.trim() ? "pointer" : "default",
                  transition: "all 0.2s",
                }}>
                🚀 토론 시작
              </button>
            </div>
          )}

          {/* Streaming / Wait CEO / Done phases — message list */}
          {phase !== "setup" && (
            <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
              {entries.map((e) => (
                <div key={e.id} style={{
                  display: "flex", gap: 10,
                  justifyContent: e.type === "ceo" ? "flex-end" : "flex-start",
                }}>
                  {e.type !== "ceo" && (
                    <div style={{
                      width: 36, height: 36, borderRadius: "50%", flexShrink: 0,
                      display: "flex", alignItems: "center", justifyContent: "center",
                      background: e.type === "system" ? "#333" : e.color,
                      color: "#fff", fontWeight: 700, fontSize: 14,
                    }}>
                      {e.avatar}
                    </div>
                  )}
                  <div style={{
                    maxWidth: "75%", padding: "10px 14px", borderRadius: 12,
                    background: e.type === "ceo"
                      ? "linear-gradient(135deg, #e74c3c, #c0392b)"
                      : e.type === "system"
                      ? "var(--ct-card, #1e1e2e)"
                      : e.type === "synthesis"
                      ? "linear-gradient(135deg, #6c5ce7, #a29bfe)"
                      : `${e.color}22`,
                    border: e.type === "system" ? "1px solid var(--ct-border, #333)" : "none",
                    color: e.type === "ceo" || e.type === "synthesis" ? "#fff" : "var(--ct-text, #e0e0e0)",
                  }}>
                    {e.type === "participant" && (
                      <div style={{ fontSize: 12, fontWeight: 700, marginBottom: 4, color: e.color }}>
                        {e.name} <span style={{ fontWeight: 400, color: "#888" }}>({e.model})</span>
                      </div>
                    )}
                    {e.type === "system" ? (
                      <div style={{ fontSize: 13, color: "#888", textAlign: "center" }}>{e.content}</div>
                    ) : (
                      <div style={{ fontSize: 14, lineHeight: 1.6, whiteSpace: "pre-wrap" }}>{e.content}</div>
                    )}
                  </div>
                  {e.type === "ceo" && (
                    <div style={{
                      width: 36, height: 36, borderRadius: "50%", flexShrink: 0,
                      display: "flex", alignItems: "center", justifyContent: "center",
                      background: "#e74c3c", color: "#fff", fontWeight: 700, fontSize: 14,
                    }}>👤</div>
                  )}
                </div>
              ))}

              {/* Synthesis block */}
              {synthesis && (
                <div style={{
                  padding: 16, borderRadius: 12, marginTop: 8,
                  background: "linear-gradient(135deg, rgba(108,92,231,0.15), rgba(162,155,254,0.15))",
                  border: "1px solid rgba(108,92,231,0.3)",
                }}>
                  <div style={{ fontWeight: 700, fontSize: 15, color: "#a29bfe", marginBottom: 10, display: "flex", alignItems: "center", gap: 6 }}>
                    🧠 최종 종합
                  </div>
                  <div style={{ fontSize: 14, lineHeight: 1.7, color: "var(--ct-text, #e0e0e0)", whiteSpace: "pre-wrap" }}>
                    {synthesis}
                  </div>
                </div>
              )}

              {/* Streaming indicator */}
              {phase === "streaming" && (
                <div style={{ display: "flex", alignItems: "center", gap: 8, padding: "8px 0" }}>
                  <div style={{
                    width: 8, height: 8, borderRadius: "50%", background: "#6c5ce7",
                    animation: "pulse 1.5s infinite",
                  }} />
                  <span style={{ fontSize: 13, color: "#888" }}>AI 응답 대기 중...</span>
                  <button onClick={handleCancel} style={{
                    marginLeft: "auto", padding: "4px 12px", borderRadius: 6,
                    background: "rgba(231,76,60,0.2)", border: "1px solid rgba(231,76,60,0.4)",
                    color: "#e74c3c", cursor: "pointer", fontSize: 12,
                  }}>중단</button>
                </div>
              )}

              {/* Error during streaming */}
              {error && phase !== "setup" && (
                <div style={{ color: "#e74c3c", fontSize: 13, padding: 10, background: "rgba(231,76,60,0.1)", borderRadius: 8, border: "1px solid rgba(231,76,60,0.3)" }}>
                  ⚠️ {error}
                </div>
              )}
            </div>
          )}
        </div>

        {/* Footer: CEO input (wait_ceo phase) */}
        {phase === "wait_ceo" && (
          <div style={{
            padding: "12px 20px", borderTop: "1px solid var(--ct-border, #333)",
            background: "var(--ct-card, #1e1e2e)",
            display: "flex", gap: 8,
          }}>
            <input
              value={ceoInput}
              onChange={(e) => setCeoInput(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); handleCeoSend(); }}}
              placeholder="다음 / 계속 / 그만 / 또는 지시사항 입력..."
              style={{
                flex: 1, padding: 10, borderRadius: 8,
                background: "var(--ct-input-bg, #16213e)", border: "1px solid var(--ct-border, #333)",
                color: "var(--ct-text, #e0e0e0)", fontSize: 14,
              }}
            />
            <button onClick={handleCeoSend} style={{
              padding: "10px 20px", borderRadius: 8, border: "none",
              background: "linear-gradient(135deg, #6c5ce7, #a29bfe)",
              color: "#fff", fontWeight: 600, cursor: "pointer", fontSize: 14,
            }}>전송</button>
          </div>
        )}

        {/* Footer: Done phase */}
        {phase === "done" && (
          <div style={{
            padding: "12px 20px", borderTop: "1px solid var(--ct-border, #333)",
            background: "var(--ct-card, #1e1e2e)",
            display: "flex", justifyContent: "space-between", alignItems: "center",
          }}>
            <span style={{ fontSize: 13, color: "#888" }}>
              토론 완료 · 총 비용: ${totalCost.toFixed(2)}
              {discussionId && ` · ID: ${discussionId}`}
            </span>
            <button onClick={onClose} style={{
              padding: "8px 20px", borderRadius: 8, border: "none",
              background: "#27ae60", color: "#fff", fontWeight: 600, cursor: "pointer", fontSize: 14,
            }}>닫기</button>
          </div>
        )}
      </div>

      {/* pulse animation */}
      <style>{`@keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.3; } }`}</style>
    </div>
  );
}
'''

TARGET.write_text(CONTENT, encoding="utf-8")
print(f"OK: wrote {len(CONTENT)} bytes to {TARGET}")
