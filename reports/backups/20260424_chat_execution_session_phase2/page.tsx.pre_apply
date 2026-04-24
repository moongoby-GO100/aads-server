// AADS-dashboard-rebuild: refactored
"use client";
import React, { useState, useEffect, useLayoutEffect, useRef, startTransition, useCallback, useMemo, memo } from "react";
import ChatInput, { ChatInputHandle } from "./ChatInput";
import ChatSidebar from "./ChatSidebar";
import ChatArtifactPanel from "./ChatArtifactPanel";
import { MODEL_OPTIONS, DEFAULT_MODEL } from "@/components/chat/ModelSelector";
import { CodePanel } from "@/components/CodePanel";
import { useDiffApproval } from "@/hooks/useDiffApproval";
import "@/styles/code-editor.css";
import MemoryContextBar from "@/components/chat/MemoryContextBar";
import SessionSummaryCard from "@/components/chat/SessionSummaryCard";
import ConfidenceBadge from "@/components/chat/ConfidenceBadge";
import ArtifactTaskMonitor from "@/components/chat/ArtifactTaskMonitor";
import ChatOpsDock from "@/components/chat/ChatOpsDock";
import ShortcutHelp from "@/components/chat/ShortcutHelp";
import { useVersionCheck } from "@/hooks/useVersionCheck";
import UpdateBanner from "@/components/UpdateBanner";
import { Workspace, ChatSession, ChatMessage, Artifact, Theme, ArtifactMode, ArtifactTab, ScreenSize, DARK, LIGHT } from "./types";
import { BASE_URL, getToken, authHdrs, chatApi, uploadChatFile } from "./api";
import { processInline, InlineMd, CopyableCodeBlock, MarkdownBlock } from "./MarkdownRenderer";

type AuthKeyStatus = {
  label?: string;
  key_name?: string;
  slot?: string;
  priority?: number;
  rate_limited_until?: string | null;
  is_rate_limited?: boolean;
  last_used_at?: string | null;
  last_verified_at?: string | null;
  notes?: string;
  is_current?: boolean;
};

type ApiKeyInfoState = {
  litellm?: string;
  type?: string;
  label?: string;
  cliLabel?: string;
  keyName?: string;
  slot?: string;
  relayStatus?: string;
  relayTokenAvailable?: boolean;
  keys?: AuthKeyStatus[];
};

type LlmRegistryModel = {
  provider: string;
  model_id: string;
  display_name?: string;
  input_cost?: string | number | null;
  output_cost?: string | number | null;
  is_active?: boolean;
};

type ChatModelPreference = {
  model_id: string;
  display_order: number;
  is_hidden: boolean;
  is_favorite: boolean;
  is_pinned: boolean;
};

type SelectableModelOption = {
  id: string;
  name: string;
  provider: string;
  cost: string;
  isActive: boolean;
  isPinned?: boolean;
  isFavorite?: boolean;
  isHidden?: boolean;
};

const STATIC_MODEL_OPTION_MAP = new Map(MODEL_OPTIONS.map((option) => [option.id, option]));

const MODEL_ID_TO_SELECTOR_ID: Record<string, string> = {
  auto: "mixture",
  mixture: "mixture",
  "claude-sonnet": "claude-sonnet-4-6",
  "claude-opus": "claude-opus-4-7",
  "claude-opus-46": "claude-opus-4-6",
  "claude-haiku": "claude-haiku-4-5-20251001",
};

function normalizeModelIdForSelector(modelId?: string | null): string {
  const trimmed = (modelId || "").trim();
  return MODEL_ID_TO_SELECTOR_ID[trimmed] ?? trimmed;
}

function formatCostLabel(inputCost?: string | number | null, outputCost?: string | number | null): string {
  const input = Number(inputCost);
  const output = Number(outputCost);
  if (!Number.isFinite(input) || !Number.isFinite(output)) return "변동";
  if (input === 0 && output === 0) return "무료";
  return `$${input}/$${output}`;
}

function buildSelectableModelOption(row: LlmRegistryModel): SelectableModelOption {
  const optionId = normalizeModelIdForSelector(row.model_id);
  const staticOption = STATIC_MODEL_OPTION_MAP.get(optionId);
  return {
    id: optionId,
    name: staticOption?.name || row.display_name || optionId,
    provider: staticOption?.provider || row.provider,
    cost: staticOption?.cost || formatCostLabel(row.input_cost, row.output_cost),
    isActive: true,
  };
}

function buildNormalizedPreferenceMap(
  preferences: ChatModelPreference[],
): Map<string, ChatModelPreference> {
  const normalized = new Map<string, ChatModelPreference>();
  for (const item of preferences) {
    normalized.set(item.model_id, item);
    normalized.set(normalizeModelIdForSelector(item.model_id), item);
  }
  return normalized;
}

function compareSelectableModels(
  a: SelectableModelOption,
  b: SelectableModelOption,
  preferenceMap: Map<string, ChatModelPreference>,
): number {
  const aPref = preferenceMap.get(a.id);
  const bPref = preferenceMap.get(b.id);
  if (!!aPref?.is_pinned !== !!bPref?.is_pinned) return aPref?.is_pinned ? -1 : 1;
  if (!!aPref?.is_favorite !== !!bPref?.is_favorite) return aPref?.is_favorite ? -1 : 1;
  const aOrder = aPref?.display_order ?? 1000;
  const bOrder = bPref?.display_order ?? 1000;
  if (aOrder !== bOrder) return aOrder - bOrder;
  if (a.provider !== b.provider) return a.provider.localeCompare(b.provider);
  return a.name.localeCompare(b.name);
}

// ── MessageItem: React.memo로 개별 메시지 리렌더링 최적화 ──
interface MessageItemProps {
  msg: ChatMessage;
  idx: number;
  streaming: boolean;
  editingMsgId: string | null;
  editText: string;
  setEditingMsgId: (id: string | null) => void;
  setEditText: (text: string) => void;
  handleDeleteMessage: (id: string, role: string) => void;
  handleCopyToInput: (content: string) => void;
  handleEditResend: (msgId: string, newContent: string) => void;
  onRegenerate?: (msgId: string) => void;
  onReplyTo?: (msg: ChatMessage) => void;
  onBranch?: (msg: ChatMessage) => void;
  allMessages?: ChatMessage[];
  isActiveStreaming?: boolean;
  streamingContent?: string;
  streamToolStatus?: string | null;
  streamToolLogs?: Array<{icon: string; text: string; sub?: string}>;
  onStopStreaming?: () => void;
  onViewReport?: () => void;
  linkedArtifact?: { id: string; title: string; artifact_type: string; content: string };
  onViewArtifact?: (artifactId: string) => void;
  onOpenLightbox?: (srcs: string[], idx: number) => void;
  isLastAssistantMsg?: boolean;
}

