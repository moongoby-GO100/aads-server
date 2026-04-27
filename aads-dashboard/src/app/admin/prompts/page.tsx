"use client";

import { useCallback, useEffect, useState } from "react";
import Header from "@/components/Header";
import { api } from "@/lib/api";

type Tab = "dashboard" | "editor" | "preview" | "tokens" | "assets";
type AssetLayerFilter = "all" | 1 | 2 | 3 | 4 | 5;

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

interface PromptAsset {
  id: number;
  slug: string;
  title: string;
  layer_id: number;
  layer_name: string;
  content: string;
  chars: number;
  workspace_scope: string[];
  role_scope: string[];
  intent_scope: string[];
  target_models: string[];
  priority: number;
  enabled: boolean;
  updated_at: string | null;
}

interface PromptAssetFormState {
  slug: string;
  title: string;
  content: string;
  layer_id: number;
  priority: number;
  enabled: boolean;
  workspace_scope: string;
  role_scope: string;
  intent_scope: string;
  target_models: string;
}

const ALL_INTENTS = ["greeting", "casual", "search", "code_task", "strategy", "directive", "status_check", "pipeline_runner"];
const DEFAULT_SCOPE_JSON = JSON.stringify(["*"], null, 2);
const ASSET_LAYER_FILTERS: { key: AssetLayerFilter; icon: string; label: string; description: string }[] = [
  { key: "all", icon: "📚", label: "전체", description: "전체 5-Layer 에셋" },
  { key: 1, icon: "🌐", label: "L1 Global", description: "공통 기본 에셋" },
  { key: 2, icon: "📁", label: "L2 Project", description: "AADS / KIS / GO100 / SF / NTV2 / NAS" },
  { key: 3, icon: "🎭", label: "L3 Role", description: "CEO / CTO / PM / Dev / QA / Ops / KAKAOBOT" },
  { key: 4, icon: "🎯", label: "L4 Intent", description: "status / research / strategy / code" },
  { key: 5, icon: "🤖", label: "L5 Model", description: "Haiku / Sonnet / Opus" },
];

function createEmptyAssetForm(layerId: number): PromptAssetFormState {
  return {
    slug: "",
    title: "",
    content: "",
    layer_id: layerId,
    priority: 10,
    enabled: true,
    workspace_scope: DEFAULT_SCOPE_JSON,
    role_scope: DEFAULT_SCOPE_JSON,
    intent_scope: DEFAULT_SCOPE_JSON,
    target_models: DEFAULT_SCOPE_JSON,
  };
}

function assetToFormState(asset: PromptAsset): PromptAssetFormState {
  return {
    slug: asset.slug,
    title: asset.title,
    content: asset.content,
    layer_id: asset.layer_id,
    priority: asset.priority,
    enabled: asset.enabled,
    workspace_scope: JSON.stringify(asset.workspace_scope?.length ? asset.workspace_scope : ["*"], null, 2),
    role_scope: JSON.stringify(asset.role_scope?.length ? asset.role_scope : ["*"], null, 2),
    intent_scope: JSON.stringify(asset.intent_scope?.length ? asset.intent_scope : ["*"], null, 2),
    target_models: JSON.stringify(asset.target_models?.length ? asset.target_models : ["*"], null, 2),
  };
}

function formatAssetScope(values: string[] | null | undefined): string {
  if (!values || values.length === 0) return "*";
  return values.join(", ");
}

function formatAssetDate(value: string | null): string {
  if (!value) return "-";
  return value.replace("T", " ").replace(/\.\d+/, "").slice(0, 16);
}

function getErrorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function parseJsonStringArray(label: string, raw: string): string[] {
  try {
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) {
      throw new Error("문자열 배열이어야 합니다.");
    }
    const normalized = parsed.map((value) => {
      if (typeof value !== "string") {
        throw new Error("배열의 모든 값은 문자열이어야 합니다.");
      }
      return value.trim();
    }).filter(Boolean);
    return normalized.length > 0 ? normalized : ["*"];
  } catch (error) {
    throw new Error(`${label} JSON 오류: ${getErrorMessage(error)}`);
  }
}

