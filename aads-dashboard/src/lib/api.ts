// T-072: Flat API response types
export interface DirectiveItem { task_id: string; title: string; project: string; status: string; error_type: string | null; started_at: string; completed_at: string | null; duration_seconds: number | null; created_at: string; file_path: string; }
export interface DirectivesResponse { status: string; total: number; running: number; completed: number; error: number; error_breakdown: Record<string, number>; project_breakdown: Record<string, number>; summary: Record<string, number>; items: DirectiveItem[]; directives: DirectiveItem[]; }

import type {
  HealthResponse,
  ProjectListResponse,
  ProjectStatusResponse,
  CreateProjectResponse,
  AutoRunResponse,
  LoginResponse,
  MeResponse,
  ConversationsResponse,
  ConversationStatsResponse,
  PublicSummaryResponse,
  MemorySearchResponse,
  MemoryInboxResponse,
} from "@/types";

const BASE_URL = process.env.NEXT_PUBLIC_API_URL || "https://aads.newtalk.kr/api/v1";

function getAuthHeaders(): Record<string, string> {
  if (typeof window === "undefined") return {};
  const token = localStorage.getItem("aads_token");
  return token ? { Authorization: `Bearer ${token}` } : {};
}

// AADS-AUTH-401: 401 응답 시 토큰 정리 + 로그인 페이지 리다이렉트 (중복 실행 방지)
let _redirecting401 = false;
function handle401Redirect(): void {
  if (typeof window === "undefined") return;
  if (_redirecting401) return;
  _redirecting401 = true;
  try {
    localStorage.removeItem("aads_token");
    document.cookie = "aads_token=; path=/; max-age=0";
  } catch {}
  const cur = window.location.pathname + window.location.search;
  if (!cur.startsWith("/login")) {
    const next = encodeURIComponent(cur);
    window.location.href = `/login?next=${next}&reason=session_expired`;
  }
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE_URL}${path}`, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...getAuthHeaders(),
      ...options?.headers,
    },
  });
  if (res.status === 401) {
    handle401Redirect();
    throw new Error("401: 세션이 만료되었습니다. 다시 로그인해주세요.");
  }
  if (!res.ok) {
    throw new Error(`API error ${res.status}: ${await res.text()}`);
  }
  return res.json() as Promise<T>;
}

export const api = {
  getHealth: () => request<HealthResponse>("/health"),

  getProjects: (limit = 20, offset = 0) =>
    request<ProjectListResponse>(`/projects?limit=${limit}&offset=${offset}`),

  getProjectStatus: (id: string) =>
    request<ProjectStatusResponse>(`/projects/${id}/status`),

  createProject: (description: string) =>
    request<CreateProjectResponse>("/projects", {
      method: "POST",
      body: JSON.stringify({ description }),
    }),

  autoRunProject: (id: string) =>
    request<AutoRunResponse>(`/projects/${id}/auto_run`, { method: "POST" }),

  getProjectCosts: (id: string) =>
    request<{ project_id: string; costs: import("@/types").CostInfo }>(`/projects/${id}/costs`),

  resumeProject: (id: string, approved: boolean = true, feedback: string = "승인") =>
    request<{ project_id: string; status: string; checkpoint_stage: string }>(
      `/projects/${id}/resume?approved=${approved}&feedback=${encodeURIComponent(feedback)}`,
      { method: "POST" }
    ),

  login: (email: string, password: string) =>
    request<LoginResponse>("/auth/login", {
      method: "POST",
      body: JSON.stringify({ email, password }),
    }),

  getMe: (token: string) =>
    fetch(`${BASE_URL}/auth/me`, {
      headers: { Authorization: `Bearer ${token}` },
    }).then((r) => (r.ok ? r.json() as Promise<MeResponse> : null)),

  getConversations: (project?: string, keyword?: string, limit = 50, offset = 0) =>
    request<ConversationsResponse>(
      `/conversations?${project ? `project=${project}&` : ''}${keyword ? `keyword=${encodeURIComponent(keyword)}&` : ''}limit=${limit}&offset=${offset}`
    ),
  getConversationStats: () => request<ConversationStatsResponse>('/conversations/stats'),
  // T-077: Channel-based conversation API
  getConversationChannels: () => request<any>('/conversations/channels'),
  getConversationMessages: (channel: string, limit = 50, offset = 0) =>
    request<any>(`/conversations/messages?channel=${encodeURIComponent(channel)}&limit=${limit}&offset=${offset}`),
  searchConversations: (q: string, channel = 'ALL', limit = 20) =>
    request<any>(`/conversations/search?q=${encodeURIComponent(q)}&channel=${encodeURIComponent(channel)}&limit=${limit}`),
  getPublicSummary: () => request<PublicSummaryResponse>('/context/public-summary'),
  getMemorySearch: (params?: { agent_id?: string; memory_type?: string; keyword?: string }) => {
    const q = new URLSearchParams();
    if (params?.agent_id) q.set('agent_id', params.agent_id);
    if (params?.memory_type) q.set('memory_type', params.memory_type);
    if (params?.keyword) q.set('keyword', params.keyword);
    return request<MemorySearchResponse>(`/memory/search?${q.toString()}`);
  },
  getManagerInbox: (agentId: string) => request<MemoryInboxResponse>(`/memory/inbox/${agentId}`),

  // T-049: CEO Dashboard extensions
  getProjectDashboard: () => request<any>("/projects/dashboard"),
  getProjectDetail: (id: string) => request<any>(`/projects/dashboard/${id}`),
  getTimeline: () => request<any>("/projects/dashboard/timeline"),
  getAlerts: () => request<any>("/projects/dashboard/alerts"),
  getCeoDecisions: (days?: number) => request<any>(`/memory/ceo-decisions?days=${days || 30}`),

  // T-072: Directives + Reports + Task-History (flat response)
  getDirectives: (project?: string) => request<any>(`/dashboard/directives${project && project !== "all" ? `?project=${encodeURIComponent(project)}` : ""}`),
  getReports: (project?: string) => request<any>(`/dashboard/reports${project && project !== "all" ? `?project=${encodeURIComponent(project)}` : ""}`),
  getReportDetail: (filename: string) => request<any>(`/dashboard/reports/${encodeURIComponent(filename)}`),
  getTaskHistory: () => request<any>("/dashboard/task-history"),
  getDirectiveDetail: (taskId: string) => request<any>(`/dashboard/directives/${encodeURIComponent(taskId)}`),

  // T-067: Analytics
  getAnalytics: () => request<any>("/dashboard/analytics"),
  getDocuments: (tag?: string) => request<any>("/documents" + (tag ? "?tag=" + encodeURIComponent(tag) : "")),
  getDocumentContent: (docId: string) => request<any>("/documents/" + encodeURIComponent(docId)),

  // T-073: CEO Chat v2 — Legacy, /chat으로 통합됨 (2026-03-11 비활성화)
  completeRunning: (project?: string) => request<any>("/dashboard/complete-running", {
    method: "POST",
    body: JSON.stringify({ project: project || "all" }),
  }),

  // T-103: 대화창(Channels) CRUD + context-package
  getChannels: () => request<any>("/channels"),
  getChannel: (id: string) => request<any>(`/channels/${encodeURIComponent(id)}`),
  createChannel: (data: { id: string; name: string; description: string; url: string; status?: string; project?: string; server?: string; context_docs?: {role: string; url: string}[]; system_prompt?: string }) =>
    request<any>("/channels", { method: "POST", body: JSON.stringify(data) }),
  updateChannel: (id: string, data: { name?: string; description?: string; url?: string; status?: string; project?: string; server?: string; context_docs?: {role: string; url: string}[]; system_prompt?: string }) =>
    request<any>(`/channels/${encodeURIComponent(id)}`, { method: "PUT", body: JSON.stringify(data) }),
  deleteChannel: (id: string) =>
    request<any>(`/channels/${encodeURIComponent(id)}`, { method: "DELETE" }),
  getContextPackage: (id: string) => request<any>(`/channels/${encodeURIComponent(id)}/context-package`),

  // AADS-114: Ops Monitor
  getOpsHealthCheck: () => request<any>("/ops/health-check"),
  getOpsDirectiveLifecycle: (limit = 20) => request<any>(`/ops/directive-lifecycle?limit=${limit}`),
  getOpsCostSummary: () => request<any>("/ops/cost/summary"),
  getOpsAccountUsage: () => request<any>("/ops/account-usage"),
  getOpsEnvHistory: (serverId: number | string) => request<any>(`/ops/env-history/${serverId}`),
  getOpsBridgeLog: (limit = 30) => request<any>(`/ops/bridge-log?limit=${limit}`),

  // AADS-123: Lessons API
  getLessons: (category?: string, project?: string) => {
    const q = new URLSearchParams();
    if (category) q.set("category", category);
    if (project) q.set("project", project);
    const qs = q.toString();
    return request<any>(`/lessons${qs ? "?" + qs : ""}`);
  },
  getLesson: (id: string) => request<any>(`/lessons/${encodeURIComponent(id)}`),
  getOpsDirectiveLifecycleByProject: (project: string, limit = 10) =>
    request<any>(`/ops/directive-lifecycle?project=${encodeURIComponent(project)}&limit=${limit}`),

  // AADS-146: Managers API
  getManagers: () => request<any>("/managers"),
  getManagerDetail: (agentId: string) => request<any>(`/managers/${encodeURIComponent(agentId)}`),

  // AADS-143: message_queue / context POST (트리거 메시지 전송)
  setContext: (data: { category: string; key: string; value: unknown }) =>
    request<any>("/context/system", { method: "POST", body: JSON.stringify(data) }),

  // AADS-148: Project Docs (중요 문서 링크 CRUD + 자동 push)
  getProjectDocs: () => request<any>("/ops/project-docs"),
  syncProjectDocs: (projectDocs: Record<string, { label: string; url: string }[]>) =>
    request<any>("/ops/sync-project-docs", {
      method: "POST",
      body: JSON.stringify({ project_docs: projectDocs }),
    }),
  getTriggerMessages: () => request<any>("/ops/trigger-messages"),
  syncTriggerMessages: (triggerMessages: Record<string, string>) =>
    request<any>("/ops/sync-trigger-messages", {
      method: "POST",
      body: JSON.stringify({ trigger_messages: triggerMessages }),
    }),

  // T-038: Watchdog
  getWatchdogSummary: () => request<any>("/watchdog/summary"),
  getWatchdogServices: () => request<any>("/watchdog/services"),
  getWatchdogErrors: (status?: string, limit = 20) =>
    request<any>(`/watchdog/errors?${status ? `status=${status}&` : ""}limit=${limit}`),

  // AADS-163: QA Results + Design Reviews
  getOpsQaResults: (limit = 20) => request<any>(`/ops/qa-results?limit=${limit}`),
  getOpsDesignReviews: (limit = 10) => request<any>(`/ops/design-reviews?limit=${limit}`),

  // AADS-166: Pipeline Health Check
  getDirectiveFolder: (status: string) => request<any>(`/directives/${status}`),
  getOpsPipelineStatus: () => request<any>("/ops/pipeline-status"),
  getOpsInfraCheck: () => request<any>("/ops/infra-check"),
  getOpsConsistencyCheck: () => request<any>("/ops/consistency-check"),
  getOpsFullHealth: () => request<any>("/ops/full-health"),

  // AADS-169: Claude Bot Status + Process Control
  getClaudeProcesses: (limit = 5) => request<any>(`/ops/claude-processes?limit=${limit}`),
  postClaudeCleanup: (server?: string | null, dry_run = false, reason = "manual_ceo_trigger") =>
    request<any>("/ops/claude-cleanup", {
      method: "POST",
      body: JSON.stringify({ server: server || null, dry_run, reason }),
    }),
  postBridgeRestart: (reason = "manual_ceo_trigger") =>
    request<any>("/ops/bridge-restart", {
      method: "POST",
      body: JSON.stringify({ reason }),
    }),

  // AADS-170: Chat-First System API
  getChatWorkspaces: () => request<any>("/chat/workspaces"),
  createChatWorkspace: (data: { name: string; description?: string; icon?: string; color?: string }) =>
    request<any>("/chat/workspaces", { method: "POST", body: JSON.stringify(data) }),
  updateChatWorkspace: (id: string, data: Record<string, unknown>) =>
    request<any>(`/chat/workspaces/${id}`, { method: "PUT", body: JSON.stringify(data) }),
  deleteChatWorkspace: (id: string) =>
    request<any>(`/chat/workspaces/${id}`, { method: "DELETE" }),

  getChatSessions: (workspaceId?: string) =>
    request<any>(`/chat/sessions${workspaceId ? `?workspace_id=${encodeURIComponent(workspaceId)}` : ""}`),
  getChatSession: (sessionId: string) =>
    request<any>(`/chat/sessions/${encodeURIComponent(sessionId)}`),
  createChatSession: (data: { workspace_id: string; title?: string; current_model?: string }) =>
    request<any>("/chat/sessions", { method: "POST", body: JSON.stringify(data) }),
  updateChatSession: (id: string, data: { title?: string; pinned?: boolean }) =>
    request<any>(`/chat/sessions/${id}`, { method: "PUT", body: JSON.stringify(data) }),
  deleteChatSession: (id: string) =>
    request<any>(`/chat/sessions/${id}`, { method: "DELETE" }),

  sendChatMessage: (sessionId: string, content: string, workspaceId?: string) =>
    request<any>("/chat/messages", {
      method: "POST",
      body: JSON.stringify({ session_id: sessionId, content, workspace_id: workspaceId }),
    }),
  getChatMessages: (sessionId: string, limit = 50, offset = 0) =>
    request<any>(`/chat/messages?session_id=${sessionId}&limit=${limit}&offset=${offset}`),
  searchChatMessages: (q: string, workspaceId?: string, limit = 20) => {
    const params = new URLSearchParams({ q, limit: String(limit) });
    if (workspaceId) params.set("workspace_id", workspaceId);
    return request<any>(`/chat/messages/search?${params.toString()}`);
  },
  toggleChatBookmark: (messageId: string) =>
    request<any>(`/chat/messages/${messageId}/bookmark`, { method: "PUT" }),

  getChatArtifacts: (sessionId: string) =>
    request<any>(`/chat/artifacts?session_id=${sessionId}`),
  getChatArtifact: (id: string) => request<any>(`/chat/artifacts/${id}`),
  updateChatArtifact: (id: string, data: Record<string, unknown>) =>
    request<any>(`/chat/artifacts/${id}`, { method: "PUT", body: JSON.stringify(data) }),
  exportChatArtifact: (id: string, format: "pdf" | "md" | "html") =>
    request<any>(`/chat/artifacts/${id}/export`, { method: "POST", body: JSON.stringify({ format }) }),

  getChatDrive: (workspaceId: string) =>
    request<any>(`/chat/drive?workspace_id=${workspaceId}`),
  deleteChatFile: (fileId: string) =>
    request<any>(`/chat/drive/${fileId}`, { method: "DELETE" }),

  getChatResearch: (topic: string) =>
    request<any>(`/chat/research?topic=${encodeURIComponent(topic)}`),
  getChatResearchHistory: (limit = 50) =>
    request<any>(`/chat/research/history?limit=${limit}`),

  // PC Agent
  getPCAgents: () => request<any>("/pc-agent/agents"),
  sendPCCommand: (agent_id: string, command_type: string, params?: Record<string, unknown>) =>
    request<any>("/pc-agent/execute", {
      method: "POST",
      body: JSON.stringify({ agent_id, command_type, params: params || {} }),
    }),
  getPCResult: (command_id: string, timeout = 30) =>
    request<any>(`/pc-agent/result/${encodeURIComponent(command_id)}?timeout=${timeout}`),
  startPCStream: (agent_id: string, config?: { fps?: number; quality?: number; scale?: number }) =>
    request<any>(`/pc-agent/stream/${encodeURIComponent(agent_id)}/start`, {
      method: "POST",
      body: JSON.stringify(config || {}),
    }),
  stopPCStream: (agent_id: string) =>
    request<any>(`/pc-agent/stream/${encodeURIComponent(agent_id)}/stop`, { method: "POST" }),

  // Memory Evolution Dashboard
  getOpsMemoryStats: () => request<any>("/ops/memory/stats"),
  getOpsMemoryEntries: (params?: { category?: string; project?: string; search?: string; page?: number; page_size?: number }) => {
    const q = new URLSearchParams();
    if (params?.category) q.set("category", params.category);
    if (params?.project) q.set("project", params.project);
    if (params?.search) q.set("search", params.search);
    if (params?.page) q.set("page", String(params.page));
    if (params?.page_size) q.set("page_size", String(params.page_size));
    const qs = q.toString();
    return request<any>(`/ops/memory/entries${qs ? "?" + qs : ""}`);
  },
  deleteOpsMemoryEntry: (source: string, id: string) =>
    request<any>(`/ops/memory/entries/${encodeURIComponent(source)}/${encodeURIComponent(id)}`, { method: "DELETE" }),
  deduplicateOpsMemory: () =>
    request<any>("/ops/memory/deduplicate", { method: "POST" }),
  getOpsMemoryLearningHealth: () =>
    request<any>("/ops/memory/learning-health"),

  // CEO 아젠다 관리
  getAgendas: (params?: { status?: string; project?: string; priority?: string; session_id?: string; limit?: number; offset?: number }) => {
    const q = new URLSearchParams();
    if (params?.status) q.set("status", params.status);
    if (params?.project) q.set("project", params.project);
    if (params?.priority) q.set("priority", params.priority);
    if (params?.session_id) q.set("source_session_id", params.session_id);
    if (params?.limit) q.set("limit", String(params.limit));
    if (params?.offset) q.set("offset", String(params.offset));
    const qs = q.toString();
    return request<any>(`/agenda/${qs ? "?" + qs : ""}`);
  },
  getAgenda: (id: number) => request<any>(`/agenda/${id}`),
  getAgendaSessions: (project?: string) => {
    const qs = project ? `?project=${encodeURIComponent(project)}` : "";
    return request<any>(`/agenda/sessions${qs}`);
  },

  // Runner Model Config: 러너 모델 우선순위 설정 (AADS-241)
  getRunnerModels: () => request<any>("/settings/runner-models"),
  updateRunnerModels: (configs: Array<{ size: string; models: string[] }>) =>
    request<any>("/settings/runner-models", { method: "PUT", body: JSON.stringify({ configs }) }),

  // Project Docs: 프로젝트별 문서 통합 조회
  scanProjectDocs: (force?: boolean) => request<any>(`/project-docs/scan${force ? "?force=true" : ""}`),
  getProjectDocContent: (project: string, basePath: string, filePath: string) =>
    request<any>(`/project-docs/content?project=${encodeURIComponent(project)}&base_path=${encodeURIComponent(basePath)}&file_path=${encodeURIComponent(filePath)}`),

  // Admin: Prompt Management
  getPromptSections: () => request<any>("/admin/prompts/sections"),
  getPromptIntentGroups: () => request<any>("/admin/prompts/intent-groups"),
  previewPrompt: (ws: string, intent: string) =>
    request<any>("/admin/prompts/preview", { method: "POST", body: JSON.stringify({ workspace_key: ws, intent }) }),
  getWorkspacePrompts: () => request<any>("/admin/prompts/workspaces"),
  updateWorkspacePrompt: (id: string, system_prompt: string) =>
    request<any>(`/admin/prompts/workspace/${id}`, { method: "PUT", body: JSON.stringify({ system_prompt }) }),
  getPromptVersions: (section?: string) =>
    request<any>(`/admin/prompts/versions${section ? "?section=" + encodeURIComponent(section) : ""}`),
  getPromptVersion: (id: number) => request<any>(`/admin/prompts/versions/${id}`),
  getTokenProfile: () => request<any>("/admin/prompts/token-profile"),
  getGovernance: () => request<any>("/admin/governance"),
  getGovernanceLayers: () => request<any>("/admin/governance/layers"),
  getAdminAgents: () => request<any>("/admin/agents"),
  getAdminAgent: (role: string) => request<any>(`/admin/agents/${encodeURIComponent(role)}`),
  getAdminAgentStats: () => request<any>("/admin/agents/stats"),
  getAdminTasks: (params?: { status?: string; page?: number; page_size?: number }) => {
    const q = new URLSearchParams();
    if (params?.status) q.set("status", params.status);
    if (params?.page) q.set("page", String(params.page));
    if (params?.page_size) q.set("page_size", String(params.page_size));
    return request<any>(`/admin/tasks${q.size ? `?${q.toString()}` : ""}`);
  },
  getAdminDeployStatus: () => request<any>("/admin/deploy/status"),
  getAdminTask: (jobId: string) => request<any>(`/admin/tasks/${encodeURIComponent(jobId)}`),
  getAdminTaskStats: () => request<any>("/admin/tasks/stats"),

  // LLM API 키 관리 (AADS-188)
  getLlmKeys: () => request<any[]>("/llm-keys"),
  createLlmKey: (data: { provider: string; key_name: string; value: string; label?: string; priority?: number; notes?: string }) =>
    request<any>("/llm-keys", { method: "POST", body: JSON.stringify(data) }),
  updateLlmKey: (id: number, data: Partial<{ value: string; label: string; priority: number; is_active: boolean; notes: string }>) =>
    request<any>(`/llm-keys/${id}`, { method: "PUT", body: JSON.stringify(data) }),
  deleteLlmKey: (id: number) => request<any>(`/llm-keys/${id}`, { method: "DELETE" }),
};