const MessageItem = memo(function MessageItem({
  msg, idx, streaming, editingMsgId, editText,
  setEditingMsgId, setEditText, handleDeleteMessage, handleCopyToInput, handleEditResend,
  onRegenerate, onReplyTo, onBranch, allMessages,
  isActiveStreaming, streamingContent, streamToolStatus, streamToolLogs, onStopStreaming,
  onViewReport, linkedArtifact, onViewArtifact, onOpenLightbox, isLastAssistantMsg,
}: MessageItemProps) {
  // reply_to_id가 있으면 원본 메시지 찾기
  const replyTarget = msg.reply_to_id && allMessages
    ? allMessages.find((m) => m.id === msg.reply_to_id)
    : null;

  // P1: 긴 보고서 접이식 상태
  const [contentCollapsed, setContentCollapsed] = useState(
    () => msg.role === "assistant" && msg.content.length > 800 && !msg.intent?.startsWith("streaming") && !isLastAssistantMsg
  );

  // 마지막 응답 자동 펼침/접힘: isLastAssistantMsg 변화 시 동기화
  useEffect(() => {
    if (msg.role === "assistant" && msg.content.length > 800) {
      setContentCollapsed(!isLastAssistantMsg);
    }
  }, [isLastAssistantMsg]);

  return (
    <div
      className="ct-msg-enter group"
      style={{
        display: "flex",
        justifyContent: msg.role === "user" ? "flex-end" : "flex-start",
        ...(msg.branch_id ? { marginLeft: "24px", borderLeft: "2px solid rgba(34,197,94,0.4)", paddingLeft: "12px" } : {}),
      }}
    >
      {/* P2-2: 분기 배지 */}
      {msg.branch_point_id && msg.role === "user" && (
        <div style={{ marginBottom: "4px", marginRight: "4px", textAlign: "right" }}>
          <span style={{
            display: "inline-flex", alignItems: "center", gap: "4px",
            padding: "2px 8px", borderRadius: "12px", fontSize: "11px", fontWeight: 600,
            background: "rgba(34,197,94,0.15)", color: "#22c55e", border: "1px solid #22c55e33",
          }}>🔀 분기</span>
        </div>
      )}
      {/* 방식A/B 버튼: 사용자 메시지 왼쪽에 호버 시 표시 */}
      {msg.role === "user" && msg.intent === "system_trigger" && (
        <div style={{ marginBottom: "4px", marginRight: "4px", textAlign: "right" }}>
          <span style={{
            display: "inline-flex", alignItems: "center", gap: "4px",
            padding: "2px 8px", borderRadius: "12px", fontSize: "11px", fontWeight: 600,
            background: "rgba(59,130,246,0.15)", color: "#3b82f6", border: "1px solid #3b82f633",
          }}>⚙️ 시스템 트리거</span>
        </div>
      )}
      {/* user buttons moved to bottom */ false && !streaming && !msg.id.startsWith("tmp-") && msg.intent !== "system_trigger" && (
        <div className="flex items-center gap-1 mr-2 opacity-0 group-hover:opacity-100 transition-opacity">
          <button
            onClick={() => {
              setEditingMsgId(msg.id);
              setEditText(msg.content);
            }}
            title="수정 후 재전송"
            style={{
              width: "28px", height: "28px", borderRadius: "50%",
              background: "var(--ct-ai)", border: "1px solid var(--ct-border)",
              color: "var(--ct-text2)", fontSize: "13px",
              display: "flex", alignItems: "center", justifyContent: "center",
              cursor: "pointer",
            }}
          >✏️</button>
          <button
            onClick={() => handleCopyToInput(msg.content)}
            title="입력창에 복사 (재지시)"
            style={{
              width: "28px", height: "28px", borderRadius: "50%",
              background: "var(--ct-ai)", border: "1px solid var(--ct-border)",
              color: "var(--ct-text2)", fontSize: "13px",
              display: "flex", alignItems: "center", justifyContent: "center",
              cursor: "pointer",
            }}
          >🔄</button>
          {onBranch && (
            <button
              onClick={() => onBranch?.(msg)}
              title="여기서 분기 (다른 질문으로 대화 분기)"
              style={{
                width: "28px", height: "28px", borderRadius: "50%",
                background: "rgba(34,197,94,0.1)", border: "1px solid rgba(34,197,94,0.3)",
                color: "#22c55e", fontSize: "13px",
                display: "flex", alignItems: "center", justifyContent: "center",
                cursor: "pointer",
              }}
            >🔀</button>
          )}
          <button
            onClick={() => handleDeleteMessage(msg.id, "user")}
            title="메시지 삭제 (AI 응답 포함)"
            style={{
              width: "28px", height: "28px", borderRadius: "50%",
              background: "rgba(239,68,68,0.1)", border: "1px solid rgba(239,68,68,0.3)",
              color: "#ef4444", fontSize: "13px",
              display: "flex", alignItems: "center", justifyContent: "center",
              cursor: "pointer",
            }}
          >🗑️</button>
        </div>
      )}

      <div style={{ maxWidth: "min(98%, calc(100vw - 20px))" }}>
        {/* Reply-to 인용 표시 */}
        {replyTarget && (
          <div style={{
            marginBottom: "4px", marginLeft: "4px", padding: "4px 10px",
            borderLeft: "3px solid var(--ct-accent)", background: "rgba(99,102,241,0.08)",
            borderRadius: "0 8px 8px 0", fontSize: "12px", color: "var(--ct-text2)",
            maxWidth: "100%", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
          }}>
            ↩ {replyTarget.content.slice(0, 100)}{replyTarget.content.length > 100 ? "..." : ""}
          </div>
        )}
        {/* 출처 배지: Pipeline Runner / Agent / System */}
        {msg.role === "assistant" && (() => {
          const badgeMap: Record<string, { icon: string; label: string; color: string; bg: string }> = {
            pipeline_runner: { icon: "🤖", label: "Pipeline Runner", color: "#f59e0b", bg: "rgba(245,158,11,0.15)" },
            agent_result: { icon: "⚡", label: "Agent", color: "#8b5cf6", bg: "rgba(139,92,246,0.15)" },
            system_recovery: { icon: "🔧", label: "System", color: "#ef4444", bg: "rgba(239,68,68,0.15)" },
            regenerated: { icon: "🔄", label: "이전 응답", color: "#6b7280", bg: "rgba(107,114,128,0.15)" },
          };
          const badge = msg.intent ? badgeMap[msg.intent] : null;
          return badge ? (
            <div style={{ marginBottom: "4px", marginLeft: "4px" }}>
              <span style={{
                display: "inline-flex", alignItems: "center", gap: "4px",
                padding: "2px 8px", borderRadius: "12px", fontSize: "11px", fontWeight: 600,
                background: badge.bg, color: badge.color, border: `1px solid ${badge.color}33`,
              }}>{badge.icon} {badge.label}</span>
            </div>
          ) : null;
        })()}
        {/* 인라인 편집 모드 (방식A) */}
        {msg.role === "user" && editingMsgId === msg.id ? (
          <div style={{
            borderRadius: "18px", overflow: "hidden",
            border: "2px solid var(--ct-accent)", borderBottomRightRadius: "4px",
          }}>
            <textarea
              autoFocus
              value={editText}
              onChange={(e) => setEditText(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); handleEditResend(msg.id, editText.trim()); }
                if (e.key === "Escape") { setEditingMsgId(null); setEditText(""); }
              }}
              style={{
                width: "100%", padding: "12px 16px", fontSize: "14px",
                background: "rgba(109,40,217,0.15)", color: "#fff",
                border: "none", outline: "none", resize: "none",
                minHeight: "120px", maxHeight: "400px", lineHeight: "1.6",
              }}
              rows={Math.min(editText.split("\n").length + 1, 8)}
            />
            <div style={{
              display: "flex", justifyContent: "flex-end", gap: "8px",
              padding: "8px 12px", background: "rgba(0,0,0,0.3)",
            }}>
              <button
                onClick={() => { setEditingMsgId(null); setEditText(""); }}
                style={{
                  fontSize: "12px", padding: "4px 12px", borderRadius: "8px",
                  background: "var(--ct-ai)", color: "var(--ct-text2)", border: "none", cursor: "pointer",
                }}
              >취소</button>
              <button
                onClick={() => handleEditResend(msg.id, editText.trim())}
                style={{
                  fontSize: "12px", padding: "4px 12px", borderRadius: "8px",
                  background: "var(--ct-accent)", color: "#fff", border: "none", cursor: "pointer",
                  fontWeight: 600,
                }}
              >수정 후 재전송</button>
            </div>
          </div>
        ) : (
        <div
          style={{
            padding: "12px 16px",
            borderRadius: "18px",
            fontSize: "14px",
            lineHeight: "1.6",
            ...(msg.role === "user"
              ? msg.is_system_group
                ? {
                    background: "transparent",
                    color: "var(--ct-text2)",
                    border: "1px dashed rgba(99,102,241,0.3)",
                    borderBottomRightRadius: "4px",
                    fontSize: "12px",
                    opacity: 0.75,
                  }
                : msg.intent === "system_trigger"
                ? {
                    background: "linear-gradient(135deg, var(--ct-ai), rgba(59,130,246,0.1))",
                    color: "var(--ct-text)",
                    border: "1px solid #3b82f644",
                    borderBottomRightRadius: "4px",
                    whiteSpace: "pre-wrap" as const,
                    fontStyle: "italic" as const,
                  }
                : {
                    background: "var(--ct-user)",
                    color: "#fff",
                    borderBottomRightRadius: "4px",
                    whiteSpace: "pre-wrap",
                  }
              : {
                  background: msg.intent === "streaming_placeholder"
                    ? "linear-gradient(135deg, var(--ct-ai), rgba(59,130,246,0.15))"
                    : msg.intent === "rate_limited"
                    ? "linear-gradient(135deg, var(--ct-ai), rgba(245,158,11,0.15))"
                    : msg.intent && ["pipeline_runner","agent_result","system_recovery"].includes(msg.intent)
                    ? `linear-gradient(135deg, var(--ct-ai), ${msg.intent === "pipeline_runner" ? "rgba(245,158,11,0.1)" : msg.intent === "agent_result" ? "rgba(139,92,246,0.1)" : "rgba(239,68,68,0.1)"})`
                    : "var(--ct-ai)",
                  color: "var(--ct-text)",
                  border: msg.intent === "streaming_placeholder"
                    ? "1px solid #3b82f666"
                    : msg.intent === "rate_limited"
                    ? "1px solid #f59e0b66"
                    : msg.intent && ["pipeline_runner","agent_result","system_recovery"].includes(msg.intent)
                    ? `1px solid ${msg.intent === "pipeline_runner" ? "#f59e0b44" : msg.intent === "agent_result" ? "#8b5cf644" : "#ef444444"}`
                    : "1px solid var(--ct-border)",
                  
                  ...(msg.intent === "regenerated" ? { opacity: 0.45 } : {}),
                  borderBottomLeftRadius: "4px",
                }),
          }}
        >
          {/* 첨부 이미지 표시: 그리드 레이아웃 + 라이트박스 */}
          {msg.role === "user" && (() => {
            const previews = msg.attachmentPreviews || [];
            const serverAtts = (msg.attachments || []).filter(
              (a) => (a.type === "image" || a.mime_type?.startsWith("image/") || a.media_type?.startsWith("image/")) && (a.file_url || a.base64)
            );
            const allImgs: string[] = [
              ...previews,
              ...serverAtts.map(att => att.file_url
                ? `${process.env.NEXT_PUBLIC_API_URL || "https://aads.newtalk.kr/api/v1"}${att.file_url}`
                : att.base64
                  ? `data:${att.mime_type || att.media_type || att.mime || "image/png"};base64,${att.base64}`
                  : ""
              ),
            ].filter(Boolean) as string[];
            if (allImgs.length === 0) return null;

            const maxVisible = Math.min(allImgs.length, 4);
            const hiddenCount = allImgs.length - 4;
            const cols = allImgs.length === 1 ? 1 : 2;
            const cellSize = allImgs.length === 1 ? "280px" : "136px";

            return (
              <div style={{
                display: "grid",
                gridTemplateColumns: `repeat(${cols}, ${cellSize})`,
                gap: "4px",
                marginBottom: "8px",
              }}>
                {allImgs.slice(0, maxVisible).map((url, imgIdx) => (
                  <div key={imgIdx} style={{ position: "relative", width: cellSize, height: cellSize, cursor: "pointer" }}
                    onClick={() => onOpenLightbox?.(allImgs, imgIdx)}>
                    <img src={url} alt="첨부 이미지"
                      style={{ width: "100%", height: "100%", objectFit: "cover", borderRadius: "8px",
                        border: "1px solid rgba(255,255,255,0.1)" }} />
                    {imgIdx === 3 && hiddenCount > 0 && (
                      <div style={{ position: "absolute", inset: 0,
                        background: "rgba(0,0,0,0.65)", display: "flex",
                        alignItems: "center", justifyContent: "center",
                        color: "#fff", fontSize: "22px", fontWeight: 700, borderRadius: "8px" }}>
                        +{hiddenCount}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            );
          })()}
          {msg.role === "user" ? (
            msg.is_system_group ? (
              <details style={{ cursor: "pointer" }}>
                <summary style={{ listStyle: "none", userSelect: "none", outline: "none", display: "flex", alignItems: "center", gap: "5px" }}>
                  <span style={{ fontSize: "10px", opacity: 0.5 }}>▶</span>
                  <span style={{ opacity: 0.7 }}>{msg.content.split("\n")[0]}</span>
                </summary>
                <div style={{ marginTop: "6px", paddingLeft: "10px", borderLeft: "2px solid rgba(99,102,241,0.3)" }}>
                  <MarkdownBlock text={msg.content.split("\n").slice(1).join("\n")} />
                </div>
              </details>
            ) : msg.intent === "system_trigger" ? <MarkdownBlock text={msg.content} /> : processInline(msg.content, { linkColor: "#fff" })
          ) : isActiveStreaming ? (
            <>
              {(streamToolLogs && streamToolLogs.length > 0 || streamToolStatus) && (
                <div style={{
                  fontSize: "12px", borderRadius: "8px",
                  background: "rgba(108,99,255,0.06)",
                  border: "1px solid rgba(108,99,255,0.2)",
                  padding: "8px 10px",
                  marginBottom: streamingContent ? "8px" : "0",
                  maxHeight: "180px", overflowY: "auto" as const,
                }}>
                  {streamToolLogs?.map((log, i) => (
                    <div key={i} style={{ marginBottom: "4px" }}>
                      <div style={{ display: "flex", alignItems: "center", gap: "5px", color: log.icon === "✅" ? "#4ade80" : "var(--ct-accent)" }}>
                        <span>{log.icon}</span>
                        <span style={{ fontWeight: 500 }}>{log.text}</span>
                      </div>
                      {log.sub && (
                        <div style={{ color: "#888", fontSize: "11px", marginLeft: "18px", fontFamily: "monospace", wordBreak: "break-all" as const }}>
                          {log.sub}
                        </div>
                      )}
                    </div>
                  ))}
                  {streamToolStatus && (
                    <div style={{ display: "flex", alignItems: "center", gap: "5px", color: "var(--ct-accent)", marginTop: (streamToolLogs?.length || 0) > 0 ? "4px" : "0" }}>
                      <span style={{ width: "6px", height: "6px", borderRadius: "50%", background: "var(--ct-accent)", animation: "ct-bounce 1.2s infinite", display: "inline-block" }} />
                      <span>{streamToolStatus}</span>
                    </div>
                  )}
                </div>
              )}
              {streamingContent ? (
                <>
                  <MarkdownBlock text={streamingContent} />
                  <span style={{
                    display: "inline-block", width: "2px", height: "14px",
                    background: "var(--ct-accent)", marginLeft: "2px",
                    animation: "ct-blink 1s step-end infinite",
                    verticalAlign: "text-bottom",
                  }} />
                </>
              ) : !streamToolStatus && (!streamToolLogs || streamToolLogs.length === 0) ? (
                <div style={{ display: "flex", gap: "4px", alignItems: "center", height: "20px" }}>
                  {[0, 1, 2].map((i) => (
                    <span key={i} style={{
                      width: "7px", height: "7px", borderRadius: "50%",
                      background: "var(--ct-accent)", display: "inline-block",
                      animation: "ct-bounce 1.2s infinite",
                      animationDelay: `${i * 0.2}s`,
                    }} />
                  ))}
                </div>
              ) : null}
            </>
          ) : (
            <>
              {msg.tools_called && Array.isArray(msg.tools_called) && msg.tools_called.length > 0 && (() => {
                const toolIcons: Record<string, string> = {
                  read_remote_file: "📄", read_github_file: "📄", list_remote_dir: "📁",
                  write_remote_file: "✏️", patch_remote_file: "✏️",
                  run_remote_command: "⚡", query_database: "🗄️", query_project_database: "🗄️",
                  web_search: "🔍", web_search_brave: "🔍", search_naver: "🔍", search_kakao: "🔍",
                  jina_read: "🌐", crawl4ai_fetch: "🌐", deep_crawl: "🌐", deep_research: "🔬",
                  health_check: "💊", get_all_service_status: "📊",
                  pipeline_runner_submit: "🚀", delegate_to_agent: "🤖",
                  save_note: "📝", recall_notes: "🧠", generate_image: "🎨",
                  send_telegram: "📨", fact_check: "🔎", evaluate_alerts: "🔔",
                };
                const getIcon = (name: string) => toolIcons[name] || "🔧";
                const getParam = (inp: any) => {
                  if (!inp || typeof inp !== 'object') return '';
                  const v = inp.path || inp.query || inp.url || inp.command || inp.file_path || inp.task || inp.project
                    || (Object.values(inp).filter((x: unknown) => typeof x === 'string')[0] as string) || '';
                  return String(v).slice(0, 80);
                };
                const toolUseCount = msg.tools_called!.filter((e: any) => e.type === 'tool_use').length;
                const lastEvent = [...msg.tools_called!].reverse().find((e: any) => e.type === 'tool_use' || e.type === 'tool_result');
                return (
                  <details style={{marginBottom: '8px'}}>
                    <summary style={{
                      cursor: 'pointer', fontSize: '12px', padding: '6px 10px',
                      borderRadius: '8px', background: 'rgba(108,99,255,0.06)',
                      border: '1px solid rgba(108,99,255,0.2)',
                      display: 'flex', alignItems: 'center', gap: '6px',
                      listStyle: 'none', userSelect: 'none' as const,
                    }}>
                      <span style={{fontSize: '10px', opacity: 0.6, transition: 'transform 0.2s'}}>▶</span>
                      <span style={{fontWeight: 500, color: 'var(--ct-accent)'}}>도구 {toolUseCount}개 사용</span>
                      {lastEvent && (
                        <span style={{opacity: 0.6, fontSize: '11px', marginLeft: '4px'}}>
                          — {lastEvent.type === 'tool_result' ? '✅' : getIcon(lastEvent.tool_name)} {lastEvent.tool_name}
                        </span>
                      )}
                    </summary>
                    <div style={{
                      padding: '8px 10px', marginTop: '4px',
                      borderRadius: '8px', background: 'rgba(108,99,255,0.06)',
                      border: '1px solid rgba(108,99,255,0.2)',
                      fontSize: '12px', maxHeight: '240px', overflowY: 'auto',
                    }}>
                      {msg.tools_called!.map((ev: any, i: number) => (
                        <div key={i} style={{marginBottom: '4px'}}>
                          {ev.type === 'tool_use' && (
                            <>
                              <div style={{display: 'flex', alignItems: 'center', gap: '5px', color: 'var(--ct-accent)'}}>
                                <span>{getIcon(ev.tool_name)}</span>
                                <span style={{fontWeight: 500}}>{ev.tool_name} 실행</span>
                              </div>
                              {ev.tool_input && getParam(ev.tool_input) && (
                                <div style={{color: '#888', fontSize: '11px', marginLeft: '18px', fontFamily: 'monospace', wordBreak: 'break-all' as const}}>
                                  {getParam(ev.tool_input)}
                                </div>
                              )}
                            </>
                          )}
                          {ev.type === 'tool_result' && (
                            <>
                              <div style={{display: 'flex', alignItems: 'center', gap: '5px', color: '#4ade80'}}>
                                <span>✅</span>
                                <span style={{fontWeight: 500}}>{ev.tool_name} 완료</span>
                              </div>
                              {ev.content && (
                                <div style={{color: '#888', fontSize: '11px', marginLeft: '18px', fontFamily: 'monospace', wordBreak: 'break-all' as const}}>
                                  {String(ev.content).slice(0, 120).replace(/\n/g, ' ')}
                                </div>
                              )}
                            </>
                          )}
                          {ev.type === 'thinking' && (
                            <div style={{display: 'flex', alignItems: 'center', gap: '5px'}}>
                              <span>💭</span>
                              <span style={{opacity: 0.7, fontSize: '11px'}}>{typeof ev.content === 'string' ? ev.content.slice(0, 100) : ''}</span>
                            </div>
                          )}
                        </div>
                      ))}
                    </div>
                  </details>
                );
              })()}
              {/* P1: 인라인 아티팩트 카드 — 긴 메시지 접이식 */}
              {msg.role === "assistant" && contentCollapsed && msg.content.length > 800 ? (
                <div>
                  {/* 아티팩트 카드 미리보기 */}
                  <div style={{
                    background: "linear-gradient(135deg, rgba(108,99,255,0.08), rgba(108,99,255,0.02))",
                    border: "1px solid rgba(108,99,255,0.25)",
                    borderLeft: "3px solid var(--ct-accent)",
                    borderRadius: "0 10px 10px 0",
                    padding: "10px 14px",
                    marginBottom: "4px",
                  }}>
                    {linkedArtifact && (
                      <div style={{ display: "flex", alignItems: "center", gap: "6px", marginBottom: "6px" }}>
                        <span style={{ fontSize: "14px" }}>
                          {linkedArtifact.artifact_type === "code" ? "💻" : linkedArtifact.artifact_type === "chart" ? "📊" : "📄"}
                        </span>
                        <span style={{ fontSize: "12px", fontWeight: 600, color: "var(--ct-accent)" }}>
                          {linkedArtifact.title}
                        </span>
                      </div>
                    )}
                    <div style={{ fontSize: "13px", color: "var(--ct-text)", lineHeight: "1.6" }}>
                      <MarkdownBlock text={msg.content.substring(0, 300) + "\n\n..."} />
                    </div>
                    <div style={{ display: "flex", gap: "8px", marginTop: "8px" }}>
                      <button
                        onClick={() => setContentCollapsed(false)}
                        style={{
                          fontSize: "11px", padding: "3px 10px", borderRadius: "6px",
                          background: "rgba(108,99,255,0.1)", color: "var(--ct-accent)",
                          border: "1px solid rgba(108,99,255,0.3)", cursor: "pointer",
                          fontWeight: 500,
                        }}
                      >
                        전체 펼치기 ▾
                      </button>
                      {onViewArtifact && linkedArtifact && (
                        <button
                          onClick={() => onViewArtifact(linkedArtifact.id)}
                          style={{
                            fontSize: "11px", padding: "3px 10px", borderRadius: "6px",
                            background: "var(--ct-accent)", color: "#fff",
                            border: "none", cursor: "pointer",
                            fontWeight: 500,
                          }}
                        >
                          우측 패널에서 보기 →
                        </button>
                      )}
                      {onViewReport && !linkedArtifact && (
                        <button
                          onClick={onViewReport}
                          style={{
                            fontSize: "11px", padding: "3px 10px", borderRadius: "6px",
                            background: "var(--ct-accent)", color: "#fff",
                            border: "none", cursor: "pointer",
                            fontWeight: 500,
                          }}
                        >
                          우측 패널에서 보기 →
                        </button>
                      )}
                    </div>
                  </div>
                </div>
              ) : (
                <>
                  <MarkdownBlock text={msg.content} />
                  {msg.role === "assistant" && msg.content.length > 800 && !contentCollapsed && (
                    <div style={{ textAlign: "right", marginTop: "4px" }}>
                      <button
                        onClick={() => setContentCollapsed(true)}
                        style={{
                          fontSize: "11px", padding: "2px 8px", borderRadius: "6px",
                          background: "rgba(108,99,255,0.08)", color: "var(--ct-text2)",
                          border: "1px solid var(--ct-border)", cursor: "pointer",
                        }}
                      >
                        접기 ▴
                      </button>
                    </div>
                  )}
                </>
              )}
              {/* 3번: rate_limited 안내 — 자동 재개 중임을 사용자에게 표시 */}
              {msg.intent === "rate_limited" && (
                <div style={{ fontSize: "12px", color: "#f59e0b", marginTop: "8px", opacity: 0.85 }}>
                  ⏳ API 한도 도달 — 자동으로 이어집니다...
                </div>
              )}
            </>
          )}
        </div>
        )}
        {/* P1: 기존 pipeline_runner 카드 — 접이식으로 통합됨 */}
        {/* 사용자 메시지 타임스탬프 + (수정됨) 표시 */}
        {msg.role === "user" && msg.created_at && (
          <div style={{ fontSize: "11px", color: "var(--ct-text2)", marginTop: "4px", textAlign: "right", marginRight: "4px" }}>
            {msg.edited_at && <span style={{ color: "var(--ct-accent)" }}>(수정됨) </span>}
            {new Date(msg.created_at).toLocaleTimeString("ko-KR", { timeZone: "Asia/Seoul", hour: "2-digit", minute: "2-digit" })}
          </div>
        )}
        {isActiveStreaming && onStopStreaming && (
          <button type="button" onClick={onStopStreaming} style={{
            marginTop: "4px", marginLeft: "4px", padding: "2px 8px",
            fontSize: "11px", fontWeight: 500,
            background: "transparent", color: "var(--ct-muted)",
            border: "1px solid var(--ct-border)", borderRadius: "10px",
            cursor: "pointer", transition: "all 0.15s",
          }}
          onMouseEnter={(e) => { e.currentTarget.style.background = "#ef4444"; e.currentTarget.style.color = "#fff"; e.currentTarget.style.borderColor = "#ef4444"; }}
          onMouseLeave={(e) => { e.currentTarget.style.background = "transparent"; e.currentTarget.style.color = "var(--ct-muted)"; e.currentTarget.style.borderColor = "var(--ct-border)"; }}
          >■ 중지</button>
        )}
        {msg.role === "assistant" && !isActiveStreaming && (
          <div
            style={{
              fontSize: "11px",
              color: "var(--ct-text2)",
              marginTop: "4px",
              marginLeft: "4px",
              display: "flex",
              alignItems: "center",
              gap: "4px",
            }}
          >
            <span>
              {msg.model_used && !['recovered','streaming','stopped','interrupted','semantic_cache'].includes(msg.model_used) && <span>[{msg.model_used}</span>}
              {(msg.input_tokens || msg.tokens_in) ? ` · ${(msg.input_tokens || msg.tokens_in || 0).toLocaleString()}in` : ""}
              {(msg.output_tokens || msg.tokens_out) ? ` · ${(msg.output_tokens || msg.tokens_out || 0).toLocaleString()}out` : ""}
              {(() => { const c = msg.cost_usd || msg.cost; return c && Number(c) > 0 ? ` · $${Number(c).toFixed(4)}` : ""; })()}
              {msg.model_used && !['recovered','streaming','stopped','interrupted','semantic_cache'].includes(msg.model_used) && <span>]</span>}
              {msg.created_at && (
                <span style={{ marginLeft: msg.model_used ? "6px" : "0" }}>
                  {new Date(msg.created_at).toLocaleString("ko-KR", {
                    timeZone: "Asia/Seoul",
                    month: "numeric", day: "numeric",
                    hour: "2-digit", minute: "2-digit", second: "2-digit",
                  })}
                </span>
              )}
              {msg.confidence_label && (
                <ConfidenceBadge label={msg.confidence_label} />
              )}
            </span>
            {onReplyTo && !streaming && !msg.id.startsWith("tmp-") && (
              <button
                onClick={() => onReplyTo(msg)}
                title="이 응답에 답글"
                style={{
                  width: "28px", height: "28px", borderRadius: "6px",
                  background: "rgba(99,102,241,0.08)", border: "1px solid rgba(99,102,241,0.2)",
                  color: "#6366f1", fontSize: "14px", fontWeight: "bold",
                  display: "inline-flex", alignItems: "center", justifyContent: "center",
                  cursor: "pointer", opacity: 0.7, transition: "all 0.2s",
                  marginLeft: "4px",
                }}
                onMouseEnter={(e) => { (e.target as HTMLElement).style.opacity = "1"; (e.target as HTMLElement).style.background = "rgba(99,102,241,0.15)"; (e.target as HTMLElement).style.borderColor = "#6366f1"; }}
                onMouseLeave={(e) => { (e.target as HTMLElement).style.opacity = "0.7"; (e.target as HTMLElement).style.background = "rgba(99,102,241,0.08)"; (e.target as HTMLElement).style.borderColor = "rgba(99,102,241,0.2)"; }}
              >↩</button>
            )}
            {onRegenerate && !streaming && !msg.id.startsWith("tmp-") && msg.intent !== "streaming_placeholder" && msg.intent !== "rate_limited" && (
              <button
                onClick={() => onRegenerate(msg.id)}
                title="다시 생성"
                style={{
                  width: "28px", height: "28px", borderRadius: "6px",
                  background: "rgba(34,197,94,0.08)", border: "1px solid rgba(34,197,94,0.2)",
                  color: "#22c55e", fontSize: "14px",
                  display: "inline-flex", alignItems: "center", justifyContent: "center",
                  cursor: "pointer", opacity: 0.7, transition: "all 0.2s",
                  marginLeft: "4px",
                }}
                onMouseEnter={(e) => { (e.target as HTMLElement).style.opacity = "1"; (e.target as HTMLElement).style.background = "rgba(34,197,94,0.15)"; (e.target as HTMLElement).style.borderColor = "#22c55e"; }}
                onMouseLeave={(e) => { (e.target as HTMLElement).style.opacity = "0.7"; (e.target as HTMLElement).style.background = "rgba(34,197,94,0.08)"; (e.target as HTMLElement).style.borderColor = "rgba(34,197,94,0.2)"; }}
              >🔄</button>
            )}
            <button
              onClick={() => handleDeleteMessage(msg.id, "assistant")}
              title="이 응답 삭제"
              style={{
                width: "20px", height: "20px", borderRadius: "50%",
                background: "transparent", border: "1px solid transparent",
                color: "var(--ct-text2)", fontSize: "11px",
                display: "inline-flex", alignItems: "center", justifyContent: "center",
                cursor: "pointer", opacity: 0.4, transition: "opacity 0.2s",
              }}
              onMouseEnter={(e) => { (e.target as HTMLElement).style.opacity = "1"; (e.target as HTMLElement).style.color = "#ef4444"; }}
              onMouseLeave={(e) => { (e.target as HTMLElement).style.opacity = "0.4"; (e.target as HTMLElement).style.color = "var(--ct-text2)"; }}
            >🗑️</button>
          </div>
        )}
        {/* user bottom action buttons */}
        {msg.role === "user" && !streaming && !msg.id.startsWith("tmp-") && msg.intent !== "system_trigger" && editingMsgId !== msg.id && (
          <div style={{
            display: "flex", justifyContent: "flex-end", gap: "4px",
            marginTop: "4px", marginRight: "4px",
          }}>
            <button
              onClick={() => { setEditingMsgId(msg.id); setEditText(msg.content); }}
              title="수정 후 재전송"
              style={{
                width: "26px", height: "26px", borderRadius: "6px",
                background: "rgba(109,40,217,0.08)", border: "1px solid rgba(109,40,217,0.2)",
                color: "var(--ct-text2)", fontSize: "12px",
                display: "flex", alignItems: "center", justifyContent: "center",
                cursor: "pointer", opacity: 0.7, transition: "all 0.2s",
              }}
              onMouseEnter={(e) => { (e.target as HTMLElement).style.opacity = "1"; (e.target as HTMLElement).style.background = "rgba(109,40,217,0.15)"; }}
              onMouseLeave={(e) => { (e.target as HTMLElement).style.opacity = "0.7"; (e.target as HTMLElement).style.background = "rgba(109,40,217,0.08)"; }}
            >✏️</button>
            <button
              onClick={() => handleCopyToInput(msg.content)}
              title="입력창에 복사 (재지시)"
              style={{
                width: "26px", height: "26px", borderRadius: "6px",
                background: "rgba(59,130,246,0.08)", border: "1px solid rgba(59,130,246,0.2)",
                color: "var(--ct-text2)", fontSize: "12px",
                display: "flex", alignItems: "center", justifyContent: "center",
                cursor: "pointer", opacity: 0.7, transition: "all 0.2s",
              }}
              onMouseEnter={(e) => { (e.target as HTMLElement).style.opacity = "1"; (e.target as HTMLElement).style.background = "rgba(59,130,246,0.15)"; }}
              onMouseLeave={(e) => { (e.target as HTMLElement).style.opacity = "0.7"; (e.target as HTMLElement).style.background = "rgba(59,130,246,0.08)"; }}
            >🔄</button>
            {onBranch && (
              <button
                onClick={() => onBranch?.(msg)}
                title="여기서 분기"
                style={{
                  width: "26px", height: "26px", borderRadius: "6px",
                  background: "rgba(34,197,94,0.08)", border: "1px solid rgba(34,197,94,0.2)",
                  color: "#22c55e", fontSize: "12px",
                  display: "flex", alignItems: "center", justifyContent: "center",
                  cursor: "pointer", opacity: 0.7, transition: "all 0.2s",
                }}
                onMouseEnter={(e) => { (e.target as HTMLElement).style.opacity = "1"; (e.target as HTMLElement).style.background = "rgba(34,197,94,0.15)"; }}
                onMouseLeave={(e) => { (e.target as HTMLElement).style.opacity = "0.7"; (e.target as HTMLElement).style.background = "rgba(34,197,94,0.08)"; }}
              >🔀</button>
            )}
            <button
              onClick={() => handleDeleteMessage(msg.id, "user")}
              title="메시지 삭제 (AI 응답 포함)"
              style={{
                width: "26px", height: "26px", borderRadius: "6px",
                background: "rgba(239,68,68,0.08)", border: "1px solid rgba(239,68,68,0.2)",
                color: "#ef4444", fontSize: "12px",
                display: "flex", alignItems: "center", justifyContent: "center",
                cursor: "pointer", opacity: 0.7, transition: "all 0.2s",
              }}
              onMouseEnter={(e) => { (e.target as HTMLElement).style.opacity = "1"; (e.target as HTMLElement).style.background = "rgba(239,68,68,0.15)"; }}
              onMouseLeave={(e) => { (e.target as HTMLElement).style.opacity = "0.7"; (e.target as HTMLElement).style.background = "rgba(239,68,68,0.08)"; }}
            >🗑️</button>
          </div>
        )}
      </div>
    </div>
  );
}, (prev, next) =>
  prev.msg.id === next.msg.id &&
  prev.msg.content === next.msg.content &&
  prev.msg.role === next.msg.role &&
  prev.msg.intent === next.msg.intent &&
  prev.msg.reply_to_id === next.msg.reply_to_id &&
  prev.streaming === next.streaming &&
  prev.editingMsgId === next.editingMsgId &&
  (prev.editingMsgId === prev.msg.id ? prev.editText === next.editText : true) &&
  prev.msg.tools_called === next.msg.tools_called &&
  prev.isActiveStreaming === next.isActiveStreaming &&
  prev.streamingContent === next.streamingContent &&
  prev.streamToolStatus === next.streamToolStatus &&
  prev.streamToolLogs === next.streamToolLogs
);

// Main component
// ══════════════════════════════════════════════════════════════════
export default function ChatPage() {
  // ── Theme / layout ──
  const [theme, setTheme] = useState<Theme>("dark");
  const [leftOpen, setLeftOpen] = useState(true);
  const [artifactMode, setArtifactMode] = useState<ArtifactMode>("full");
  const [artifactTab, setArtifactTab] = useState<ArtifactTab>("report");
  const [unreadLogCount, setUnreadLogCount] = useState(0);
  const [screenSize, setScreenSize] = useState<ScreenSize>("desktop");

  // ── Data ──
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [activeWs, setActiveWs] = useState<string | null>(null);
  const [sessions, setSessions] = useState<ChatSession[]>([]);
  const [activeSession, setActiveSession] = useState<ChatSession | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [artifacts, setArtifacts] = useState<Artifact[]>([]);

  // ── Chat state ──
  const [input, setInput] = useState("");
  const [hasInput, setHasInput] = useState(false);
  const [model, setModel] = useState(DEFAULT_MODEL);
  const [runtimeModels, setRuntimeModels] = useState<LlmRegistryModel[] | null>(null);
  const [modelPreferences, setModelPreferences] = useState<ChatModelPreference[]>([]);
  const [streaming, setStreaming] = useState(false);
  const [streamBuf, setStreamBuf] = useState("");
  const streamBufRef = useRef("");
  const [toolStatus, setToolStatus] = useState<string | null>(null);
  const [toolLogs, setToolLogs] = useState<{icon:string; text:string; sub?:string}[]>([]);
  // AADS-190: 세션 비용/턴 + Yellow 경고 + 도구턴 한도
  const [sessionCost, setSessionCost] = useState<string | null>(null);
  const [sessionTurns, setSessionTurns] = useState<number | null>(null);
  const [yellowWarning, setYellowWarning] = useState<string | null>(null);
  const [toolTurnInfo, setToolTurnInfo] = useState<string | null>(null);
  const msgQueueRef = useRef<string[]>([]);
  const [queueCount, setQueueCount] = useState(0);
  // API 키 상태 표시
  const [apiKeyInfo, setApiKeyInfo] = useState<ApiKeyInfoState | null>(null);
  const [showAuthPanel, setShowAuthPanel] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [messagesLoading, setMessagesLoading] = useState(false);
  const [showImageGen, setShowImageGen] = useState(false);
  const [showMobileActions, setShowMobileActions] = useState(true);
  const [imageGenPrompt, setImageGenPrompt] = useState("");
  const [imageGenLoading, setImageGenLoading] = useState(false);
  // 메시지 수정/재지시
  const [editingMsgId, setEditingMsgId] = useState<string | null>(null);
  const [editText, setEditText] = useState("");
  const [editMode, setEditMode] = useState<string | null>(null);  // 재지시 배너용
  const [replyToMessage, setReplyToMessage] = useState<ChatMessage | null>(null);
  // P2-2: 대화 분기
  const [branchPoint, setBranchPoint] = useState<ChatMessage | null>(null);
  const branchPointRef = useRef(branchPoint);
  useEffect(() => { branchPointRef.current = branchPoint; }, [branchPoint]);

  // 라이트박스
  const [lightboxSrcs, setLightboxSrcs] = useState<string[]>([]);
  const [lightboxIdx, setLightboxIdx] = useState(0);

  // 배포 버전 체크 (30초 간격)
  const { updateAvailable, doRefresh, setStreaming: setVersionStreaming } = useVersionCheck(30000);

  // ── UI state ──
  const [search, setSearch] = useState("");
  const [tagFilter, setTagFilter] = useState<string | null>(null);
  const [tagEditSession, setTagEditSession] = useState<{ id: string; tags: string[] } | null>(null);
  const [tagInput, setTagInput] = useState("");
  const [contextMenu, setContextMenu] = useState<{ x: number; y: number; session: ChatSession } | null>(null);
  const [renaming, setRenaming] = useState<{ id: string; value: string } | null>(null);
  const [mobileOverlay, setMobileOverlay] = useState<"sidebar" | "artifact" | null>(null);
  const [showShortcutHelp, setShowShortcutHelp] = useState(false);
  const swipeRef = useRef<{ startX: number; startY: number; t: number } | null>(null);
  const [selectedArtifactIdx, setSelectedArtifactIdx] = useState(0);

  // ── 프로젝트 추가 모달 ──
  const [showAddProject, setShowAddProject] = useState(false);
  const [newProjectCode, setNewProjectCode] = useState("");
  const [newProjectName, setNewProjectName] = useState("");
  const [newProjectIcon, setNewProjectIcon] = useState("📁");

  // ── Prompt Templates (P2-10) ──
  const [showTemplates, setShowTemplates] = useState(false);
  const [templates, setTemplates] = useState<Array<{ id: string; title: string; content: string; category: string; usage_count: number; created_at: string; updated_at: string }>>([]);
  const [templateTab, setTemplateTab] = useState("전체");
  const [showNewTemplate, setShowNewTemplate] = useState(false);
  const [newTplTitle, setNewTplTitle] = useState("");
  const [newTplCategory, setNewTplCategory] = useState("일반");

  // ── Proactive Briefing ──
  const [briefing, setBriefing] = useState<{ message: string; collapsed: boolean } | null>(null);
  const briefingShownRef = useRef<Set<string>>(new Set());

  // ── AADS-188D: diff_preview 승인 패널 ──
  const diffApproval = useDiffApproval();

  const selectableModels = useMemo<SelectableModelOption[]>(() => {
    const preferenceMap = buildNormalizedPreferenceMap(modelPreferences);
    const autoOption = {
      ...(STATIC_MODEL_OPTION_MAP.get("mixture") || { id: "mixture", name: "자동 라우팅 (혼합)", provider: "auto", cost: "자동" }),
      isActive: true,
      isPinned: preferenceMap.get("mixture")?.is_pinned ?? false,
      isFavorite: preferenceMap.get("mixture")?.is_favorite ?? false,
      isHidden: preferenceMap.get("mixture")?.is_hidden ?? false,
    };

    if (runtimeModels === null) {
      const currentModelId = normalizeModelIdForSelector(model || DEFAULT_MODEL);
      const currentOption = STATIC_MODEL_OPTION_MAP.get(currentModelId);
      return currentModelId && currentModelId !== "mixture"
        ? [
            autoOption,
            currentOption
              ? {
                  ...currentOption,
                  isActive: true,
                  isPinned: preferenceMap.get(currentModelId)?.is_pinned ?? false,
                  isFavorite: preferenceMap.get(currentModelId)?.is_favorite ?? false,
                  isHidden: preferenceMap.get(currentModelId)?.is_hidden ?? false,
                }
              : {
                  id: currentModelId,
                  name: currentModelId,
                  provider: "legacy",
                  cost: "변동",
                  isActive: true,
                  isPinned: preferenceMap.get(currentModelId)?.is_pinned ?? false,
                  isFavorite: preferenceMap.get(currentModelId)?.is_favorite ?? false,
                  isHidden: preferenceMap.get(currentModelId)?.is_hidden ?? false,
                },
          ]
        : [autoOption];
    }

    const currentModelId = normalizeModelIdForSelector(model);
    const activeOptions = runtimeModels.map((row) => {
      const option = buildSelectableModelOption(row);
      const preference = preferenceMap.get(option.id);
      return {
        ...option,
        isPinned: preference?.is_pinned ?? false,
        isFavorite: preference?.is_favorite ?? false,
        isHidden: preference?.is_hidden ?? false,
      };
    });
    const activeOptionsMap = new Map(activeOptions.map((option) => [option.id, option]));
    const orderedOptions = Array.from(activeOptionsMap.values())
      .filter((option) => !option.isHidden || option.id === currentModelId)
      .sort((a, b) => compareSelectableModels(a, b, preferenceMap))
      .map((option) => ({
        ...option,
        name: `${option.isPinned ? "📌 " : option.isFavorite ? "★ " : ""}${option.name}`,
      }));

    const options: SelectableModelOption[] = [autoOption, ...orderedOptions];
    if (currentModelId && currentModelId !== "mixture" && !options.some((option) => option.id === currentModelId)) {
      const currentOption = STATIC_MODEL_OPTION_MAP.get(currentModelId);
      options.push(
        currentOption
          ? {
              ...currentOption,
              name: `${currentOption.name} (비활성)`,
              isActive: false,
              isPinned: preferenceMap.get(currentModelId)?.is_pinned ?? false,
              isFavorite: preferenceMap.get(currentModelId)?.is_favorite ?? false,
              isHidden: preferenceMap.get(currentModelId)?.is_hidden ?? false,
            }
          : {
              id: currentModelId,
              name: `${currentModelId} (비활성)`,
              provider: "legacy",
              cost: "변동",
              isActive: false,
              isPinned: preferenceMap.get(currentModelId)?.is_pinned ?? false,
              isFavorite: preferenceMap.get(currentModelId)?.is_favorite ?? false,
              isHidden: preferenceMap.get(currentModelId)?.is_hidden ?? false,
            }
      );
    }
    return options;
  }, [model, modelPreferences, runtimeModels]);

  const activeSelectableModelIds = useMemo(
    () => new Set(selectableModels.filter((option) => option.isActive).map((option) => option.id)),
    [selectableModels]
  );

  // ── Refs ──
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const messagesContainerRef = useRef<HTMLDivElement>(null);
  const isInitialLoadRef = useRef(true);
  const isNearBottomRef = useRef(true);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const chatInputRef = useRef<ChatInputHandle>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const pendingAttachments = useRef<Array<Record<string, any>>>([]);
  const [pendingPreviewFiles, setPendingPreviewFiles] = useState<File[]>([]);
  const [screenHiddenMode] = useState(true);
  const screenContextRef = useRef<File | null>(null);
  const aiCaptureRequestedRef = useRef(false);
  // C-2: Object URL 캐싱 + 메모리 누수 방지
  const pendingPreviewUrls = useMemo(
    () => pendingPreviewFiles.map((f) => f.type.startsWith("image/") ? URL.createObjectURL(f) : null),
    [pendingPreviewFiles]
  );
  useEffect(() => {
    return () => { pendingPreviewUrls.forEach((u) => u && URL.revokeObjectURL(u)); };
  }, [pendingPreviewUrls]);
  // P2-2: 분기 모드 활성화 시 입력창 포커스
  useEffect(() => { if (branchPoint) textareaRef.current?.focus(); }, [branchPoint]);
  const abortCtrl = useRef<AbortController | null>(null);
  const sessionSwitchRef = useRef(false);
  const activeSessionRef = useRef<string | null>(null);
  // BUG-2 FIX: 초기 로드와 워크스페이스 전환 구분
  const initialWsLoadRef = useRef(true);
  // BUG-REFRESH FIX: 초기 마운트 시 hash 삭제 방지
  const isFirstMountRef = useRef(true);
  // 스트리밍 중인 세션 ID 추적 — 세션 전환 시 다른 세션 내용 깜빡임 방지
  const streamingSessionRef = useRef<string | null>(null);

  const refreshChatModelCatalog = useCallback(async () => {
    const [modelsRes, preferencesRes] = await Promise.allSettled([
      chatApi<{ models: LlmRegistryModel[] }>("/llm-models?active_only=true"),
      chatApi<{ preferences: ChatModelPreference[] }>("/llm-models/chat-preferences"),
    ]);
    if (modelsRes.status === "fulfilled") {
      setRuntimeModels(Array.isArray(modelsRes.value.models) ? modelsRes.value.models : []);
    }
    if (preferencesRes.status === "fulfilled") {
      setModelPreferences(Array.isArray(preferencesRes.value.preferences) ? preferencesRes.value.preferences : []);
    } else {
      setModelPreferences([]);
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      const [modelsRes, preferencesRes] = await Promise.allSettled([
        chatApi<{ models: LlmRegistryModel[] }>("/llm-models?active_only=true"),
        chatApi<{ preferences: ChatModelPreference[] }>("/llm-models/chat-preferences"),
      ]);
      if (cancelled) return;
      if (modelsRes.status === "fulfilled") {
        setRuntimeModels(Array.isArray(modelsRes.value.models) ? modelsRes.value.models : []);
      }
      if (preferencesRes.status === "fulfilled") {
        setModelPreferences(Array.isArray(preferencesRes.value.preferences) ? preferencesRes.value.preferences : []);
      } else {
        setModelPreferences([]);
      }
    };
    void load();

    const handleVisibility = () => {
      if (document.visibilityState === "visible") {
        void refreshChatModelCatalog();
      }
    };
    const handleFocus = () => {
      void refreshChatModelCatalog();
    };
    window.addEventListener("focus", handleFocus);
    document.addEventListener("visibilitychange", handleVisibility);
    return () => {
      cancelled = true;
      window.removeEventListener("focus", handleFocus);
      document.removeEventListener("visibilitychange", handleVisibility);
    };
  }, [refreshChatModelCatalog]);

  useEffect(() => {
    if (!activeSession || runtimeModels === null) return;
    const currentModelId = normalizeModelIdForSelector(model);
    if (!currentModelId || currentModelId === "mixture" || activeSelectableModelIds.has(currentModelId)) return;
    const fallbackModel = activeSelectableModelIds.has(DEFAULT_MODEL) ? DEFAULT_MODEL : "mixture";
    if (!fallbackModel || fallbackModel === currentModelId) return;

    setModel(fallbackModel);
    setSessions((prev) =>
      prev.map((session) =>
        session.id === activeSession.id ? { ...session, current_model: fallbackModel } : session
      )
    );
    setActiveSession((prev) =>
      prev && prev.id === activeSession.id ? { ...prev, current_model: fallbackModel } : prev
    );
    chatApi(`/chat/sessions/${activeSession.id}`, {
      method: "PUT",
      body: JSON.stringify({ current_model: fallbackModel }),
    }).catch(() => {});
  }, [activeSelectableModelIds, activeSession, model, runtimeModels]);
  // 세션 이동 시 생성 중이던 세션 ID 추적 (돌아오면 빠른 폴링)
  const pendingResponseSessions = useRef<Set<string>>(new Set());
  const [waitingBgResponse, setWaitingBgResponse] = useState(false);
  const [bgPartialContent, setBgPartialContent] = useState("");
  const [completionToast, setCompletionToast] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [hasMoreMessages, setHasMoreMessages] = useState(false);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const lastToastTimeRef = useRef<number>(0);
  const lastToastedAiIdRef = useRef<string>("");   // 토스트 발생한 AI 메시지 ID — 동일 메시지 이중 토스트 차단
  const lastKnownMsgIdRef = useRef<string | null>(null);  // PERF: 폴링 최적화 — streaming-status의 last_message_id 변경 감지
  const rateLimitedPollRef = useRef(false);  // 2번: rate_limited 메시지 감지 시 자동 폴링 활성 추적
  const [expandedDupeGroups, setExpandedDupeGroups] = useState<Set<string>>(new Set());  // 4번: 중복 메시지 그룹 펼침 상태
  const lastEventIdRef = useRef<string>("");  // Phase4: Redis Stream entry ID — SSE 재연결 시 Last-Event-ID로 사용
  const completionToastTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const yellowWarningTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [artifactToast, setArtifactToast] = useState<string | null>(null);
  const artifactToastTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const artifactFetchingRef = useRef(false); // 중복 re-fetch 방지
  const artifactFetchTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // PERF: 이전 메시지 로드 — cursor 기반 페이지네이션
  const loadOlderMessages = useCallback(async () => {
    if (!activeSession?.id || messages.length === 0 || !nextCursor) return;
    const container = messagesContainerRef.current;
    const prevScrollHeight = container?.scrollHeight || 0;
    const result = await chatApi<{ messages: ChatMessage[]; next_cursor: string | null; has_more: boolean }>(
      `/chat/messages?session_id=${activeSession.id}&limit=100&cursor=${encodeURIComponent(nextCursor)}`
    ).catch(() => null);
    if (result && result.messages.length > 0) {
      setHasMoreMessages(result.has_more);
      setNextCursor(result.next_cursor);
      const filtered = result.messages.map(m => {
        if (m.intent !== "streaming_placeholder") return m;
        // FIX: placeholder 삭제 금지 — 내용 있으면 recovered로, 없으면 생성 중 표시
        if (m.content && m.content.trim().length > 10) return { ...m, intent: undefined, model_used: "recovered" };
        return { ...m, content: m.content || "⏳ AI가 응답을 생성 중입니다..." };
      }) as ChatMessage[];
      setMessages(prev => {
        const existingIds = new Set(prev.map(m => m.id));
        const unique = filtered.filter(m => !existingIds.has(m.id));
        return [...unique, ...prev];
      });
      requestAnimationFrame(() => {
        if (container) container.scrollTop = container.scrollHeight - prevScrollHeight;
      });
    } else {
      setHasMoreMessages(false);
      setNextCursor(null);
    }
  }, [activeSession?.id, messages.length, nextCursor]);

  // 개선2: 자동 트리거 응답 판별 함수 — 3곳 중복 제거
  const isAutoTriggerResponse = (lastUser: ChatMessage | undefined, lastAi: ChatMessage | undefined): boolean => {
    return !!(lastUser?.content?.startsWith("[시스템]") || lastUser?.intent === "auto_reaction" || lastUser?.intent === "system_trigger" || lastAi?.intent === "auto_reaction" || lastAi?.intent === "runner_response" || lastAi?.intent === "interrupted");
  };

  // ── Performance: ref로 폴링 useEffect 의존성 폭탄 방지 ──
  const streamingRef = useRef(streaming);
  const waitingBgRef = useRef(waitingBgResponse);
  const waitingBgTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => { streamingRef.current = streaming; }, [streaming]);
  useEffect(() => { waitingBgRef.current = waitingBgResponse; }, [waitingBgResponse]);

  // ── H-3: sendMessage useCallback 안정화용 ref ──
  const activeSessionObjRef = useRef(activeSession);
  const modelRef = useRef(model);
  const activeWsRef = useRef(activeWs);
  const pendingPreviewFilesRef = useRef(pendingPreviewFiles);
  const replyToMessageRef = useRef(replyToMessage);
  const inputRef = useRef(input);
  const toolStatusRef = useRef(toolStatus);
  const screenSizeRef = useRef(screenSize);
  const uploadingRef = useRef(uploading);
  const queueCountRef = useRef(queueCount);
  useEffect(() => { activeSessionObjRef.current = activeSession; }, [activeSession]);
  useEffect(() => { modelRef.current = model; }, [model]);
  useEffect(() => { activeWsRef.current = activeWs; }, [activeWs]);
  useEffect(() => { pendingPreviewFilesRef.current = pendingPreviewFiles; }, [pendingPreviewFiles]);
  useEffect(() => { replyToMessageRef.current = replyToMessage; }, [replyToMessage]);
  useEffect(() => { inputRef.current = input; }, [input]);
  useEffect(() => { toolStatusRef.current = toolStatus; }, [toolStatus]);
  useEffect(() => { screenSizeRef.current = screenSize; }, [screenSize]);
  useEffect(() => { uploadingRef.current = uploading; }, [uploading]);
  useEffect(() => { queueCountRef.current = queueCount; }, [queueCount]);

  // Runner 응답 판별 — intent 또는 컨텐츠 패턴으로 소급 적용
  const isRunnerMsg = (m: ChatMessage) =>
    m.intent === "runner_response" ||
    (m.role === "assistant" && (
      m.content?.includes("[Pipeline Runner]") ||
      m.content?.includes("[Runner]") ||
      (m.content?.startsWith("Step ") && m.content?.includes("runner-")) ||
      m.content?.includes("pipeline_runner_approve") ||
      m.content?.includes("배포 검증 5단계")
    ));

  // 시스템 메시지 목록 (로그 탭용)
  const systemMessages = messages.filter(
    (m) => m.intent === "auto_reaction" || m.intent === "runner_response" || m.intent === "pipeline_c" || isRunnerMsg(m) || (m.role === "user" && m.intent === "system_trigger")
  );
  // ── 로그 탭 unread 카운트 ──
  const prevSystemMsgCountRef = useRef(0);
  useEffect(() => {
    if (artifactTab === "log") setUnreadLogCount(0);
  }, [artifactTab]);
  useEffect(() => {
    const current = messages.filter(
      (m) => m.intent === "auto_reaction" || m.intent === "runner_response" || m.intent === "pipeline_c" || isRunnerMsg(m) || (m.role === "user" && m.intent === "system_trigger")
    ).length;
    if (current > prevSystemMsgCountRef.current && artifactTab !== "log") {
      setUnreadLogCount(n => n + (current - prevSystemMsgCountRef.current));
    }
    prevSystemMsgCountRef.current = current;
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [messages.length]);

  // ── 타이머 ref 언마운트 정리 ──
  useEffect(() => {
    return () => {
      if (completionToastTimerRef.current) clearTimeout(completionToastTimerRef.current);
      if (yellowWarningTimerRef.current) clearTimeout(yellowWarningTimerRef.current);
    };
  }, []);

  // ── 토스트 디바운스 (5초 내 중복 차단) ──
  const showCompletionToast = useCallback((msg: string) => {
    const now = Date.now();
    if (now - lastToastTimeRef.current < 5000) return;
    lastToastTimeRef.current = now;
    setCompletionToast(msg);
    if (completionToastTimerRef.current) clearTimeout(completionToastTimerRef.current);
    completionToastTimerRef.current = setTimeout(() => setCompletionToast(null), 3000);
  }, []);

  // ── Init theme ──
  useEffect(() => {
    const saved = typeof window !== "undefined" ? localStorage.getItem("aads-chat-theme") : null;
    if (saved === "dark" || saved === "light") {
      setTheme(saved);
    } else if (typeof window !== "undefined" && window.matchMedia("(prefers-color-scheme: light)").matches) {
      setTheme("light");
    }
  }, []);

  // ── API 키 상태 조회 (5분 간격) ──
  const fetchKeyStatus = useCallback(async () => {
    try {
      const BASE = process.env.NEXT_PUBLIC_API_URL || "https://aads.newtalk.kr/api/v1";
      const res = await fetch(`${BASE}/health/api-keys`);
      if (!res.ok) return;
      const data = await res.json();
      const lt = data?.anthropic?.litellm;
      const cli = data?.anthropic?.cli;
      if (lt || cli) {
        setApiKeyInfo({
          litellm: lt?.prefix,
          type: lt?.type || cli?.type,
          label: cli?.label || lt?.label,
          cliLabel: cli?.label,
          keyName: data?.anthropic?.db_keys?.find((key: AuthKeyStatus) => key?.is_current)?.key_name,
          slot: cli?.account,
          relayStatus: cli?.status,
          relayTokenAvailable: cli?.token_available,
          keys: data?.anthropic?.db_keys || [],
        });
      }
    } catch {}
  }, []);

  useEffect(() => {
    fetchKeyStatus();
    const iv = setInterval(fetchKeyStatus, 300_000);
    return () => clearInterval(iv);
  }, [fetchKeyStatus]);

  // ── 버전 체크: 스트리밍 상태 동기화 ──
  useEffect(() => {
    setVersionStreaming(streaming);
  }, [streaming, setVersionStreaming]);

  // ── Ctrl+V 클립보드 파일 붙여넣기 (이미지 포함 모든 파일) ──
  useEffect(() => {
    const handlePaste = (e: ClipboardEvent) => {
      const items = e.clipboardData?.items;
      if (!items) return;
      const pastedFiles: File[] = [];
      for (const item of items) {
        if (item.kind === "file") {
          const file = item.getAsFile();
          if (file) pastedFiles.push(file);
        }
      }
      if (pastedFiles.length > 0) {
        handleFiles(pastedFiles);
      }
    };
    window.addEventListener("paste", handlePaste);
    return () => window.removeEventListener("paste", handlePaste);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeWs]);

  // ── Responsive ──
  useEffect(() => {
    let debounceTimer: ReturnType<typeof setTimeout> | null = null;
    function check() {
      const w = window.innerWidth;
      const size: ScreenSize = w >= 1280 ? "desktop" : w >= 768 ? "tablet" : "mobile";
      setScreenSize(size);
      if (size === "mobile") { setLeftOpen(false); setArtifactMode("hidden"); }
      else if (size === "tablet") { setLeftOpen(false); }
      else { setLeftOpen(true); }
    }
    function debouncedCheck() {
      if (debounceTimer) clearTimeout(debounceTimer);
      debounceTimer = setTimeout(check, 300);
    }
    check();
    window.addEventListener("resize", debouncedCheck);
    return () => { window.removeEventListener("resize", debouncedCheck); if (debounceTimer) clearTimeout(debounceTimer); };
  }, []);

  // ── Load workspaces (restore last active from localStorage) ──
  useEffect(() => {
    chatApi<Workspace[]>("/chat/workspaces")
      .then(async (ws) => {
        setWorkspaces(ws);
        if (ws.length === 0) return;

        // 1. URL hash에서 세션 ID 추출
        const hashSid = typeof window !== "undefined" && window.location.hash
          ? window.location.hash.replace(/^#/, "")
          : null;

        // 2. hash 세션이 있으면 해당 세션의 워크스페이스를 먼저 확인
        if (hashSid) {
          try {
            const session = await chatApi<ChatSession>(`/chat/sessions/${hashSid}`);
            if (session && session.workspace_id) {
              const wsMatch = ws.find((w) => w.id === session.workspace_id);
              if (wsMatch) {
                setActiveWs(wsMatch.id);
                return;
              }
            }
          } catch {
            // 세션이 삭제된 경우 무시하고 fallback
          }
        }

        // 3. hash 세션 없으면 기존 localStorage 복원
        const savedWs = localStorage.getItem("aads-chat-activeWs");
        const match = savedWs && ws.find((w) => w.id === savedWs);
        setActiveWs(match ? match.id : ws[0].id);
      })
      .catch((err) => {
        console.error("워크스페이스 로드 실패:", err);
        const detail = err?.status ? `(${err.status})` : "(네트워크 오류)";
        setYellowWarning(`워크스페이스 목록 로드 실패 ${detail}`);
        if (yellowWarningTimerRef.current) clearTimeout(yellowWarningTimerRef.current);
        yellowWarningTimerRef.current = setTimeout(() => setYellowWarning(null), 5000);
      });
  }, []);

  // ── Load sessions on workspace change (restore last session from localStorage) ──
  useEffect(() => {
    if (!activeWs) return;
    localStorage.setItem("aads-chat-activeWs", activeWs);
    // BUG-2 FIX: 초기 로드 시에는 세션/메시지 초기화 생략 (새로고침 시 세션 유지)
    const isInitial = initialWsLoadRef.current;
    if (isInitial) {
      initialWsLoadRef.current = false;
    } else {
      // 실제 워크스페이스 전환 시에만 이전 세션 해제 — 프로젝트 컨텍스트 분리
      isInitialLoadRef.current = true;
      setActiveSession(null);
      setMessages([]);
    }
    chatApi<ChatSession[]>(`/chat/sessions?workspace_id=${activeWs}`)
      .then(async (loaded) => {
        setSessions(loaded);
        if (loaded.length === 0) {
          setActiveSession(null);
          setMessages([]);
          return;
        }
        // localStorage에 저장된 세션 복원 시도
        const hashSid = typeof window !== "undefined" && window.location.hash ? window.location.hash.replace(/^#/, "") : null;
        const lsSid = localStorage.getItem(`aads-chat-activeSession-${activeWs}`);
        const savedSid = hashSid || lsSid;
        let match = savedSid ? loaded.find((s) => s.id === savedSid) : null;
        // BUG-REFRESH FIX: 목록에 없으면 직접 API로 조회 시도
        if (savedSid && !match) {
          try {
            const directSession = await chatApi<ChatSession>(`/chat/sessions/${savedSid}`);
            if (directSession && directSession.workspace_id === activeWs) {
              loaded.unshift(directSession);
              setSessions([directSession, ...loaded.filter(s => s.id !== directSession.id)]);
              setActiveSession(directSession);
              return;
            }
          } catch {
            // 세션이 삭제된 경우 무시하고 fallback
          }
        }
        // BUG-2 FIX: updated_at 기준 정렬 후 최신 세션 선택
        const sorted = [...loaded].sort((a, b) =>
          new Date(b.updated_at || b.created_at).getTime() - new Date(a.updated_at || a.created_at).getTime()
        );
        const chosen = match || sorted[0];
        setActiveSession(chosen);
      })
      .catch((err) => {
        console.error("세션 목록 로드 실패:", err);
        const detail = err?.status ? `(${err.status})` : "(네트워크 오류)";
        setYellowWarning(`세션 목록 로드 실패 ${detail}`);
        if (yellowWarningTimerRef.current) clearTimeout(yellowWarningTimerRef.current);
        yellowWarningTimerRef.current = setTimeout(() => setYellowWarning(null), 5000);
      });
  }, [activeWs]);

  // ── Load messages & artifacts on session change ──
  useEffect(() => {
    // BUG-REFRESH FIX: 초기 마운트 시 activeSession이 null이면 hash 클리어 없이 리턴
    if (isFirstMountRef.current) {
      isFirstMountRef.current = false;
      if (!activeSession) return;
    }
    // 먼저 이전 세션ID 저장 (ref 업데이트 전에 읽어야 함)
    const prevSid = activeSessionRef.current;
    activeSessionRef.current = activeSession?.id || null;
    // 세션 ID를 localStorage에 저장 (페이지 새로고침 시 복원용)
    if (activeSession?.id && activeWs) {
      localStorage.setItem(`aads-chat-activeSession-${activeWs}`, activeSession.id);
      if (typeof window !== "undefined") {
        const currentHash = window.location.hash.replace(/^#/, "");
        if (currentHash !== activeSession.id) {
          window.history.replaceState(null, "", `#${activeSession.id}`);
        }
      }
    }
    // 세션 전환 시 진행 중인 스트리밍 중단 (이전 응답이 새 세션에 혼입 방지)
    if (streaming) {
      // 생성 중이던 세션 기록 — 돌아올 때 빠른 폴링으로 응답 감지
      if (prevSid) pendingResponseSessions.current.add(prevSid);
      sessionSwitchRef.current = true;
      streamingSessionRef.current = null;
      abortCtrl.current?.abort();
      setStreaming(false);
      setStreamBuf("");
      setToolStatus(null);
      setYellowWarning(null);
      setToolTurnInfo(null);
      msgQueueRef.current = [];
      setQueueCount(0);
    }
    // 세션 전환 시 edit state 초기화
    setEditingMsgId(null);
    setEditText("");
    // FIX: 세션 전환 시 즉시 초기화 (이전 세션 메시지/버블 flash 방지)
    setMessages([]);
    setNextCursor(null);
    setMessagesLoading(true);
    if (waitingBgTimeoutRef.current) { clearTimeout(waitingBgTimeoutRef.current); waitingBgTimeoutRef.current = null; }
    // A-3: 세션 전환 시 모든 타이머 정리
    if (completionToastTimerRef.current) { clearTimeout(completionToastTimerRef.current); completionToastTimerRef.current = null; }
    if (yellowWarningTimerRef.current) { clearTimeout(yellowWarningTimerRef.current); yellowWarningTimerRef.current = null; }
    setCompletionToast(null);
    setWaitingBgResponse(false); setBgPartialContent("");
    setStreamBuf("");
    setSelectedArtifactIdx(0);
    if (!activeSession) {
      setArtifacts([]); setSessionCost(null); setSessionTurns(null); setBriefing(null);
      if (typeof window !== "undefined" && window.location.hash) {
        window.history.replaceState(null, "", window.location.pathname);
      }
      return;
    }
    // 백그라운드 생성 중이던 세션이면 빠른 폴링 시작
    const isPending = pendingResponseSessions.current.has(activeSession.id);
    // FIX: streaming-status API 확인 전까지 false 유지 (엉뚱한 세션에 버블 표시 방지)
    // isPending이어도 API로 재확인 후 설정
    // 세션 진입 시: streaming-status를 먼저 확인 → 결과에 따라 messages fetch
    // (병렬 실행하면 race condition으로 빈 화면 발생하므로 순차 실행)
    const fetchSid = activeSession.id;
    // BUG-1 FIX: cancelled 클로저로 race condition 방지 (activeSessionRef 대신)
    let cancelled = false;
    const loadMessages = (filterPlaceholder: boolean) =>
      chatApi<{ messages: ChatMessage[]; next_cursor: string | null; has_more: boolean }>(
        `/chat/messages?session_id=${fetchSid}&limit=100`
      )
        .then((result) => {
          const msgs = result.messages;
          if (cancelled) return msgs;
          setHasMoreMessages(result.has_more);
          setNextCursor(result.next_cursor);
          const processed = filterPlaceholder
            ? msgs.map((m) => {
                if (m.intent !== "streaming_placeholder") return m;
                // FIX: placeholder 삭제 금지 — 내용 있으면 recovered로, 없으면 생성 중 표시
                if (m.content && m.content.trim().length > 10) return { ...m, intent: undefined, model_used: "recovered" };
                return { ...m, content: m.content || "⏳ AI가 응답을 생성 중입니다..." };
              })
            : msgs.map((m) =>
                m.intent === "streaming_placeholder"
                  ? { ...m, content: m.content || bgPartialContent || "⏳ AI가 응답을 생성 중입니다..." }
                  : m
              );
          if (processed.length > 0 || msgs.length === 0) {
            setMessages(processed);
            // 2번: rate_limited 메시지가 있으면 자동 폴링 활성화 (CEO 수동 재전송 불필요)
            if (processed.some(m => m.intent === "rate_limited") && !waitingBgRef.current) {
              rateLimitedPollRef.current = true;
              setWaitingBgResponse(true);
              if (waitingBgTimeoutRef.current) clearTimeout(waitingBgTimeoutRef.current);
              waitingBgTimeoutRef.current = setTimeout(() => { rateLimitedPollRef.current = false; setWaitingBgResponse(false); setBgPartialContent(""); }, 300000);
            }
          }
          setMessagesLoading(false);
          return processed;
        })
        .catch((err) => {
          console.error("메시지 로드 실패:", err);
          const detail = err?.status ? `(${err.status})` : "(네트워크 오류)";
          setMessagesLoading(false);
          setYellowWarning(`메시지 로드 실패 ${detail}`);
          if (yellowWarningTimerRef.current) clearTimeout(yellowWarningTimerRef.current);
          yellowWarningTimerRef.current = setTimeout(() => setYellowWarning(null), 5000);
          return [] as ChatMessage[];
        });

    chatApi<{ is_streaming: boolean; just_completed?: boolean; tool_count?: number; last_tool?: string }>(
      `/chat/sessions/${fetchSid}/streaming-status`
    ).then(async (status) => {
      if (cancelled) return;
      if (status.is_streaming) {
        setWaitingBgResponse(true);
        pendingResponseSessions.current.add(fetchSid);
        if (waitingBgTimeoutRef.current) clearTimeout(waitingBgTimeoutRef.current);
        waitingBgTimeoutRef.current = setTimeout(() => {
          setWaitingBgResponse(false); setBgPartialContent("");
          pendingResponseSessions.current.delete(fetchSid);
        }, 180000); // P1-FIX: 60s→180s (장시간 도구 실행 대응)
        // 스트리밍 중 → placeholder 포함하여 메시지 로드
        await loadMessages(false);
      } else if (status.just_completed) {
        // 방금 완료 → placeholder 제외하고 메시지 로드
        pendingResponseSessions.current.delete(fetchSid);
        const msgs = await loadMessages(true);
        // 완료 직후인데 최종 응답이 아직 DB에 없을 수 있음 → 빠른 폴링 + 1.5초 후 재시도
        if (msgs && msgs.length > 0 && msgs[msgs.length - 1].role === "user") {
          setWaitingBgResponse(true); // 빠른 폴링(1초) 활성화하여 최종 응답 캐치
          setTimeout(() => {
            if (cancelled) return;
            loadMessages(true).then((retryMsgs) => {
              if (retryMsgs && retryMsgs.length > 0 && retryMsgs[retryMsgs.length - 1].role === "assistant") {
                setWaitingBgResponse(false); setBgPartialContent("");
              }
              // 여전히 없으면 폴링이 계속 잡아줌 (60초 타임아웃)
            });
          }, 1500);
          if (waitingBgTimeoutRef.current) clearTimeout(waitingBgTimeoutRef.current);
          waitingBgTimeoutRef.current = setTimeout(() => { setWaitingBgResponse(false); setBgPartialContent(""); }, 60000);
        } else {
          setWaitingBgResponse(false); setBgPartialContent("");
        }
      } else {
        // 스트리밍 아님 → 일반 로드
        const msgs = await loadMessages(true);
        // pending 세션이었는데 assistant 응답이 없으면 → 재시도 (placeholder 삭제~응답 저장 gap)
        if (isPending && msgs && msgs.length > 0 && msgs[msgs.length - 1].role === "user") {
          setWaitingBgResponse(true);
          setTimeout(() => {
            if (cancelled) return;
            loadMessages(true).then((retryMsgs) => {
              if (retryMsgs && retryMsgs.length > 0 && retryMsgs[retryMsgs.length - 1].role === "assistant") {
                setWaitingBgResponse(false); setBgPartialContent("");
                pendingResponseSessions.current.delete(fetchSid);
              }
            });
          }, 2000);
        } else if (isPending) {
          pendingResponseSessions.current.delete(fetchSid);
        }
        // 서버 재시작으로 인한 미완료 대화 감지: 마지막 메시지가 user이고 10분 이내
        if (!isPending && msgs && msgs.length > 0 && msgs[msgs.length - 1].role === "user") {
          const lastMsg = msgs[msgs.length - 1];
          const msgAge = Date.now() - new Date(lastMsg.created_at || Date.now()).getTime();
          if (msgAge < 10 * 60 * 1000) {
            setToolStatus("🔄 서버 재시작 감지 — 자동으로 이어집니다...");
            setWaitingBgResponse(true);
            pendingResponseSessions.current.add(fetchSid);
            // 서버 백그라운드 auto_resume이 처리 중일 수 있으므로 폴링으로 응답 대기
            setTimeout(() => {
              if (cancelled) return;
              setToolStatus(null);
              loadMessages(true).then((retryMsgs) => {
                if (retryMsgs && retryMsgs.length > 0 && retryMsgs[retryMsgs.length - 1].role === "assistant") {
                  setWaitingBgResponse(false); setBgPartialContent("");
                  pendingResponseSessions.current.delete(fetchSid);
                }
                // 여전히 user가 마지막이면 폴링이 계속 잡아줌
              });
            }, 5000);
            if (waitingBgTimeoutRef.current) clearTimeout(waitingBgTimeoutRef.current);
            waitingBgTimeoutRef.current = setTimeout(() => {
              setWaitingBgResponse(false); setBgPartialContent("");
              pendingResponseSessions.current.delete(fetchSid);
            }, 120000);
          }
        }
      }
    }).catch(() => {
      // streaming-status API 실패 시 폴백: 일반 메시지 로드
      loadMessages(isPending ? false : true);
    });
    chatApi<Artifact[]>(`/chat/artifacts?workspace_id=${activeWs}`)
      .then(setArtifacts)
      .catch(() => setArtifacts([]));
    // Sync model from session (세션별 분리: current_model 있으면 사용, 없으면 DEFAULT_MODEL)
    {
      const sessionModel = normalizeModelIdForSelector(activeSession.current_model || DEFAULT_MODEL);
      setModel(sessionModel);
    }
    // AADS-190: 세션 전환 시 누적비용 즉시 표시
    const ct = activeSession.cost_total;
    if (ct && Number(ct) > 0) {
      setSessionCost(`$${Number(ct).toFixed(2)}`);
      setSessionTurns(activeSession.message_count || null);
    } else {
      setSessionCost(null);
      setSessionTurns(null);
    }
    // 프로액티브 브리핑: 세션 진입 시 1회만 표시
    const sid = activeSession.id;
    const shownKey = `briefing_${sid}`;
    if (!briefingShownRef.current.has(sid) && !sessionStorage.getItem(shownKey)) {
      chatApi<{ has_briefing: boolean; briefing_message: string }>(`/briefing?session_id=${sid}`)
        .then((res) => {
          if (res.has_briefing && res.briefing_message) {
            setBriefing({ message: res.briefing_message, collapsed: false });
            briefingShownRef.current.add(sid);
            sessionStorage.setItem(shownKey, "1");
          } else {
            setBriefing(null);
          }
        })
        .catch(() => setBriefing(null));
    } else {
      setBriefing(null);
    }
    // BUG-1 FIX: cleanup — 세션 전환 시 이전 fetch 응답 폐기
    return () => { cancelled = true; };
  }, [activeSession?.id]);

  // ── 안전장치: 메시지가 빈 배열로 렌더링될 때 500ms 후 자동 재시도 ──
  useEffect(() => {
    if (!activeSession?.id || messages.length > 0 || streaming) return;
    const sid = activeSession.id;
    const timer = setTimeout(() => {
      if (activeSessionRef.current !== sid) return;
      chatApi<{ messages: ChatMessage[]; has_more: boolean; next_cursor: string | null }>(`/chat/messages?session_id=${sid}&limit=100`)
        .then((result) => result.messages)
        .then((msgs) => {
          if (activeSessionRef.current !== sid) return;
          if (msgs.length > 0) {
            setMessages(msgs.map((m) => {
              if (m.intent !== "streaming_placeholder") return m;
              // FIX: placeholder 삭제 금지 — 내용 있으면 recovered로, 없으면 생성 중 표시
              if (m.content && m.content.trim().length > 10) return { ...m, intent: undefined, model_used: "recovered" };
              return { ...m, content: m.content || "⏳ AI가 응답을 생성 중입니다..." };
            }) as ChatMessage[]);
          }
        })
        .catch(() => {});
    }, 500);
    return () => clearTimeout(timer);
  }, [activeSession?.id, messages.length, streaming]);

  // 스크롤 이벤트로 near-bottom 감지 + 맨 위 도달 시 이전 메시지 자동 로드
  const loadingOlderRef = useRef(false);
  useEffect(() => {
    const container = messagesContainerRef.current;
    if (!container) return;
    const handleScroll = () => {
      isNearBottomRef.current = container.scrollTop + container.clientHeight >= container.scrollHeight - 150;
      // 맨 위 근접 시 이전 메시지 자동 로드
      if (container.scrollTop < 80 && hasMoreMessages && !loadingOlderRef.current && !isInitialLoadRef.current) {
        loadingOlderRef.current = true;
        loadOlderMessages().finally(() => { loadingOlderRef.current = false; });
      }
    };
    container.addEventListener("scroll", handleScroll, { passive: true });
    return () => container.removeEventListener("scroll", handleScroll);
  }, [hasMoreMessages, loadOlderMessages]);

  // ── Auto-scroll (초기 로드: instant, 이후: near-bottom일 때만) ──
  useLayoutEffect(() => {
    const container = messagesContainerRef.current;
    if (!container) return;
    if (isInitialLoadRef.current) {
      if (messages.length === 0) return; // FIX-2: 빈 DOM에서 stabilizer 낭비 방지
      container.scrollTop = container.scrollHeight;
      // PERF: ResizeObserver로 DOM 변화 감지 (setInterval 50ms → 이벤트 기반)
      const observer = new ResizeObserver(() => {
        container.scrollTop = container.scrollHeight;
      });
      observer.observe(container);
      // 3초 후 자동 해제 (초기 로드 완료)
      const timeout = setTimeout(() => {
        observer.disconnect();
        isInitialLoadRef.current = false;
        isNearBottomRef.current = true;
      }, 3000);
      return () => { observer.disconnect(); clearTimeout(timeout); };
    } else if (isNearBottomRef.current) {
      // near-bottom일 때만 instant 스크롤 (smooth는 이전 위치에서 애니메이션 → 튀어감 방지)
      const container2 = messagesContainerRef.current;
      if (container2) container2.scrollTop = container2.scrollHeight;
    }
  }, [messages]); // streamBuf 의존성 제거!

  // 스트리밍 중 스크롤 (200ms throttle, near-bottom일 때만)
  useEffect(() => {
    if (!streaming || !streamBuf || !isNearBottomRef.current) return;
    const container = messagesContainerRef.current;
    if (!container) return;
    const timer = setTimeout(() => {
      container.scrollTop = container.scrollHeight;
    }, 200);
    return () => clearTimeout(timer);
  }, [streaming, streamBuf]);

  // ★ streamBufRef 동기화 — SSE finally에서 streamBuf 값 참조용
  useEffect(() => { streamBufRef.current = streamBuf; }, [streamBuf]);


  // PERSIST-FIX: streaming 중 2초마다 streamBuf를 message.content에 동기화
  // SSE 끊김/streaming 해제 시에도 버블에 텍스트가 남아있게 보장
  useEffect(() => {
    if (!streaming) return;
    const syncTimer = setInterval(() => {
      const buf = streamBufRef.current;
      if (!buf) return;
      setMessages(prev => prev.map(m =>
        m.intent === "streaming_placeholder" ? { ...m, content: buf } : m
      ));
    }, 2000);
    return () => {
      clearInterval(syncTimer);
      // cleanup: streaming 종료 시 마지막 한번 동기화
      const buf = streamBufRef.current;
      if (buf) {
        setMessages(prev => prev.map(m =>
          m.intent === "streaming_placeholder" ? { ...m, content: buf } : m
        ));
      }
    };
  }, [streaming]);

  // FIX-4: 브리핑 렌더 후 재스크롤 (브리핑이 DOM에 추가되면 scrollHeight 변경됨)
  useEffect(() => {
    if (!briefing || isInitialLoadRef.current) return;
    const container = messagesContainerRef.current;
    if (container && isNearBottomRef.current) {
      requestAnimationFrame(() => {
        container.scrollTop = container.scrollHeight;
      });
    }
  }, [briefing]);

  // ── 백그라운드 메시지 폴링 (Pipeline Runner / Agent 완료 메시지 실시간 수신) ──
  // P1-FIX: waitingBgResponse=true→1초, 아니면 5초 폴링
  // + just_completed 감지 시 자동 reload + 토스트 표시
  // PERF: streaming/waitingBgResponse를 ref로 참조하여 의존성 폭탄 방지
  useEffect(() => {
    if (!activeSession?.id) return;
    const sid = activeSession.id;
    // BUG-SESSION-MIX FIX: cancelled 클로저로 세션 전환 시 in-flight 폴링 응답 폐기
    let cancelled = false;
    // PERF: 3초 interval, waitingBg=true 3초/아닐 때 15초 폴링 (성능 최적화)
    let tickCount = 0;
    let prevWaitingBg = false; // waitingBg 전환 감지용
    const iv = setInterval(async () => {
      if (cancelled) return;
      // FIX-3: 초기 스크롤 완료 전까지 폴링 skip (간섭 방지)
      if (isInitialLoadRef.current) return;
      const _streaming = streamingRef.current;
      const _waitingBg = waitingBgRef.current;
      // PERF-FIX: waitingBg true->false 전환 시 tickCount 리셋
      // 대기 중 카운터가 증가하여 false 전환 직후 즉시 실행되는 현상 방지
      if (prevWaitingBg && !_waitingBg) { tickCount = 0; }
      prevWaitingBg = _waitingBg;
      tickCount++;
      if (!_waitingBg && tickCount % 5 !== 0) return;
      // ── just_completed 감지: streaming-status 폴링 (스트리밍 중에도 항상 체크) ──
      let ss: { is_streaming: boolean; just_completed?: boolean; partial_content?: string; last_message_id?: string } | null = null;
      try {
        ss = await chatApi<{ is_streaming: boolean; just_completed?: boolean; partial_content?: string; last_message_id?: string }>(
          `/chat/sessions/${sid}/streaming-status`
        );
        if (cancelled) return;
        if (ss.partial_content) {
          setBgPartialContent(ss.partial_content);
          // Invisible Recovery: streaming=true + waitingBg=true → partial_content를 streamBuf에 주입 (타이핑 효과)
          if (_streaming && _waitingBg) {
            setStreamBuf(ss.partial_content);
          }
        }
        if (ss.just_completed) {
          pendingResponseSessions.current.delete(sid);
          setWaitingBgResponse(false); setBgPartialContent("");
          // ★ FIX: streaming 버블 유지 — 메시지 교체 후 부드럽게 전환 (새 버블 방지)
          streamingSessionRef.current = null;
          // 끊김 복구 후 대기 메시지 큐 클리어 (interrupt로 이미 전달됨 or 폐기)
          if (msgQueueRef.current.length > 0) { msgQueueRef.current = []; setQueueCount(0); }
          const freshMsgs = await chatApi<ChatMessage[]>(`/chat/messages?session_id=${sid}&limit=50&sort=desc`).then(msgs => msgs.reverse());
          if (cancelled) return;
          if (freshMsgs) {
            const filtered = freshMsgs;
            if (filtered.length > 0) {
              // ★ FIX: 최종 AI 메시지를 streamBuf에 먼저 표시 (같은 버블에서 전환)
              const _lastAiJc = filtered.filter((m: ChatMessage) => m.role === "assistant").pop();
              if (_lastAiJc?.content) setStreamBuf(_lastAiJc.content);
              setMessages(prev => {
                // ★ 완전 in-place: placeholder를 최종 AI 메시지로 교체 (같은 React key → 새 버블 방지)
                const placeholder = prev.find(m => m.intent === "streaming_placeholder");
                if (placeholder && _lastAiJc) {
                  // placeholder의 id 유지 → React DOM 재사용 (새 버블 생성 불가)
                  const inPlaceMsg = { ..._lastAiJc, id: placeholder.id };
                  return prev.map(m => m.intent === "streaming_placeholder" ? inPlaceMsg : m);
                }
                // fallback: placeholder 없을 때
                const freshIds = new Set(filtered.map(m => m.id));
                const oldestFreshTime = new Date(filtered[0]?.created_at || 0).getTime();
                const preserved = prev.filter(m => !freshIds.has(m.id) && !m.id.startsWith("tmp-") && !m.id.startsWith("ai-") && !m.id.startsWith("stopped-") && new Date(m.created_at || 0).getTime() < oldestFreshTime);
                return [...preserved, ...filtered];
              });
              // ★ FIX: 다음 프레임에서 스트리밍 버블 제거 (깜빡임 방지)
              requestAnimationFrame(() => { setStreaming(false); setStreamBuf(""); });
            } else {
              setStreaming(false); setStreamBuf("");
            }
          } else {
            setStreaming(false); setStreamBuf("");
          }
          // 자동 트리거(시스템 메시지) 응답이면 토스트 생략
          // freshMsgs는 ASC(시간순) → .slice().reverse()로 DESC(최신순) 후 최신 user/ai 기준 판단
          const _lastUser979 = freshMsgs?.slice().reverse().find((m: ChatMessage) => m.role === "user");
          const _lastAi979 = freshMsgs?.slice().reverse().find((m: ChatMessage) => m.role === "assistant" && m.intent !== "streaming_placeholder" && m.intent !== "rate_limited");
          if (!isAutoTriggerResponse(_lastUser979, _lastAi979)) {
            if (_lastAi979?.id) lastToastedAiIdRef.current = _lastAi979.id;
            showCompletionToast("응답이 완료되었습니다");
          } else if (_lastAi979?.id) {
            lastToastedAiIdRef.current = _lastAi979.id;  // 자동트리거도 ID 기록 — 이중 토스트 방지
          }
          return;
        }
        // 서버에서 스트리밍 아님 + 프론트 streaming=true → SSE 끊김 감지
        if (!ss.is_streaming && !ss.just_completed && _streaming) {
          // SSE reader가 아직 활성 상태면 streamBuf 유지 (폴링 레이스 방지)
          if (streamingSessionRef.current) {
            return;
          }
          // ★ FIX: streaming 버블 유지 — 메시지 교체 후 부드럽게 전환 (새 버블 방지)
          streamingSessionRef.current = null;
          setWaitingBgResponse(false); setBgPartialContent("");
          // 끊김 후 대화 못이어가는 문제 방지 — 대기 큐 클리어
          if (msgQueueRef.current.length > 0) { msgQueueRef.current = []; setQueueCount(0); }
          const freshMsgs = await chatApi<ChatMessage[]>(`/chat/messages?session_id=${sid}&limit=50&sort=desc`).then(msgs => msgs.reverse());
          if (cancelled) return;
          if (freshMsgs) {
            const filtered = freshMsgs;
            if (filtered.length > 0) {
              // ★ FIX: 최종 AI 메시지를 streamBuf에 표시 후 전환
              const _lastAiSse = filtered.filter((m: ChatMessage) => m.role === "assistant").pop();
              if (_lastAiSse?.content) setStreamBuf(_lastAiSse.content);
              setMessages(prev => {
                // ★ 완전 in-place: placeholder를 최종 AI 메시지로 교체 (새 버블 방지)
                const placeholder = prev.find(m => m.intent === "streaming_placeholder");
                if (placeholder && _lastAiSse) {
                  const inPlaceMsg = { ..._lastAiSse, id: placeholder.id };
                  return prev.map(m => m.intent === "streaming_placeholder" ? inPlaceMsg : m);
                }
                // fallback: placeholder 없을 때
                const freshIds = new Set(filtered.map(m => m.id));
                const oldestFreshTime = new Date(filtered[0]?.created_at || 0).getTime();
                const preserved = prev.filter(m => !freshIds.has(m.id) && !m.id.startsWith("tmp-") && !m.id.startsWith("ai-") && !m.id.startsWith("stopped-") && new Date(m.created_at || 0).getTime() < oldestFreshTime);
                return [...preserved, ...filtered];
              });
              requestAnimationFrame(() => { setStreaming(false); setStreamBuf(""); });
            } else {
              setStreaming(false); setStreamBuf("");
            }
          } else {
            setStreaming(false); setStreamBuf("");
          }
          return;
        }
        // 서버에서 스트리밍 아님 + waitingBg=true → 강제 해제 (placeholder 삭제 등으로 stuck 방지)
        // 단, rate_limited 자동 폴링 중에는 강제 해제 금지 (2번: 서버 재시도 대기 중)
        if (!ss.is_streaming && !ss.just_completed && _waitingBg && !_streaming && !rateLimitedPollRef.current) {
          setWaitingBgResponse(false); setBgPartialContent("");
          pendingResponseSessions.current.delete(sid);
          if (waitingBgTimeoutRef.current) { clearTimeout(waitingBgTimeoutRef.current); waitingBgTimeoutRef.current = null; }
        }
        // 스트리밍 중인데 waitingBgResponse가 꺼져 있으면 활성화 (세션 복귀 시)
        if (ss.is_streaming && !_waitingBg && !_streaming) {
          setWaitingBgResponse(true);
          pendingResponseSessions.current.add(sid);
          if (waitingBgTimeoutRef.current) clearTimeout(waitingBgTimeoutRef.current);
          waitingBgTimeoutRef.current = setTimeout(() => {
            setWaitingBgResponse(false); setBgPartialContent("");
            pendingResponseSessions.current.delete(sid);
          }, 180000);
        }
      } catch { /* streaming-status 실패 시 아래 메시지 폴링으로 폴백 */ }
      // PERF: streaming-status에서 last_message_id 캡처 — 변경 없으면 messages fetch skip
      const _ssLastMsgId = ss?.last_message_id || null;
      if (_ssLastMsgId && _ssLastMsgId === lastKnownMsgIdRef.current && !_waitingBg) return;
      if (_ssLastMsgId) lastKnownMsgIdRef.current = _ssLastMsgId;
      // 메시지 폴링은 스트리밍 중이면 생략 (SSE로 수신 중)
      if (_streaming && !_waitingBg) return;
      try {
        const rawLatest = await chatApi<ChatMessage[]>(`/chat/messages?session_id=${sid}&limit=5&sort=desc&fields=minimal`);
        if (cancelled) return;
        if (!rawLatest || rawLatest.length === 0) return;
        const latest = _waitingBg
          ? rawLatest.map((m) => m.intent === "streaming_placeholder" ? { ...m, content: m.content || bgPartialContent || "⏳ AI가 응답을 생성 중입니다..." } : m)
          // FIX: placeholder 삭제 금지 — streaming 아닐 때도 placeholder는 표시 유지
          : rawLatest.map((m) => m.intent === "streaming_placeholder" ? { ...m, content: m.content || "⏳ AI가 응답을 생성 중입니다..." } : m);
        if (latest.length === 0) return;
        if (_waitingBg) {
          const hasPlaceholder = rawLatest.some((m) => m.intent === "streaming_placeholder");
          const _latestFinalAi = rawLatest.find((m) => m.role === "assistant" && m.intent !== "streaming_placeholder" && m.intent !== "rate_limited");
          const hasNewFinalAi = _latestFinalAi && _latestFinalAi.id !== lastToastedAiIdRef.current;
          // PERF: AI 메시지 도착 즉시 waitingBgResponse 해제 (placeholder 잔존 여부 무관)
          if (hasNewFinalAi) {
            rateLimitedPollRef.current = false;  // 2번: rate_limited 해소 시 ref 초기화
            pendingResponseSessions.current.delete(sid);
            setWaitingBgResponse(false); setBgPartialContent("");
            try {
              const allMsgs = await chatApi<ChatMessage[]>(`/chat/messages?session_id=${sid}&limit=50&sort=desc`).then(msgs => msgs.reverse());
              if (cancelled) return;
              if (allMsgs) {
                const filtered = allMsgs;
                if (filtered.length > 0) {
                  setMessages(prev => {
                    const freshIds = new Set(filtered.map(m => m.id));
                    const oldestFreshTime = new Date(filtered[0]?.created_at || 0).getTime();
                    const preserved = prev.filter(m => !freshIds.has(m.id) && !m.id.startsWith("tmp-") && !m.id.startsWith("ai-") && !m.id.startsWith("stopped-") && new Date(m.created_at || 0).getTime() < oldestFreshTime);
                    return [...preserved, ...filtered];
                  });
                }
              }
            } catch { /* 재조회 실패 무시 */ }
            // 자동 트리거(시스템 메시지) 응답이면 토스트 생략
            // rawLatest는 이미 DESC(최신순) — .reverse() 제거하여 최신 user 메시지 기준 판단
            const _lastUser1029 = rawLatest?.find((m: ChatMessage) => m.role === "user");
            const _lastAi1029 = rawLatest?.find((m: ChatMessage) => m.role === "assistant" && m.intent !== "streaming_placeholder" && m.intent !== "rate_limited");
            if (!isAutoTriggerResponse(_lastUser1029, _lastAi1029)) {
              if (_lastAi1029?.id) lastToastedAiIdRef.current = _lastAi1029.id;
              showCompletionToast("응답이 완료되었습니다");
            } else if (_lastAi1029?.id) {
              lastToastedAiIdRef.current = _lastAi1029.id;
            }
            return;
          }
          if (hasPlaceholder) {
            const phMsg = rawLatest.find((m) => m.intent === "streaming_placeholder");
            if (phMsg) {
              setMessages(prev => {
                const idx = prev.findIndex((m) => m.intent === "streaming_placeholder");
                if (idx >= 0) {
                  const updated = [...prev];
                  updated[idx] = { ...phMsg, content: phMsg.content || bgPartialContent || "⏳ 생성 중..." };
                  return updated;
                }
                return [...prev, { ...phMsg, content: phMsg.content || bgPartialContent || "⏳ 생성 중..." }];
              });
              return;
            }
          }
        }
        setMessages((prev) => {
          const hasStoppedMsg = prev.some((m) => m.id.startsWith("stopped-"));
          if (hasStoppedMsg && !_waitingBg) return prev;
          const existingIds = new Set(prev.map((m) => m.id));
          const existingHashes = new Set(
            prev.map((m) => `${m.role}:${(m.content || "").slice(0, 200)}`)
          );
          const newMsgs = latest.filter(
            (m) => !existingIds.has(m.id) && !existingHashes.has(`${m.role}:${(m.content || "").slice(0, 200)}`)
          );
          if (newMsgs.length === 0) {
            let replaced = false;
            const updated = prev
              .map((m) => {
                if (m.id.startsWith("ai-") || m.id.startsWith("tmp-") || m.id.startsWith("stopped-")) {
                  const match = latest.find(
                    (l) => l.role === m.role && (l.content || "").slice(0, 200) === (m.content || "").slice(0, 200)
                  );
                  if (match) {
                    replaced = true;
                    // Bug 3: match ID가 이미 state에 있으면 temp 메시지 제거 (실제 DB 버전이 이미 존재)
                    if (existingIds.has(match.id)) return null;
                    // Bug 1: fields=minimal로 잘린 응답이 긴 기존 content를 덮어쓰지 않도록 보존
                    const content = (m.content || "").length > (match.content || "").length ? m.content : match.content;
                    return { ...match, content };
                  }
                }
                return m;
              })
              .filter((m) => m !== null) as typeof prev;
            return replaced ? updated : prev;
          }
          // FIX: DB 메시지 도착 시 클라이언트 임시 메시지(ai-*/stopped-*/tmp-*) 제거 → 버블 중복 방지
          const removedTemps = prev.filter((m) =>
            m.id.startsWith("ai-") || m.id.startsWith("stopped-") || m.id.startsWith("tmp-")
          );
          const cleanPrev = prev.filter((m) =>
            !(m.id.startsWith("ai-") || m.id.startsWith("stopped-") || m.id.startsWith("tmp-"))
          );
          // Bug 1: 제거된 temp 메시지보다 짧은 content를 가진 신규 DB 메시지에 긴 content 복원
          const preservedNewMsgs = newMsgs.map((m) => {
            const tempMatch = removedTemps.find((t) =>
              t.role === m.role &&
              (t.content || "").slice(0, (m.content || "").length) === (m.content || "") &&
              (t.content || "").length > (m.content || "").length
            );
            return tempMatch ? { ...m, content: tempMatch.content! } : m;
          });
          // Bug 3: cleanPrev에 이미 있는 ID는 preservedNewMsgs에서 제거 (중복 방지)
          const newMsgIds = new Set(preservedNewMsgs.map((m) => m.id));
          const dedupedCleanPrev = cleanPrev.filter((m) => !newMsgIds.has(m.id));
          return [...dedupedCleanPrev, ...preservedNewMsgs].sort(
            (a, b) => new Date(a.created_at || 0).getTime() - new Date(b.created_at || 0).getTime()
          );
        });
      } catch { /* 폴링 실패 무시 */ }
    }, 3000); // 3초 간격: waitingBg=true 3초, 아닐 때 15초 폴링 (성능 최적화)
    return () => { cancelled = true; clearInterval(iv); };
  }, [activeSession?.id]); // PERF: 의존성을 세션 ID만으로 축소

  // ── Toggle theme ──
  function toggleTheme() {
    const next: Theme = theme === "dark" ? "light" : "dark";
    setTheme(next);
    if (typeof window !== "undefined") localStorage.setItem("aads-chat-theme", next);
  }

  // ── Session management ──
  const createSession = useCallback(async function createSession(workspaceId?: string) {
    const wsId = workspaceId || activeWsRef.current;
    if (!wsId) return null;
    try {
      const s = await chatApi<ChatSession>("/chat/sessions", {
        method: "POST",
        body: JSON.stringify({ workspace_id: wsId, title: "새 대화", current_model: modelRef.current }),
      });
      setSessions((prev) => [s, ...prev]);
      isInitialLoadRef.current = true;
      setActiveSession(s);
      setMessages([]);
      if (screenSizeRef.current !== "desktop") setMobileOverlay(null);
      return s;
    } catch {
      return null;
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function addProject() {
    const code = newProjectCode.trim().toUpperCase();
    const name = newProjectName.trim();
    if (!code || !name) return;
    try {
      const ws = await chatApi<Workspace>("/chat/workspaces", {
        method: "POST",
        body: JSON.stringify({
          name: `[${code}] ${name}`,
          icon: newProjectIcon || "📁",
          color: "#6366F1",
        }),
      });
      setWorkspaces((prev) => [...prev, ws]);
      setActiveWs(ws.id);
      setShowAddProject(false);
      setNewProjectCode("");
      setNewProjectName("");
      setNewProjectIcon("📁");
    } catch { /* ignore */ }
  }

  async function deleteSession(id: string) {
    try {
      await chatApi(`/chat/sessions/${id}`, { method: "DELETE" });
      setSessions((prev) => prev.filter((s) => s.id !== id));
      if (activeSession?.id === id) { setActiveSession(null); setMessages([]); }
    } catch { /* ignore */ }
    setContextMenu(null);
  }

  async function togglePin(session: ChatSession) {
    try {
      const updated = await chatApi<ChatSession>(`/chat/sessions/${session.id}`, {
        method: "PUT",
        body: JSON.stringify({ pinned: !session.pinned }),
      });
      setSessions((prev) => prev.map((s) => (s.id === session.id ? { ...s, pinned: updated.pinned } : s)));
    } catch { /* ignore */ }
    setContextMenu(null);
  }

  async function updateSessionTags(sessionId: string, tags: string[]) {
    try {
      const updated = await chatApi<ChatSession>(`/chat/sessions/${sessionId}`, {
        method: "PUT",
        body: JSON.stringify({ tags }),
      });
      setSessions((prev) => prev.map((s) => (s.id === sessionId ? { ...s, tags: updated.tags || [] } : s)));
    } catch { /* ignore */ }
  }

  async function commitRename() {
    if (!renaming) return;
    try {
      const updated = await chatApi<ChatSession>(`/chat/sessions/${renaming.id}`, {
        method: "PUT",
        body: JSON.stringify({ title: renaming.value }),
      });
      setSessions((prev) => prev.map((s) => (s.id === renaming.id ? updated : s)));
      if (activeSession?.id === renaming.id) setActiveSession(updated);
    } catch { /* ignore */ }
    setRenaming(null);
    setContextMenu(null);
  }

  // ── Image generation ──
  const handleImageGen = async () => {
    if (!imageGenPrompt.trim() || !activeSession) return;
    setImageGenLoading(true);
    try {
      const res = await fetch(`${BASE_URL}/image/generate`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHdrs() },
        body: JSON.stringify({ prompt: imageGenPrompt }),
      });
      const data = await res.json();
      if (data.url) {
        setMessages((prev) => [
          ...prev,
          {
            id: `user-img-${Date.now()}`,
            session_id: activeSession.id,
            role: "user",
            content: `🎨 이미지 생성: ${imageGenPrompt}`,
            created_at: new Date().toISOString(),
          },
          {
            id: `ai-img-${Date.now()}`,
            session_id: activeSession.id,
            role: "assistant",
            content: `![generated](${data.url})\n\n> 🖼️ **${data.provider}** 생성 완료`,
            created_at: new Date().toISOString(),
          },
        ]);
        setShowImageGen(false);
        setImageGenPrompt("");
      } else {
        setYellowWarning(data.detail || "이미지 생성 실패");
      }
    } catch (e) {
      setYellowWarning("이미지 생성 중 오류가 발생했습니다");
    } finally {
      setImageGenLoading(false);
    }
  };

  // ── Send message (SSE streaming) ──
  const sendMessage = useCallback(async function sendMessage(queuedContent?: string, _unused?: undefined, retryCount?: number, _existingMsgId?: string) {
    const content = queuedContent || (chatInputRef.current?.getValue() || inputRef.current).trim();
    const hasFiles = pendingAttachments.current.length > 0;
    if (!content && !hasFiles) return;
    sessionSwitchRef.current = false;

    // 이미지 생성 명령 감지: "이미지: [설명]" 또는 "/img [설명]"
    const imgMatch = content.match(/^(?:이미지[:：]\s*|\/img\s+)(.+)/i);
    if (imgMatch && !queuedContent) {
      const imgPrompt = imgMatch[1].trim();
      setInput(""); chatInputRef.current?.clear();
      setImageGenLoading(true);
      // 유저 메시지로 표시
      const userImgMsg: ChatMessage = {
        id: `tmp-img-${Date.now()}`,
        session_id: activeSessionObjRef.current?.id || "",
        role: "user",
        content: content,
        created_at: new Date().toISOString(),
      };
      setMessages((prev) => [...prev, userImgMsg]);
      try {
        const imgData = await chatApi<{ url?: string; data?: string; error?: string }>("/image/generate", {
          method: "POST",
          body: JSON.stringify({ prompt: imgPrompt }),
        });
        const imgSrc = imgData.url || (imgData.data ? `data:image/png;base64,${imgData.data}` : null);
        const aiImgMsg: ChatMessage = {
          id: `img-${Date.now()}`,
          session_id: activeSessionObjRef.current?.id || "",
          role: "assistant",
          content: imgSrc
            ? `![생성된 이미지](${imgSrc})

> 프롬프트: ${imgPrompt}`
            : "이미지 생성에 실패했습니다.",
          created_at: new Date().toISOString(),
        };
        setMessages((prev) => [...prev, aiImgMsg]);
      } catch {
        // 이미지 생성 오류
      } finally {
        setImageGenLoading(false);
      }
      return;
    }

    // streaming 중이면 백엔드 인터럽트 큐에 push (CEO 인터럽트)
    if (streamingRef.current && !queuedContent) {
      const interruptContent = content || "(파일 첨부)";
      // 첨부파일 캡처 후 즉시 클리어
      const interruptAttachments = pendingAttachments.current.length > 0
        ? [...pendingAttachments.current] : [];
      pendingAttachments.current = [];
      setPendingPreviewFiles([]);
      msgQueueRef.current.push(interruptContent);
      setQueueCount(msgQueueRef.current.length);
      setInput(""); chatInputRef.current?.clear();
      if (textareaRef.current) textareaRef.current.style.height = "auto";
      // 대화창에 추가 지시를 user 메시지로 즉시 표시 (첨부파일 포함)
      const attachLabel = interruptAttachments.length > 0
        ? ` 📎 ${interruptAttachments.length}개 파일` : "";
      setMessages(prev => [...prev, {
        id: `interrupt-${Date.now()}`,
        session_id: activeSessionObjRef.current?.id || "",
        role: "user" as const,
        content: `💬 **[추가 지시]** ${interruptContent}${attachLabel}`,
        created_at: new Date().toISOString(),
      }]);
      // 백엔드 인터럽트 큐에 push (첨부파일 포함)
      if (activeSessionObjRef.current?.id) {
        chatApi(`/chat/sessions/${activeSessionObjRef.current.id}/interrupt`, {
          method: "POST",
          body: JSON.stringify({ content: interruptContent, attachments: interruptAttachments }),
        }).then(() => {
          // interrupt API 성공 → 큐에서 제거 (done 후 재전송 방지)
          const idx = msgQueueRef.current.indexOf(interruptContent);
          if (idx !== -1) msgQueueRef.current.splice(idx, 1);
          setQueueCount(msgQueueRef.current.length);
          // 대기 완료 시 경고도 즉시 해제
          if (msgQueueRef.current.length === 0) setYellowWarning(null);
        }).catch((e: unknown) => {
          console.warn("interrupt push failed, keeping in queue for retry:", e);
        });
      }
      // 추가 지시 접수 안내
      setYellowWarning(`추가 지시 접수됨 (대기 ${msgQueueRef.current.length}건)${attachLabel}`);
      if (yellowWarningTimerRef.current) clearTimeout(yellowWarningTimerRef.current);
      yellowWarningTimerRef.current = setTimeout(() => setYellowWarning(null), 5000);
      return;
    }

    // Auto-create session if none active
    let sessionId = activeSessionObjRef.current?.id;
    if (!sessionId) {
      if (!activeWsRef.current) return;
      const s = await createSession();
      if (!s) return;
      sessionId = s.id;
    }

    if (!_existingMsgId) { if (!_existingMsgId) { setInput(""); chatInputRef.current?.clear(); } }
    setEditMode(null);
    setReplyToMessage(null);
    // P2-2: 분기 모드 캡처 후 초기화
    const _capturedBranch = branchPointRef.current;
    setBranchPoint(null);
    setStreaming(true);
    setStreamBuf("");
    setToolLogs([]);
    streamingSessionRef.current = sessionId;
    // 스트리밍 placeholder ID 생성 (messages 추가는 userMsg 생성 후 단일 호출로 순서 보장)
    const streamingPlaceholderId = `ai-streaming-${Date.now()}`;
    if (textareaRef.current) { textareaRef.current.style.height = "auto"; }

    // C-3: stale closure 방지 — state 초기화 전에 로컬 변수로 캡처
    const filesToSend = [...pendingPreviewFilesRef.current];
    setPendingPreviewFiles([]);

    // 첨부 이미지 미리보기 URL 캡처 (메시지 버블 표시용)
    const _previewUrls = filesToSend
      .filter((f) => f.type.startsWith("image/"))
      .map((f) => URL.createObjectURL(f));

    // 이 요청의 세션 ID 캡처 — 세션 전환 감지용
    const requestSessionId = sessionId;
    const isStale = () => activeSessionRef.current !== requestSessionId;

    const _capturedReplyTo = replyToMessageRef.current;
    const userMsg: ChatMessage = {
      id: _existingMsgId || `tmp-${Date.now()}`,
      session_id: sessionId,
      role: "user",
      content,
      created_at: new Date().toISOString(),
      attachmentPreviews: _previewUrls.length > 0 ? _previewUrls : undefined,
      ...(_capturedReplyTo ? { reply_to_id: _capturedReplyTo.id } : {}),
      ...(_capturedBranch ? { branch_point_id: _capturedBranch.id, branch_id: `tmp-branch-${Date.now()}` } : {}),
    };
    // ★ FIX: user 메시지 → AI placeholder 순서로 단일 setMessages (새 버블 방지 + 순서 보장)
    if (!_existingMsgId) {
      setMessages(prev => [
        ...prev.filter(m => m.intent !== "streaming_placeholder"),
        userMsg,
        { id: streamingPlaceholderId, session_id: sessionId!, role: "assistant" as const, content: "", intent: "streaming_placeholder", created_at: new Date(Date.now() + 1).toISOString() }
      ]);
    } else {
      setMessages(prev => [
        ...prev.filter(m => m.intent !== "streaming_placeholder"),
        { id: streamingPlaceholderId, session_id: sessionId!, role: "assistant" as const, content: "", intent: "streaming_placeholder", created_at: new Date(Date.now() + 1).toISOString() }
      ]);
    }

    abortCtrl.current = new AbortController();
    // 90초 비활성 타임아웃 → heartbeat(5초) + 실제 데이터 모두 리셋
    // 절대 타임아웃(300초)이 무한 연장 방지 안전망
    let sseTimeout = setTimeout(() => {
      abortCtrl.current?.abort();
    }, 150000);
    const resetSseTimeout = () => {
      clearTimeout(sseTimeout);
      sseTimeout = setTimeout(() => {
        abortCtrl.current?.abort();
      }, 150000);
    };

    // 절대 타임아웃 1시간 — heartbeat와 무관하게 streaming 강제 종료
    const maxStreamTimeout = setTimeout(() => {
      abortCtrl.current?.abort();
    }, 3600000);

    let full = "";
    let _invisibleRecoveryActivated = false;
    try {
      // 히든 스크린 컨텍스트: 화면 관련 키워드 있을 때 첨부
      const SCREEN_KEYWORDS = ["화면", "보이지", "버튼", "UI", "여기", "이거", "이것", "클릭", "탭", "창", "팝업", "오른쪽", "왼쪽"];
      const hasScreenKeyword = SCREEN_KEYWORDS.some((kw) => content.includes(kw));
      if (screenContextRef.current && hasScreenKeyword) {
        const screenFile = screenContextRef.current;
        screenContextRef.current = null;
        try {
          const arrBuf = await screenFile.arrayBuffer();
          const b64 = btoa(String.fromCharCode(...new Uint8Array(arrBuf)));
          pendingAttachments.current.push({ type: "image", name: screenFile.name, media_type: "image/png", base64: b64, _hidden: true });
        } catch {}
      }

      const rawFiles = filesToSend;
      const attachments = pendingAttachments.current.length > 0
        ? [...pendingAttachments.current] : [];
      pendingAttachments.current = [];

      let fetchBody: BodyInit;
      let fetchHeaders: Record<string, string> = { ...authHdrs() };
      let fetchUrl = `${BASE_URL}/chat/messages/send`;
      // Stage 3: idempotency key — 502 재시도 시 동일 메시지 중복 저장 방지
      const _idempotencyKey = crypto.randomUUID();

      // P2-2: 분기 모드일 때 branch endpoint 사용
      if (_capturedBranch) {
        fetchHeaders["Content-Type"] = "application/json";
        fetchBody = JSON.stringify({ content, model_override: modelRef.current, attachments, idempotency_key: _idempotencyKey });
        fetchUrl = `${BASE_URL}/chat/messages/${_capturedBranch.id}/branch`;
      } else if (rawFiles.length > 0) {
        // FormData: raw File 객체로 전송 (서버에서 base64 변환)
        const formData = new FormData();
        formData.append("session_id", sessionId!);
        formData.append("content", content);
        if (modelRef.current) formData.append("model_override", modelRef.current);
        rawFiles.forEach((f) => formData.append("files", f));
        if (replyToMessageRef.current) formData.append("reply_to_id", replyToMessageRef.current.id);
        formData.append("idempotency_key", _idempotencyKey);
        fetchBody = formData;
        // Content-Type 헤더는 브라우저가 multipart/form-data + boundary 자동 설정
      } else {
        fetchHeaders["Content-Type"] = "application/json";
        fetchBody = JSON.stringify({ session_id: sessionId, content, model_override: modelRef.current, attachments, idempotency_key: _idempotencyKey, ...(replyToMessageRef.current ? { reply_to_id: replyToMessageRef.current.id } : {}) });
      }

      const res = await fetch(fetchUrl, {
        method: "POST",
        headers: fetchHeaders,
        body: fetchBody,
        signal: abortCtrl.current.signal,
      });

      if (!res.ok) {
        const statusCode = res.status;
        // 502/503/504: 서버 재시작 — 자동 재시도 (최대 3회, 지수 백오프)
        if ((statusCode === 502 || statusCode === 503 || statusCode === 504) && (retryCount || 0) < 3) {
          // 사용자 메시지 버블 유지 — 깜빡임 방지 (재시도 중에도 화면에 남음)



          setStreaming(false);
          setStreamBuf("");
          const attempt = (retryCount || 0) + 1;
          const delay = Math.min(5000 * Math.pow(1.5, attempt - 1), 15000);
          setToolStatus(`🔄 서버 재시작 감지 — ${Math.round(delay/1000)}초 후 자동 재전송 (${attempt}/3)...`);
          await new Promise((r) => setTimeout(r, delay));
          setToolStatus(null);
          return sendMessage(content, undefined, attempt, userMsg.id);
        }
        const _errMap: Record<number, string> = {
          502: "서버가 재시작 중입니다.",
          503: "서버가 일시적으로 과부하 상태입니다.",
          504: "응답 시간이 초과되었습니다.",
          429: "요청이 너무 많습니다.",
        };
        // 실패 시 사용자 메시지를 입력창에 복원
        setInput(content); chatInputRef.current?.setValue(content);
        // 프론트엔드에 추가한 사용자 메시지 제거 (DB 미저장이므로)
        setMessages((prev) => prev.filter((m) => m.id !== userMsg.id));
        throw new Error((_errMap[statusCode] || `서버 오류 (${statusCode})`) + " 메시지가 입력창에 복원되었습니다.");
      }

      const reader = res.body?.getReader();
      if (!reader) throw new Error("No response body");

      const decoder = new TextDecoder();
      let buf = "";
      let gotFinal = false;

      // Phase4: 토큰 버퍼링 — SSE 끊김 시에도 표시 지속 (2초 분량 선행 버퍼)
      const _tokenQueue: string[] = [];
      let _displayedText = "";
      let _drainTimer: ReturnType<typeof setInterval> | null = null;
      const _startDrain = () => {
        if (_drainTimer) return;
        _drainTimer = setInterval(() => {
          if (_tokenQueue.length > 0) {
            _displayedText += _tokenQueue.shift()!;
            if (!isStale()) setStreamBuf(_displayedText);
          }
        }, 30);
      };
      const _stopDrain = () => {
        if (_drainTimer) { clearInterval(_drainTimer); _drainTimer = null; }
        while (_tokenQueue.length > 0) _displayedText += _tokenQueue.shift()!;
      };

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        // 세션이 전환되었으면 남은 스트림 무시
        if (isStale()) { reader.cancel(); break; }
        buf += decoder.decode(value, { stream: true });
        const lines = buf.split("\n");
        buf = lines.pop() ?? "";

        for (const line of lines) {
          if (isStale()) break;
          // Phase4: Redis Stream entry ID 캡처 (Last-Event-ID 재연결용)
          if (line.startsWith("id:")) { lastEventIdRef.current = line.slice(3).trim(); continue; }
          if (!line.startsWith("data: ")) continue;
          const raw = line.slice(6).trim();
          if (raw === "[DONE]") continue;
          let sseError: Error | null = null;
          try {
            const ev = JSON.parse(raw);
            // P0-FIX: heartbeat도 timeout 리셋 — 도구 30s+ 실행 시 연결 유지 필수
            // 절대 타임아웃(300s)이 무한 연장 방지 안전망 역할
            if (ev.type === "stream_start") {
              // SSE 재연결 프로토콜: stream_id 저장 (복구 시 사용)
              resetSseTimeout();
              // 즉시 typing placeholder — 첫 delta 수신 전 빈 버블 방지
              if (!isStale()) setStreamBuf("분석 중...");
              continue;
            }
            if (ev.type === "heartbeat") {
              resetSseTimeout();
              // 도구 실행 중 진행상황 표시 (서버가 tool_count/last_tool 포함 시)
              if (ev.tool_count && ev.last_tool) {
                setToolStatus(`🔧 ${ev.last_tool} 실행 중... (도구 ${ev.tool_count}회)`);
              }
              continue;
            }
            // real data events도 timeout 리셋
            resetSseTimeout();
            if (ev.type === "stream_reset") {
              // F8: 출력 검증 실패 → 재시도 시 이전 텍스트 초기화
              full = "";
              setStreamBuf("");
              setToolStatus("🔄 응답 재검증 중...");
              continue;
            } else if (ev.type === "delta" && typeof ev.content === "string") {
              let deltaContent = ev.content;
              if (deltaContent.includes("[SCREEN_CAPTURE_REQUEST]")) {
                aiCaptureRequestedRef.current = true;
                deltaContent = deltaContent.replace(/\[SCREEN_CAPTURE_REQUEST\]/g, "");
              }
              full += deltaContent;
              // Phase4: 버퍼에 토큰 추가 → 드레인 타이머가 30ms 간격으로 표시
              _tokenQueue.push(deltaContent);
              _startDrain();
              if (toolStatusRef.current && !isStale()) setToolStatus(null);
            } else if (ev.type === "token" && typeof ev.text === "string") {
              // legacy fallback
              full += ev.text;
              if (!isStale()) setStreamBuf(full);
            } else if (ev.type === "done") {
              gotFinal = true;
              _stopDrain();  // Phase4: 버퍼 즉시 플러시
              setStreamBuf("");
              setStreaming(false);
              setToolStatus(null);
              setToolLogs([]);
              setYellowWarning(null);
              setToolTurnInfo(null);
              // AADS-190: 세션 비용/턴 업데이트
              if (ev.session_cost) setSessionCost(ev.session_cost);
              if (ev.session_turns) setSessionTurns(ev.session_turns);
              // UX: 스트리밍 완료 시 아티팩트 자동 갱신 + 신규 저장 토스트
              // 500ms 딜레이: 서버 DB 저장 완료 대기 / 중복 호출 방지
              if (activeWsRef.current && !artifactFetchingRef.current) {
                artifactFetchingRef.current = true;
                if (artifactFetchTimerRef.current) clearTimeout(artifactFetchTimerRef.current);
                const wsIdAtDone = activeWsRef.current;
                artifactFetchTimerRef.current = setTimeout(() => {
                  chatApi<Artifact[]>(`/chat/artifacts?workspace_id=${wsIdAtDone}`)
                    .then((newArtifacts) => {
                      setArtifacts((prev) => {
                        const prevIds = new Set(prev.map((a) => a.id));
                        const added = newArtifacts.filter((a) => !prevIds.has(a.id));
                        if (added.length > 0) {
                          const typeLabels: Record<string, string> = {
                            report: "📄 보고서가 저장되었습니다",
                            text: "📄 보고서가 저장되었습니다",
                            code: "💻 코드가 저장되었습니다",
                            chart: "📊 차트가 저장되었습니다",
                            image: "🖼️ 이미지가 저장되었습니다",
                            file: "📎 파일이 저장되었습니다",
                            table: "📋 테이블이 저장되었습니다",
                            html_preview: "🖼️ HTML 미리보기가 저장되었습니다",
                          };
                          const firstType = added[0].artifact_type;
                          const msg = typeLabels[firstType] ?? "📁 아티팩트가 저장되었습니다";
                          if (firstType === "html_preview") {
                            setArtifactTab("html_preview");
                          }
                          setArtifactToast(msg);
                          if (artifactToastTimerRef.current) clearTimeout(artifactToastTimerRef.current);
                          artifactToastTimerRef.current = setTimeout(() => setArtifactToast(null), 3000);
                        }
                        return newArtifacts;
                      });
                    })
                    .catch(() => {})
                    .finally(() => { artifactFetchingRef.current = false; });
                }, 500);
              }
              // full이 비어있으면 빈 버블 방지 — 도구만 실행된 경우
              if (full.trim()) {
                // ★ in-place 업데이트: placeholder를 최종 응답으로 교체 (새 버블 방지)
                setMessages((prev) => {
                  const hasPlaceholder = prev.some(m => m.intent === "streaming_placeholder");
                  const finalMsg = {
                    id: hasPlaceholder
                      ? (prev.find(m => m.intent === "streaming_placeholder")?.id || `ai-${Date.now()}`)
                      : `ai-${Date.now()}`,
                    session_id: requestSessionId!,
                    role: "assistant" as const,
                    content: full,
                    model_used: ev.model || undefined,
                    intent: ev.intent || undefined,
                    input_tokens: ev.input_tokens || undefined,
                    output_tokens: ev.output_tokens || undefined,
                    cost_usd: ev.cost ? parseFloat(ev.cost) : undefined,
                    confidence_label: ev.confidence_label || undefined,
                    created_at: new Date().toISOString(),
                  };
                  if (hasPlaceholder) {
                    return prev.map(m => m.intent === "streaming_placeholder" ? finalMsg : m);
                  }
                  return [...prev.filter(m => m.intent !== "streaming_placeholder"), finalMsg];
                });
              }
              break; // done 이벤트 수신 → for 루프 탈출
            } else if (ev.type === "tool_use" && ev.tool_name) {
              const toolIcons: Record<string, string> = {
                read_remote_file: "📄", read_github_file: "📄", list_remote_dir: "📁",
                write_remote_file: "✏️", patch_remote_file: "✏️",
                run_remote_command: "⚡", query_database: "🗄️", query_project_database: "🗄️",
                web_search: "🔍", web_search_brave: "🔍", jina_read: "🌐",
                crawl4ai_fetch: "🌐", deep_crawl: "🌐", deep_research: "🔬",
                health_check: "💊", get_all_service_status: "📊",
                pipeline_runner_submit: "🚀", delegate_to_agent: "🤖",
                save_note: "📝", recall_notes: "🧠",
              };
              const icon = toolIcons[ev.tool_name] || "🔧";
              const inp = ev.tool_input || {};
              const paramText = inp.path || inp.query || inp.url || inp.command
                || inp.file_path || inp.task || inp.project
                || (Object.values(inp).filter((v: unknown) => typeof v === "string")[0] as string)
                || "";
              const sub = paramText ? String(paramText).slice(0, 80) : undefined;
              if (!isStale()) {
                setToolLogs(prev => [...prev, { icon, text: `${ev.tool_name} 실행 중`, sub }]);
                setToolStatus(`${icon} ${ev.tool_name} 실행 중...`);
              }
            } else if (ev.type === "tool_result" && ev.tool_name) {
              const resultPreview = ev.content ? String(ev.content).slice(0, 60).replace(/\n/g, " ") : "";
              if (!isStale()) {
                setToolLogs(prev => {
                  const updated = [...prev];
                  const lastIdx = [...updated].reverse().findIndex(l => l.text.includes(ev.tool_name));
                  if (lastIdx >= 0) {
                    const realIdx = updated.length - 1 - lastIdx;
                    updated[realIdx] = { ...updated[realIdx], icon: "✅", text: `${ev.tool_name} 완료`, sub: resultPreview || undefined };
                  }
                  return updated;
                });
                setToolStatus(`✅ ${ev.tool_name} 완료 — 응답 생성 중...`);
              }
            } else if (ev.type === "thinking" && ev.content) {
              setToolStatus("💭 사고 중...");
              // thinking 텍스트가 있으면 streamBuf에 즉시 표시 — delta가 오면 자동 교체됨
              if (!isStale() && !full) setStreamBuf(ev.content || "분석 중...");
            } else if (ev.type === "sdk_session") {
              setToolStatus("🤖 Agent SDK 연결됨");
            } else if (ev.type === "sdk_complete") {
              setToolStatus(null);
            } else if (ev.type === "diff_preview") {
              diffApproval.onDiffPreview({
                type: "diff_preview",
                file_path: ev.file_path || "",
                tool_use_id: ev.tool_use_id || "",
                original_content: ev.original_content,
                modified_content: ev.modified_content,
              });
            } else if (ev.type === "message_done" && ev.message) {
              // legacy fallback
              gotFinal = true;
              setStreamBuf("");
              setStreaming(false);
              setToolStatus(null);
              // ★ in-place 업데이트
              setMessages((prev) => {
                const hasPlaceholder = prev.some(m => m.intent === "streaming_placeholder");
                if (hasPlaceholder) {
                  return prev.map(m => m.intent === "streaming_placeholder" ? (ev.message as ChatMessage) : m);
                }
                return [...prev.filter(m => m.intent !== "streaming_placeholder"), ev.message as ChatMessage];
              });
              break; // done → for 루프 탈출
            } else if (ev.type === "yellow_limit") {
              // Yellow 도구 연속 실행 경고
              setYellowWarning(ev.content || `쓰기 도구 연속 ${ev.consecutive_count || 5}회 호출`);
            } else if (ev.type === "tool_turn_limit") {
              // 도구 턴 한도 자동 연장 알림
              setToolTurnInfo(ev.content || `도구 턴 ${ev.current_turn}회 → ${ev.extended_to}회 연장`);
            } else if (ev.type === "interrupt_applied") {
              // CEO 인터럽트가 LLM에 반영됨 → 큐에서 해당 지시 제거 (완료 후 중복 전송 방지)
              if (msgQueueRef.current.length > 0) {
                msgQueueRef.current.shift();
              }
              // 무조건 큐 카운트 동기화 (배지 확실 해제)
              setQueueCount(msgQueueRef.current.length);
              // 토스트로만 알림 (assistant 메시지 추가 안 함 → 중복 방지)
              setYellowWarning(`✅ 추가 지시 반영됨 (대기 ${msgQueueRef.current.length}건)`);
              if (yellowWarningTimerRef.current) clearTimeout(yellowWarningTimerRef.current);
              yellowWarningTimerRef.current = setTimeout(() => setYellowWarning(null), 3000);
            } else if (ev.type === "error") {
              // 서버 재시작/LLM 장애 → Invisible Recovery로 처리 (버블 생성 없이 자동 복구)
              if (!isStale()) setToolStatus("🔄 서버 재시작 감지 — 자동으로 이어집니다...");
              sseError = new Error("SSE_SERVER_RESTART");
            }
          } catch {
            // ignore malformed SSE lines (JSON parse 실패 등)
          }
          // SSE error 이벤트는 outer catch로 전파
          if (sseError) throw sseError;
        }
        if (gotFinal) break; // done 이벤트 수신 → while 루프 탈출
      }

      // AI 트리거 캡처: AI가 [SCREEN_CAPTURE_REQUEST]를 응답에 포함한 경우 자동 캡처 + 재전송
      if (gotFinal && aiCaptureRequestedRef.current && !isStale()) {
        aiCaptureRequestedRef.current = false;
        const captureFile = screenContextRef.current;
        if (captureFile) {
          // 연속 캡처에서 이미 저장된 파일 사용
          screenContextRef.current = null;
          setTimeout(async () => {
            try {
              const arrBuf = await captureFile.arrayBuffer();
              const b64 = btoa(String.fromCharCode(...new Uint8Array(arrBuf)));
              pendingAttachments.current.push({ type: "image", name: captureFile.name, media_type: "image/png", base64: b64 });
              sendMessage("[AI 요청 화면 캡처]");
            } catch { /* ignore */ }
          }, 300);
        } else {
          // 연속 캡처 미사용 시 즉시 캡처 → screenContextRef 업데이트 대기 → 자동 전송
          chatInputRef.current?.captureNow();
          setTimeout(async () => {
            const f = screenContextRef.current;
            if (f) {
              screenContextRef.current = null;
              try {
                const arrBuf = await f.arrayBuffer();
                const b64 = btoa(String.fromCharCode(...new Uint8Array(arrBuf)));
                pendingAttachments.current.push({ type: "image", name: f.name, media_type: "image/png", base64: b64 });
                sendMessage("[AI 요청 화면 캡처]");
              } catch { /* ignore */ }
            }
          }, 1000);
        }
      }

      if (isStale()) { /* 세션 전환됨 — UI 업데이트 안 함 */ }
      else if (!gotFinal && full) {
        setStreamBuf("");
        // ★ in-place 업데이트
        setMessages((prev) => {
          const hasPlaceholder = prev.some(m => m.intent === "streaming_placeholder");
          const finalMsg = {
            id: hasPlaceholder
              ? (prev.find(m => m.intent === "streaming_placeholder")?.id || `ai-${Date.now()}`)
              : `ai-${Date.now()}`,
            session_id: requestSessionId!,
            role: "assistant" as const,
            content: full,
          };
          if (hasPlaceholder) {
            return prev.map(m => m.intent === "streaming_placeholder" ? finalMsg : m);
          }
          return [...prev.filter(m => m.intent !== "streaming_placeholder"), finalMsg];
        });
        // SSE 무음 종료 — 서버가 응답을 이어서 생성 중일 수 있으므로 폴링 활성화
        if (sessionId) {
          setWaitingBgResponse(true);
          pendingResponseSessions.current.add(sessionId);
          setToolStatus("🔄 응답 확인 중...");
          if (waitingBgTimeoutRef.current) clearTimeout(waitingBgTimeoutRef.current);
          waitingBgTimeoutRef.current = setTimeout(() => {
            setWaitingBgResponse(false); setBgPartialContent("");
            pendingResponseSessions.current.delete(sessionId!);
            setToolStatus(null);
          }, 60000);  // 1분 후 자동 해제
        }
      } else if (!gotFinal && !full) {
        // 도구만 실행되고 텍스트 없이 스트림 종료 — DB에서 응답 복구 시도
        setStreamBuf("");
        setToolStatus("⏳ 응답 확인 중...");
        for (let retry = 0; retry < 3; retry++) {
          await new Promise((r) => setTimeout(r, 3000 * (retry + 1)));
          try {
            const msgs = await chatApi<ChatMessage[]>(
              `/chat/messages?session_id=${requestSessionId}&limit=5`
            );
            const aiMsg = [...msgs].reverse().find((m) => m.role === "assistant" && m.intent !== "streaming_placeholder" && m.intent !== "rate_limited");
            if (aiMsg) {
              // ★ in-place 업데이트
              setMessages((prev) => {
                const hasPlaceholder = prev.some(m => m.intent === "streaming_placeholder");
                if (hasPlaceholder) {
                  return prev.map(m => m.intent === "streaming_placeholder" ? aiMsg : m);
                }
                return [...prev.filter(m => m.intent !== "streaming_placeholder"), aiMsg];
              });
              break;
            }
          } catch { /* retry */ }
        }
        setToolStatus(null);
      }
    } catch (e: unknown) {
      let gotFinal = false;
      const err = e as Error;
      const isAbort = err.name === "AbortError";
      const isNetwork = err.message?.includes("fetch") || err.message?.includes("network") || err.message?.includes("Failed") || err.message === "SSE_SERVER_RESTART";
      // 세션 전환으로 인한 abort → 이전 응답을 새 세션에 추가하지 않음
      if (sessionSwitchRef.current) {
        sessionSwitchRef.current = false;
        return;
      }
      if (isAbort || isNetwork) {
        // ── Invisible Recovery: 같은 버블 유지 + 무음 재연결 ──
        // SSE가 끊겨도 streaming=true 유지, streamBuf에 기존 텍스트 보존 (버블 사라짐 방지)
        // toolStatus는 설정하지 않음 (무음 재연결)
        const frozenContent = full;  // 끊기 직전까지의 텍스트 캡처
        // PERSIST-FIX: SSE 끊김 즉시 message content에 캡처 (버블 사라짐 방지)
        if (frozenContent) {
          setMessages(prev => prev.map(m =>
            m.intent === "streaming_placeholder" ? { ...m, content: frozenContent } : m
          ));
        }
        // A-2: 복구 중 표시 (사용자에게 끊김 대신 "복구 중" 인식)
        if (!isStale()) setToolStatus("🔄 응답 복구 중...");
        // streaming=true 유지 → AI 버블 그대로 보임

        if (sessionId) {
          pendingResponseSessions.current.add(sessionId);
        }

        // SSE resume 시도 — 서버 UP까지 무한 재시도 (최대 5분)
        let resumed = false;
        let skipToPolling = false;
        const resumeStartTime = Date.now();
        const MAX_RESUME_DURATION = 300000; // 5분(300초)

        while (!resumed && !gotFinal && !isStale() && !skipToPolling) {
          // 5분 초과 시 안내 메시지 후 종료
          if (Date.now() - resumeStartTime > MAX_RESUME_DURATION) {
            if (!isStale()) {
              setMessages(prev => prev.map(m =>
                m.intent === "streaming_placeholder"
                  ? { ...m, content: (frozenContent || "") + "\n\n🔧 서버 점검 중입니다. 잠시 후 새로고침해주세요.", intent: undefined }
                  : m
              ));
              setStreaming(false);
              setStreamBuf("");
              setToolStatus(null);
            }
            break;
          }

          try {
            const resumeAbort = new AbortController();
            const resumeTimeout = setTimeout(() => resumeAbort.abort(), 120000);
            let resumeResp: Response;
            try {
              resumeResp = await fetch(
                `${process.env.NEXT_PUBLIC_API_URL || ""}/chat/sessions/${sessionId}/stream-resume?offset=${full.length}&last_event_id=${encodeURIComponent(lastEventIdRef.current)}`,
                { headers: { "Authorization": `Bearer ${localStorage.getItem("aads_token") || ""}` }, signal: resumeAbort.signal }
              );
            } catch (resumeFetchErr) {
              clearTimeout(resumeTimeout);
              throw resumeFetchErr;
            }
            clearTimeout(resumeTimeout);
            if (!resumeResp.ok || !resumeResp.body) throw new Error("resume failed");

            const resumeReader = resumeResp.body.getReader();
            const resumeDecoder = new TextDecoder();
            let resumeBuf = "";

            while (true) {
              const { done: rDone, value: rVal } = await resumeReader.read();
              if (rDone) break;
              resumeBuf += resumeDecoder.decode(rVal, { stream: true });
              const rLines = resumeBuf.split("\n");
              resumeBuf = rLines.pop() || "";

              for (const rLine of rLines) {
                // Phase4: Redis Stream entry ID 캡처 (재연결 체인용)
                if (rLine.startsWith("id:")) { lastEventIdRef.current = rLine.slice(3).trim(); continue; }
                if (!rLine.startsWith("data: ")) continue;
                try {
                  const rev = JSON.parse(rLine.slice(6).trim());
                  if (rev.type === "delta" && rev.content) {
                    full += rev.content;
                    if (!isStale()) {
                      setStreamBuf(full);
                      setToolStatus(null); // 재연결 성공 — 인디케이터 제거
                    }
                    resumed = true;
                  } else if (rev.type === "resume_done") {
                    gotFinal = true;
                    setToolStatus(null);
                    if (full.trim()) {
                      // ★ in-place 업데이트: placeholder를 최종 응답으로 교체
                      setMessages((prev) => {
                        const hasPlaceholder = prev.some(m => m.intent === "streaming_placeholder");
                        if (hasPlaceholder) {
                          return prev.map(m => m.intent === "streaming_placeholder"
                            ? { ...m, content: full, intent: undefined }
                            : m
                          );
                        }
                        const _ph = prev.find(m => m.id.startsWith("ai-partial-"));
                        const _reuseId = _ph?.id || `ai-${Date.now()}`;
                        const _reuseTime = _ph?.created_at || new Date().toISOString();
                        return [
                          ...prev.filter(m => !m.id.startsWith("ai-partial-")),
                          { id: _reuseId, session_id: sessionId!, role: "assistant" as const, content: full, created_at: _reuseTime },
                        ];
                      });
                      requestAnimationFrame(() => { setStreamBuf(""); setStreaming(false); });
                    } else {
                      setStreamBuf(""); setStreaming(false);
                    }
                    resumed = true;
                    break;
                  } else if (rev.type === "resume_generating") {
                    // 서버에서 아직 생성 중 — 폴링 전환
                    skipToPolling = true;
                    break;
                  } else if (rev.type === "resume_unavailable" || rev.type === "resume_timeout") {
                    break;
                  } else if (rev.type === "heartbeat") {
                    // heartbeat 수신 = 서버 생존 확인, 무음 유지
                    if (rev.tool_count && rev.last_tool) {
                      setToolStatus(`🔧 ${rev.last_tool} 실행 중... (도구 ${rev.tool_count}회)`);
                    }
                  }
                } catch { /* skip malformed */ }
              }
              if (gotFinal) break;
            }
            if (resumed) break;
          } catch {
            // 재연결 실패 → health-check 폴링으로 서버 UP 대기
            if (!isStale()) setToolStatus("🔄 재연결 중...");

            // health-check 폴링 (2초 간격, 서버 UP까지)
            let serverUp = false;
            while (!serverUp && !isStale() && (Date.now() - resumeStartTime < MAX_RESUME_DURATION)) {
              await new Promise(r => setTimeout(r, 2000));
              if (isStale()) break;
              try {
                const hcAbort = new AbortController();
                const hcTimeout = setTimeout(() => hcAbort.abort(), 3000);
                const hcResp = await fetch(
                  `/api/v1/ops/health-check`,
                  { signal: hcAbort.signal }
                );
                clearTimeout(hcTimeout);
                if (hcResp.ok) { serverUp = true; }
              } catch { /* server still down */ }
            }
            if (!serverUp || isStale()) break;
            // 서버 UP 감지 — 즉시 재시도 (백오프 없이)
          }
        }

        // resume 실패 → polling 모드 전환 (Invisible: 같은 버블에서 partial_content 이어쓰기)
        if (!resumed && !gotFinal) {
          // A-3: frozenContent를 streamBuf에 유지 (버블 사라짐 방지)
          // 드레인 타이머는 try 스코프에서 종료되었으므로 직접 설정
          if (!isStale()) setStreamBuf(frozenContent);

          // waitingBgResponse 활성화하되 streaming도 유지 → 폴링이 partial_content를 streamBuf에 주입
          setWaitingBgResponse(true);
          _invisibleRecoveryActivated = true;
          setBgPartialContent(frozenContent);  // 폴링에서 비교 기준점
          if (waitingBgTimeoutRef.current) clearTimeout(waitingBgTimeoutRef.current);
          waitingBgTimeoutRef.current = setTimeout(() => {
            pendingResponseSessions.current.delete(sessionId!);
            setWaitingBgResponse(false); setBgPartialContent("");
            // ★ FIX: 타임아웃 시 버블 유지 — placeholder를 partial 메시지로 교체
            setMessages((prev) => prev.map(m =>
              m.intent === "streaming_placeholder"
                ? { ...m, content: (m.content || "") + "\n\n⏳ _응답 복구 대기 중..._", intent: undefined }
                : m
            ));
            setStreaming(false);
            setStreamBuf("");
          }, 120000);

          // last-response 폴백도 시도 (조용히)
          for (let retry = 0; retry < 3; retry++) {
            await new Promise((r) => setTimeout(r, 2000 * Math.pow(1.5, retry)));
            if (isStale()) break;
            try {
              const resp = await chatApi<{found: boolean; generating?: boolean; message?: ChatMessage}>(
                `/chat/sessions/${sessionId}/last-response`
              );
              // generating=true → 아직 생성 중, 이전 답변 교체 금지
              if ((resp as any).generating) {
                // 폴링으로 완료 대기
                break;
              }
              if (resp.found && resp.message) {
                if (!frozenContent || resp.message.content.length > frozenContent.length) {
                  // ★ in-place 업데이트
                  if (!isStale()) setStreamBuf(resp.message.content);
                  setMessages((prev) => {
                    const hasPlaceholder = prev.some(m => m.intent === "streaming_placeholder");
                    if (hasPlaceholder) {
                      return prev.map(m => m.intent === "streaming_placeholder"
                        ? { ...resp.message!, intent: undefined }
                        : m
                      );
                    }
                    const filtered = prev.filter(m => !m.id.startsWith("ai-partial-") && m.intent !== "streaming_placeholder");
                    return [...filtered, resp.message!];
                  });
                  requestAnimationFrame(() => {
                    setStreaming(false);
                    setStreamBuf("");
                  });
                  setWaitingBgResponse(false); setBgPartialContent("");
                  gotFinal = true;
                }
                break;
              }
            } catch { /* retry */ }
          }
          // 최종 폴백 실패 시에도 버블 유지 — 폴링(streaming-status)이 partial_content/just_completed 감지
          if (!gotFinal && frozenContent) {
            // 부분 텍스트를 ai-partial 메시지로 저장 (streaming 종료 후에도 보이도록)
            // streaming은 유지 → 폴링에서 just_completed 감지 시 최종 교체
          }
        }
      } else {
        setMessages((prev) => [
          ...prev,
          {
            id: `err-${Date.now()}`,
            session_id: sessionId!,
            role: "assistant",
            content: `⚠️ 오류: ${err.message}`,
          },
        ]);
      }
    } finally {
      clearTimeout(sseTimeout);
      clearTimeout(maxStreamTimeout);
      // Invisible Recovery: waitingBgResponse 활성화 중이면 streaming 유지 (버블 보존)
      // 폴링이 just_completed 감지 시 streaming을 해제함
      const _isInvisibleRecovery = _invisibleRecoveryActivated || waitingBgRef.current;
      if (streamingSessionRef.current === sessionId) {
        if (!_isInvisibleRecovery) streamingSessionRef.current = null;  // invisible recovery 시 유지
        if (!_isInvisibleRecovery) {
          setStreaming(false);
          setStreamBuf("");
        } else {
          // invisible recovery: 폴링이 완료 감지할 때까지 streaming 유지
          // 하지만 30초 안전장치 — 폴링도 실패하면 강제 해제
          if (waitingBgTimeoutRef.current) clearTimeout(waitingBgTimeoutRef.current);
          waitingBgTimeoutRef.current = setTimeout(() => {
            if (waitingBgRef.current) {
              setWaitingBgResponse(false);
              setBgPartialContent("");
              // ★ FIX: 버블 유지 — streaming 해제하되 placeholder를 partial 메시지로 교체
              setMessages((prev) => prev.map(m =>
                m.intent === "streaming_placeholder"
                  ? { ...m, content: (m.content || "") + "\n\n⏳ _응답 복구 대기 중..._", intent: undefined }
                  : m
              ));
              setStreaming(false);
              setStreamBuf("");
            }
          }, 120000);
        }
      }
      setToolStatus(null);
      if (!isStale() && !_isInvisibleRecovery) {
        // streaming_placeholder 잔여물 정리 — 내용 있으면 버블 유지 (사라짐 방지)
        setMessages((prev) => {
          const capturedBuf = streamBufRef.current;
          return prev.map((m) => {
            if (m.intent !== "streaming_placeholder") return m;
            const preserved = capturedBuf || m.content || "";
            if (preserved.trim()) {
              return { ...m, content: preserved, intent: undefined, model_used: "interrupted" };
            }
            return null;
          }).filter(Boolean) as ChatMessage[];
        });

        // P1-FIX: SSE 종료 직후 즉시 just_completed 체크 (interval 대기 없이)
        // 백그라운드 완료 메시지를 놓치지 않도록 500ms/2s/5s 3회 원샷 체크
        if (sessionId) {
          const _sid = sessionId;
          const _checkCompletion = async (delay: number) => {
            await new Promise((r) => setTimeout(r, delay));
            if (activeSessionRef.current !== _sid) return;
            try {
              const ss = await chatApi<{ is_streaming: boolean; just_completed?: boolean }>(
                `/chat/sessions/${_sid}/streaming-status`
              );
              if (ss.just_completed) {
                pendingResponseSessions.current.delete(_sid);
                setWaitingBgResponse(false); setBgPartialContent("");
                const freshMsgs = await chatApi<ChatMessage[]>(`/chat/messages?session_id=${_sid}&limit=50&sort=desc`).then(msgs => msgs.reverse());
                if (freshMsgs) {
                  const filtered = freshMsgs;
                  if (filtered.length > 0) {
                    setMessages(prev => {
                      // ★ in-place 업데이트: placeholder가 있으면 최종 메시지로 교체
                      const hasPlaceholder = prev.some(m => m.intent === "streaming_placeholder");
                      if (hasPlaceholder) {
                        const _lastAiJc = [...filtered].reverse().find((m: ChatMessage) => m.role === "assistant");
                        if (_lastAiJc) {
                          const freshIds = new Set(filtered.map(m => m.id));
                          const oldestFreshTime = new Date(filtered[0]?.created_at || 0).getTime();
                          const preserved = prev.filter(m =>
                            m.intent !== "streaming_placeholder" &&
                            !freshIds.has(m.id) && !m.id.startsWith("tmp-") && !m.id.startsWith("ai-") && !m.id.startsWith("stopped-") &&
                            new Date(m.created_at || 0).getTime() < oldestFreshTime
                          );
                          return [...preserved, ...filtered];
                        }
                      }
                      const freshIds = new Set(filtered.map(m => m.id));
                      const oldestFreshTime = new Date(filtered[0]?.created_at || 0).getTime();
                      const preserved = prev.filter(m => !freshIds.has(m.id) && !m.id.startsWith("tmp-") && !m.id.startsWith("ai-") && !m.id.startsWith("stopped-") && new Date(m.created_at || 0).getTime() < oldestFreshTime);
                      return [...preserved, ...filtered];
                    });
                    requestAnimationFrame(() => { setStreaming(false); setStreamBuf(""); });
                  }
                }
                // 자동 트리거(시스템 메시지) 응답이면 토스트 생략
                const _lastUser1696 = freshMsgs?.slice().reverse().find((m: ChatMessage) => m.role === "user");
                const _lastAi1696 = freshMsgs?.slice().reverse().find((m: ChatMessage) => m.role === "assistant" && m.intent !== "streaming_placeholder" && m.intent !== "rate_limited");
                if (!isAutoTriggerResponse(_lastUser1696, _lastAi1696)) {
                  if (_lastAi1696?.id) lastToastedAiIdRef.current = _lastAi1696.id;
                  showCompletionToast("응답이 완료되었습니다");
                } else if (_lastAi1696?.id) {
                  lastToastedAiIdRef.current = _lastAi1696.id;
                }
              }
            } catch { /* 원샷 체크 실패 — 기존 interval 폴링이 대신 감지 */ }
          };
          _checkCompletion(300);
        }

        // 스트리밍 완료 시 큐 잔여분 전체 클리어 (interrupt로 이미 전달됨)
        if (msgQueueRef.current.length > 0) {
          msgQueueRef.current = [];
        }
        setQueueCount(0);
      }
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [createSession, showCompletionToast]);

  function stopStreaming() {
    abortCtrl.current?.abort();
    const buf = streamBuf;
    setStreaming(false);
    setStreamBuf("");
    setToolStatus(null);
    setYellowWarning(null);
    setToolTurnInfo(null);
    isNearBottomRef.current = false;
    if (buf && activeSession) {
      // ★ in-place 업데이트: placeholder를 stopped 메시지로 교체
      setMessages((prev) => {
        const hasPlaceholder = prev.some(m => m.intent === "streaming_placeholder");
        const stoppedMsg: ChatMessage = {
          id: hasPlaceholder
            ? (prev.find(m => m.intent === "streaming_placeholder")?.id || `stopped-${Date.now()}`)
            : `stopped-${Date.now()}`,
          session_id: activeSession!.id,
          role: "assistant",
          content: buf + "\n\n_(응답 중지됨)_",
        };
        if (hasPlaceholder) {
          return prev.map(m => m.intent === "streaming_placeholder" ? stoppedMsg : m);
        }
        return [...prev, stoppedMsg];
      });
    }
    // FIX: 중지 후 스크롤 맨 아래로 강제 이동 (스트리밍 버블 제거로 인한 스크롤 점프 방지)
    requestAnimationFrame(() => {
      const container = messagesContainerRef.current;
      if (container) container.scrollTop = container.scrollHeight;
      isNearBottomRef.current = true;
    });
    // 백엔드 프로세스도 강제 중단
    if (activeSession) {
      fetch(`${BASE_URL}/chat/sessions/${activeSession.id}/stop`, {
        method: "POST",
        headers: { ...authHdrs() },
      }).catch(() => {});
      // 중지 후 DB에서 최신 상태를 한 번 fetch하여 동기화 (폴링 중복 방지)
      setTimeout(() => {
        if (!activeSession) return;
        chatApi<ChatMessage[]>(`/chat/messages?session_id=${activeSession.id}&limit=100&sort=desc`)
          .then((msgs) => msgs.reverse())
          .then((msgs) => {
            if (activeSessionRef.current !== activeSession.id) return;
            const filtered = msgs.map((m) => m.intent === "streaming_placeholder"
              ? { ...m, content: m.content || "⏳ AI가 응답을 생성 중입니다..." }
              : m
            );
            // FIX: placeholder 삭제 금지 — stopped/interrupt 메시지가 있으면 유지하면서 DB 메시지와 병합
            isNearBottomRef.current = false;
            setMessages((prev) => {
              const localMsgs = prev.filter((m) => m.id.startsWith("stopped-") || m.id.startsWith("interrupt-"));
              const dbIds = new Set(filtered.map((m) => m.id));
              // DB에 없는 로컬 메시지만 끝에 추가
              const merged = [...filtered, ...localMsgs.filter((m) => !dbIds.has(m.id))];
              return merged.sort(
                (a, b) => new Date(a.created_at || 0).getTime() - new Date(b.created_at || 0).getTime()
              );
            });
            // FIX: DB 동기화 후에도 스크롤 맨 아래로
            requestAnimationFrame(() => {
              const container = messagesContainerRef.current;
              if (container) container.scrollTop = container.scrollHeight;
              isNearBottomRef.current = true;
            });
          })
          .catch(() => {});
      }, 1500);
    }
  }

  /** 백그라운드 생성 중(waitingBgResponse) 전용 — 서버 /stop + 플래그·타이머 정리 (UI에 중지 버튼 없을 때 대비) */
  function stopBackgroundStreaming() {
    if (!activeSession) return;
    if (waitingBgTimeoutRef.current) {
      clearTimeout(waitingBgTimeoutRef.current);
      waitingBgTimeoutRef.current = null;
    }
    setWaitingBgResponse(false);
    setBgPartialContent("");
    fetch(`${BASE_URL}/chat/sessions/${activeSession.id}/stop`, {
      method: "POST",
      headers: { ...authHdrs() },
    }).catch(() => {});
    const sid = activeSession.id;
    setTimeout(() => {
      if (activeSessionRef.current !== sid) return;
      chatApi<{ messages: ChatMessage[]; has_more: boolean; next_cursor: string | null }>(`/chat/messages?session_id=${sid}&limit=100`)
        .then((result) => result.messages)
        .then((msgs) => {
          if (activeSessionRef.current !== sid) return;
          const filtered = msgs.map((m: ChatMessage) => m.intent === "streaming_placeholder"
            ? { ...m, content: m.content || "⏳ AI가 응답을 생성 중입니다..." }
            : m
          );
          // FIX: placeholder 삭제 금지
          setMessages((prev) => {
            const localMsgs = prev.filter((m) => m.id.startsWith("stopped-") || m.id.startsWith("interrupt-"));
            const dbIds = new Set(filtered.map((m) => m.id));
            const merged = [...filtered, ...localMsgs.filter((m) => !dbIds.has(m.id))];
            return merged.sort(
              (a, b) => new Date(a.created_at || 0).getTime() - new Date(b.created_at || 0).getTime()
            );
          });
        })
        .catch(() => {});
    }, 1200);
  }

  // ── 방식A: 수정 후 재전송 ──
  const handleEditResend = useCallback(async (msgId: string, newContent: string) => {
    if (!activeSessionObjRef.current) return;
    try {
      // 1) 기존 메시지 + AI 응답 삭제
      const res = await fetch(`${BASE_URL}/chat/messages/${msgId}`, {
        method: "DELETE",
        headers: { ...authHdrs() },
      });
      if (res.ok) {
        const data = await res.json();
        const deletedCount = data.deleted_count || 0;
        // 프론트에서도 해당 메시지 + 바로 다음 AI 메시지 제거
        setMessages((prev) => {
          const idx = prev.findIndex((m) => m.id === msgId);
          if (idx < 0) return prev;
          // 해당 메시지 + 바로 다음 assistant 메시지 제거
          const next = prev[idx + 1];
          const idsToRemove = new Set([msgId]);
          if (next && next.role === "assistant") idsToRemove.add(next.id);
          return prev.filter((m) => !idsToRemove.has(m.id));
        });
      }
      // 2) 수정된 내용으로 재전송
      await sendMessage(newContent);
    } catch (e) {
    }
    setEditingMsgId(null);
    setEditText("");
  }, [sendMessage]);

  // ── 메시지 삭제 (user: 메시지+AI응답 삭제, assistant: 해당 응답만 삭제) ──
  const handleDeleteMessage = useCallback(async (msgId: string, role: string) => {
    if (!confirm(role === "user" ? "이 메시지와 AI 응답을 삭제할까요?" : "이 응답을 삭제할까요?")) return;
    try {
      const res = await fetch(`${BASE_URL}/chat/messages/${msgId}`, {
        method: "DELETE",
        headers: { ...authHdrs() },
      });
      if (res.ok) {
        setMessages((prev) => {
          if (role === "user") {
            const idx = prev.findIndex((m) => m.id === msgId);
            if (idx < 0) return prev;
            const idsToRemove = new Set([msgId]);
            const next = prev[idx + 1];
            if (next && next.role === "assistant") idsToRemove.add(next.id);
            return prev.filter((m) => !idsToRemove.has(m.id));
          } else {
            return prev.filter((m) => m.id !== msgId);
          }
        });
      }
    } catch (e) {
    }
  }, []);

  // ── AI 응답 재생성 (Regenerate) ──
  const [regeneratingId, setRegeneratingId] = useState<string | null>(null);
  const handleRegenerate = useCallback(async (msgId: string) => {
    if (!activeSessionObjRef.current || streaming) return;
    const sessionId = activeSessionObjRef.current.id;

    setRegeneratingId(msgId);
    setStreaming(true);
    setStreamBuf("");
    setToolLogs([]);
    streamingSessionRef.current = sessionId;

    const requestSessionId = sessionId;
    const isStale = () => activeSessionRef.current !== requestSessionId;

    abortCtrl.current = new AbortController();
    let sseTimeout = setTimeout(() => { abortCtrl.current?.abort(); }, 150000);
    const resetSseTimeout = () => { clearTimeout(sseTimeout); sseTimeout = setTimeout(() => { abortCtrl.current?.abort(); }, 150000); };
    const maxStreamTimeout = setTimeout(() => { abortCtrl.current?.abort(); }, 3600000);

    let full = "";
    try {
      const res = await fetch(`${BASE_URL}/chat/messages/${msgId}/regenerate`, {
        method: "POST",
        headers: { ...authHdrs() },
        signal: abortCtrl.current.signal,
      });
      if (!res.ok) throw new Error(`서버 오류 (${res.status})`);

      const reader = res.body?.getReader();
      if (!reader) throw new Error("No response body");

      const decoder = new TextDecoder();
      let buf = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        if (isStale()) { reader.cancel(); break; }
        buf += decoder.decode(value, { stream: true });
        const lines = buf.split("\n");
        buf = lines.pop() ?? "";

        for (const line of lines) {
          if (isStale()) break;
          // Phase4: Redis Stream entry ID 캡처
          if (line.startsWith("id:")) { lastEventIdRef.current = line.slice(3).trim(); continue; }
          if (!line.startsWith("data: ")) continue;
          const raw = line.slice(6).trim();
          if (raw === "[DONE]") continue;
          try {
            const ev = JSON.parse(raw);
            if (ev.type === "heartbeat") { resetSseTimeout(); continue; }
            resetSseTimeout();
            if (ev.type === "delta" && typeof ev.content === "string") {
              full += ev.content;
              if (!isStale()) setStreamBuf(full);
            } else if (ev.type === "token" && typeof ev.text === "string") {
              full += ev.text;
              if (!isStale()) setStreamBuf(full);
            } else if (ev.type === "done") {
              setStreamBuf("");
              setStreaming(false);
              setToolStatus(null);
              setToolLogs([]);
              if (ev.session_cost) setSessionCost(ev.session_cost);
              if (ev.session_turns) setSessionTurns(ev.session_turns);
              // 기존 AI 메시지 intent를 regenerated로 표시 + 새 메시지 추가
              const finalMsg: ChatMessage = ev.message || {
                id: ev.message_id || `regen-${Date.now()}`,
                session_id: sessionId,
                role: "assistant",
                content: full,
                created_at: new Date().toISOString(),
                model_used: ev.model,
                input_tokens: ev.input_tokens,
                output_tokens: ev.output_tokens,
                cost_usd: ev.cost_usd,
              };
              setMessages((prev) => {
                // 기존 메시지의 intent를 regenerated로 변경
                const updated = prev.map((m) =>
                  m.id === msgId ? { ...m, intent: "regenerated" } : m
                );
                // 새 메시지 추가 (기존 메시지 바로 뒤에)
                const idx = updated.findIndex((m) => m.id === msgId);
                if (idx >= 0) {
                  updated.splice(idx + 1, 0, finalMsg);
                  return [...updated];
                }
                return [...updated, finalMsg];
              });
              break;
            }
          } catch {}
        }
      }
    } catch (e: unknown) {
      const errMsg = e instanceof Error ? e.message : "재생성 실패";
      console.error("regenerate error:", errMsg);
    } finally {
      clearTimeout(sseTimeout);
      clearTimeout(maxStreamTimeout);
      setStreaming(false);
      setStreamBuf("");
      setRegeneratingId(null);
    }
  }, [streaming]);

  // ── 방식B: 입력창에 복사 (재지시) ──
  const handleCopyToInput = useCallback((content: string) => {
    setInput(content); chatInputRef.current?.setValue(content);
    setEditMode("resend");
    // 포커스
    setTimeout(() => {
      const ta = document.querySelector("textarea");
      if (ta) { ta.focus(); ta.setSelectionRange(ta.value.length, ta.value.length); }
    }, 100);
  }, []);

  // ── 히든 스크린 컨텍스트 캡처 핸들러 ──
  const handleHiddenScreenCapture = useCallback((file: File) => {
    screenContextRef.current = file;
  }, []);

  // ── File attachment (클라이언트 측 inline 변환 — 서버 업로드 불필요) ──
  async function handleFiles(files: FileList | File[] | null) {
    if (!files || files.length === 0) return;
    const fileArray = Array.from(files);
    // 로컬 미리보기용 File 객체 즉시 저장
    setPendingPreviewFiles((prev) => [...prev, ...fileArray]);

    const IMAGE_EXTS = new Set(["jpg", "jpeg", "png", "gif", "webp"]);
    const TEXT_EXTS = new Set([
      "txt", "md", "csv", "json", "py", "js", "ts", "tsx", "jsx",
      "html", "css", "yaml", "yml", "toml", "sh", "sql", "log",
      "xml", "ini", "conf", "cfg", "rs", "go", "java", "c", "cpp",
      "h", "rb", "php", "swift", "kt",
    ]);
    const VIDEO_EXTS = new Set(["mp4", "webm", "mov", "avi", "mkv", "flv", "m4v"]);
    const VIDEO_MAX_BYTES = 20 * 1024 * 1024; // 20MB
    const _sid = activeSession?.id;

    for (const file of fileArray) {
      const ext = file.name.split(".").pop()?.toLowerCase() || "";
      const isImage = IMAGE_EXTS.has(ext) || file.type.startsWith("image/");
      const isText = TEXT_EXTS.has(ext) || file.type.startsWith("text/");
      const isVideo = VIDEO_EXTS.has(ext) || file.type.startsWith("video/");

      // 이미지: 서버 업로드 → file_id 기반 (fallback: base64)
      if (isImage && _sid) {
        try {
          const result = await uploadChatFile(file, _sid);
          pendingAttachments.current.push({
            type: "image", file_id: result.file_id,
            media_type: result.mime_type, name: result.original_name,
            file_url: result.file_url, thumbnail_url: result.thumbnail_url,
          });
        } catch {
          // fallback: base64
          const base64 = await new Promise<string>((resolve, reject) => {
            const reader = new FileReader();
            reader.onload = () => resolve((reader.result as string).split(",")[1] ?? "");
            reader.onerror = reject;
            reader.readAsDataURL(file);
          });
          const mediaType = file.type || `image/${ext === "jpg" ? "jpeg" : ext}`;
          pendingAttachments.current.push({ type: "image", base64, media_type: mediaType, name: file.name });
        }
      } else if (isImage) {
        // 세션 없으면 기존 base64 방식
        const base64 = await new Promise<string>((resolve, reject) => {
          const reader = new FileReader();
          reader.onload = () => resolve((reader.result as string).split(",")[1] ?? "");
          reader.onerror = reject;
          reader.readAsDataURL(file);
        });
        const mediaType = file.type || `image/${ext === "jpg" ? "jpeg" : ext}`;
        pendingAttachments.current.push({ type: "image", base64, media_type: mediaType, name: file.name });
      } else if (isText) {
        // 텍스트 파일: 서버 업로드 시도 → fallback: 로컬 읽기
        if (_sid) {
          try {
            const result = await uploadChatFile(file, _sid);
            pendingAttachments.current.push({
              type: "text", file_id: result.file_id, name: result.original_name,
              file_url: result.file_url, file_size: result.file_size,
            });
          } catch {
            // fallback: 로컬 읽기
            const content = await new Promise<string>((resolve) => {
              const reader = new FileReader();
              reader.onload = () => resolve(reader.result as string);
              reader.onerror = () => resolve("");
              reader.readAsText(file.slice(0, 500_000));
            });
            pendingAttachments.current.push({ type: "text", name: file.name, content });
          }
        } else {
          const content = await new Promise<string>((resolve) => {
            const reader = new FileReader();
            reader.onload = () => resolve(reader.result as string);
            reader.onerror = () => resolve("");
            reader.readAsText(file.slice(0, 500_000));
          });
          pendingAttachments.current.push({ type: "text", name: file.name, content });
        }
      } else if (ext === "pdf" || file.type === "application/pdf") {
        // PDF: 서버 업로드 시도 → fallback base64
        if (_sid) {
          try {
            const result = await uploadChatFile(file, _sid);
            pendingAttachments.current.push({
              type: "pdf", file_id: result.file_id, name: result.original_name,
              media_type: "application/pdf", file_url: result.file_url,
            });
          } catch {
            const base64 = await new Promise<string>((resolve) => {
              const reader = new FileReader();
              reader.onload = () => resolve((reader.result as string).split(",")[1] ?? "");
              reader.onerror = () => resolve("");
              reader.readAsDataURL(file);
            });
            pendingAttachments.current.push({ type: "pdf", base64, name: file.name, media_type: "application/pdf" });
          }
        } else {
          const base64 = await new Promise<string>((resolve) => {
            const reader = new FileReader();
            reader.onload = () => resolve((reader.result as string).split(",")[1] ?? "");
            reader.onerror = () => resolve("");
            reader.readAsDataURL(file);
          });
          pendingAttachments.current.push({ type: "pdf", base64, name: file.name, media_type: "application/pdf" });
        }
      } else if (isVideo) {
        // 동영상: 20MB 이하 → 서버 업로드 시도 → fallback base64
        if (file.size > VIDEO_MAX_BYTES) {
          pendingAttachments.current.push({ type: "file", name: file.name, error: `동영상 파일이 너무 큽니다 (최대 20MB). 현재: ${(file.size / 1024 / 1024).toFixed(1)}MB` });
        } else if (_sid) {
          try {
            const result = await uploadChatFile(file, _sid);
            pendingAttachments.current.push({
              type: "video", file_id: result.file_id, name: result.original_name,
              media_type: result.mime_type, file_url: result.file_url,
            });
          } catch {
            const base64 = await new Promise<string>((resolve) => {
              const reader = new FileReader();
              reader.onload = () => resolve((reader.result as string).split(",")[1] ?? "");
              reader.onerror = () => resolve("");
              reader.readAsDataURL(file);
            });
            const mediaType = file.type || `video/${ext}`;
            pendingAttachments.current.push({ type: "video", base64, name: file.name, media_type: mediaType });
          }
        } else {
          const base64 = await new Promise<string>((resolve) => {
            const reader = new FileReader();
            reader.onload = () => resolve((reader.result as string).split(",")[1] ?? "");
            reader.onerror = () => resolve("");
            reader.readAsDataURL(file);
          });
          const mediaType = file.type || `video/${ext}`;
          pendingAttachments.current.push({ type: "video", base64, name: file.name, media_type: mediaType });
        }
      } else {
        // 기타 파일: 서버 업로드 시도
        if (_sid) {
          try {
            const result = await uploadChatFile(file, _sid);
            pendingAttachments.current.push({
              type: "file", file_id: result.file_id, name: result.original_name,
              file_url: result.file_url, file_size: result.file_size,
            });
          } catch {
            pendingAttachments.current.push({ type: "file", name: file.name });
          }
        } else {
          pendingAttachments.current.push({ type: "file", name: file.name });
        }
      }
    }
    textareaRef.current?.focus();
  }


  // Ctrl+V 클립보드 붙여넣기 — 위(activeWs 의존) 핸들러가 모든 파일 타입 처리

  // 개별 첨부 파일 제거
  function removePendingFile(idx: number) {
    setPendingPreviewFiles((prev) => prev.filter((_, i) => i !== idx));
    pendingAttachments.current = pendingAttachments.current.filter((_, i) => i !== idx);
  }

  function onDrop(e: React.DragEvent) {
    e.preventDefault();
    handleFiles(e.dataTransfer.files);
  }

  // ── Action chips ──
  function applyChip(prefix: string) {
    setInput((prev) => (prev ? `${prefix} ${prev}` : `${prefix} `)); setHasInput(true);
    textareaRef.current?.focus();
  }

  // ── P2-10: 템플릿 함수들 ──
  async function fetchTemplates() {
    try {
      const list = await chatApi<Array<{ id: string; title: string; content: string; category: string; usage_count: number; created_at: string; updated_at: string }>>("/chat/templates");
      setTemplates(list);
    } catch { /* ignore */ }
  }
  async function handleUseTemplate(tpl: { id: string; content: string }) {
    try { await chatApi(`/chat/templates/${tpl.id}/use`, { method: "POST" }); } catch { /* ignore */ }
    setShowTemplates(false);
    chatInputRef.current?.setValue(tpl.content);
    setInput(tpl.content);
    setHasInput(true);
    chatInputRef.current?.focus();
  }
  async function handleCreateTemplate() {
    const content = chatInputRef.current?.getValue()?.trim() || "";
    if (!newTplTitle.trim() || !content) return;
    try {
      await chatApi("/chat/templates", {
        method: "POST",
        body: JSON.stringify({ title: newTplTitle.trim(), content, category: newTplCategory }),
      });
      setNewTplTitle("");
      setNewTplCategory("일반");
      setShowNewTemplate(false);
      fetchTemplates();
    } catch { /* ignore */ }
  }
  async function handleDeleteTemplate(id: string) {
    try { await chatApi(`/chat/templates/${id}`, { method: "DELETE" }); fetchTemplates(); } catch { /* ignore */ }
  }

  // ── Keyboard (useCallback으로 안정화 → ChatInput memo 유효화, IME 깨짐 방지) ──
  const onKeyDown = useCallback((e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    // 한글 IME 조합 중이면 키 이벤트 무시 (깨짐 방지)
    if (e.nativeEvent.isComposing || e.keyCode === 229) return;
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); if (!uploadingRef.current) sendMessage(); return; }
    // Ctrl+Z: 마지막 큐 메시지 취소
    if (e.ctrlKey && e.key === "z" && queueCountRef.current > 0) {
      e.preventDefault();
      const removed = msgQueueRef.current.pop();
      setQueueCount(msgQueueRef.current.length);
      if (removed) { setInput(removed); chatInputRef.current?.setValue(removed); }
      return;
    }
    if (e.ctrlKey && e.key === "]") {
      e.preventDefault();
      setArtifactMode((m) => (m === "full" ? "mini" : m === "mini" ? "hidden" : "full"));
    }
  }, [sendMessage]);

  // ── Context menu ──
  function onSessionContextMenu(e: React.MouseEvent, session: ChatSession) {
    e.preventDefault();
    setContextMenu({ x: e.clientX, y: e.clientY, session });
  }

  // ── Artifacts ──
  function copyArtifact(content: string) {
    navigator.clipboard?.writeText(content).catch(() => {});
  }
  async function toDirective(artifact: Artifact) {
    const text = `TITLE: ${artifact.title}\nDESCRIPTION: |\n  ${artifact.content.split("\n").join("\n  ")}`;
    await navigator.clipboard?.writeText(text).catch(() => {});
    showCompletionToast("지시서 형식이 클립보드에 복사되었습니다");
  }

  // ── Derived ──
  const vars = theme === "dark" ? DARK : LIGHT;
  const activeWsObj = workspaces.find((w) => w.id === activeWs);
  const activeWsName = activeWsObj?.name || "워크스페이스";
  const filteredSessions = sessions.filter(
    (s) => {
      if (search && !s.title.toLowerCase().includes(search.toLowerCase())) return false;
      if (tagFilter && !(s.tags || []).includes(tagFilter)) return false;
      return true;
    }
  );
  // 전체 세션에서 사용 중인 태그 목록 수집
  const allTags = Array.from(new Set(sessions.flatMap((s) => s.tags || [])));
  const filteredArtifacts = artifacts.filter((a) => {
    if (artifactTab === "report") return a.artifact_type === "report" || a.artifact_type === "text" || a.artifact_type === "file" || a.artifact_type === "table";
    if (artifactTab === "dialog") return a.artifact_type === "full_response";
    if (artifactTab === "code") return a.artifact_type === "code";
    if (artifactTab === "chart") return a.artifact_type === "chart" || a.artifact_type === "image";
    if (artifactTab === "agenda") return false;
    if (artifactTab === "html_preview") return a.artifact_type === "html_preview";
  });
  const activeArtifact = filteredArtifacts[selectedArtifactIdx] || filteredArtifacts[0] || null;
  const artifactCounts: Record<string, number> = {
    report: artifacts.filter((a) => a.artifact_type === "report" || a.artifact_type === "text" || a.artifact_type === "file" || a.artifact_type === "table").length,
    dialog: artifacts.filter((a) => a.artifact_type === "full_response").length,
    code: artifacts.filter((a) => a.artifact_type === "code").length,
    chart: artifacts.filter((a) => a.artifact_type === "chart" || a.artifact_type === "image").length,
    agenda: 0,
    log: systemMessages.length,
    html_preview: artifacts.filter((a) => a.artifact_type === "html_preview").length,
  };
  // C1: swipe gesture handlers
  function onSwipeStart(e: React.TouchEvent) {
    if (screenSize === "desktop") return;
    const t = e.touches[0];
    swipeRef.current = { startX: t.clientX, startY: t.clientY, t: Date.now() };
  }
  function onSwipeEnd(e: React.TouchEvent) {
    if (screenSize === "desktop" || !swipeRef.current) return;
    const t = e.changedTouches[0];
    const dx = t.clientX - swipeRef.current.startX;
    const dy = t.clientY - swipeRef.current.startY;
    const dt = Date.now() - swipeRef.current.t;
    swipeRef.current = null;
    if (dt > 500 || Math.abs(dy) > Math.abs(dx)) return;
    if (dx > 80 && !mobileOverlay) setMobileOverlay("sidebar");
    else if (dx < -80 && !mobileOverlay) setMobileOverlay("artifact");
    else if (dx < -80 && mobileOverlay === "sidebar") setMobileOverlay(null);
    else if (dx > 80 && mobileOverlay === "artifact") setMobileOverlay(null);
  }

  // Responsive: whether to show overlays
  const showLeftSidebar =
    screenSize === "desktop" ? leftOpen : mobileOverlay === "sidebar";
  const showArtifactPanel =
    screenSize === "desktop" ? artifactMode !== "hidden" : mobileOverlay === "artifact";

  // ── 키보드 단축키 ──
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      const mod = e.ctrlKey || e.metaKey;
      // Ctrl/Cmd + N: 새 대화
      if (mod && e.key === "n") {
        e.preventDefault();
        createSession();
      }
      // Ctrl/Cmd + Shift + S: 사이드바 토글
      if (mod && e.shiftKey && (e.key === "S" || e.key === "s")) {
        e.preventDefault();
        setLeftOpen((prev) => !prev);
      }
      // Escape: 스트리밍 중단
      if (e.key === "Escape" && streamingRef.current) {
        e.preventDefault();
        stopStreaming();
      }
      // Ctrl/Cmd + /: 단축키 도움말
      if (mod && e.key === "/") {
        e.preventDefault();
        setShowShortcutHelp((prev) => !prev);
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [createSession]);

  // ══════════════════════════════════════════════════════════════════
  // Render
  // ══════════════════════════════════════════════════════════════════
  return (
    <div
      style={{
        ...vars,
        display: "flex",
        height: "100dvh",
        overflow: "hidden",
        background: "var(--ct-bg)",
        color: "var(--ct-text)",
        transition: "background 0.3s, color 0.3s",
        fontFamily: "Arial, Helvetica, sans-serif",
        position: "relative",
      }}
      onClick={() => setContextMenu(null)}
      onTouchStart={onSwipeStart}
      onTouchEnd={onSwipeEnd}
    >
      {updateAvailable && <UpdateBanner onRefresh={doRefresh} />}
      {/* ── 라이트박스 ── */}
      {lightboxSrcs.length > 0 && (
        <div onClick={() => setLightboxSrcs([])} style={{
          position: "fixed", inset: 0, zIndex: 9999,
          background: "rgba(0,0,0,0.92)",
          display: "flex", alignItems: "center", justifyContent: "center",
        }}>
          <img src={lightboxSrcs[lightboxIdx]} onClick={e => e.stopPropagation()}
            style={{ maxWidth: "90vw", maxHeight: "90vh", objectFit: "contain", borderRadius: "8px" }} />
          {lightboxSrcs.length > 1 && <>
            <button onClick={e => { e.stopPropagation(); setLightboxIdx(i => (i - 1 + lightboxSrcs.length) % lightboxSrcs.length); }}
              style={{ position: "absolute", left: "16px", top: "50%", transform: "translateY(-50%)",
                background: "rgba(255,255,255,0.15)", border: "none", color: "#fff",
                fontSize: "24px", padding: "8px 16px", borderRadius: "8px", cursor: "pointer" }}>◀</button>
            <button onClick={e => { e.stopPropagation(); setLightboxIdx(i => (i + 1) % lightboxSrcs.length); }}
              style={{ position: "absolute", right: "16px", top: "50%", transform: "translateY(-50%)",
                background: "rgba(255,255,255,0.15)", border: "none", color: "#fff",
                fontSize: "24px", padding: "8px 16px", borderRadius: "8px", cursor: "pointer" }}>▶</button>
            <div style={{ position: "absolute", bottom: "16px", color: "#fff", fontSize: "14px",
              background: "rgba(0,0,0,0.5)", padding: "4px 12px", borderRadius: "12px" }}>
              {lightboxIdx + 1} / {lightboxSrcs.length}
            </div>
          </>}
          <button onClick={() => setLightboxSrcs([])} style={{
            position: "absolute", top: "16px", right: "16px",
            background: "rgba(255,255,255,0.15)", border: "none", color: "#fff",
            fontSize: "20px", padding: "6px 12px", borderRadius: "8px", cursor: "pointer" }}>✕</button>
        </div>
      )}
      {/* ── 완료 토스트 ── */}
      {completionToast && (
        <div style={{
          position: "fixed", top: 24, left: "50%", transform: "translateX(-50%)",
          zIndex: 9999, background: "#22c55e", color: "#fff", padding: "10px 24px",
          borderRadius: 8, fontSize: 14, fontWeight: 600, boxShadow: "0 4px 12px rgba(0,0,0,0.3)",
          animation: "fadeIn 0.3s ease",
        }}>
          {completionToast}
        </div>
      )}
      {/* ── 아티팩트 저장 토스트 (우하단) ── */}
      {artifactToast && (
        <div style={{
          position: "fixed", bottom: 24, right: 24,
          zIndex: 9999, background: "var(--ct-accent)", color: "#fff", padding: "10px 18px",
          borderRadius: 8, fontSize: 13, fontWeight: 600, boxShadow: "0 4px 12px rgba(0,0,0,0.3)",
          animation: "fadeIn 0.3s ease",
        }}>
          {artifactToast}
        </div>
      )}
      {/* ── Image generation modal ── */}
      {showImageGen && (
        <div
          style={{
            position: "fixed",
            inset: 0,
            background: "rgba(0,0,0,0.7)",
            zIndex: 1000,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
          }}
          onClick={(e) => { if (e.target === e.currentTarget) setShowImageGen(false); }}
        >
          <div
            style={{
              background: "var(--ct-card, #1e2130)",
              borderRadius: "16px",
              padding: "24px",
              width: "400px",
              maxWidth: "90vw",
              border: "1px solid var(--ct-border, #2d3148)",
            }}
          >
            <h3 style={{ color: "#a78bfa", marginBottom: "16px", fontSize: "1rem" }}>
              🎨 AI 이미지 생성
            </h3>
            <textarea
              value={imageGenPrompt}
              onChange={(e) => setImageGenPrompt(e.target.value)}
              placeholder="이미지 프롬프트 입력 (예: 서울 야경, 미래도시, 귀여운 강아지...)"
              onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); handleImageGen(); } }}
              style={{
                width: "100%",
                height: "100px",
                background: "var(--ct-bg, #0f1117)",
                border: "1px solid var(--ct-border, #2d3148)",
                borderRadius: "8px",
                color: "var(--ct-text, #e2e8f0)",
                padding: "10px",
                fontSize: "0.85rem",
                resize: "none",
                outline: "none",
                boxSizing: "border-box",
              }}
            />
            <div style={{ display: "flex", gap: "8px", marginTop: "12px", justifyContent: "flex-end" }}>
              <button
                onClick={() => setShowImageGen(false)}
                style={{
                  padding: "8px 16px",
                  borderRadius: "8px",
                  border: "1px solid var(--ct-border, #2d3148)",
                  background: "transparent",
                  color: "var(--ct-text2, #94a3b8)",
                  cursor: "pointer",
                }}
              >
                취소
              </button>
              <button
                onClick={handleImageGen}
                disabled={imageGenLoading}
                style={{
                  padding: "8px 20px",
                  borderRadius: "8px",
                  border: "none",
                  background: "linear-gradient(135deg, #7c3aed, #4f46e5)",
                  color: "#fff",
                  cursor: imageGenLoading ? "wait" : "pointer",
                  fontWeight: 600,
                  opacity: imageGenLoading ? 0.7 : 1,
                }}
              >
                {imageGenLoading ? "생성 중..." : "생성"}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ── Keyframe styles ── */}
      <style>{`
        @keyframes ct-bounce {
          0%,80%,100%{transform:scale(0.6);opacity:0.4}
          40%{transform:scale(1);opacity:1}
        }
        @keyframes ct-blink {
          0%,100%{opacity:1} 50%{opacity:0}
        }
        @keyframes ct-theme {
          from{opacity:0.7} to{opacity:1}
        }
        @keyframes ct-pulse-dot {
          0%,100%{box-shadow:0 0 0 0 rgba(239,68,68,0.5)}
          50%{box-shadow:0 0 0 5px rgba(239,68,68,0)}
        }
        .ct-msg-enter { animation: ct-theme 0.2s ease; }
      `}</style>

      {/* ── Context Menu ── */}
      {contextMenu && (
        <div
          style={{
            position: "fixed",
            left: contextMenu.x,
            top: contextMenu.y,
            zIndex: 2000,
            background: "var(--ct-card)",
            border: "1px solid var(--ct-border)",
            borderRadius: "8px",
            padding: "4px",
            boxShadow: "0 4px 20px rgba(0,0,0,0.35)",
            minWidth: "140px",
          }}
          onClick={(e) => e.stopPropagation()}
        >
          {[
            {
              icon: "🔗",
              label: "새창에서 열기",
              color: "var(--ct-text)",
              action: () => {
                window.open(`${window.location.origin}/chat#${contextMenu.session.id}`, "_blank");
                setContextMenu(null);
              },
            },
            {
              icon: contextMenu.session.pinned ? "📌" : "📌",
              label: contextMenu.session.pinned ? "고정 해제" : "고정",
              color: "var(--ct-text)",
              action: () => togglePin(contextMenu.session),
            },
            {
              icon: "🏷️",
              label: "태그 편집",
              color: "var(--ct-text)",
              action: () => {
                setTagEditSession({ id: contextMenu.session.id, tags: [...(contextMenu.session.tags || [])] });
                setTagInput("");
                setContextMenu(null);
              },
            },
            {
              icon: "✏️",
              label: "이름 변경",
              color: "var(--ct-text)",
              action: () => {
                setRenaming({ id: contextMenu.session.id, value: contextMenu.session.title });
                setContextMenu(null);
              },
            },
            {
              icon: "🗑️",
              label: "삭제",
              color: "#ef4444",
              action: () => deleteSession(contextMenu.session.id),
            },
          ].map((item) => (
            <button
              key={item.label}
              onClick={item.action}
              style={{
                display: "block",
                width: "100%",
                textAlign: "left",
                padding: "8px 12px",
                fontSize: "13px",
                background: "none",
                border: "none",
                cursor: "pointer",
                color: item.color,
                borderRadius: "4px",
              }}
              onMouseEnter={(e) => (e.currentTarget.style.background = "var(--ct-hover)")}
              onMouseLeave={(e) => (e.currentTarget.style.background = "none")}
            >
              {item.icon} {item.label}
            </button>
          ))}
        </div>
      )}

      {/* ── 태그 편집 모달 ── */}
      {tagEditSession && (
        <div
          style={{
            position: "fixed", inset: 0, zIndex: 3000,
            background: "rgba(0,0,0,0.5)",
            display: "flex", alignItems: "center", justifyContent: "center",
          }}
          onClick={() => setTagEditSession(null)}
        >
          <div
            style={{
              background: "var(--ct-card)", borderRadius: "12px", padding: "20px",
              width: "340px", maxWidth: "90vw",
              boxShadow: "0 8px 30px rgba(0,0,0,0.4)",
            }}
            onClick={(e) => e.stopPropagation()}
          >
            <div style={{ fontWeight: 700, fontSize: "14px", color: "var(--ct-text)", marginBottom: "12px" }}>
              태그 편집
            </div>
            {/* 현재 태그 */}
            <div style={{ display: "flex", flexWrap: "wrap", gap: "6px", marginBottom: "10px", minHeight: "28px" }}>
              {tagEditSession.tags.map((t) => (
                <span
                  key={t}
                  style={{
                    display: "inline-flex", alignItems: "center", gap: "4px",
                    padding: "3px 8px", fontSize: "11px", borderRadius: "12px",
                    background: "var(--ct-accent)", color: "#fff",
                  }}
                >
                  {t}
                  <button
                    onClick={() => {
                      const next = tagEditSession.tags.filter((x) => x !== t);
                      setTagEditSession({ ...tagEditSession, tags: next });
                    }}
                    style={{
                      background: "none", border: "none", cursor: "pointer",
                      color: "#fff", fontSize: "12px", padding: "0 2px", lineHeight: 1,
                    }}
                  >
                    ×
                  </button>
                </span>
              ))}
            </div>
            {/* 기본 태그 제안 */}
            <div style={{ display: "flex", flexWrap: "wrap", gap: "4px", marginBottom: "10px" }}>
              {["KIS", "GO100", "AADS", "SF", "NTV2", "기능개선", "버그수정", "전략"].map((t) => (
                <button
                  key={t}
                  onClick={() => {
                    if (!tagEditSession.tags.includes(t)) {
                      setTagEditSession({ ...tagEditSession, tags: [...tagEditSession.tags, t] });
                    }
                  }}
                  style={{
                    padding: "2px 8px", fontSize: "10px", borderRadius: "10px",
                    background: tagEditSession.tags.includes(t) ? "var(--ct-accent)" : "var(--ct-hover)",
                    color: tagEditSession.tags.includes(t) ? "#fff" : "var(--ct-text2)",
                    border: "1px solid var(--ct-border)", cursor: "pointer",
                  }}
                >
                  {t}
                </button>
              ))}
            </div>
            {/* 커스텀 태그 입력 */}
            <div style={{ display: "flex", gap: "6px", marginBottom: "14px" }}>
              <input
                value={tagInput}
                onChange={(e) => setTagInput(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && tagInput.trim()) {
                    if (!tagEditSession.tags.includes(tagInput.trim())) {
                      setTagEditSession({ ...tagEditSession, tags: [...tagEditSession.tags, tagInput.trim()] });
                    }
                    setTagInput("");
                  }
                }}
                placeholder="커스텀 태그 입력..."
                style={{
                  flex: 1, padding: "6px 10px", fontSize: "12px",
                  background: "var(--ct-input)", color: "var(--ct-text)",
                  border: "1px solid var(--ct-border)", borderRadius: "6px", outline: "none",
                }}
              />
            </div>
            {/* 저장/취소 */}
            <div style={{ display: "flex", justifyContent: "flex-end", gap: "8px" }}>
              <button
                onClick={() => setTagEditSession(null)}
                style={{
                  padding: "6px 14px", fontSize: "12px", borderRadius: "6px",
                  background: "var(--ct-hover)", color: "var(--ct-text)",
                  border: "1px solid var(--ct-border)", cursor: "pointer",
                }}
              >
                취소
              </button>
              <button
                onClick={() => {
                  updateSessionTags(tagEditSession.id, tagEditSession.tags);
                  setTagEditSession(null);
                }}
                style={{
                  padding: "6px 14px", fontSize: "12px", borderRadius: "6px",
                  background: "var(--ct-accent)", color: "#fff",
                  border: "none", cursor: "pointer",
                }}
              >
                저장
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ── 프로젝트 추가 모달 ── */}
      {showAddProject && (
        <div
          style={{
            position: "fixed", inset: 0, zIndex: 3000,
            background: "rgba(0,0,0,0.5)",
            display: "flex", alignItems: "center", justifyContent: "center",
          }}
          onClick={() => setShowAddProject(false)}
        >
          <div
            style={{
              background: "var(--ct-card)", borderRadius: "16px",
              padding: "24px", width: "360px", maxWidth: "90vw",
              border: "1px solid var(--ct-border)",
              boxShadow: "0 8px 40px rgba(0,0,0,0.4)",
            }}
            onClick={(e) => e.stopPropagation()}
          >
            <h3 style={{ margin: "0 0 16px 0", fontSize: "16px", color: "var(--ct-text)" }}>
              새 프로젝트 추가
            </h3>
            <div style={{ display: "flex", flexDirection: "column", gap: "10px" }}>
              <div>
                <label style={{ fontSize: "11px", color: "var(--ct-text2)", display: "block", marginBottom: "4px" }}>
                  프로젝트 코드 (영문)
                </label>
                <input
                  autoFocus
                  value={newProjectCode}
                  onChange={(e) => setNewProjectCode(e.target.value.toUpperCase().replace(/[^A-Z0-9]/g, ""))}
                  placeholder="예: MYAPP"
                  maxLength={10}
                  style={{
                    width: "100%", padding: "8px 12px", fontSize: "14px",
                    background: "var(--ct-input)", color: "var(--ct-text)",
                    border: "1px solid var(--ct-border)", borderRadius: "8px",
                    outline: "none", boxSizing: "border-box", fontWeight: 700,
                    letterSpacing: "1px",
                  }}
                  onFocus={(e) => (e.target.style.borderColor = "var(--ct-accent)")}
                  onBlur={(e) => (e.target.style.borderColor = "var(--ct-border)")}
                />
              </div>
              <div>
                <label style={{ fontSize: "11px", color: "var(--ct-text2)", display: "block", marginBottom: "4px" }}>
                  프로젝트 이름
                </label>
                <input
                  value={newProjectName}
                  onChange={(e) => setNewProjectName(e.target.value)}
                  placeholder="예: 내 프로젝트"
                  maxLength={50}
                  style={{
                    width: "100%", padding: "8px 12px", fontSize: "14px",
                    background: "var(--ct-input)", color: "var(--ct-text)",
                    border: "1px solid var(--ct-border)", borderRadius: "8px",
                    outline: "none", boxSizing: "border-box",
                  }}
                  onFocus={(e) => (e.target.style.borderColor = "var(--ct-accent)")}
                  onBlur={(e) => (e.target.style.borderColor = "var(--ct-border)")}
                  onKeyDown={(e) => { if (e.key === "Enter") addProject(); }}
                />
              </div>
              <div>
                <label style={{ fontSize: "11px", color: "var(--ct-text2)", display: "block", marginBottom: "4px" }}>
                  아이콘 (이모지)
                </label>
                <div style={{ display: "flex", gap: "6px", flexWrap: "wrap" }}>
                  {["📁", "💻", "🚀", "📊", "🎯", "🔧", "📱", "🌐", "🤖", "💰"].map((icon) => (
                    <button
                      key={icon}
                      onClick={() => setNewProjectIcon(icon)}
                      style={{
                        padding: "6px 10px", fontSize: "16px",
                        background: newProjectIcon === icon ? "var(--ct-accent)" : "var(--ct-hover)",
                        border: newProjectIcon === icon ? "2px solid var(--ct-accent)" : "1px solid var(--ct-border)",
                        borderRadius: "8px", cursor: "pointer",
                      }}
                    >
                      {icon}
                    </button>
                  ))}
                </div>
              </div>
              {newProjectCode && (
                <div style={{ fontSize: "12px", color: "var(--ct-text2)", padding: "4px 0" }}>
                  미리보기: <strong style={{ color: "var(--ct-text)" }}>[{newProjectCode}] {newProjectName || "..."}</strong>
                  <br />
                  세션명 예시: <strong style={{ color: "var(--ct-accent)" }}>{newProjectCode}-001</strong>, {newProjectCode}-002, ...
                </div>
              )}
              <div style={{ display: "flex", gap: "8px", marginTop: "4px" }}>
                <button
                  onClick={() => setShowAddProject(false)}
                  style={{
                    flex: 1, padding: "8px", fontSize: "13px",
                    background: "var(--ct-hover)", color: "var(--ct-text)",
                    border: "1px solid var(--ct-border)", borderRadius: "8px",
                    cursor: "pointer",
                  }}
                >
                  취소
                </button>
                <button
                  onClick={addProject}
                  disabled={!newProjectCode || !newProjectName.trim()}
                  style={{
                    flex: 1, padding: "8px", fontSize: "13px", fontWeight: 600,
                    background: newProjectCode && newProjectName.trim() ? "var(--ct-accent)" : "var(--ct-hover)",
                    color: "#fff", border: "none", borderRadius: "8px",
                    cursor: newProjectCode && newProjectName.trim() ? "pointer" : "not-allowed",
                    opacity: newProjectCode && newProjectName.trim() ? 1 : 0.5,
                  }}
                >
                  추가
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* ── Hidden file input ── */}
      <input
        ref={fileInputRef}
        type="file"
        multiple
        accept="*/*"
        style={{ display: "none" }}
        onChange={(e) => { handleFiles(e.target.files); e.target.value = ""; }}
      />

      {/* ── Mobile/Tablet overlay backdrop ── */}
      {mobileOverlay && screenSize !== "desktop" && (
        <div
          style={{
            position: "fixed",
            inset: 0,
            background: "rgba(0,0,0,0.5)",
            zIndex: 100,
          }}
          onClick={() => setMobileOverlay(null)}
        />
      )}

      {/* LEFT SIDEBAR */}
      <ChatSidebar
        screenSize={screenSize} leftOpen={leftOpen} setLeftOpen={setLeftOpen}
        mobileOverlay={mobileOverlay} setMobileOverlay={setMobileOverlay}
        activeWsObj={activeWsObj} activeWsName={activeWsName}
        workspaces={workspaces} activeWs={activeWs} setActiveWs={setActiveWs}
        filteredSessions={filteredSessions}
        renaming={renaming} setRenaming={setRenaming} commitRename={commitRename}
        activeSession={activeSession} setActiveSession={setActiveSession}
        isInitialLoadRef={isInitialLoadRef}
        onSessionContextMenu={onSessionContextMenu}
        search={search} setSearch={setSearch}
        createSession={createSession} setShowAddProject={setShowAddProject}
        theme={theme} toggleTheme={toggleTheme}
        tagFilter={tagFilter} setTagFilter={setTagFilter} allTags={allTags}
      />


      {/* ════════════════════════════════════════════════════════════
          CENTER CHAT AREA
      ════════════════════════════════════════════════════════════ */}
      <div
        style={{ flex: 1, minWidth: "0", display: "flex", flexDirection: "column", overflow: "hidden" }}
        onDrop={onDrop}
        onDragOver={(e) => e.preventDefault()}
      >
        {/* Chat Header */}
        <div
          style={{
            padding: "10px 14px",
            borderBottom: "1px solid var(--ct-border)",
            background: "var(--ct-sb)",
            display: "flex",
            alignItems: "center",
            gap: "10px",
            flexShrink: 0,
          }}
        >
          {/* Mobile: hamburger for left sidebar */}
          {screenSize !== "desktop" && (
            <button
              onClick={() => setMobileOverlay("sidebar")}
              style={{
                background: "none",
                border: "none",
                cursor: "pointer",
                color: "var(--ct-text2)",
                fontSize: "18px",
                padding: "4px",
              }}
            >
              ☰
            </button>
          )}
          {/* Desktop: expand sidebar if collapsed */}
          {screenSize === "desktop" && !leftOpen && (
            <button
              onClick={() => setLeftOpen(true)}
              style={{
                background: "none",
                border: "none",
                cursor: "pointer",
                color: "var(--ct-text2)",
                fontSize: "16px",
                padding: "4px",
              }}
              title="사이드바 펼치기"
            >
              ▶
            </button>
          )}

          {/* Session title */}
          <div style={{ flex: 1, minWidth: 0 }}>
            <div
              style={{
                fontWeight: 600,
                fontSize: "14px",
                overflow: "hidden",
                textOverflow: "ellipsis",
                whiteSpace: "nowrap",
              }}
            >
              {activeSession?.title || "새 대화를 시작하세요"}
            </div>
            <div style={{ fontSize: "11px", color: "var(--ct-text2)", marginTop: "1px" }}>
              {activeSession
                ? `${activeSession.id.slice(0, 8)}... · ${activeSession.message_count ?? 0}개 메시지`
                : "세션 없음"}
            </div>
          </div>

          {/* Model selector */}
          <select
            value={model}
            onChange={(e) => {
              const newModel = e.target.value;
              setModel(newModel);
              if (activeSession) {
                chatApi(`/chat/sessions/${activeSession.id}`, {
                  method: "PUT",
                  body: JSON.stringify({ current_model: newModel }),
                }).catch(() => {});
              }
            }}
            style={{
              fontSize: "12px",
              padding: "5px 8px",
              background: "var(--ct-card)",
              color: "var(--ct-text)",
              border: "1px solid var(--ct-border)",
              borderRadius: "6px",
              cursor: "pointer",
              maxWidth: "200px",
              outline: "none",
            }}
          >
            {selectableModels.map((m) => (
              <option key={m.id} value={m.id} disabled={!m.isActive}>
                {m.name} ({["자동", "무료", "변동"].includes(m.cost) ? m.cost : `${m.cost}/M`})
              </option>
            ))}
          </select>

          {/* Export session */}
          {activeSession && (
            <button
              onClick={() => {
                const a = document.createElement("a");
                a.href = `${BASE_URL}/chat/sessions/${activeSession.id}/export?format=markdown`;
                a.download = "";
                document.body.appendChild(a);
                a.click();
                document.body.removeChild(a);
              }}
              title="대화 내보내기 (.md)"
              style={{
                padding: "5px 10px",
                fontSize: "12px",
                background: "var(--ct-hover)",
                border: "none",
                borderRadius: "6px",
                cursor: "pointer",
                color: "var(--ct-text2)",
                whiteSpace: "nowrap",
                flexShrink: 0,
              }}
            >
              ⬇ 내보내기
            </button>
          )}

          {/* Artifact toggle */}
          <button
            onClick={() =>
              screenSize !== "desktop"
                ? setMobileOverlay("artifact")
                : setArtifactMode((m) => (m === "full" ? "mini" : m === "mini" ? "hidden" : "full"))
            }
            title="아티팩트 패널 토글 (Ctrl+])"
            style={{
              padding: "5px 10px",
              fontSize: "12px",
              background: "var(--ct-hover)",
              border: "none",
              borderRadius: "6px",
              cursor: "pointer",
              color: "var(--ct-text2)",
              whiteSpace: "nowrap",
              flexShrink: 0,
            }}
          >
            📄{artifactMode === "hidden" && screenSize === "desktop" ? "▶" : "◀"}
          </button>
        </div>

        {/* Messages */}
        <div
          ref={messagesContainerRef}
          className="ct-messages-scroll"
          style={{
            flex: 1,
            overflowY: "auto",
            padding: screenSize === "mobile" ? "12px 8px" : "16px",
            display: "flex",
            flexDirection: "column",
            gap: "12px",
          }}
        >
          {hasMoreMessages && (
            <button
              onClick={loadOlderMessages}
              disabled={loadingOlderRef.current}
              style={{
                alignSelf: "center",
                padding: "6px 16px",
                fontSize: "13px",
                color: "var(--ct-text2)",
                background: "var(--ct-bg2)",
                border: "1px solid var(--ct-border)",
                borderRadius: "8px",
                cursor: "pointer",
                marginBottom: "8px",
                opacity: loadingOlderRef.current ? 0.6 : 1,
              }}
            >
              ▲ 이전 대화 불러오기
            </button>
          )}
          {/* P0-4: 세션 요약 카드 — 세션에 메시지가 있을 때 최초 진입 시 표시 */}
          {activeSession?.id && messages.length > 0 && (
            <SessionSummaryCard sessionId={activeSession.id} />
          )}

          {/* C-1: 로딩 스켈레톤 */}
          {messagesLoading && messages.length === 0 && (
            <div style={{ display: "flex", flexDirection: "column", gap: "16px", padding: "20px 0" }}>
              {[1, 2, 3].map((i) => (
                <div key={i} style={{ display: "flex", gap: "12px", alignItems: i % 2 === 0 ? "flex-end" : "flex-start", flexDirection: "column" }}>
                  <div style={{
                    width: i % 2 === 0 ? "60%" : "75%",
                    height: "60px",
                    borderRadius: "12px",
                    background: "var(--ct-bg2)",
                    animation: "pulse 1.5s ease-in-out infinite",
                    alignSelf: i % 2 === 0 ? "flex-end" : "flex-start",
                  }} />
                </div>
              ))}
              <style>{"@keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.4; } }"}</style>
            </div>
          )}
          {messages.length === 0 && !streaming && !messagesLoading && (
            <div
              style={{
                textAlign: "center",
                paddingTop: "60px",
                color: "var(--ct-text2)",
              }}
            >
              <div style={{ fontSize: "42px", marginBottom: "14px" }}>💬</div>
              <div style={{ fontSize: "18px", fontWeight: 700, marginBottom: "8px" }}>
                CEO Chat
              </div>
              <div style={{ fontSize: "13px", marginBottom: "20px" }}>
                메시지를 입력하거나 왼쪽에서 세션을 선택하세요.
              </div>
              <div
                style={{
                  display: "inline-flex",
                  flexDirection: "column",
                  gap: "6px",
                  textAlign: "left",
                  fontSize: "12px",
                  background: "var(--ct-card)",
                  border: "1px solid var(--ct-border)",
                  borderRadius: "12px",
                  padding: "14px 20px",
                }}
              >
                <span>⚡ 상태 확인 → Haiku (빠름·저비용)</span>
                <span>🔧 코드·수정 → Sonnet (균형)</span>
                <span>🧠 설계·분석 → Opus (고성능)</span>
              </div>
            </div>
          )}

          {/* 프로액티브 브리핑 카드 */}
          {briefing && (
            <div
              className="ct-msg-enter"
              style={{
                display: "flex",
                justifyContent: "flex-start",
                marginBottom: "8px",
              }}
            >
              <div style={{ maxWidth: "85%", width: "100%" }}>
                <div
                  style={{
                    padding: briefing.collapsed ? "10px 16px" : "14px 18px",
                    borderRadius: "18px",
                    borderBottomLeftRadius: "4px",
                    fontSize: "13px",
                    lineHeight: "1.7",
                    background: theme === "dark"
                      ? "linear-gradient(135deg, rgba(59,130,246,0.12), rgba(99,102,241,0.08))"
                      : "linear-gradient(135deg, rgba(59,130,246,0.08), rgba(99,102,241,0.05))",
                    color: "var(--ct-text)",
                    border: `1px solid ${theme === "dark" ? "rgba(99,102,241,0.3)" : "rgba(59,130,246,0.2)"}`,
                  }}
                >
                  <div
                    style={{
                      display: "flex",
                      justifyContent: "space-between",
                      alignItems: "center",
                      cursor: "pointer",
                    }}
                    onClick={() =>
                      setBriefing((prev) =>
                        prev ? { ...prev, collapsed: !prev.collapsed } : null
                      )
                    }
                  >
                    <span style={{ fontWeight: 600, fontSize: "13px" }}>
                      📋 프로액티브 브리핑
                    </span>
                    <span
                      style={{
                        fontSize: "11px",
                        color: "var(--ct-text2)",
                        marginLeft: "8px",
                        userSelect: "none",
                      }}
                    >
                      {briefing.collapsed ? "▶ 펼치기" : "▼ 접기"}
                    </span>
                  </div>
                  {!briefing.collapsed && (
                    <div style={{ marginTop: "8px" }}>
                      <MarkdownBlock text={briefing.message} />
                    </div>
                  )}
                </div>
                <div
                  style={{
                    fontSize: "11px",
                    color: "var(--ct-text2)",
                    marginTop: "4px",
                    marginLeft: "4px",
                  }}
                >
                  시스템 자동 브리핑 · {new Date().toLocaleString("ko-KR", {
                    timeZone: "Asia/Seoul",
                    month: "numeric",
                    day: "numeric",
                    hour: "2-digit",
                    minute: "2-digit",
                  })}
                </div>
              </div>
            </div>
          )}

          {loadError && (
            <div style={{ padding: "12px 16px", background: "rgba(239,68,68,0.1)", border: "1px solid rgba(239,68,68,0.3)", borderRadius: "8px", color: "#ef4444", margin: "8px 16px", display: "flex", justifyContent: "space-between", alignItems: "center" }}>
              <span>{loadError}</span>
              <button onClick={() => { setLoadError(null); window.location.reload(); }} style={{ background: "#ef4444", color: "white", border: "none", borderRadius: "4px", padding: "4px 12px", cursor: "pointer" }}>새로고침</button>
            </div>
          )}

          {/* 4번: 중복 user 메시지 압축 렌더링 — UI 레벨만, DB 수정 없음 */}
          {(() => {
            const sorted = [...messages]
              .filter(m => m.intent !== "ai_review_warning")
              .sort((a, b) => { const ta = new Date(a.created_at || 0).getTime(); const tb = new Date(b.created_at || 0).getTime(); if (ta !== tb) return ta - tb; if (a.role === "user" && b.role === "assistant") return -1; if (a.role === "assistant" && b.role === "user") return 1; return 0; });
            type DisplayItem = { msg: typeof sorted[0]; idx: number; hiddenMsgs?: typeof sorted };
            const display: DisplayItem[] = [];
            let i = 0;
            while (i < sorted.length) {
              const msg = sorted[i];
              // 4번: 연속 동일 user 메시지 그룹핑 (원본으로 시작하는 [시스템] 포함 메시지도 압축)
              if (msg.role === "user" && !msg.content?.includes("[시스템]")) {
                const baseContent = msg.content || "";
                let j = i + 1;
                while (j < sorted.length &&
                  sorted[j].role === "user" &&
                  (sorted[j].content === baseContent ||
                   (baseContent.length > 0 && sorted[j].content?.startsWith(baseContent)))) {
                  j++;
                }
                if (j > i + 1) {
                  display.push({ msg, idx: i, hiddenMsgs: sorted.slice(i + 1, j) });
                  i = j;
                  continue;
                }
              }
              display.push({ msg, idx: i });
              i++;
            }
            const lastAssistantId = display.slice().reverse().find(d => {
              const m = d.msg;
              const isSystemMsg = m.intent === "auto_reaction" || m.intent === "runner_response" || m.intent === "pipeline_c" || isRunnerMsg(m) || (m.role === "user" && m.content?.startsWith("[시스템]"));
              return !isSystemMsg && m.role === "assistant" && m.intent !== "streaming_placeholder";
            })?.msg.id;
            return display.map(({ msg, idx, hiddenMsgs }) => {
              const isExpanded = expandedDupeGroups.has(msg.id);
              // 시스템 메시지: 접이식 한 줄 표시
              // 시스템 메시지는 로그 탭으로 이동 — 채팅에서 숨김
              const isSystemMsg = msg.intent === "auto_reaction" || msg.intent === "runner_response" || msg.intent === "pipeline_c" || isRunnerMsg(msg) || (msg.role === "user" && msg.content?.startsWith("[시스템]"));
              if (isSystemMsg) return null;
              return (
                <React.Fragment key={msg.id || idx}>
                  <MessageItem
                    msg={msg}
                    idx={idx}
                    streaming={streaming}
                    editingMsgId={editingMsgId}
                    editText={editText}
                    setEditingMsgId={setEditingMsgId}
                    setEditText={setEditText}
                    handleDeleteMessage={handleDeleteMessage}
                    handleCopyToInput={handleCopyToInput}
                    handleEditResend={handleEditResend}
                    onRegenerate={handleRegenerate}
                    onReplyTo={setReplyToMessage}
                    onBranch={setBranchPoint}
                    allMessages={messages}
                    isActiveStreaming={
                      msg.intent === "streaming_placeholder" &&
                      streaming &&
                      streamingSessionRef.current === activeSession?.id
                    }
                    streamingContent={
                      msg.intent === "streaming_placeholder" && streaming ? streamBuf : undefined
                    }
                    streamToolStatus={
                      msg.intent === "streaming_placeholder" && streaming ? toolStatus : undefined
                    }
                    streamToolLogs={
                      msg.intent === "streaming_placeholder" && streaming ? toolLogs : undefined
                    }
                    onStopStreaming={
                      msg.intent === "streaming_placeholder" && streaming ? stopStreaming : undefined
                    }
                    onViewReport={msg.intent === "pipeline_runner" ? () => { setArtifactMode("full"); setArtifactTab("report"); } : undefined}
                    linkedArtifact={msg.artifact_id ? artifacts.find(a => a.id === msg.artifact_id) : undefined}
                    onViewArtifact={(artifactId) => {
                      const idx = filteredArtifacts.findIndex(a => a.id === artifactId);
                      if (idx >= 0) setSelectedArtifactIdx(idx);
                      setArtifactMode("full");
                      setArtifactTab("report");
                    }}
                    onOpenLightbox={(srcs, i) => { setLightboxSrcs(srcs); setLightboxIdx(i); }}
                    isLastAssistantMsg={msg.id === lastAssistantId}
                  />
                  {hiddenMsgs && hiddenMsgs.length > 0 && !isExpanded && (
                    <div style={{ display: "flex", justifyContent: "flex-end", marginTop: "-6px", marginBottom: "4px", paddingRight: "4px" }}>
                      <button
                        onClick={() => setExpandedDupeGroups(prev => { const next = new Set(prev); next.add(msg.id); return next; })}
                        style={{ fontSize: "11px", color: "var(--ct-text2)", background: "rgba(255,255,255,0.06)", border: "1px solid var(--ct-border)", borderRadius: "10px", padding: "2px 8px", cursor: "pointer" }}
                      >같은 메시지 +{hiddenMsgs.length}회 ▾</button>
                    </div>
                  )}
                  {hiddenMsgs && isExpanded && hiddenMsgs.map((hm, hi) => (
                    <MessageItem
                      key={hm.id || `hidden-${hi}`}
                      msg={hm}
                      idx={idx + hi + 1}
                      streaming={streaming}
                      editingMsgId={editingMsgId}
                      editText={editText}
                      setEditingMsgId={setEditingMsgId}
                      setEditText={setEditText}
                      handleDeleteMessage={handleDeleteMessage}
                      handleCopyToInput={handleCopyToInput}
                      handleEditResend={handleEditResend}
                      onRegenerate={handleRegenerate}
                      onReplyTo={setReplyToMessage}
                      onBranch={setBranchPoint}
                      allMessages={messages}
                      isActiveStreaming={false}
                      onViewReport={hm.intent === "pipeline_runner" ? () => { setArtifactMode("full"); setArtifactTab("report"); } : undefined}
                      linkedArtifact={hm.artifact_id ? artifacts.find(a => a.id === hm.artifact_id) : undefined}
                      onViewArtifact={(artifactId) => {
                        const idx = filteredArtifacts.findIndex(a => a.id === artifactId);
                        if (idx >= 0) setSelectedArtifactIdx(idx);
                        setArtifactMode("full");
                        setArtifactTab("report");
                      }}
                      onOpenLightbox={(srcs, i) => { setLightboxSrcs(srcs); setLightboxIdx(i); }}
                      isLastAssistantMsg={false}
                    />
                  ))}
                  {hiddenMsgs && hiddenMsgs.length > 0 && isExpanded && (
                    <div style={{ display: "flex", justifyContent: "flex-end", marginTop: "-6px", marginBottom: "4px", paddingRight: "4px" }}>
                      <button
                        onClick={() => setExpandedDupeGroups(prev => { const next = new Set(prev); next.delete(msg.id); return next; })}
                        style={{ fontSize: "11px", color: "var(--ct-text2)", background: "rgba(255,255,255,0.06)", border: "1px solid var(--ct-border)", borderRadius: "10px", padding: "2px 8px", cursor: "pointer" }}
                      >접기 ▴</button>
                    </div>
                  )}
                </React.Fragment>
              );
            });
          })()}

          {/* Invisible Recovery: 백그라운드 응답 — streaming=false일 때만 표시 (streaming=true면 스트리밍 버블이 대신 표시) */}
          {waitingBgResponse && !streaming && (
            <div style={{ display: "flex", flexDirection: "column", alignItems: "flex-start" }}>
              <div
                style={{
                  padding: "12px 16px",
                  borderRadius: "18px",
                  borderBottomLeftRadius: "4px",
                  fontSize: "14px",
                  lineHeight: "1.6",
                  maxWidth: "80%",
                  background: "var(--ct-ai)",
                  color: "var(--ct-text)",
                  border: "1px solid var(--ct-border)",
                }}
              >
                {bgPartialContent ? (
                  <>
                    <MarkdownBlock text={bgPartialContent} />
                    <span style={{
                      display: "inline-block", width: "2px", height: "14px",
                      background: "var(--ct-accent)", marginLeft: "2px",
                      animation: "ct-blink 1s step-end infinite",
                      verticalAlign: "text-bottom",
                    }} />
                  </>
                ) : (
                  <div style={{ display: "flex", gap: "4px", alignItems: "center", height: "20px" }}>
                    {[0, 1, 2].map((i) => (
                      <span
                        key={i}
                        style={{
                          width: "7px", height: "7px", borderRadius: "50%",
                          background: "var(--ct-accent)", display: "inline-block",
                          animation: "ct-bounce 1.2s infinite",
                          animationDelay: `${i * 0.2}s`,
                        }}
                      />
                    ))}
                  </div>
                )}
              </div>
              <button
                type="button"
                onClick={stopBackgroundStreaming}
                style={{
                  marginTop: "4px", marginLeft: "4px",
                  padding: "2px 8px",
                  fontSize: "11px", fontWeight: 500,
                  background: "transparent", color: "var(--ct-muted)",
                  border: "1px solid var(--ct-border)", borderRadius: "10px",
                  cursor: "pointer",
                  transition: "all 0.15s",
                }}
                onMouseEnter={(e) => { e.currentTarget.style.background = "#ef4444"; e.currentTarget.style.color = "#fff"; e.currentTarget.style.borderColor = "#ef4444"; }}
                onMouseLeave={(e) => { e.currentTarget.style.background = "transparent"; e.currentTarget.style.color = "var(--ct-muted)"; e.currentTarget.style.borderColor = "var(--ct-border)"; }}
              >■ 중지</button>
            </div>
          )}

          <div ref={messagesEndRef} />
        </div>

        {/* Input Area */}
        <div
          style={{
            padding: screenSize === "mobile" ? "8px 8px" : "12px 14px",
            paddingBottom: screenSize === "mobile" ? "calc(56px + env(safe-area-inset-bottom, 0px))" : "12px",
            borderTop: "1px solid var(--ct-border)",
            background: "var(--ct-sb)",
            flexShrink: 0,
          }}
        >
          {/* 메모리 & 맥락 뷰어 */}
          <MemoryContextBar sessionId={activeSession?.id ?? null} />

          {/* AADS-190: Yellow 경고 바 */}
          {yellowWarning && streaming && (
            <div style={{
              display: "flex", alignItems: "center", justifyContent: "space-between",
              padding: "8px 12px", marginBottom: "8px", borderRadius: "8px",
              background: "#f59e0b20", border: "1px solid #f59e0b60",
              fontSize: "13px", color: "#f59e0b",
            }}>
              <span>⚠️ {yellowWarning}</span>
              <div style={{ display: "flex", gap: "6px" }}>
                <button
                  onClick={() => setYellowWarning(null)}
                  style={{
                    padding: "4px 12px", fontSize: "12px", fontWeight: 600,
                    background: "#f59e0b", color: "#fff", border: "none", borderRadius: "6px",
                    cursor: "pointer",
                  }}
                >계속</button>
                <button
                  onClick={() => { setYellowWarning(null); abortCtrl.current?.abort(); }}
                  style={{
                    padding: "4px 12px", fontSize: "12px", fontWeight: 600,
                    background: "#ef4444", color: "#fff", border: "none", borderRadius: "6px",
                    cursor: "pointer",
                  }}
                >중단</button>
              </div>
            </div>
          )}

          {/* AADS-190: 도구 턴 연장 알림 */}
          {toolTurnInfo && streaming && (
            <div style={{
              padding: "6px 12px", marginBottom: "8px", borderRadius: "8px",
              background: "#6366f120", border: "1px solid #6366f160",
              fontSize: "12px", color: "#6366f1",
            }}>
              🔄 {toolTurnInfo}
            </div>
          )}

          {/* 업로드 진행 표시 */}
          {uploading && (
            <div style={{
              display: "flex", alignItems: "center", gap: "6px",
              marginBottom: "6px", padding: "4px 10px",
              fontSize: "12px", color: "var(--ct-accent)",
              background: "var(--ct-hover)", borderRadius: "8px",
            }}>
              ⏳ 파일 업로드 중...
            </div>
          )}

          {/* P2-2: 분기 모드 배너 */}
          {branchPoint && (
            <div style={{
              display: "flex", alignItems: "center", gap: "8px",
              marginBottom: "6px", padding: "6px 12px",
              borderLeft: "3px solid #22c55e", background: "rgba(34,197,94,0.08)",
              borderRadius: "0 8px 8px 0", fontSize: "13px", color: "#22c55e",
            }}>
              <span style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                🔀 분기 모드: &quot;{branchPoint.content.slice(0, 80)}{branchPoint.content.length > 80 ? "..." : ""}&quot; 시점에서 분기
              </span>
              <button
                onClick={() => setBranchPoint(null)}
                style={{
                  background: "none", border: "none", color: "#22c55e",
                  cursor: "pointer", fontSize: "16px", padding: "0 4px", lineHeight: 1,
                }}
                title="분기 취소"
              >✕</button>
            </div>
          )}

          {/* Reply-to 인용 미리보기 바 */}
          {replyToMessage && (
            <div style={{
              display: "flex", alignItems: "center", gap: "8px",
              marginBottom: "6px", padding: "6px 12px",
              borderLeft: "3px solid var(--ct-accent)", background: "var(--ct-hover)",
              borderRadius: "0 8px 8px 0", fontSize: "13px", color: "var(--ct-text2)",
            }}>
              <span style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                ↩ {replyToMessage.content.slice(0, 100)}{replyToMessage.content.length > 100 ? "..." : ""}
              </span>
              <button
                onClick={() => setReplyToMessage(null)}
                style={{
                  background: "none", border: "none", color: "var(--ct-text2)",
                  cursor: "pointer", fontSize: "16px", padding: "0 4px", lineHeight: 1,
                }}
                title="답글 취소"
              >✕</button>
            </div>
          )}


          {/* 첨부된 파일 목록 (이미지 썸네일 + 텍스트 파일 배지) */}
          {pendingPreviewFiles.length > 0 && (
            <div style={{
              display: "flex", flexWrap: "wrap", gap: "6px", marginBottom: "6px",
            }}>
              {pendingPreviewFiles.map((file, i) => {
                const isImg = file.type.startsWith("image/");
                return (
                  <div key={i} style={{ position: "relative", display: "inline-flex", alignItems: "center" }}>
                    {isImg && pendingPreviewUrls[i] ? (
                      <img
                        src={pendingPreviewUrls[i]}
                        alt={file.name}
                        style={{
                          width: "64px", height: "64px", objectFit: "cover",
                          borderRadius: "8px", border: "1px solid var(--ct-border)",
                        }}
                      />
                    ) : (
                      <span style={{
                        fontSize: "11px", padding: "4px 10px",
                        background: "var(--ct-hover)", borderRadius: "8px",
                        color: "var(--ct-text2)", border: "1px solid var(--ct-border)",
                        maxWidth: "140px", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
                      }}>
                        📄 {file.name}
                      </span>
                    )}
                    <button
                      onClick={() => removePendingFile(i)}
                      style={{
                        position: "absolute", top: "-4px", right: "-4px",
                        width: "16px", height: "16px", borderRadius: "50%",
                        background: "#ef4444", color: "#fff", border: "none",
                        cursor: "pointer", fontSize: "10px", lineHeight: "16px",
                        display: "flex", alignItems: "center", justifyContent: "center",
                        padding: 0,
                      }}
                    >✕</button>
                  </div>
                );
              })}
            </div>
          )}

          {/* Action chips — mobile: toggled grid / desktop: always visible row */}
          {(screenSize !== "mobile" || showMobileActions) && (
            <div
              className={screenSize === "mobile" ? "ct-action-grid" : undefined}
              style={screenSize !== "mobile" ? {
                display: "flex", gap: "6px", marginBottom: "8px", flexWrap: "wrap",
              } : undefined}
            >
              {[
                { icon: "🔍", label: "검색", prefix: "[검색]" },
                { icon: "🧪", label: "딥리서치", prefix: "[딥리서치]" },
                { icon: "📎", label: "파일", action: "file" as const },
                { icon: "🎨", label: "이미지생성", action: "imagegen" as const },
                { icon: "📹", label: "동영상", prefix: "[동영상]" },
                { icon: "🎤", label: "음성", prefix: "[음성]" },
                { icon: "📋", label: "템플릿", action: "template" as const },
              ].map((chip) => (
                <button
                  key={chip.label}
                  onClick={() => {
                    if ("action" in chip && chip.action === "file") {
                      fileInputRef.current?.click();
                      if (screenSize === "mobile") setShowMobileActions(false);
                      return;
                    }
                    if ("action" in chip && chip.action === "imagegen") {
                      setShowImageGen(true);
                      if (screenSize === "mobile") setShowMobileActions(false);
                      return;
                    }
                    if ("action" in chip && chip.action === "template") {
                      fetchTemplates();
                      setShowTemplates(true);
                      if (screenSize === "mobile") setShowMobileActions(false);
                      return;
                    }
                    if ("prefix" in chip) {
                      applyChip(chip.prefix);
                      if (screenSize === "mobile") setShowMobileActions(false);
                    }
                  }}
                  style={{
                    padding: screenSize === "mobile" ? "10px 14px" : "4px 10px",
                    fontSize: screenSize === "mobile" ? "14px" : "12px",
                    background: "var(--ct-hover)",
                    border: "1px solid var(--ct-border)",
                    borderRadius: screenSize === "mobile" ? "12px" : "16px",
                    cursor: "pointer",
                    color: "var(--ct-text)",
                    display: "flex",
                    alignItems: "center",
                    gap: "6px",
                    ...(screenSize === "mobile" ? { justifyContent: "center" } : {}),
                  }}
                >
                  {chip.icon} {chip.label}
                </button>
              ))}
            </div>
          )}

          {/* 재지시 모드 배너 (방식B) */}
          {editMode && (
            <div style={{
              display: "flex", alignItems: "center", justifyContent: "space-between",
              marginBottom: "8px", padding: "6px 12px", borderRadius: "8px",
              background: "rgba(109,40,217,0.15)", border: "1px solid rgba(109,40,217,0.3)",
              fontSize: "12px", color: "var(--ct-accent)",
            }}>
              <span>🔄 이전 메시지를 수정하여 재전송합니다</span>
              <button onClick={() => { setEditMode(null); setInput(""); chatInputRef.current?.clear(); }}
                style={{ marginLeft: "8px", padding: "2px 8px", borderRadius: "6px",
                  background: "rgba(255,255,255,0.1)", border: "none", color: "var(--ct-text2)",
                  cursor: "pointer", fontSize: "11px" }}>
                취소
              </button>
            </div>
          )}
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "8px", gap: "8px" }}>
            <ChatOpsDock activeSessionId={activeSession?.id || null} screenSize={screenSize} />
          </div>
          {/* Textarea + send button — mobile: [+] [textarea [send]] */}
          <div style={{ display: "flex", gap: screenSize === "mobile" ? "6px" : "8px", alignItems: "flex-end" }}>
            {/* Mobile "+" toggle button */}
            {screenSize === "mobile" && (
              <button
                onClick={() => setShowMobileActions(!showMobileActions)}
                style={{
                  width: "44px", height: "44px", flexShrink: 0,
                  display: "flex", alignItems: "center", justifyContent: "center",
                  background: showMobileActions ? "var(--ct-accent)" : "var(--ct-hover)",
                  color: showMobileActions ? "#fff" : "var(--ct-text)",
                  border: "1px solid var(--ct-border)",
                  borderRadius: "50%", cursor: "pointer",
                  fontSize: "20px", fontWeight: 300,
                  transition: "all 0.2s",
                }}
              >
                {showMobileActions ? "✕" : "+"}
              </button>
            )}
            {/* Mobile: newline button */}
            {screenSize === "mobile" && (
              <button
                onClick={() => {
                  const ta = chatInputRef.current;
                  if (ta) {
                    const cur = ta.getValue();
                    ta.setValue(cur + "\n");
                    ta.focus();
                  }
                }}
                style={{
                  width: "36px", height: "44px", flexShrink: 0,
                  display: "flex", alignItems: "center", justifyContent: "center",
                  background: "none", border: "none",
                  color: "var(--ct-text2)", cursor: "pointer",
                  fontSize: "18px", padding: 0,
                }}
                title="줄바꿈"
              >
                ↵
              </button>
            )}
            {/* Textarea wrapper with integrated send */}
            <div style={{ flex: 1, position: "relative", display: "flex", alignItems: "flex-end" }}>
              <ChatInput
                ref={chatInputRef}
                screenSize={screenSize}
                onKeyDown={onKeyDown}
                onHasInput={setHasInput}
                onLocalMessage={(text) => {
                  const localMsg: ChatMessage = {
                    id: `local-${Date.now()}`,
                    session_id: activeSessionObjRef.current?.id || "",
                    role: "assistant",
                    content: text,
                    created_at: new Date().toISOString(),
                  };
                  setMessages((prev) => [...prev, localMsg]);
                }}
                placeholder={screenSize === "mobile" ? "메시지 입력... (↵으로 줄바꿈)" : undefined}
                onScreenShare={(file) => handleFiles([file])}
                onHiddenScreenCapture={handleHiddenScreenCapture}
                screenHiddenMode={screenHiddenMode}
              />
              {/* Mobile: send button inside textarea area */}
              {screenSize === "mobile" && (
                <button
                  onClick={streaming && !hasInput && pendingPreviewFiles.length === 0 ? stopStreaming : () => { if (!uploading) sendMessage(); }}
                  disabled={uploading || (!streaming && !hasInput && pendingPreviewFiles.length === 0)}
                  style={{
                    position: "absolute", right: "6px", bottom: "6px",
                    width: "36px", height: "36px",
                    display: "flex", alignItems: "center", justifyContent: "center",
                    background: uploading ? "#9ca3af" : streaming ? (hasInput || pendingPreviewFiles.length > 0 ? "var(--ct-accent)" : "#ef4444") : "var(--ct-accent)",
                    color: "#fff", border: "none", borderRadius: "50%",
                    cursor: uploading ? "wait" : (streaming || hasInput || pendingPreviewFiles.length > 0 ? "pointer" : "not-allowed"),
                    opacity: uploading || (!streaming && !hasInput && pendingPreviewFiles.length === 0) ? 0.4 : 1,
                    transition: "all 0.2s",
                    fontSize: "16px",
                  }}
                >
                  {uploading ? "⏳" : streaming ? (hasInput ? "📋" : "⏹") : "➤"}
                </button>
              )}
            </div>
            {/* Desktop: separate button group */}
            {screenSize !== "mobile" && (
            <div style={{ display: "flex", gap: "6px", flexShrink: 0, alignItems: "center" }}>
              {/* API 키 상태 표시 */}
              {/* 인증 키 토글 (클릭하여 Naver/Gmail 전환) */}
              <button
                onClick={async () => {
                  try {
                    const BASE = process.env.NEXT_PUBLIC_API_URL || "https://aads.newtalk.kr/api/v1";
                    const token = localStorage.getItem("aads_token");
                    const headers: Record<string, string> = { "Content-Type": "application/json" };
                    if (token) headers["Authorization"] = `Bearer ${token}`;
                    // 현재 순서 조회
                    const cur = await fetch(`${BASE}/settings/auth-keys`, { headers }).then(r => r.json());
                    const keys = cur?.keys || [];
                    const currentPrimary = keys?.[0];
                    const nextKey = keys?.find((k: AuthKeyStatus) => k?.key_name && k.key_name !== currentPrimary?.key_name) || keys?.[1];
                    if (!nextKey?.key_name) return;
                    // 순서 변경
                    await fetch(`${BASE}/settings/auth-keys`, {
                      method: "POST", headers, body: JSON.stringify({ primary: nextKey.key_name }),
                    });
                    await fetchKeyStatus();
                  } catch { /* ignore */ }
                }}
                title={`현재 1순위: ${apiKeyInfo?.label || "?"} (클릭하여 다음 계정으로 전환)`}
                style={{
                  fontSize: "10px", whiteSpace: "nowrap",
                  padding: "2px 8px", borderRadius: "8px",
                  background: apiKeyInfo?.slot === "1" ? "#3b82f618" : "#22c55e18",
                  color: apiKeyInfo?.slot === "1" ? "#3b82f6" : "#22c55e",
                  border: `1px solid ${apiKeyInfo?.slot === "1" ? "#3b82f640" : "#22c55e40"}`,
                  cursor: "pointer",
                  transition: "all 0.2s",
                }}
              >
                {apiKeyInfo?.slot === "1" ? "🔵" : "🟢"} {apiKeyInfo?.label || "?"}{apiKeyInfo?.slot ? ` (slot ${apiKeyInfo.slot})` : ""}{apiKeyInfo?.cliLabel && apiKeyInfo.cliLabel !== apiKeyInfo.label ? ` / CLI:${apiKeyInfo.cliLabel}` : ""}
              </button>
              <div style={{ position: "relative" }}>
                <button
                  onClick={() => setShowAuthPanel((prev) => !prev)}
                  title="Claude 계정 상태"
                  style={{
                    fontSize: "10px",
                    whiteSpace: "nowrap",
                    padding: "2px 8px",
                    borderRadius: "8px",
                    background: "var(--ct-hover)",
                    color: "var(--ct-text2)",
                    border: "1px solid var(--ct-border)",
                    cursor: "pointer",
                  }}
                >
                  Claude 상태
                </button>
                {showAuthPanel && (
                  <div style={{
                    position: "absolute",
                    right: 0,
                    bottom: "calc(100% + 8px)",
                    width: "360px",
                    maxWidth: "min(360px, 92vw)",
                    background: "var(--ct-panel)",
                    border: "1px solid var(--ct-border)",
                    borderRadius: "14px",
                    boxShadow: "0 20px 40px rgba(0,0,0,0.28)",
                    padding: "12px",
                    zIndex: 30,
                  }}>
                    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "10px" }}>
                      <div>
                        <div style={{ fontSize: "12px", fontWeight: 700, color: "var(--ct-text)" }}>Claude 계정 상태판</div>
                        <div style={{ fontSize: "11px", color: "var(--ct-text2)", marginTop: "2px" }}>
                          Relay {apiKeyInfo?.relayStatus || "unknown"} / 현재 slot {apiKeyInfo?.slot || "?"} / token {apiKeyInfo?.relayTokenAvailable ? "ready" : "missing"}
                        </div>
                      </div>
                      <button
                        onClick={() => void fetchKeyStatus()}
                        style={{
                          fontSize: "11px",
                          padding: "4px 8px",
                          borderRadius: "8px",
                          background: "var(--ct-hover)",
                          color: "var(--ct-text2)",
                          border: "1px solid var(--ct-border)",
                          cursor: "pointer",
                        }}
                      >
                        새로고침
                      </button>
                    </div>
                    <div style={{ display: "flex", flexDirection: "column", gap: "8px" }}>
                      {(apiKeyInfo?.keys || []).map((key) => (
                        <div
                          key={`${key.key_name || key.label || "unknown"}-${key.slot || "?"}`}
                          style={{
                            border: `1px solid ${key.is_current ? "#22c55e55" : "var(--ct-border)"}`,
                            background: key.is_current ? "#22c55e12" : "var(--ct-bg2)",
                            borderRadius: "12px",
                            padding: "10px",
                          }}
                        >
                          <div style={{ display: "flex", justifyContent: "space-between", gap: "8px", alignItems: "center" }}>
                            <div style={{ color: "var(--ct-text)", fontSize: "12px", fontWeight: 700 }}>
                              {key.label || key.key_name || "Unknown"}
                            </div>
                            <div style={{ color: key.is_rate_limited ? "#f59e0b" : "#22c55e", fontSize: "11px", fontWeight: 600 }}>
                              {key.is_current ? "ACTIVE" : `P${key.priority || "?"}`} {key.slot ? `/ slot ${key.slot}` : ""}
                            </div>
                          </div>
                          <div style={{ marginTop: "4px", color: "var(--ct-text2)", fontSize: "11px" }}>
                            {key.key_name || "-"}
                          </div>
                          <div style={{ marginTop: "6px", color: "var(--ct-text2)", fontSize: "11px", lineHeight: 1.5 }}>
                            {key.is_rate_limited
                              ? `Rate limit until ${key.rate_limited_until ? new Date(key.rate_limited_until).toLocaleString("ko-KR") : "-"}` 
                              : "Rate limit 없음"}
                            <br />
                            Last verified: {key.last_verified_at ? new Date(key.last_verified_at).toLocaleString("ko-KR") : "-"}
                            <br />
                            Last used: {key.last_used_at ? new Date(key.last_used_at).toLocaleString("ko-KR") : "-"}
                            {key.notes ? (
                              <>
                                <br />
                                Notes: {key.notes}
                              </>
                            ) : null}
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>
                )}
              </div>
              {/* AADS-190: 세션 비용/턴 표시 */}
              {sessionCost && (
                <span style={{
                  fontSize: "11px", color: "var(--ct-text2)", whiteSpace: "nowrap",
                  padding: "2px 8px", background: "var(--ct-hover)", borderRadius: "8px",
                }}>
                  {sessionCost}{sessionTurns ? ` | ${sessionTurns}턴` : ""}
                </span>
              )}
              {/* 큐 상태 표시 + 취소 버튼 (큐에 메시지가 있을 때) */}
              {queueCount > 0 && (
                <div style={{
                  display: "flex", alignItems: "center", gap: "6px",
                  padding: "4px 10px", fontSize: "12px",
                  background: "#f59e0b20", border: "1px solid #f59e0b60",
                  borderRadius: "10px", color: "#f59e0b", whiteSpace: "nowrap",
                }}>
                  <span title={msgQueueRef.current.join(" | ")}>
                    📋 대기 {queueCount}건: {(msgQueueRef.current[0] || "").slice(0, 20)}{(msgQueueRef.current[0] || "").length > 20 ? "..." : ""}
                  </span>
                  <button
                    onClick={() => { msgQueueRef.current = []; setQueueCount(0); setYellowWarning(null); }}
                    style={{
                      padding: "2px 8px", fontSize: "11px", fontWeight: 600,
                      background: "#f59e0b", color: "#fff", border: "none", borderRadius: "6px",
                      cursor: "pointer",
                    }}
                    title="대기 메시지 전체 취소 (Ctrl+Z)"
                  >✕ 취소</button>
                </div>
              )}
              {/* 전송/대기추가/중단 버튼 */}
              <button
                onClick={streaming && !hasInput && pendingPreviewFiles.length === 0 ? stopStreaming : () => { if (!uploading) sendMessage(); }}
                disabled={uploading || (!streaming && !hasInput && pendingPreviewFiles.length === 0)}
                style={{
                  padding: "10px 20px", fontSize: "14px", fontWeight: 600,
                  background: uploading ? "#9ca3af" : streaming ? (hasInput || pendingPreviewFiles.length > 0 ? "var(--ct-accent)" : "#ef4444") : "var(--ct-accent)",
                  color: "#fff", border: "none", borderRadius: "12px",
                  cursor: uploading ? "wait" : (streaming || hasInput || pendingPreviewFiles.length > 0 ? "pointer" : "not-allowed"),
                  opacity: uploading || (!streaming && !hasInput && pendingPreviewFiles.length === 0) ? 0.5 : 1,
                  transition: "background 0.2s", whiteSpace: "nowrap",
                }}
              >
                {uploading ? "업로드중..." : streaming ? (hasInput ? "대기 전송" : "⏹ 중단") : "전송"}
              </button>
            </div>
            )}
          </div>
          {/* Mobile: queue count badge */}
          {screenSize === "mobile" && queueCount > 0 && (
            <div style={{
              display: "flex", alignItems: "center", justifyContent: "center", gap: "6px",
              padding: "4px 10px", marginTop: "6px", fontSize: "12px",
              background: "#f59e0b20", border: "1px solid #f59e0b60",
              borderRadius: "10px", color: "#f59e0b",
            }}>
              <span>📋 대기 {queueCount}건</span>
              <button
                onClick={() => { msgQueueRef.current = []; setQueueCount(0); }}
                style={{
                  padding: "2px 8px", fontSize: "11px", fontWeight: 600,
                  background: "#f59e0b", color: "#fff", border: "none", borderRadius: "6px",
                  cursor: "pointer",
                }}
              >취소</button>
            </div>
          )}
        </div>
      </div>

      {/* AADS-188D: Code 패널 (diff_preview 시에만 표시) */}
      {diffApproval.payload && (
        <CodePanel
          visible
          payload={diffApproval.payload}
          sessionId={activeSession?.id ?? null}
          theme={theme}
          countdown={diffApproval.countdown}
          onClose={diffApproval.close}
          onResult={(action, msg) => {
            if (msg) setMessages((prev) => [...prev, {
              id: `sys-${Date.now()}`,
              session_id: activeSession?.id ?? "",
              role: "assistant",
              content: `[코드 수정 ${action === "approve" ? "승인" : "거부"}] ${msg}`,
            }]);
          }}
        />
      )}

      {/* RIGHT ARTIFACT PANEL */}
      <ChatArtifactPanel
        screenSize={screenSize} showArtifactPanel={showArtifactPanel}
        artifactMode={artifactMode} setArtifactMode={setArtifactMode}
        mobileOverlay={mobileOverlay} setMobileOverlay={setMobileOverlay}
        artifacts={artifacts} artifactTab={artifactTab} setArtifactTab={setArtifactTab}
        artifactCounts={artifactCounts}
        systemMessages={systemMessages}
        unreadLogCount={unreadLogCount}
        filteredArtifacts={filteredArtifacts} activeArtifact={activeArtifact}
        selectedArtifactIdx={selectedArtifactIdx} setSelectedArtifactIdx={setSelectedArtifactIdx}
        activeSession={activeSession} copyArtifact={copyArtifact} toDirective={toDirective}
        sessionId={activeSession?.id ?? ""}
      />

      {/* P2-10: 프롬프트 템플릿 모달 */}
      {showTemplates && (
        <div style={{
          position: "fixed", inset: 0, zIndex: 9999,
          background: "rgba(0,0,0,0.5)", display: "flex", alignItems: "center", justifyContent: "center",
        }} onClick={() => setShowTemplates(false)}>
          <div onClick={(e) => e.stopPropagation()} style={{
            background: "var(--ct-bg)", border: "1px solid var(--ct-border)", borderRadius: "16px",
            width: "min(520px, 90vw)", maxHeight: "70vh", display: "flex", flexDirection: "column",
            boxShadow: "0 20px 60px rgba(0,0,0,0.3)",
          }}>
            {/* 헤더 */}
            <div style={{
              display: "flex", alignItems: "center", justifyContent: "space-between",
              padding: "16px 20px", borderBottom: "1px solid var(--ct-border)",
            }}>
              <span style={{ fontSize: "16px", fontWeight: 700, color: "var(--ct-text)" }}>📋 프롬프트 템플릿</span>
              <div style={{ display: "flex", gap: "8px" }}>
                <button onClick={() => setShowNewTemplate(!showNewTemplate)} style={{
                  padding: "4px 12px", fontSize: "12px", borderRadius: "8px",
                  background: "var(--ct-accent)", color: "#fff", border: "none", cursor: "pointer",
                }}>+ 저장</button>
                <button onClick={() => setShowTemplates(false)} style={{
                  padding: "4px 8px", background: "none", border: "none",
                  color: "var(--ct-text2)", cursor: "pointer", fontSize: "18px",
                }}>✕</button>
              </div>
            </div>
            {/* 새 템플릿 저장 폼 */}
            {showNewTemplate && (
              <div style={{
                padding: "12px 20px", borderBottom: "1px solid var(--ct-border)",
                display: "flex", gap: "8px", alignItems: "center", flexWrap: "wrap",
              }}>
                <input
                  value={newTplTitle} onChange={(e) => setNewTplTitle(e.target.value)}
                  placeholder="템플릿 제목"
                  style={{
                    flex: 1, minWidth: "120px", padding: "6px 10px", fontSize: "13px",
                    background: "var(--ct-hover)", border: "1px solid var(--ct-border)",
                    borderRadius: "8px", color: "var(--ct-text)", outline: "none",
                  }}
                />
                <select value={newTplCategory} onChange={(e) => setNewTplCategory(e.target.value)} style={{
                  padding: "6px 10px", fontSize: "13px",
                  background: "var(--ct-hover)", border: "1px solid var(--ct-border)",
                  borderRadius: "8px", color: "var(--ct-text)", outline: "none",
                }}>
                  {["운영", "개발", "분석", "일반"].map((c) => <option key={c} value={c}>{c}</option>)}
                </select>
                <button onClick={handleCreateTemplate} style={{
                  padding: "6px 14px", fontSize: "13px", borderRadius: "8px",
                  background: "var(--ct-accent)", color: "#fff", border: "none", cursor: "pointer",
                }}>저장</button>
              </div>
            )}
            {/* 카테고리 탭 */}
            <div style={{
              display: "flex", gap: "4px", padding: "12px 20px 0",
              borderBottom: "1px solid var(--ct-border)",
            }}>
              {["전체", "운영", "개발", "분석", "일반"].map((cat) => (
                <button key={cat} onClick={() => setTemplateTab(cat)} style={{
                  padding: "6px 14px", fontSize: "12px", fontWeight: templateTab === cat ? 700 : 400,
                  borderRadius: "8px 8px 0 0", cursor: "pointer",
                  background: templateTab === cat ? "var(--ct-hover)" : "transparent",
                  color: templateTab === cat ? "var(--ct-accent)" : "var(--ct-text2)",
                  border: templateTab === cat ? "1px solid var(--ct-border)" : "1px solid transparent",
                  borderBottom: templateTab === cat ? "1px solid var(--ct-bg)" : "1px solid transparent",
                  marginBottom: "-1px",
                }}>{cat}</button>
              ))}
            </div>
            {/* 템플릿 목록 */}
            <div style={{ flex: 1, overflowY: "auto", padding: "8px 12px" }}>
              {templates
                .filter((t) => templateTab === "전체" || t.category === templateTab)
                .map((tpl) => (
                  <div key={tpl.id} style={{
                    display: "flex", alignItems: "center", gap: "12px",
                    padding: "10px 12px", margin: "4px 0", borderRadius: "10px",
                    background: "var(--ct-hover)", cursor: "pointer",
                    transition: "background 0.15s",
                  }}
                    onMouseEnter={(e) => (e.currentTarget.style.background = "var(--ct-border)")}
                    onMouseLeave={(e) => (e.currentTarget.style.background = "var(--ct-hover)")}
                  >
                    <div style={{ flex: 1, minWidth: 0 }} onClick={() => handleUseTemplate(tpl)}>
                      <div style={{ fontSize: "13px", fontWeight: 600, color: "var(--ct-text)", marginBottom: "4px" }}>
                        {tpl.title}
                        <span style={{
                          marginLeft: "8px", fontSize: "10px", padding: "1px 6px",
                          borderRadius: "6px", background: "var(--ct-bg)", color: "var(--ct-text2)",
                        }}>{tpl.category}</span>
                      </div>
                      <div style={{
                        fontSize: "11px", color: "var(--ct-text2)",
                        whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis",
                      }}>{tpl.content}</div>
                    </div>
                    <span style={{ fontSize: "10px", color: "var(--ct-text2)", whiteSpace: "nowrap" }}>
                      {tpl.usage_count}회
                    </span>
                    <button onClick={() => handleDeleteTemplate(tpl.id)} style={{
                      padding: "2px 6px", background: "none", border: "none",
                      color: "var(--ct-text2)", cursor: "pointer", fontSize: "14px",
                      opacity: 0.5,
                    }} title="삭제">✕</button>
                  </div>
                ))}
              {templates.filter((t) => templateTab === "전체" || t.category === templateTab).length === 0 && (
                <div style={{ padding: "24px", textAlign: "center", color: "var(--ct-text2)", fontSize: "13px" }}>
                  템플릿이 없습니다
                </div>
              )}
            </div>
          </div>
        </div>
      )}

      {/* 키보드 단축키 도움말 모달 */}
      <ShortcutHelp open={showShortcutHelp} onClose={() => setShowShortcutHelp(false)} />

    </div>
  );
}
