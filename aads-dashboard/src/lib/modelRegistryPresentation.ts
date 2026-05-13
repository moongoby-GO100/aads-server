export type RegistryPresentationModel = {
  provider?: string | null;
  model_id: string;
  display_name?: string | null;
  family?: string | null;
  category?: string | null;
  execution_model_id?: string | null;
  is_active?: boolean;
  is_selectable?: boolean;
  is_executable?: boolean;
};

const PROVIDER_LABELS: Record<string, string> = {
  auto: "Auto",
  legacy: "Legacy",
  anthropic: "Claude",
  claude: "Claude",
  codex: "Codex",
  openai: "OpenAI",
  gemini: "Gemini",
  google: "Gemini",
  deepseek: "DeepSeek",
  qwen: "Qwen",
  groq: "Groq",
  kimi: "Kimi",
  minimax: "MiniMax",
  openrouter: "OpenRouter",
  litellm: "LiteLLM",
  local: "Local",
  ollama: "Local",
  pc_ollama: "Local",
  gemma: "Gemma",
};

const CATEGORY_LABELS: Record<string, string> = {
  text: "General",
  reasoning: "Reasoning",
  coding: "Coding",
  vision: "Vision",
  image: "Image",
  video: "Video",
  audio: "Audio",
};

const FAMILY_LABELS: Record<string, string> = {
  claude: "Claude",
  gpt: "GPT",
  "o-series": "O-Series",
  codex: "Codex",
  gemini: "Gemini",
  gemma: "Gemma",
  deepseek: "DeepSeek",
  qwen: "Qwen",
  groq: "Groq",
  kimi: "Kimi",
  minimax: "MiniMax",
  openrouter: "OpenRouter",
  local: "Local",
};

const ROUTING_PREFIXES = new Set([
  "anthropic",
  "auto",
  "claude",
  "codex",
  "deepseek",
  "gemini",
  "google",
  "groq",
  "kimi",
  "legacy",
  "litellm",
  "local",
  "minimax",
  "ollama",
  "openai",
  "openrouter",
  "pc_ollama",
  "qwen",
]);

function normalizeToken(value?: string | null): string {
  return String(value || "").trim().toLowerCase();
}