export default function PromptsPage() {
  const [tab, setTab] = useState<Tab>("dashboard");
  const [sections, setSections] = useState<Record<string, Section>>({});
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [intentGroups, setIntentGroups] = useState<Record<string, IntentGroup>>({});
  const [liteIntents, setLiteIntents] = useState<string[]>([]);
  const [noToolsIntents, setNoToolsIntents] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);

  const [editWs, setEditWs] = useState<Workspace | null>(null);
  const [editContent, setEditContent] = useState("");
  const [saving, setSaving] = useState(false);

  const [previewWs, setPreviewWs] = useState("CEO");
  const [previewIntent, setPreviewIntent] = useState("directive");
  const [previewResult, setPreviewResult] = useState<any>(null);
  const [previewing, setPreviewing] = useState(false);

  const [tokenProfile, setTokenProfile] = useState<any>(null);

  const [assets, setAssets] = useState<PromptAsset[]>([]);
  const [assetLayerFilter, setAssetLayerFilter] = useState<AssetLayerFilter>("all");
  const [assetCount, setAssetCount] = useState(0);
  const [assetsLoading, setAssetsLoading] = useState(false);
  const [assetsLoaded, setAssetsLoaded] = useState(false);
  const [assetBusySlug, setAssetBusySlug] = useState<string | null>(null);
  const [assetEditorOpen, setAssetEditorOpen] = useState(false);
  const [assetEditorMode, setAssetEditorMode] = useState<"create" | "edit">("create");
  const [assetForm, setAssetForm] = useState<PromptAssetFormState>(() => createEmptyAssetForm(1));
  const [assetSubmitting, setAssetSubmitting] = useState(false);

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
    } catch (error) {
      console.error("Load failed:", error);
    } finally {
      setLoading(false);
    }
  }, []);

  const loadAssets = useCallback(async (layer: AssetLayerFilter, options?: { silent?: boolean }) => {
    if (!options?.silent) {
      setAssetsLoading(true);
    }
    try {
      const response = await api.getPromptAssets(layer === "all" ? undefined : layer);
      setAssets(response.assets || []);
      setAssetCount(response.count || 0);
      setAssetsLoaded(true);
    } catch (error) {
      console.error("Prompt asset load failed:", error);
      alert("에셋 로드 실패: " + getErrorMessage(error));
    } finally {
      if (!options?.silent) {
        setAssetsLoading(false);
      }
    }
  }, []);

  useEffect(() => {
    loadData();
  }, [loadData]);

  useEffect(() => {
    if (tab === "tokens" && !tokenProfile) {
      void loadTokenProfile();
    }
  }, [tab, tokenProfile]);

  useEffect(() => {
    if (tab === "assets" && !assetsLoaded) {
      void loadAssets(assetLayerFilter);
    }
  }, [tab, assetsLoaded, assetLayerFilter, loadAssets]);

  const handleSave = async () => {
    if (!editWs) return;
    setSaving(true);
    try {
      await api.updateWorkspacePrompt(editWs.id, editContent);
      await loadData();
      setEditWs(null);
    } catch (error) {
      alert("저장 실패: " + getErrorMessage(error));
    } finally {
      setSaving(false);
    }
  };

  const handlePreview = async () => {
    setPreviewing(true);
    try {
      const result = await api.previewPrompt(previewWs, previewIntent);
      setPreviewResult(result);
    } catch (error) {
      console.error(error);
    } finally {
      setPreviewing(false);
    }
  };

  const loadTokenProfile = async () => {
    try {
      const result = await api.getTokenProfile();
      setTokenProfile(result);
    } catch (error) {
      console.error(error);
    }
  };

  const resetAssetEditor = (layer: AssetLayerFilter = assetLayerFilter) => {
    const nextLayerId = layer === "all" ? 1 : layer;
    setAssetEditorOpen(false);
    setAssetEditorMode("create");
    setAssetForm(createEmptyAssetForm(nextLayerId));
  };

  const openAssetCreate = () => {
    const nextLayerId = assetLayerFilter === "all" ? 1 : assetLayerFilter;
    setAssetEditorMode("create");
    setAssetForm(createEmptyAssetForm(nextLayerId));
    setAssetEditorOpen(true);
  };

  const openAssetEdit = (asset: PromptAsset) => {
    setAssetEditorMode("edit");
    setAssetForm(assetToFormState(asset));
    setAssetEditorOpen(true);
  };

  const closeAssetEditor = () => {
    if (assetSubmitting) return;
    resetAssetEditor();
  };

  const updateAssetForm = <K extends keyof PromptAssetFormState>(field: K, value: PromptAssetFormState[K]) => {
    setAssetForm((current) => ({ ...current, [field]: value }));
  };

  const handleAssetFilterChange = async (layer: AssetLayerFilter) => {
    setAssetLayerFilter(layer);
    await loadAssets(layer);
  };

  const handleAssetToggle = async (asset: PromptAsset) => {
    setAssetBusySlug(asset.slug);
    try {
      await api.updatePromptAsset(asset.slug, { enabled: !asset.enabled });
      await loadAssets(assetLayerFilter, { silent: true });
    } catch (error) {
      alert("활성 상태 변경 실패: " + getErrorMessage(error));
    } finally {
      setAssetBusySlug(null);
    }
  };

  const handleAssetDelete = async (asset: PromptAsset) => {
    if (!window.confirm(`에셋 '${asset.slug}'를 삭제하시겠습니까?`)) return;
    setAssetBusySlug(asset.slug);
    try {
      await api.deletePromptAsset(asset.slug);
      await loadAssets(assetLayerFilter, { silent: true });
    } catch (error) {
      alert("에셋 삭제 실패: " + getErrorMessage(error));
    } finally {
      setAssetBusySlug(null);
    }
  };

  const handleAssetSubmit = async () => {
    const slug = assetForm.slug.trim();
    const title = assetForm.title.trim();

    if (!slug) {
      alert("slug를 입력하세요.");
      return;
    }
    if (!title) {
      alert("title을 입력하세요.");
      return;
    }
    if (!assetForm.content.trim()) {
      alert("body를 입력하세요.");
      return;
    }

    let workspaceScope: string[];
    let roleScope: string[];
    let intentScope: string[];
    let targetModels: string[];

    try {
      workspaceScope = parseJsonStringArray("workspace_scope", assetForm.workspace_scope);
      roleScope = parseJsonStringArray("role_scope", assetForm.role_scope);
      intentScope = parseJsonStringArray("intent_scope", assetForm.intent_scope);
      targetModels = parseJsonStringArray("target_models", assetForm.target_models);
    } catch (error) {
      alert(getErrorMessage(error));
      return;
    }

    setAssetSubmitting(true);
    try {
      if (assetEditorMode === "create") {
        await api.createPromptAsset({
          slug,
          title,
          content: assetForm.content,
          layer_id: assetForm.layer_id,
          priority: assetForm.priority,
          workspace_scope: workspaceScope,
          role_scope: roleScope,
          intent_scope: intentScope,
          target_models: targetModels,
        });
        if (!assetForm.enabled) {
          await api.updatePromptAsset(slug, { enabled: false });
        }
      } else {
        await api.updatePromptAsset(assetForm.slug, {
          title,
          content: assetForm.content,
          layer_id: assetForm.layer_id,
          priority: assetForm.priority,
          enabled: assetForm.enabled,
          workspace_scope: workspaceScope,
          role_scope: roleScope,
          intent_scope: intentScope,
          target_models: targetModels,
        });
      }
      await loadAssets(assetLayerFilter, { silent: true });
      resetAssetEditor(assetLayerFilter);
    } catch (error) {
      alert(`${assetEditorMode === "create" ? "에셋 생성" : "에셋 저장"} 실패: ${getErrorMessage(error)}`);
    } finally {
      setAssetSubmitting(false);
    }
  };

  const tabs: { key: Tab; label: string; icon: string }[] = [
    { key: "dashboard", label: "대시보드", icon: "📊" },
    { key: "editor", label: "편집기", icon: "✏️" },
    { key: "preview", label: "미리보기", icon: "🔍" },
    { key: "tokens", label: "토큰 프로파일", icon: "📈" },
    { key: "assets", label: "에셋(5-Layer)", icon: "🧱" },
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

  const inputStyle = {
    width: "100%",
    background: "var(--bg-primary)",
    color: "var(--text-primary)",
    border: "1px solid var(--border)",
    borderRadius: "6px",
    padding: "8px 12px",
  };

  const labelStyle = {
    color: "var(--text-secondary)",
    fontSize: "12px",
    display: "block",
    marginBottom: "6px",
  };

  const textAreaStyle = {
    ...inputStyle,
    fontFamily: "monospace",
    fontSize: "13px",
    resize: "vertical" as const,
    lineHeight: "1.5",
  };

  const activeAssetFilter = ASSET_LAYER_FILTERS.find((item) => item.key === assetLayerFilter);

  return (
    <div className="flex flex-col h-full" style={{ background: "var(--bg-primary)" }}>
      <Header title="시스템 프롬프트 관리" />
      <div className="flex-1 p-3 md:p-6 overflow-auto">
        <div className="flex gap-2 mb-4 flex-wrap">
          {tabs.map((item) => (
            <button key={item.key} onClick={() => setTab(item.key)} style={btnStyle(tab === item.key)}>
              {item.icon} {item.label}
            </button>
          ))}
        </div>

        {loading ? (
          <div style={{ color: "var(--text-secondary)", textAlign: "center", padding: "40px" }}>로딩 중...</div>
        ) : (
          <>
            {tab === "dashboard" && (
              <div className="grid gap-4">
                <div style={cardStyle}>
                  <h3 style={{ color: "var(--text-primary)", marginBottom: "12px", fontSize: "16px", fontWeight: 600 }}>
                    코드 섹션 (system_prompt_v2.py)
                  </h3>
                  <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-3">
                    {Object.entries(sections)
                      .filter(([name]) => !name.startsWith("ROLE_") && !name.startsWith("CAP_"))
                      .sort(([, left], [, right]) => right.est_tokens - left.est_tokens)
                      .map(([name, section]) => (
                        <div key={name} style={{ ...cardStyle, padding: "12px" }}>
                          <div style={{ color: "var(--text-secondary)", fontSize: "11px" }}>{name}</div>
                          <div style={{ color: "var(--accent)", fontSize: "20px", fontWeight: 700 }}>~{section.est_tokens}tok</div>
                          <div style={{ color: "var(--text-secondary)", fontSize: "11px" }}>{section.chars.toLocaleString()} chars</div>
                        </div>
                      ))}
                  </div>
                </div>

                <div style={cardStyle}>
                  <h3 style={{ color: "var(--text-primary)", marginBottom: "12px", fontSize: "16px", fontWeight: 600 }}>
                    워크스페이스별 역할 (WS_ROLES)
                  </h3>
                  <table style={{ width: "100%", borderCollapse: "collapse" }}>
                    <thead>
                      <tr style={{ borderBottom: "1px solid var(--border)" }}>
                        {["워크스페이스", "ROLE 토큰", "CAP 토큰", "DB 프롬프트", "합계"].map((header) => (
                          <th key={header} style={{ padding: "8px", textAlign: "left", color: "var(--text-secondary)", fontSize: "12px" }}>{header}</th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {workspaces.map((workspace) => {
                        const roleKey = `ROLE_${workspace.name.replace(/\[|\]/g, "").split(" ")[0].replace(/\(.*\)/, "")}`;
                        const capKey = `CAP_${workspace.name.replace(/\[|\]/g, "").split(" ")[0].replace(/\(.*\)/, "")}`;
                        const roleTokens = sections[roleKey]?.est_tokens || 0;
                        const capTokens = sections[capKey]?.est_tokens || 0;

                        return (
                          <tr key={workspace.id} style={{ borderBottom: "1px solid var(--border)" }}>
                            <td style={{ padding: "8px", color: "var(--text-primary)", fontSize: "13px" }}>
                              {workspace.icon} {workspace.name}
                            </td>
                            <td style={{ padding: "8px", color: "var(--accent)", fontSize: "13px" }}>~{roleTokens}</td>
                            <td style={{ padding: "8px", color: "var(--accent)", fontSize: "13px" }}>~{capTokens}</td>
                            <td style={{ padding: "8px", color: "var(--text-secondary)", fontSize: "13px" }}>
                              ~{workspace.est_tokens}tok ({workspace.chars}자)
                            </td>
                            <td style={{ padding: "8px", color: "var(--success)", fontSize: "13px", fontWeight: 600 }}>
                              ~{roleTokens + capTokens + workspace.est_tokens}
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>

                <div style={cardStyle}>
                  <h3 style={{ color: "var(--text-primary)", marginBottom: "12px", fontSize: "16px", fontWeight: 600 }}>
                    인텐트 그룹 (Phase 2 Adaptive Prompt)
                  </h3>
                  <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
                    {Object.entries(intentGroups).map(([name, group]) => (
                      <div key={name} style={{ ...cardStyle, padding: "12px" }}>
                        <div style={{ color: "var(--accent)", fontWeight: 600, marginBottom: "6px" }}>{name}</div>
                        <div style={{ color: "var(--text-secondary)", fontSize: "11px", marginBottom: "4px" }}>
                          Skip: {group.skip.length > 0 ? group.skip.join(", ") : "없음 (전체 포함)"}
                        </div>
                        <div style={{ color: "var(--text-secondary)", fontSize: "11px" }}>
                          인텐트 {group.intents.length}개: {group.intents.slice(0, 4).join(", ")}
                          {group.intents.length > 4 ? "..." : ""}
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

            {tab === "editor" && (
              <div className="grid gap-4">
                <div style={cardStyle}>
                  <h3 style={{ color: "var(--text-primary)", marginBottom: "12px", fontSize: "16px", fontWeight: 600 }}>
                    DB 워크스페이스 프롬프트 편집
                  </h3>
                  <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                    {workspaces.map((workspace) => (
                      <div
                        key={workspace.id}
                        onClick={() => {
                          setEditWs(workspace);
                          setEditContent(workspace.system_prompt);
                        }}
                        style={{
                          ...cardStyle,
                          padding: "12px",
                          cursor: "pointer",
                          borderColor: editWs?.id === workspace.id ? "var(--accent)" : "var(--border)",
                        }}
                      >
                        <div className="flex justify-between items-center">
                          <span style={{ color: "var(--text-primary)", fontWeight: 500 }}>
                            {workspace.icon} {workspace.name}
                          </span>
                          <span style={{ color: "var(--text-secondary)", fontSize: "11px" }}>~{workspace.est_tokens}tok</span>
                        </div>
                        <div style={{ color: "var(--text-secondary)", fontSize: "11px", marginTop: "4px" }}>
                          {workspace.system_prompt ? workspace.system_prompt.slice(0, 80) + "..." : "(비어있음)"}
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
                      onChange={(event) => setEditContent(event.target.value)}
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

            {tab === "preview" && (
              <div className="grid gap-4">
                <div style={cardStyle}>
                  <div className="flex gap-3 items-end flex-wrap">
                    <div>
                      <label style={{ color: "var(--text-secondary)", fontSize: "12px", display: "block", marginBottom: "4px" }}>워크스페이스</label>
                      <select
                        value={previewWs}
                        onChange={(event) => setPreviewWs(event.target.value)}
                        style={{ background: "var(--bg-primary)", color: "var(--text-primary)", border: "1px solid var(--border)", borderRadius: "6px", padding: "8px 12px" }}
                      >
                        {["CEO", "AADS", "KIS", "GO100", "SF", "NTV2", "NAS", "KAKAOBOT"].map((workspace) => (
                          <option key={workspace} value={workspace}>{workspace}</option>
                        ))}
                      </select>
                    </div>
                    <div>
                      <label style={{ color: "var(--text-secondary)", fontSize: "12px", display: "block", marginBottom: "4px" }}>인텐트</label>
                      <select
                        value={previewIntent}
                        onChange={(event) => setPreviewIntent(event.target.value)}
                        style={{ background: "var(--bg-primary)", color: "var(--text-primary)", border: "1px solid var(--border)", borderRadius: "6px", padding: "8px 12px" }}
                      >
                        {ALL_INTENTS.map((intent) => (
                          <option key={intent} value={intent}>{intent}</option>
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
                            {tokenProfile.workspaces && Object.entries(tokenProfile.workspaces).map(([workspace, intents]: [string, any]) => (
                              <tr key={workspace} style={{ borderBottom: "1px solid var(--border)" }}>
                                <td style={{ padding: "8px", color: "var(--text-primary)", fontSize: "13px", fontWeight: 500 }}>{workspace}</td>
                                {Object.entries(intents).map(([intent, value]: [string, any]) => {
                                  const tokens = value.est_tokens;
                                  const intensity = Math.min(1, tokens / 6000);
                                  const background = `rgba(59, 130, 246, ${0.1 + intensity * 0.5})`;

                                  return (
                                    <td key={intent} style={{ padding: "8px", textAlign: "center", fontSize: "12px", color: "var(--text-primary)", background }}>
                                      {tokens}
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
                          .filter(([name]: [string, any]) => !name.startsWith("ROLE_") && !name.startsWith("CAP_"))
                          .sort(([, left]: [string, any], [, right]: [string, any]) => right.est_tokens - left.est_tokens)
                          .map(([name, section]: [string, any]) => {
                            const total = Object.values(tokenProfile.sections as Record<string, Section>)
                              .filter((_, index) => !Object.keys(tokenProfile.sections)[index].startsWith("ROLE_") && !Object.keys(tokenProfile.sections)[index].startsWith("CAP_"))
                              .reduce((sum, value) => sum + value.est_tokens, 0);
                            const percent = ((section.est_tokens / total) * 100).toFixed(1);

                            return (
                              <div key={name} style={{ ...cardStyle, padding: "12px" }}>
                                <div style={{ color: "var(--text-secondary)", fontSize: "11px" }}>{name}</div>
                                <div className="flex items-end gap-2">
                                  <span style={{ color: "var(--accent)", fontSize: "18px", fontWeight: 700 }}>~{section.est_tokens}</span>
                                  <span style={{ color: "var(--text-secondary)", fontSize: "12px" }}>({percent}%)</span>
                                </div>
                                <div style={{ marginTop: "6px", height: "4px", background: "var(--bg-hover)", borderRadius: "2px" }}>
                                  <div style={{ height: "100%", width: `${percent}%`, background: "var(--accent)", borderRadius: "2px" }} />
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

            {tab === "assets" && (
              <div className="grid gap-4 lg:grid-cols-[280px_minmax(0,1fr)]">
                <div style={cardStyle}>
                  <div className="flex items-center justify-between mb-3">
                    <h3 style={{ color: "var(--text-primary)", fontSize: "16px", fontWeight: 600 }}>
                      Layer 필터
                    </h3>
                    <span style={{ color: "var(--text-secondary)", fontSize: "12px" }}>
                      {assetCount}건
                    </span>
                  </div>
                  <div className="grid gap-2">
                    {ASSET_LAYER_FILTERS.map((filter) => (
                      <button
                        key={String(filter.key)}
                        onClick={() => { void handleAssetFilterChange(filter.key); }}
                        style={{
                          ...cardStyle,
                          padding: "12px",
                          textAlign: "left",
                          cursor: "pointer",
                          borderColor: assetLayerFilter === filter.key ? "var(--accent)" : "var(--border)",
                          background: assetLayerFilter === filter.key ? "rgba(59, 130, 246, 0.12)" : "var(--bg-card)",
                        }}
                      >
                        <div style={{ color: "var(--text-primary)", fontWeight: 600, fontSize: "14px" }}>
                          {filter.icon} {filter.label}
                        </div>
                        <div style={{ color: "var(--text-secondary)", fontSize: "11px", marginTop: "4px", lineHeight: "1.4" }}>
                          {filter.description}
                        </div>
                      </button>
                    ))}
                  </div>
                </div>

                <div style={cardStyle}>
                  <div className="flex justify-between items-center mb-3 gap-3 flex-wrap">
                    <div>
                      <h3 style={{ color: "var(--text-primary)", fontSize: "16px", fontWeight: 600 }}>
                        에셋 목록
                      </h3>
                      <div style={{ color: "var(--text-secondary)", fontSize: "12px", marginTop: "4px" }}>
                        선택: {activeAssetFilter?.icon} {activeAssetFilter?.label} {assetsLoading ? "· 불러오는 중..." : ""}
                      </div>
                    </div>
                    <button onClick={openAssetCreate} style={btnStyle(true)}>
                      에셋 추가
                    </button>
                  </div>

                  {assetsLoading && !assetsLoaded ? (
                    <div style={{ color: "var(--text-secondary)", textAlign: "center", padding: "40px" }}>에셋 로딩 중...</div>
                  ) : assets.length === 0 ? (
                    <div style={{ color: "var(--text-secondary)", textAlign: "center", padding: "40px" }}>
                      선택한 레이어에 에셋이 없습니다.
                    </div>
                  ) : (
                    <div style={{ overflowX: "auto", opacity: assetsLoading ? 0.7 : 1, transition: "opacity 0.2s ease" }}>
                      <table style={{ width: "100%", minWidth: "1180px", borderCollapse: "collapse" }}>
                        <thead>
                          <tr style={{ borderBottom: "1px solid var(--border)" }}>
                            {["활성", "Layer", "Slug", "Title", "scope (workspace / role / intent / model)", "Priority", "길이(chars)", "수정일", "액션"].map((header) => (
                              <th
                                key={header}
                                style={{ padding: "10px 8px", textAlign: "left", color: "var(--text-secondary)", fontSize: "12px", verticalAlign: "top" }}
                              >
                                {header}
                              </th>
                            ))}
                          </tr>
                        </thead>
                        <tbody>
                          {assets.map((asset) => {
                            const busy = assetBusySlug === asset.slug;

                            return (
                              <tr key={asset.slug} style={{ borderBottom: "1px solid var(--border)" }}>
                                <td style={{ padding: "10px 8px", verticalAlign: "top" }}>
                                  <button
                                    onClick={() => { void handleAssetToggle(asset); }}
                                    disabled={busy}
                                    aria-label={`${asset.slug} 활성 토글`}
                                    style={{
                                      width: "44px",
                                      height: "24px",
                                      borderRadius: "999px",
                                      border: "none",
                                      padding: "2px",
                                      cursor: busy ? "not-allowed" : "pointer",
                                      background: asset.enabled ? "var(--accent)" : "var(--bg-hover)",
                                      opacity: busy ? 0.6 : 1,
                                      position: "relative",
                                    }}
                                  >
                                    <span
                                      style={{
                                        display: "block",
                                        width: "20px",
                                        height: "20px",
                                        borderRadius: "50%",
                                        background: "#fff",
                                        transform: asset.enabled ? "translateX(20px)" : "translateX(0)",
                                        transition: "transform 0.2s ease",
                                      }}
                                    />
                                  </button>
                                </td>
                                <td style={{ padding: "10px 8px", color: "var(--text-primary)", fontSize: "13px", verticalAlign: "top" }}>
                                  L{asset.layer_id} {asset.layer_name}
                                </td>
                                <td style={{ padding: "10px 8px", color: "var(--accent)", fontSize: "12px", fontFamily: "monospace", verticalAlign: "top" }}>
                                  {asset.slug}
                                </td>
                                <td style={{ padding: "10px 8px", color: "var(--text-primary)", fontSize: "13px", verticalAlign: "top" }}>
                                  {asset.title}
                                </td>
                                <td style={{ padding: "10px 8px", color: "var(--text-secondary)", fontSize: "11px", lineHeight: "1.5", verticalAlign: "top" }}>
                                  <div>WS: {formatAssetScope(asset.workspace_scope)}</div>
                                  <div>Role: {formatAssetScope(asset.role_scope)}</div>
                                  <div>Intent: {formatAssetScope(asset.intent_scope)}</div>
                                  <div>Model: {formatAssetScope(asset.target_models)}</div>
                                </td>
                                <td style={{ padding: "10px 8px", color: "var(--text-primary)", fontSize: "13px", verticalAlign: "top" }}>
                                  {asset.priority}
                                </td>
                                <td style={{ padding: "10px 8px", color: "var(--text-secondary)", fontSize: "13px", verticalAlign: "top" }}>
                                  {(asset.chars || asset.content.length).toLocaleString()}
                                </td>
                                <td style={{ padding: "10px 8px", color: "var(--text-secondary)", fontSize: "12px", verticalAlign: "top" }}>
                                  {formatAssetDate(asset.updated_at)}
                                </td>
                                <td style={{ padding: "10px 8px", verticalAlign: "top" }}>
                                  <div className="flex gap-2">
                                    <button
                                      onClick={() => openAssetEdit(asset)}
                                      disabled={busy}
                                      style={{ ...btnStyle(), padding: "6px 10px", opacity: busy ? 0.6 : 1 }}
                                    >
                                      편집
                                    </button>
                                    <button
                                      onClick={() => { void handleAssetDelete(asset); }}
                                      disabled={busy}
                                      style={{
                                        ...btnStyle(),
                                        padding: "6px 10px",
                                        background: "rgba(239, 68, 68, 0.14)",
                                        color: "var(--danger)",
                                        opacity: busy ? 0.6 : 1,
                                      }}
                                    >
                                      삭제
                                    </button>
                                  </div>
                                </td>
                              </tr>
                            );
                          })}
                        </tbody>
                      </table>
                    </div>
                  )}
                </div>
              </div>
            )}
          </>
        )}
      </div>

      {assetEditorOpen && (
        <div
          onClick={closeAssetEditor}
          style={{
            position: "fixed",
            inset: 0,
            background: "rgba(15, 23, 42, 0.72)",
            zIndex: 50,
            padding: "24px",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
          }}
        >
          <div
            onClick={(event) => event.stopPropagation()}
            style={{
              width: "100%",
              maxWidth: "980px",
              maxHeight: "calc(100vh - 48px)",
              overflow: "auto",
              background: "var(--bg-card)",
              border: "1px solid var(--border)",
              borderRadius: "10px",
              padding: "20px",
            }}
          >
            <div className="flex items-start justify-between gap-3 mb-4">
              <div>
                <h3 style={{ color: "var(--text-primary)", fontSize: "18px", fontWeight: 700 }}>
                  {assetEditorMode === "create" ? "에셋 추가" : "에셋 편집"}
                </h3>
                <div style={{ color: "var(--text-secondary)", fontSize: "12px", marginTop: "4px" }}>
                  body {assetForm.content.length.toLocaleString()}자 · JSON 배열 예시: ["*"]
                </div>
              </div>
              <div className="flex gap-2">
                <button onClick={closeAssetEditor} disabled={assetSubmitting} style={btnStyle()}>
                  취소
                </button>
                <button onClick={handleAssetSubmit} disabled={assetSubmitting} style={btnStyle(true)}>
                  {assetSubmitting ? "저장 중..." : "저장"}
                </button>
              </div>
            </div>

            <div className="grid gap-4 md:grid-cols-2 mb-4">
              <div>
                <label style={labelStyle}>slug</label>
                <input
                  value={assetForm.slug}
                  onChange={(event) => updateAssetForm("slug", event.target.value)}
                  readOnly={assetEditorMode === "edit"}
                  style={{
                    ...inputStyle,
                    fontFamily: "monospace",
                    opacity: assetEditorMode === "edit" ? 0.75 : 1,
                    cursor: assetEditorMode === "edit" ? "not-allowed" : "text",
                  }}
                />
              </div>
              <div>
                <label style={labelStyle}>title</label>
                <input
                  value={assetForm.title}
                  onChange={(event) => updateAssetForm("title", event.target.value)}
                  style={inputStyle}
                />
              </div>
              <div>
                <label style={labelStyle}>layer_id</label>
                <select
                  value={assetForm.layer_id}
                  onChange={(event) => updateAssetForm("layer_id", Number(event.target.value))}
                  style={inputStyle}
                >
                  <option value={1}>1 - Global</option>
                  <option value={2}>2 - Project</option>
                  <option value={3}>3 - Role</option>
                  <option value={4}>4 - Intent</option>
                  <option value={5}>5 - Model</option>
                </select>
              </div>
              <div className="grid gap-4 sm:grid-cols-[140px_minmax(0,1fr)] items-start">
                <div>
                  <label style={labelStyle}>priority</label>
                  <input
                    type="number"
                    value={assetForm.priority}
                    onChange={(event) => updateAssetForm("priority", Number(event.target.value))}
                    style={inputStyle}
                  />
                </div>
                <label className="flex items-center gap-2 mt-7" style={{ color: "var(--text-primary)", fontSize: "13px" }}>
                  <input
                    type="checkbox"
                    checked={assetForm.enabled}
                    onChange={(event) => updateAssetForm("enabled", event.target.checked)}
                  />
                  활성
                </label>
              </div>
            </div>

            <div style={{ marginBottom: "16px" }}>
              <label style={labelStyle}>body</label>
              <textarea
                value={assetForm.content}
                onChange={(event) => updateAssetForm("content", event.target.value)}
                rows={12}
                style={textAreaStyle}
              />
            </div>

            <div className="grid gap-4 md:grid-cols-2">
              <div>
                <label style={labelStyle}>workspace_scope (JSON)</label>
                <textarea
                  value={assetForm.workspace_scope}
                  onChange={(event) => updateAssetForm("workspace_scope", event.target.value)}
                  rows={5}
                  style={textAreaStyle}
                />
              </div>
              <div>
                <label style={labelStyle}>role_scope (JSON)</label>
                <textarea
                  value={assetForm.role_scope}
                  onChange={(event) => updateAssetForm("role_scope", event.target.value)}
                  rows={5}
                  style={textAreaStyle}
                />
              </div>
              <div>
                <label style={labelStyle}>intent_scope (JSON)</label>
                <textarea
                  value={assetForm.intent_scope}
                  onChange={(event) => updateAssetForm("intent_scope", event.target.value)}
                  rows={5}
                  style={textAreaStyle}
                />
              </div>
              <div>
                <label style={labelStyle}>target_models (JSON)</label>
                <textarea
                  value={assetForm.target_models}
                  onChange={(event) => updateAssetForm("target_models", event.target.value)}
                  rows={5}
                  style={textAreaStyle}
                />
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
