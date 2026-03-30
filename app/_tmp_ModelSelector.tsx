"use client";

export interface ModelOption {
  id: string;
  name: string;
  provider: string;
  cost: string;
}

export const MODEL_OPTIONS: ModelOption[] = [
  // -- 자동 라우팅 --
  { id: "mixture",                   name: "자동 라우팅 (혼합)",         provider: "auto",     cost: "자동" },
  // -- Anthropic Claude --
  { id: "claude-opus-4-6",           name: "Claude Opus 4.6",           provider: "anthropic", cost: "$5/$25" },
  { id: "claude-sonnet-4-6",         name: "Claude Sonnet 4.6",         provider: "anthropic", cost: "$3/$15" },
  { id: "claude-haiku-4-5-20251001", name: "Claude Haiku 4.5",          provider: "anthropic", cost: "$0.80/$4" },
  { id: "claude-opus-4-5",           name: "Claude Opus 4.5",           provider: "anthropic", cost: "$5/$25" },
  { id: "claude-sonnet-4-5",         name: "Claude Sonnet 4.5",         provider: "anthropic", cost: "$3/$15" },
  { id: "claude-3-5-sonnet-20241022",name: "Claude 3.5 Sonnet",         provider: "anthropic", cost: "$3/$15" },
  { id: "claude-3-5-haiku-20241022", name: "Claude 3.5 Haiku",          provider: "anthropic", cost: "$0.80/$4" },
  { id: "claude-3-opus-20240229",    name: "Claude 3 Opus",             provider: "anthropic", cost: "$15/$75" },
  { id: "claude-3-sonnet-20240229",  name: "Claude 3 Sonnet",           provider: "anthropic", cost: "$3/$15" },
  { id: "claude-3-haiku-20240307",   name: "Claude 3 Haiku",            provider: "anthropic", cost: "$0.25/$1.25" },
  { id: "claude-2.1",                name: "Claude 2.1",                provider: "anthropic", cost: "$8/$24" },
  // -- OpenAI GPT --
  { id: "gpt-5",                     name: "GPT-5",                     provider: "openai",   cost: "$10/$30" },
  { id: "gpt-5-mini",                name: "GPT-5 mini",                provider: "openai",   cost: "$0.25/$2" },
  { id: "gpt-5.2-chat-latest",       name: "GPT-5.2 Chat",             provider: "openai",   cost: "$5/$15" },
  { id: "gpt-4o",                    name: "GPT-4o",                    provider: "openai",   cost: "$5/$15" },
  { id: "gpt-4o-mini",               name: "GPT-4o mini",              provider: "openai",   cost: "$0.15/$0.60" },
  { id: "gpt-4-turbo",               name: "GPT-4 Turbo",              provider: "openai",   cost: "$10/$30" },
  { id: "gpt-4",                     name: "GPT-4",                     provider: "openai",   cost: "$30/$60" },
  { id: "gpt-3.5-turbo",             name: "GPT-3.5 Turbo",            provider: "openai",   cost: "$0.50/$1.50" },
  { id: "o1",                        name: "o1",                        provider: "openai",   cost: "$15/$60" },
  { id: "o1-mini",                   name: "o1-mini",                   provider: "openai",   cost: "$3/$12" },
  { id: "o3-mini",                   name: "o3-mini",                   provider: "openai",   cost: "$1.10/$4.40" },
  // -- Google Gemini 3.1 --
  { id: "gemini-3.1-pro-preview",    name: "Gemini 3.1 Pro Preview",    provider: "google",   cost: "$2/$12" },
  { id: "gemini-3.1-flash-lite-preview", name: "Gemini 3.1 Flash-Lite Preview", provider: "google", cost: "$0.02/$0.10" },
  // -- Google Gemini 3.0 --
  { id: "gemini-3-pro-preview",      name: "Gemini 3.0 Pro Preview",    provider: "google",   cost: "$2/$12" },
  { id: "gemini-3-flash-preview",    name: "Gemini 3.0 Flash Preview",  provider: "google",   cost: "$0.15/$0.60" },
  // -- Google Gemini 2.5 --
  { id: "gemini-2.5-pro",            name: "Gemini 2.5 Pro",            provider: "google",   cost: "$7/$21" },
  { id: "gemini-2.5-flash",          name: "Gemini 2.5 Flash",          provider: "google",   cost: "$0.15/$0.60" },
  { id: "gemini-2.5-flash-lite",     name: "Gemini 2.5 Flash-Lite",     provider: "google",   cost: "$0.02/$0.10" },
  { id: "gemini-2.5-flash-image",    name: "Gemini 2.5 Flash Image",    provider: "google",   cost: "$0.15/$0.60" },
  // -- Google Gemma 3 (오픈소스) --
  { id: "gemma-3-27b-it",            name: "Gemma 3 27B",               provider: "google",   cost: "무료" },
  // -- Groq (초고속 무료) --
  { id: "groq-qwen3-32b",            name: "Qwen3 32B",                 provider: "groq",     cost: "무료" },
  { id: "groq-kimi-k2",              name: "Kimi K2",                   provider: "groq",     cost: "무료" },
  { id: "groq-llama4-maverick",      name: "Llama 4 Maverick",          provider: "groq",     cost: "무료" },
  { id: "groq-llama4-scout",         name: "Llama 4 Scout",             provider: "groq",     cost: "무료" },
  { id: "groq-llama-70b",            name: "Llama 3.3 70B",             provider: "groq",     cost: "무료" },
  { id: "groq-gpt-oss-120b",         name: "GPT-OSS 120B",              provider: "groq",     cost: "무료" },
  { id: "groq-compound",             name: "Groq Compound",             provider: "groq",     cost: "무료" },
  // -- DeepSeek --
  { id: "deepseek-chat",             name: "DeepSeek V3",               provider: "deepseek", cost: "$0.28/$0.42" },
  { id: "deepseek-reasoner",         name: "DeepSeek R1",               provider: "deepseek", cost: "$0.55/$2.19" },
];