function titleize(value: string): string {
  return value
    .split(/[\s._-]+/)
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

function inferProviderKeyFromModelId(modelId: string): string {
  const normalized = normalizeToken(modelId);
  if (!normalized) return "legacy";
  if (normalized.startsWith("claude")) return "claude";
  if (normalized.startsWith("gpt-") || /^o\d/.test(normalized)) return "openai";
  if (normalized.startsWith("gemini")) return "gemini";
  if (normalized.startsWith("gemma")) return "gemma";
  if (normalized.startsWith("deepseek")) return "deepseek";
  if (normalized.startsWith("qwen") || normalized.startsWith("qwq")) return "qwen";
  if (normalized.startsWith("groq-")) return "groq";
  if (normalized.startsWith("kimi")) return "kimi";
  if (normalized.startsWith("minimax")) return "minimax";
  if (normalized.startsWith("openrouter-")) return "openrouter";
  if (normalized.startsWith("ollama") || normalized.includes("gemma4:")) return "local";
  return "litellm";
}

function inferCategoryKey(model: RegistryPresentationModel): string {
  const category = normalizeToken(model.category);
  if (category) return category;

  const modelId = normalizeToken(model.model_id);
  const providerKey = getModelProviderKey(model);
  if (
    modelId.includes("vision")
    || modelId.includes("image")
    || modelId.includes("omni")
    || modelId.includes("-vl")
    || modelId.includes("vl-")
  ) {
    return "vision";
  }
  if (providerKey === "claude" && modelId.startsWith("claude")) {
    return "coding";
  }
  if (providerKey === "codex" || modelId.includes("codex") || modelId.includes("coder")) {
    return "coding";
  }
  if (providerKey === "openai" && modelId.startsWith("gpt-5")) {
    return "coding";
  }
  if (modelId.includes("reasoner") || modelId.includes("thinking") || /^o\d/.test(modelId)) {
    return "reasoning";
  }
  return "text";
}

function inferFamilyKey(model: RegistryPresentationModel): string {
  const family = normalizeToken(model.family);
  if (family) return family;

  const modelId = normalizeToken(model.model_id);
  const providerKey = getModelProviderKey(model);
  if (modelId.startsWith("gpt-")) return "gpt";
  if (/^o\d/.test(modelId)) return "o-series";
  if (modelId.startsWith("claude")) return "claude";
  if (modelId.startsWith("gemini")) return "gemini";
  if (modelId.startsWith("gemma")) return "gemma";
  if (modelId.startsWith("deepseek")) return "deepseek";
  if (modelId.startsWith("qwen") || modelId.startsWith("qwq")) return "qwen";
  if (modelId.startsWith("groq-")) return "groq";
  if (modelId.startsWith("kimi")) return "kimi";
  if (modelId.startsWith("minimax")) return "minimax";
  if (modelId.startsWith("openrouter-")) return "openrouter";
  return providerKey || "legacy";
}

export function getModelProviderKey(model: RegistryPresentationModel): string {
  const provider = normalizeToken(model.provider);
  if (provider === "anthropic") return "claude";
  if (provider === "google") return "gemini";
  if (provider === "pc_ollama" || provider === "ollama" || provider === "local") return "local";
  if (provider === "litellm") return inferProviderKeyFromModelId(model.model_id);
  if (provider) return provider;
  return inferProviderKeyFromModelId(model.model_id);
}

export function getProviderDisplayLabel(model: RegistryPresentationModel): string {
  const providerKey = getModelProviderKey(model);
  return PROVIDER_LABELS[providerKey] || titleize(providerKey || "legacy");
}

export function getModelCategoryLabel(model: RegistryPresentationModel): string {
  const categoryKey = inferCategoryKey(model);
  return CATEGORY_LABELS[categoryKey] || titleize(categoryKey || "general");
}

export function getModelFamilyLabel(model: RegistryPresentationModel): string {
  const familyKey = inferFamilyKey(model);
  return FAMILY_LABELS[familyKey] || titleize(familyKey || "model");
}

export function getModelGroupLabel(model: RegistryPresentationModel): string {
  return `${getProviderDisplayLabel(model)} · ${getModelCategoryLabel(model)}`;
}

export function getCompactModelClassification(model: RegistryPresentationModel): string {
  const providerLabel = getProviderDisplayLabel(model);
  const familyLabel = getModelFamilyLabel(model);
  const categoryLabel = getModelCategoryLabel(model);
  const parts = [providerLabel];
  if (familyLabel && familyLabel !== providerLabel) parts.push(familyLabel);
  if (categoryLabel && !parts.includes(categoryLabel)) parts.push(categoryLabel);
  return parts.join(" / ");
}

export function splitStoredModelValue(value: string): { providerHint: string | null; modelId: string } {
  const trimmed = String(value || "").trim();
  const separatorIndex = trimmed.indexOf(":");
  if (separatorIndex <= 0) {
    return { providerHint: null, modelId: trimmed };
  }

  const prefix = normalizeToken(trimmed.slice(0, separatorIndex));
  if (!ROUTING_PREFIXES.has(prefix)) {
    return { providerHint: null, modelId: trimmed };
  }

  return {
    providerHint: prefix,
    modelId: trimmed.slice(separatorIndex + 1).trim(),
  };
}

export function findRegistryModelByStoredValue(
  models: RegistryPresentationModel[],
  value: string,
): RegistryPresentationModel | null {
  const { providerHint, modelId } = splitStoredModelValue(value);
  const normalizedModelId = normalizeToken(modelId);
  if (!normalizedModelId) return null;

  const inferredProviderKey = inferProviderKeyFromModelId(normalizedModelId);
  let bestMatch: { model: RegistryPresentationModel; score: number } | null = null;

  for (const model of models) {
    const registryModelId = normalizeToken(model.model_id);
    const executionModelId = normalizeToken(model.execution_model_id);
    if (!registryModelId) continue;

    let score = -1;
    if (providerHint && providerHint !== "litellm" && providerHint !== "legacy") {
      const providerKey = getModelProviderKey(model);
      const rawProvider = normalizeToken(model.provider);
      if (providerKey !== providerHint && rawProvider !== providerHint) continue;
      if (registryModelId === normalizedModelId) score = 120;
      else if (executionModelId && executionModelId === normalizedModelId) score = 108;
      else continue;
    } else {
      if (registryModelId === normalizedModelId) score = 94;
      else if (executionModelId && executionModelId === normalizedModelId) score = 84;
      else continue;
    }

    if (getModelProviderKey(model) === inferredProviderKey) score += 6;
    if (model.is_active) score += 3;
    if (model.is_selectable) score += 2;
    if (model.is_executable) score += 1;

    if (!bestMatch || score > bestMatch.score) {
      bestMatch = { model, score };
    }
  }

  return bestMatch?.model || null;
}
