"use client";

import { useCallback, useEffect, useState } from "react";

import Header from "@/components/Header";
import { api } from "@/lib/api";

type Tab = "overview" | "layers" | "intents" | "roadmap";

interface GovernanceSection {
  key: string;
  name: string;
  description: string;
  chars: number;
  est_tokens: number;
  source: string;
}

interface GovernanceLayer {
  id: number;
  name: string;
  description: string;
  section_count: number;
  est_tokens: number;
  implemented: boolean;
  source: string;
  sections?: GovernanceSection[];
}

interface GovernanceRole {
  workspace_id: string;
  name: string;
  icon: string;
  color: string;
}

interface IntentBucket {
  model: string;
  count: number;
  intents: string[];
}

interface IntentSummary {
  total_intents: number;
  model_distribution: Record<string, number>;
  by_model: IntentBucket[];
}

interface RoadmapItem {
  label: string;
  done: boolean;
}

interface RoadmapPhase {
  phase: string;
  title: string;
  status: string;
  items_done: number;
  items_total: number;
  items?: RoadmapItem[];
}

interface GovernanceData {
  layers: GovernanceLayer[];
  roles: GovernanceRole[];
  intent_summary: IntentSummary;
  memory_sections: string[];
  evolution_stats: Record<string, number>;
  roadmap: RoadmapPhase[];
}

interface GovernanceLayersResponse {
  layers: GovernanceLayer[];
  count: number;
}

function formatNumber(value: number | undefined): string {
  return new Intl.NumberFormat("ko-KR").format(value || 0);
}

function statusTone(status: string): { background: string; color: string; border: string } {
  if (status === "completed") {
    return {
      background: "rgba(34,197,94,0.12)",
      color: "var(--success)",
      border: "1px solid rgba(34,197,94,0.22)",
    };
  }
  if (status === "in_progress") {
    return {
      background: "rgba(59,130,246,0.12)",
      color: "var(--accent)",
      border: "1px solid rgba(59,130,246,0.22)",
    };
  }
  return {
    background: "rgba(148,163,184,0.12)",
    color: "var(--text-secondary)",
    border: "1px solid rgba(148,163,184,0.2)",
  };
}