export const DEFAULT_MODEL = "claude-opus-4-6";

// ─── Chat-First 모델 (AADS-172-B) ───────────────────────────────────────────

export interface ChatModelOption {
  id: string;
  label: string;
  cost: string;
  description: string;
  isDeepResearch?: boolean;
}

export const CHAT_MODEL_OPTIONS: ChatModelOption[] = [
  { id: "auto",                    label: "Auto",              cost: "자동",   description: "인텐트 기반 자동 라우팅" },
  { id: "claude-sonnet-4-6",       label: "Sonnet 4.6",        cost: "$0.03",  description: "균형잡힌 성능" },
  { id: "claude-opus-4-6",         label: "Opus 4.6",          cost: "$0.15",  description: "최고 성능" },
  { id: "gemini-3.1-pro-preview",  label: "Gemini 3.1 Pro",    cost: "$0.02",  description: "최신 Gemini" },
  { id: "gemini-2.5-flash",        label: "Flash 2.5",         cost: "$0.001", description: "빠름 · 저비용" },
  { id: "groq-qwen3-32b",          label: "Qwen3 32B",         cost: "무료",   description: "Groq 초고속 · 무료" },
  { id: "groq-kimi-k2",            label: "Kimi K2",           cost: "무료",   description: "수학/금융 최강 · 무료" },
  { id: "deepseek-chat",           label: "DeepSeek V3",       cost: "$0.001", description: "초저가 범용" },
  { id: "deep-research",           label: "Deep Research",     cost: "~$3",    description: "심층 연구 모드", isDeepResearch: true },
];

export const DEFAULT_CHAT_MODEL = "claude-opus-4-6";

interface ChatModelSelectorProps {
  value: string;
  onChange: (modelId: string) => void;
  compact?: boolean;
}

export function ChatModelSelector({ value, onChange, compact }: ChatModelSelectorProps) {
  const selected = CHAT_MODEL_OPTIONS.find((m) => m.id === value) ?? CHAT_MODEL_OPTIONS[0];

  return (
    <div className={`relative flex items-center ${compact ? "gap-1" : "gap-2"}`}>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="text-xs rounded-lg cursor-pointer"
        style={{
          background: "var(--bg-main)",
          color: "var(--text-primary)",
          border: "1px solid var(--border)",
          outline: "none",
          padding: compact ? "4px 6px" : "6px 10px",
          minWidth: compact ? "120px" : "160px",
        }}
        title={selected.description}
      >
        {CHAT_MODEL_OPTIONS.map((m) => (
          <option key={m.id} value={m.id}>
            {m.label} ({m.cost})
          </option>
        ))}
      </select>
      {!compact && (
        <span
          className="text-xs px-1.5 py-0.5 rounded"
          style={{
            background: selected.isDeepResearch ? "rgba(139,92,246,0.15)" : "var(--bg-hover)",
            color: selected.isDeepResearch ? "#a78bfa" : "var(--text-secondary)",
          }}
        >
          {selected.cost}
        </span>
      )}
    </div>
  );
}

const PROVIDER_LABELS: Record<string, string> = {
  auto: "자동",
  anthropic: "Anthropic",
  openai: "OpenAI",
  google: "Google",
  groq: "Groq (무료)",
  deepseek: "DeepSeek",
};

interface Props {
  value: string;
  onChange: (modelId: string) => void;
}

export default function ModelSelector({ value, onChange }: Props) {
  const selected = MODEL_OPTIONS.find((m) => m.id === value) ?? MODEL_OPTIONS[1];

  // Group by provider for optgroup
  const providers = ["auto", "anthropic", "openai", "google", "groq", "deepseek"];

  return (
    <div className="flex items-center gap-2 mb-2">
      <span className="text-xs whitespace-nowrap" style={{ color: "var(--text-secondary)" }}>모델:</span>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="text-xs rounded-md px-2 py-1.5 min-w-[220px] max-w-[320px] cursor-pointer"
        style={{
          background: "var(--bg-card)",
          color: "var(--text-primary)",
          border: "1px solid var(--border)",
          outline: "none",
        }}
      >
        {providers.map((prov) => {
          const group = MODEL_OPTIONS.filter((m) => m.provider === prov);
          if (group.length === 0) return null;
          return (
            <optgroup key={prov} label={PROVIDER_LABELS[prov] || prov}>
              {group.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.name} ({m.cost}/M)
                </option>
              ))}
            </optgroup>
          );
        })}
      </select>
      <span className="text-xs whitespace-nowrap" style={{ color: "var(--text-secondary)" }}>
        {selected.cost} /M
      </span>
    </div>
  );
}
