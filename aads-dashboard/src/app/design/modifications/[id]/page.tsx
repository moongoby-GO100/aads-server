"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";

import Header from "@/components/Header";
import {
  api,
  type DesignContextPackGenerateResponse,
  type DesignDecisionSummary,
  type DesignModificationRequestDetail,
  type DesignModificationRequestDetailResponse,
  type DesignVisualSnapshotSummary,
  type GeneratedDesignContextPack,
} from "@/lib/api";

type WorkbenchTab = "snapshots" | "context" | "decisions";
type ActionState = "preview" | "persist" | null;

interface SnapshotRenderItem {
  id: string;
  phase: string;
  viewport: string;
  image_url: string;
  dom_summary: unknown;
  captured_at: string | null;
}

interface DecisionRenderItem {
  id: string;
  subject: string;
  decision: string;
  rationale: string;
  applies_to: string;
  confidence: string;
}

const CONTEXT_SECTIONS = [
  "route",
  "component_paths",
  "current_state",
  "target_state",
  "locked_constraints",
  "acceptance_checks",
  "related_decisions",
  "risk_notes",
  "token_style_evidence",
];
const STATUS_UPDATE_TOOLTIP = "backend status update not yet wired";

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function asArray(value: unknown): unknown[] {
  if (Array.isArray(value)) return value;
  if (value == null || value === "") return [];
  return [value];
}