export default function GovernancePage() {
  const [tab, setTab] = useState<Tab>("overview");
  const [data, setData] = useState<GovernanceData | null>(null);
  const [layerDetails, setLayerDetails] = useState<GovernanceLayer[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const loadData = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [governanceRes, layerRes] = await Promise.all([
        api.getGovernance(),
        api.getGovernanceLayers(),
      ]);
      setData(governanceRes as GovernanceData);
      setLayerDetails((layerRes as GovernanceLayersResponse).layers || []);
    } catch (err) {
      console.error("governance load failed", err);
      setError(err instanceof Error ? err.message : "거버넌스 데이터를 불러오지 못했습니다.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadData();
  }, [loadData]);

  const tabs: { key: Tab; label: string; icon: string }[] = [
    { key: "overview", label: "개요", icon: "📊" },
    { key: "layers", label: "Layer 구조", icon: "🧱" },
    { key: "intents", label: "인텐트 정책", icon: "🧭" },
    { key: "roadmap", label: "로드맵", icon: "🗺️" },
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

  if (loading) {
    return (
      <div className="flex flex-col h-full" style={{ background: "var(--bg-primary)" }}>
        <Header title="세션 거버넌스" />
        <div className="flex-1 p-3 md:p-6 overflow-auto">
          <div style={{ ...cardStyle, color: "var(--text-secondary)", textAlign: "center" }}>로딩 중...</div>
        </div>
      </div>
    );
  }

  if (!data) {
    return (
      <div className="flex flex-col h-full" style={{ background: "var(--bg-primary)" }}>
        <Header title="세션 거버넌스" />
        <div className="flex-1 p-3 md:p-6 overflow-auto">
          <div style={{ ...cardStyle, color: "var(--danger)" }}>
            {error || "거버넌스 데이터를 표시할 수 없습니다."}
          </div>
        </div>
      </div>
    );
  }

  const summaryCards = [
    { label: "Layer", value: data.layers.length, tone: "var(--accent)" },
    { label: "Role", value: data.roles.length, tone: "var(--success)" },
    { label: "Intent", value: data.intent_summary.total_intents, tone: "var(--warning)" },
    { label: "Memory Section", value: data.memory_sections.length, tone: "var(--text-primary)" },
    { label: "Observations", value: data.evolution_stats.observations, tone: "var(--accent)" },
    { label: "Session Notes", value: data.evolution_stats.session_notes, tone: "var(--success)" },
  ];

  return (
    <div className="flex flex-col h-full" style={{ background: "var(--bg-primary)" }}>
      <Header title="세션 거버넌스" />
      <div className="flex-1 p-3 md:p-6 overflow-auto">
        <div className="flex gap-2 mb-4 flex-wrap">
          {tabs.map((item) => (
            <button key={item.key} onClick={() => setTab(item.key)} style={btnStyle(tab === item.key)}>
              {item.icon} {item.label}
            </button>
          ))}
        </div>

        {error ? (
          <div style={{ ...cardStyle, color: "var(--danger)", marginBottom: "16px" }}>{error}</div>
        ) : null}

        {tab === "overview" && (
          <div className="grid gap-4">
            <div className="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-6 gap-3">
              {summaryCards.map((item) => (
                <div key={item.label} style={{ ...cardStyle, padding: "14px" }}>
                  <div style={{ color: "var(--text-secondary)", fontSize: "12px", marginBottom: "4px" }}>
                    {item.label}
                  </div>
                  <div style={{ color: item.tone, fontWeight: 700, fontSize: "26px" }}>
                    {formatNumber(item.value)}
                  </div>
                </div>
              ))}
            </div>

            <div style={cardStyle}>
              <h3 style={{ color: "var(--text-primary)", marginBottom: "12px", fontSize: "16px", fontWeight: 600 }}>
                현재 역할 워크스페이스
              </h3>
              <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
                {data.roles.map((role) => (
                  <div key={role.workspace_id} style={{ ...cardStyle, padding: "12px" }}>
                    <div className="flex items-center justify-between gap-3">
                      <div style={{ color: "var(--text-primary)", fontWeight: 600 }}>
                        {role.icon} {role.name}
                      </div>
                      <span
                        style={{
                          width: "12px",
                          height: "12px",
                          borderRadius: "999px",
                          background: role.color || "var(--accent)",
                          display: "inline-block",
                        }}
                      />
                    </div>
                    <div style={{ color: "var(--text-secondary)", fontSize: "11px", marginTop: "6px" }}>
                      workspace_id: {role.workspace_id}
                    </div>
                  </div>
                ))}
              </div>
            </div>

            <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
              <div style={cardStyle}>
                <h3 style={{ color: "var(--text-primary)", marginBottom: "12px", fontSize: "16px", fontWeight: 600 }}>
                  메모리 섹션
                </h3>
                <div className="flex gap-2 flex-wrap">
                  {data.memory_sections.map((section) => (
                    <span
                      key={section}
                      style={{
                        padding: "6px 10px",
                        borderRadius: "999px",
                        background: "var(--bg-hover)",
                        color: "var(--text-primary)",
                        fontSize: "12px",
                        border: "1px solid var(--border)",
                      }}
                    >
                      {section}
                    </span>
                  ))}
                </div>
              </div>

              <div style={cardStyle}>
                <h3 style={{ color: "var(--text-primary)", marginBottom: "12px", fontSize: "16px", fontWeight: 600 }}>
                  진화 상태
                </h3>
                <div className="grid grid-cols-2 gap-3">
                  {Object.entries(data.evolution_stats).map(([key, value]) => (
                    <div key={key} style={{ ...cardStyle, padding: "12px" }}>
                      <div style={{ color: "var(--text-secondary)", fontSize: "11px", marginBottom: "4px" }}>
                        {key}
                      </div>
                      <div style={{ color: "var(--accent)", fontSize: "22px", fontWeight: 700 }}>
                        {formatNumber(value)}
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            </div>
          </div>
        )}

        {tab === "layers" && (
          <div className="grid gap-4">
            {layerDetails.map((layer) => (
              <div key={layer.id} style={cardStyle}>
                <div className="flex items-start justify-between gap-3 flex-wrap" style={{ marginBottom: "14px" }}>
                  <div>
                    <div style={{ color: "var(--text-secondary)", fontSize: "12px", marginBottom: "4px" }}>
                      L{layer.id}
                    </div>
                    <h3 style={{ color: "var(--text-primary)", fontSize: "18px", fontWeight: 700, marginBottom: "6px" }}>
                      {layer.name}
                    </h3>
                    <div style={{ color: "var(--text-secondary)", fontSize: "13px" }}>{layer.description}</div>
                  </div>
                  <div className="flex gap-2 flex-wrap">
                    <span
                      style={{
                        padding: "6px 10px",
                        borderRadius: "999px",
                        background: layer.implemented ? "rgba(34,197,94,0.12)" : "rgba(148,163,184,0.12)",
                        color: layer.implemented ? "var(--success)" : "var(--text-secondary)",
                        border: layer.implemented ? "1px solid rgba(34,197,94,0.22)" : "1px solid rgba(148,163,184,0.2)",
                        fontSize: "12px",
                        fontWeight: 600,
                      }}
                    >
                      {layer.implemented ? "implemented" : "planned"}
                    </span>
                    <span
                      style={{
                        padding: "6px 10px",
                        borderRadius: "999px",
                        background: "var(--bg-hover)",
                        color: "var(--text-primary)",
                        border: "1px solid var(--border)",
                        fontSize: "12px",
                      }}
                    >
                      {layer.section_count} sections
                    </span>
                    <span
                      style={{
                        padding: "6px 10px",
                        borderRadius: "999px",
                        background: "var(--bg-hover)",
                        color: "var(--accent)",
                        border: "1px solid var(--border)",
                        fontSize: "12px",
                        fontWeight: 600,
                      }}
                    >
                      ~{formatNumber(layer.est_tokens)} tok
                    </span>
                  </div>
                </div>

                <div style={{ color: "var(--text-secondary)", fontSize: "11px", marginBottom: "12px" }}>
                  source: {layer.source}
                </div>

                {layer.sections && layer.sections.length > 0 ? (
                  <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
                    {layer.sections.map((section) => (
                      <div key={section.key} style={{ ...cardStyle, padding: "12px" }}>
                        <div style={{ color: "var(--text-primary)", fontWeight: 600, marginBottom: "6px" }}>
                          {section.name}
                        </div>
                        <div style={{ color: "var(--text-secondary)", fontSize: "11px", minHeight: "32px" }}>
                          {section.description}
                        </div>
                        <div className="flex gap-3 flex-wrap" style={{ marginTop: "10px" }}>
                          <span style={{ color: "var(--accent)", fontSize: "12px", fontWeight: 600 }}>
                            ~{formatNumber(section.est_tokens)} tok
                          </span>
                          <span style={{ color: "var(--text-secondary)", fontSize: "12px" }}>
                            {formatNumber(section.chars)} chars
                          </span>
                        </div>
                        <div style={{ color: "var(--text-secondary)", fontSize: "10px", marginTop: "8px" }}>
                          {section.source}
                        </div>
                      </div>
                    ))}
                  </div>
                ) : (
                  <div style={{ color: "var(--text-secondary)", fontSize: "13px" }}>
                    아직 분리된 코드 자산이 없습니다.
                  </div>
                )}
              </div>
            ))}
          </div>
        )}

        {tab === "intents" && (
          <div className="grid gap-4">
            <div className="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-5 gap-3">
              {data.intent_summary.by_model.map((bucket) => (
                <div key={bucket.model} style={{ ...cardStyle, padding: "14px" }}>
                  <div style={{ color: "var(--text-secondary)", fontSize: "12px", marginBottom: "4px" }}>
                    {bucket.model}
                  </div>
                  <div style={{ color: "var(--accent)", fontWeight: 700, fontSize: "24px" }}>
                    {formatNumber(bucket.count)}
                  </div>
                  <div style={{ color: "var(--text-secondary)", fontSize: "11px" }}>assigned intents</div>
                </div>
              ))}
            </div>

            <div style={cardStyle}>
              <h3 style={{ color: "var(--text-primary)", marginBottom: "12px", fontSize: "16px", fontWeight: 600 }}>
                모델별 인텐트 분포
              </h3>
              <table style={{ width: "100%", borderCollapse: "collapse" }}>
                <thead>
                  <tr style={{ borderBottom: "1px solid var(--border)" }}>
                    {["모델", "인텐트 수", "인텐트 목록"].map((header) => (
                      <th
                        key={header}
                        style={{ padding: "10px 8px", textAlign: "left", color: "var(--text-secondary)", fontSize: "12px" }}
                      >
                        {header}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {data.intent_summary.by_model.map((bucket) => (
                    <tr key={bucket.model} style={{ borderBottom: "1px solid var(--border)" }}>
                      <td style={{ padding: "10px 8px", color: "var(--text-primary)", fontWeight: 600 }}>
                        {bucket.model}
                      </td>
                      <td style={{ padding: "10px 8px", color: "var(--accent)", fontWeight: 600 }}>
                        {formatNumber(bucket.count)}
                      </td>
                      <td style={{ padding: "10px 8px", color: "var(--text-secondary)", fontSize: "12px", lineHeight: 1.6 }}>
                        {bucket.intents.join(", ")}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {tab === "roadmap" && (
          <div className="grid gap-4">
            {data.roadmap.map((phase) => {
              const progress = phase.items_total > 0 ? (phase.items_done / phase.items_total) * 100 : 0;
              const tone = statusTone(phase.status);
              return (
                <div key={phase.phase} style={cardStyle}>
                  <div className="flex items-start justify-between gap-3 flex-wrap" style={{ marginBottom: "12px" }}>
                    <div>
                      <div style={{ color: "var(--text-secondary)", fontSize: "12px", marginBottom: "4px" }}>
                        {phase.phase}
                      </div>
                      <h3 style={{ color: "var(--text-primary)", fontSize: "18px", fontWeight: 700, marginBottom: "4px" }}>
                        {phase.title}
                      </h3>
                      <div style={{ color: "var(--text-secondary)", fontSize: "13px" }}>
                        {phase.items_done}/{phase.items_total} 완료
                      </div>
                    </div>
                    <span
                      style={{
                        ...tone,
                        padding: "6px 10px",
                        borderRadius: "999px",
                        fontSize: "12px",
                        fontWeight: 600,
                      }}
                    >
                      {phase.status}
                    </span>
                  </div>

                  <div
                    style={{
                      height: "10px",
                      borderRadius: "999px",
                      background: "var(--bg-hover)",
                      overflow: "hidden",
                      marginBottom: "14px",
                    }}
                  >
                    <div
                      style={{
                        width: `${progress}%`,
                        height: "100%",
                        background: "var(--accent)",
                        borderRadius: "999px",
                      }}
                    />
                  </div>

                  <div className="grid gap-2">
                    {(phase.items || []).map((item) => (
                      <div
                        key={item.label}
                        className="flex items-center justify-between gap-3"
                        style={{
                          ...cardStyle,
                          padding: "10px 12px",
                          background: item.done ? "rgba(34,197,94,0.06)" : "var(--bg-card)",
                        }}
                      >
                        <span style={{ color: "var(--text-primary)", fontSize: "13px" }}>{item.label}</span>
                        <span
                          style={{
                            color: item.done ? "var(--success)" : "var(--text-secondary)",
                            fontWeight: 600,
                            fontSize: "12px",
                          }}
                        >
                          {item.done ? "done" : "pending"}
                        </span>
                      </div>
                    ))}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