function stringifyValue(value: unknown): string {
  if (value == null || value === "") return "-";
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function firstText(source: Record<string, unknown> | undefined, keys: string[]): string {
  if (!source) return "";
  for (const key of keys) {
    const value = source[key];
    if (typeof value === "string" && value.trim()) return value.trim();
    if (Array.isArray(value) && value.length > 0) return value.map((item) => stringifyValue(item)).join(", ");
  }
  return "";
}

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

function normalizeSnapshot(value: unknown, index: number): SnapshotRenderItem | null {
  if (!isRecord(value)) return null;
  const id = String(value.id || value.snapshot_id || `snapshot-${index}`);
  return {
    id,
    phase: String(value.phase || "before").toLowerCase(),
    viewport: String(value.viewport || value.viewport_key || "unknown"),
    image_url: String(value.image_url || value.url || value.src || ""),
    dom_summary: value.dom_summary || value.summary || {},
    captured_at: typeof value.captured_at === "string" ? value.captured_at : null,
  };
}

function normalizeDecision(value: unknown, index: number): DecisionRenderItem | null {
  if (!isRecord(value)) return null;
  return {
    id: String(value.id || `decision-${index}`),
    subject: String(value.subject || value.title || "Untitled decision"),
    decision: String(value.decision || value.summary || ""),
    rationale: String(value.rationale || ""),
    applies_to: String(value.applies_to || "project"),
    confidence: value.confidence == null ? "-" : String(value.confidence),
  };
}

function getContextArray(pack: GeneratedDesignContextPack | null, key: string): unknown[] {
  const context = pack?.context;
  if (!isRecord(context)) return [];
  return asArray(context[key]);
}

function RiskNotes({ notes }: { notes: unknown[] }) {
  if (notes.length === 0) {
    return (
      <div className="text-xs" style={{ color: "var(--text-secondary)" }}>
        Context pack preview를 생성하면 risk notes가 여기에 표시됩니다.
      </div>
    );
  }
  return (
    <ul className="grid gap-2">
      {notes.map((note, index) => (
        <li key={`${index}-${stringifyValue(note).slice(0, 20)}`} className="text-xs leading-5" style={{ color: "var(--text-secondary)" }}>
          {stringifyValue(note)}
        </li>
      ))}
    </ul>
  );
}

function FieldRow({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="grid gap-1 py-3 sm:grid-cols-[150px_minmax(0,1fr)]" style={{ borderBottom: "1px solid var(--border)" }}>
      <div className="text-xs font-medium uppercase" style={{ color: "var(--text-secondary)" }}>
        {label}
      </div>
      <div className="min-w-0 text-sm leading-6 break-words" style={{ color: "var(--text-primary)" }}>
        {children}
      </div>
    </div>
  );
}

function ValueBlock({ value }: { value: unknown }) {
  const items = asArray(value);
  if (items.length === 0) return <span>-</span>;
  if (items.length === 1 && typeof items[0] !== "object") return <span>{stringifyValue(items[0])}</span>;
  return (
    <pre
      className="max-h-52 overflow-auto rounded-md p-3 text-xs leading-5"
      style={{ background: "var(--bg-primary)", border: "1px solid var(--border)", color: "var(--text-secondary)" }}
    >
      {stringifyValue(items.length === 1 ? items[0] : items)}
    </pre>
  );
}

function SnapshotPhasePanel({
  title,
  snapshots,
  riskNotes,
}: {
  title: string;
  snapshots: SnapshotRenderItem[];
  riskNotes: unknown[];
}) {
  return (
    <div className="min-w-0" style={{ border: "1px solid var(--border)", borderRadius: "8px", overflow: "hidden" }}>
      <div className="px-3 py-2 text-sm font-semibold" style={{ borderBottom: "1px solid var(--border)", color: "var(--text-primary)" }}>
        {title}
      </div>
      {snapshots.length === 0 ? (
        <div className="grid gap-3 p-3">
          <div className="rounded-md p-4 text-sm" style={{ background: "var(--bg-primary)", color: "var(--text-secondary)" }}>
            Snapshot metadata가 없습니다. 아래 risk notes를 확인한 뒤 context pack preview를 다시 생성하세요.
          </div>
          <RiskNotes notes={riskNotes} />
        </div>
      ) : (
        <div className="grid gap-0">
          {snapshots.map((snapshot) => (
            <div key={`${snapshot.id}-${snapshot.viewport}`} className="grid gap-3 p-3" style={{ borderBottom: "1px solid var(--border)" }}>
              <div className="flex flex-wrap items-center justify-between gap-2 text-xs" style={{ color: "var(--text-secondary)" }}>
                <span>{snapshot.viewport}</span>
                <span>{formatDateTime(snapshot.captured_at)}</span>
              </div>
              {snapshot.image_url ? (
                <div style={{ background: "var(--bg-primary)", border: "1px solid var(--border)", borderRadius: "8px", overflow: "hidden" }}>
                  <img
                    src={snapshot.image_url}
                    alt={`${title} ${snapshot.viewport}`}
                    className="h-auto w-full object-contain"
                    style={{ maxHeight: "360px" }}
                  />
                </div>
              ) : (
                <div className="rounded-md p-4 text-sm" style={{ background: "var(--bg-primary)", color: "var(--text-secondary)" }}>
                  이미지 URL이 없어 metadata placeholder만 표시합니다.
                </div>
              )}
              {isRecord(snapshot.dom_summary) && Object.keys(snapshot.dom_summary).length > 0 ? (
                <pre
                  className="max-h-36 overflow-auto rounded-md p-3 text-xs leading-5"
                  style={{ background: "var(--bg-primary)", border: "1px solid var(--border)", color: "var(--text-secondary)" }}
                >
                  {stringifyValue(snapshot.dom_summary)}
                </pre>
              ) : null}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function JsonSection({
  label,
  value,
  expanded,
  onToggle,
}: {
  label: string;
  value: unknown;
  expanded: boolean;
  onToggle: () => void;
}) {
  return (
    <div style={{ borderBottom: "1px solid var(--border)" }}>
      <button
        type="button"
        onClick={onToggle}
        className="flex w-full items-center justify-between gap-3 px-3 py-3 text-left text-sm font-medium"
        style={{ color: "var(--text-primary)" }}
      >
        <span>{label}</span>
        <span className="text-xs" style={{ color: "var(--text-secondary)" }}>
          {expanded ? "Collapse" : "Expand"}
        </span>
      </button>
      {expanded ? (
        <pre
          className="mx-3 mb-3 max-h-80 overflow-auto rounded-md p-3 text-xs leading-5"
          style={{ background: "var(--bg-primary)", border: "1px solid var(--border)", color: "var(--text-secondary)" }}
        >
          {stringifyValue(value)}
        </pre>
      ) : null}
    </div>
  );
}

function DisabledStatusButton({ children }: { children: ReactNode }) {
  return (
    <span title={STATUS_UPDATE_TOOLTIP}>
      <button
        type="button"
        disabled
        className="h-9 rounded-lg px-3 text-sm font-medium disabled:cursor-not-allowed disabled:opacity-55"
        style={{ background: "var(--bg-card)", border: "1px solid var(--border)", color: "var(--text-secondary)" }}
      >
        {children}
      </button>
    </span>
  );
}

function SummaryPanel({ request }: { request: DesignModificationRequestDetail }) {
  const card = isRecord(request.normalized_card) ? request.normalized_card : {};
  const route = request.screen?.route || firstText(card, ["route", "target", "screen_route"]) || "-";
  const problemType = firstText(card, ["problem_type", "type"]) || request.request_type || "other";
  const description = firstText(card, ["description", "problem", "issue"]) || request.user_prompt || "-";
  const goal = firstText(card, ["goal", "target_state", "desired_outcome"]) || "-";
  const viewportPriority =
    firstText(card, ["viewport_priority", "viewport", "viewports"]) ||
    (isRecord(request.screen?.metadata) ? firstText(request.screen?.metadata, ["viewport_priority", "viewport", "viewports"]) : "") ||
    "-";
  const tone = statusTone(request.status);

  return (
    <section className="min-w-0 rounded-lg" style={{ background: "var(--bg-card)", border: "1px solid var(--border)", overflow: "hidden" }}>
      <div className="flex flex-wrap items-start justify-between gap-3 px-4 py-3" style={{ borderBottom: "1px solid var(--border)" }}>
        <div className="min-w-0">
          <h2 className="text-base font-semibold" style={{ color: "var(--text-primary)" }}>
            Request Summary
          </h2>
          <div className="mt-1 truncate text-xs" style={{ color: "var(--text-secondary)" }}>
            {request.id}
          </div>
        </div>
        <span className="inline-flex rounded-md px-2 py-1 text-xs font-medium" style={tone}>
          {request.status || "draft"}
        </span>
      </div>
      <div className="px-4">
        <FieldRow label="Project">{request.project_key || "-"}</FieldRow>
        <FieldRow label="Route">{route}</FieldRow>
        <FieldRow label="Problem type">{problemType}</FieldRow>
        <FieldRow label="Description">{description}</FieldRow>
        <FieldRow label="Goal">{goal}</FieldRow>
        <FieldRow label="Allowed scope">
          <ValueBlock value={request.allowed_scope} />
        </FieldRow>
        <FieldRow label="Forbidden scope">
          <ValueBlock value={request.forbidden_scope} />
        </FieldRow>
        <FieldRow label="Acceptance">
          <ValueBlock value={request.acceptance_criteria} />
        </FieldRow>
        <FieldRow label="Viewport">{viewportPriority}</FieldRow>
        <FieldRow label="Updated">{formatDateTime(request.updated_at || request.created_at)}</FieldRow>
      </div>
    </section>
  );
}

export default function DesignModificationWorkbenchPage() {
  const params = useParams<{ id: string }>();
  const requestId = Array.isArray(params.id) ? params.id[0] : params.id;
  const [detail, setDetail] = useState<DesignModificationRequestDetailResponse | null>(null);
  const [contextPack, setContextPack] = useState<GeneratedDesignContextPack | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [activeTab, setActiveTab] = useState<WorkbenchTab>("snapshots");
  const [expanded, setExpanded] = useState<Record<string, boolean>>({
    route: true,
    component_paths: true,
    current_state: false,
    target_state: true,
    locked_constraints: false,
    acceptance_checks: true,
    related_decisions: false,
    risk_notes: true,
    token_style_evidence: false,
  });
  const [action, setAction] = useState<ActionState>(null);
  const [actionMessage, setActionMessage] = useState("");

  const loadDetail = useCallback(async () => {
    if (!requestId) return;
    setLoading(true);
    setError("");
    try {
      const response = await api.getDesignModificationRequest(requestId);
      setDetail(response);
    } catch (err) {
      console.error("design modification detail load failed", err);
      setError(err instanceof Error ? err.message : "디자인 수정 요청 상세를 불러오지 못했습니다.");
    } finally {
      setLoading(false);
    }
  }, [requestId]);

  useEffect(() => {
    loadDetail();
  }, [loadDetail]);

  const runContextPackAction = useCallback(async (mode: Exclude<ActionState, null>) => {
    if (!requestId) return;
    setAction(mode);
    setActionMessage("");
    try {
      const response: DesignContextPackGenerateResponse = mode === "preview"
        ? await api.previewDesignContextPack(requestId)
        : await api.persistDesignContextPack(requestId);
      setContextPack(response.context_pack);
      setActionMessage(mode === "preview" ? "Context pack preview generated." : "Context pack persisted.");
      setActiveTab("context");
    } catch (err) {
      console.error("context pack action failed", err);
      setActionMessage(err instanceof Error ? err.message : "Context pack 작업을 완료하지 못했습니다.");
    } finally {
      setAction(null);
    }
  }, [requestId]);

  const contextSnapshots = useMemo(() => {
    return getContextArray(contextPack, "snapshot_metadata")
      .map((item, index) => normalizeSnapshot(item, index))
      .filter((item): item is SnapshotRenderItem => item !== null);
  }, [contextPack]);

  const snapshots = useMemo(() => {
    const detailSnapshots = (detail?.snapshots || []).map((snapshot: DesignVisualSnapshotSummary, index) => normalizeSnapshot(snapshot, index)).filter((item): item is SnapshotRenderItem => item !== null);
    return detailSnapshots.length > 0 ? detailSnapshots : contextSnapshots;
  }, [contextSnapshots, detail?.snapshots]);

  const beforeSnapshots = snapshots.filter((snapshot) => snapshot.phase === "before");
  const afterSnapshots = snapshots.filter((snapshot) => snapshot.phase === "after");
  const riskNotes = getContextArray(contextPack, "risk_notes");

  const decisionItems = useMemo(() => {
    const detailDecisions = (detail?.decisions || []).map((decision: DesignDecisionSummary, index) => normalizeDecision(decision, index)).filter((item): item is DecisionRenderItem => item !== null);
    if (detailDecisions.length > 0) return detailDecisions;
    return getContextArray(contextPack, "related_decisions")
      .map((decision, index) => normalizeDecision(decision, index))
      .filter((item): item is DecisionRenderItem => item !== null);
  }, [contextPack, detail?.decisions]);

  const context: Record<string, unknown> = isRecord(contextPack?.context) ? contextPack.context : {};

  return (
    <div className="flex flex-col h-full" style={{ background: "var(--bg-primary)" }}>
      <Header title="Design Modification Workbench" />
      <main className="flex-1 overflow-auto p-3 md:p-6">
        <div className="mx-auto grid max-w-7xl gap-4">
          <section className="flex flex-col gap-3 lg:flex-row lg:items-end lg:justify-between">
            <div className="min-w-0">
              <Link href="/design/modifications" className="text-xs font-medium" style={{ color: "var(--accent)" }}>
                Back to Design Modifications
              </Link>
              <h1 className="mt-2 text-xl font-semibold" style={{ color: "var(--text-primary)" }}>
                Before / After Review
              </h1>
              <p className="mt-1 max-w-3xl text-sm" style={{ color: "var(--text-secondary)" }}>
                단일 디자인 수정 요청의 snapshot, context pack, 관련 결정을 검토합니다.
              </p>
            </div>
            <div className="flex flex-wrap gap-2">
              <button
                type="button"
                onClick={() => runContextPackAction("preview")}
                disabled={Boolean(action) || loading || !detail}
                className="h-9 rounded-lg px-3 text-sm font-medium disabled:opacity-60"
                style={{ background: "var(--accent)", border: "1px solid var(--accent)", color: "#fff" }}
              >
                {action === "preview" ? "Generating" : "Generate Context Pack Preview"}
              </button>
              <button
                type="button"
                onClick={() => runContextPackAction("persist")}
                disabled={Boolean(action) || loading || !detail}
                className="h-9 rounded-lg px-3 text-sm font-medium disabled:opacity-60"
                style={{ background: "var(--bg-card)", border: "1px solid var(--border)", color: "var(--text-primary)" }}
              >
                {action === "persist" ? "Persisting" : "Persist Context Pack"}
              </button>
              <DisabledStatusButton>Approve</DisabledStatusButton>
              <DisabledStatusButton>Reject</DisabledStatusButton>
            </div>
          </section>

          {actionMessage ? (
            <div className="rounded-lg px-3 py-2 text-sm" style={{ background: "var(--bg-card)", border: "1px solid var(--border)", color: "var(--text-secondary)" }}>
              {actionMessage}
            </div>
          ) : null}

          {loading ? (
            <section className="rounded-lg p-6 text-sm" style={{ background: "var(--bg-card)", border: "1px solid var(--border)", color: "var(--text-secondary)" }}>
              디자인 수정 요청 상세를 불러오는 중입니다.
            </section>
          ) : error ? (
            <section className="rounded-lg p-6" style={{ background: "var(--bg-card)", border: "1px solid var(--border)" }}>
              <div className="text-sm font-medium" style={{ color: "var(--danger)" }}>
                상세 로드 실패
              </div>
              <div className="mt-2 text-xs break-words" style={{ color: "var(--text-secondary)" }}>
                {error}
              </div>
            </section>
          ) : !detail ? (
            <section className="rounded-lg p-6 text-sm" style={{ background: "var(--bg-card)", border: "1px solid var(--border)", color: "var(--text-secondary)" }}>
              요청 데이터를 찾을 수 없습니다.
            </section>
          ) : (
            <div className="grid gap-4 xl:grid-cols-[minmax(0,420px)_minmax(0,1fr)]">
              <SummaryPanel request={detail.request} />

              <section className="min-w-0 rounded-lg" style={{ background: "var(--bg-card)", border: "1px solid var(--border)", overflow: "hidden" }}>
                <div className="flex overflow-x-auto" style={{ borderBottom: "1px solid var(--border)" }}>
                  {[
                    { key: "snapshots" as const, label: "Before / After" },
                    { key: "context" as const, label: "Context Pack JSON" },
                    { key: "decisions" as const, label: "Related Decisions" },
                  ].map((tab) => (
                    <button
                      key={tab.key}
                      type="button"
                      onClick={() => setActiveTab(tab.key)}
                      className="h-11 shrink-0 px-4 text-sm font-medium"
                      style={{
                        color: activeTab === tab.key ? "var(--accent)" : "var(--text-secondary)",
                        borderBottom: activeTab === tab.key ? "2px solid var(--accent)" : "2px solid transparent",
                      }}
                    >
                      {tab.label}
                    </button>
                  ))}
                </div>

                {activeTab === "snapshots" ? (
                  <div className="grid gap-3 p-3 lg:grid-cols-2">
                    <SnapshotPhasePanel title="Before" snapshots={beforeSnapshots} riskNotes={riskNotes} />
                    <SnapshotPhasePanel title="After" snapshots={afterSnapshots} riskNotes={riskNotes} />
                  </div>
                ) : null}

                {activeTab === "context" ? (
                  <div>
                    <div className="px-3 py-3 text-xs" style={{ color: "var(--text-secondary)", borderBottom: "1px solid var(--border)" }}>
                      {contextPack
                        ? `${contextPack.persisted ? "Persisted" : "Preview"} · sources ${contextPack.source_count} · missing ${contextPack.missing_context_count} · ${contextPack.prompt_chars} chars`
                        : "Context pack preview가 아직 생성되지 않았습니다."}
                    </div>
                    {CONTEXT_SECTIONS.map((section) => (
                      <JsonSection
                        key={section}
                        label={section}
                        value={section in context ? context[section] : "Not available"}
                        expanded={Boolean(expanded[section])}
                        onToggle={() => setExpanded((prev) => ({ ...prev, [section]: !prev[section] }))}
                      />
                    ))}
                    {contextPack ? (
                      <JsonSection
                        label="missing_context"
                        value={contextPack.missing_context}
                        expanded={Boolean(expanded.missing_context)}
                        onToggle={() => setExpanded((prev) => ({ ...prev, missing_context: !prev.missing_context }))}
                      />
                    ) : null}
                  </div>
                ) : null}

                {activeTab === "decisions" ? (
                  <div>
                    {decisionItems.length === 0 ? (
                      <div className="p-6 text-sm" style={{ color: "var(--text-secondary)" }}>
                        관련 디자인 결정이 없습니다.
                      </div>
                    ) : (
                      decisionItems.map((decision) => (
                        <div key={decision.id} className="grid gap-2 p-4" style={{ borderBottom: "1px solid var(--border)" }}>
                          <div className="flex flex-wrap items-center justify-between gap-2">
                            <div className="text-sm font-semibold" style={{ color: "var(--text-primary)" }}>
                              {decision.subject}
                            </div>
                            <div className="text-xs" style={{ color: "var(--text-secondary)" }}>
                              {decision.applies_to} · confidence {decision.confidence}
                            </div>
                          </div>
                          <div className="text-sm leading-6" style={{ color: "var(--text-primary)" }}>
                            {decision.decision || "-"}
                          </div>
                          {decision.rationale ? (
                            <div className="text-xs leading-5" style={{ color: "var(--text-secondary)" }}>
                              {decision.rationale}
                            </div>
                          ) : null}
                        </div>
                      ))
                    )}
                  </div>
                ) : null}
              </section>
            </div>
          )}
        </div>
      </main>
    </div>
  );
}
