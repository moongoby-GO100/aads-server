"use client";
import { useEffect, useState, useCallback } from "react";
import Header from "@/components/Header";
import { api } from "@/lib/api";

// ── 타입 ──
interface DocFile {
  name: string;
  path: string;
  size: number;
  modified: number;
  type: string;
  base_path: string;
  label: string;
}
interface ProjectDocs {
  project: string;
  host: string;
  total: number;
  files: DocFile[];
}
interface ScanResult {
  total: number;
  projects: ProjectDocs[];
  scanned_at: number;
}

// ── 상수 ──
const PROJECT_COLORS: Record<string, string> = {
  AADS: "bg-blue-900 text-blue-200",
  KIS: "bg-purple-900 text-purple-200",
  GO100: "bg-yellow-900 text-yellow-200",
  SF: "bg-emerald-900 text-emerald-200",
  NTV2: "bg-pink-900 text-pink-200",
};

const TYPE_LABELS: Record<string, string> = {
  doc: "문서",
  report: "리포트",
  tech: "기술문서",
  plan: "기획서",
  status: "상황보고",
  knowledge: "지식",
  code: "코드",
};

const TYPE_COLORS: Record<string, string> = {
  doc: "bg-gray-700 text-gray-200",
  report: "bg-blue-900 text-blue-200",
  tech: "bg-purple-900 text-purple-200",
  plan: "bg-cyan-900 text-cyan-200",
  status: "bg-yellow-900 text-yellow-200",
  knowledge: "bg-green-900 text-green-200",
  code: "bg-orange-900 text-orange-200",
};

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes}B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)}KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)}MB`;
}

function formatDate(ts: number): string {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  const mm = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  const hh = String(d.getHours()).padStart(2, "0");
  const mi = String(d.getMinutes()).padStart(2, "0");
  return `${mm}-${dd} ${hh}:${mi}`;
}

// ── 마크다운 렌더러 ──
function renderMarkdown(text: string): string {
  let html = text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");

  // 코드블록
  html = html.replace(/```(\w*)\n([\s\S]*?)```/g, (_m, lang, code) =>
    `<pre class="bg-gray-900 border border-gray-700 rounded-lg p-3 my-2 overflow-x-auto text-sm"><code class="text-green-300">${code.trim()}</code></pre>`
  );
  // 인라인 코드
  html = html.replace(/`([^`]+)`/g, '<code class="bg-gray-700 px-1 py-0.5 rounded text-sm text-yellow-200">$1</code>');
  // 헤딩
  html = html.replace(/^#### (.+)$/gm, '<h4 class="text-sm font-bold mt-4 mb-1 text-blue-300">$1</h4>');
  html = html.replace(/^### (.+)$/gm, '<h3 class="text-base font-bold mt-5 mb-2 text-blue-200">$1</h3>');
  html = html.replace(/^## (.+)$/gm, '<h2 class="text-lg font-bold mt-6 mb-2 text-white border-b border-gray-700 pb-1">$1</h2>');
  html = html.replace(/^# (.+)$/gm, '<h1 class="text-xl font-bold mt-6 mb-3 text-white">$1</h1>');
  // 볼드 / 이탤릭
  html = html.replace(/\*\*(.+?)\*\*/g, '<strong class="text-white">$1</strong>');
  html = html.replace(/\*(.+?)\*/g, '<em>$1</em>');
  // 리스트
  html = html.replace(/^- (.+)$/gm, '<li class="ml-4 list-disc text-sm">$1</li>');
  html = html.replace(/^(\d+)\. (.+)$/gm, '<li class="ml-4 list-decimal text-sm">$2</li>');
  // 테이블
  html = html.replace(/^\|(.+)\|$/gm, (line) => {
    const cells = line.split("|").filter(Boolean);
    if (cells.every((c) => /^[\s-:]+$/.test(c))) return "";
    const tag = "td";
    const row = cells.map((c) => `<${tag} class="border border-gray-700 px-2 py-1 text-sm">${c.trim()}</${tag}>`).join("");
    return `<tr class="hover:bg-gray-800">${row}</tr>`;
  });
  html = html.replace(/(<tr[^>]*>.*<\/tr>\n?)+/g, (m) =>
    `<table class="w-full border-collapse border border-gray-700 my-3">${m}</table>`
  );
  // 블록쿼트
  html = html.replace(/^&gt; (.+)$/gm, '<blockquote class="border-l-4 border-blue-500 pl-3 my-2 text-gray-400 italic text-sm">$1</blockquote>');
  // 줄바꿈
  html = html.replace(/\n\n/g, '<div class="my-2"></div>');
  html = html.replace(/\n/g, "<br/>");

  return html;
}

export default function DocsPage() {
  const [data, setData] = useState<ScanResult | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedProject, setSelectedProject] = useState<string>("all");
  const [search, setSearch] = useState("");
  const [selectedFile, setSelectedFile] = useState<DocFile | null>(null);
  const [fileContent, setFileContent] = useState<string | null>(null);
  const [contentLoading, setContentLoading] = useState(false);
  const [sortBy, setSortBy] = useState<"name" | "modified" | "size">("modified");

  const fetchDocs = useCallback(async (force = false) => {
    setLoading(true);
    setError(null);
    try {
      const r = await api.scanProjectDocs(force);
      setData(r);
    } catch (e: any) {
      setError(e.message || "스캔 실패");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchDocs();
  }, [fetchDocs]);

  const openFile = async (project: string, file: DocFile) => {
    setSelectedFile(file);
    setContentLoading(true);
    setFileContent(null);
    try {
      const r = await api.getProjectDocContent(project, file.base_path, file.path);
      setFileContent(r.content || "");
    } catch (e: any) {
      setFileContent(`⚠️ 파일을 불러올 수 없습니다: ${e.message}`);
    } finally {
      setContentLoading(false);
    }
  };

  // 필터링
  const filteredProjects = data?.projects?.filter(
    (p) => selectedProject === "all" || p.project === selectedProject
  ) || [];

  const allFiles: (DocFile & { project: string })[] = [];
  for (const p of filteredProjects) {
    for (const f of p.files) {
      if (search) {
        const q = search.toLowerCase();
        if (!f.name.toLowerCase().includes(q) && !f.path.toLowerCase().includes(q)) continue;
      }
      allFiles.push({ ...f, project: p.project });
    }
  }

  // 정렬
  allFiles.sort((a, b) => {
    if (sortBy === "modified") return b.modified - a.modified;
    if (sortBy === "size") return b.size - a.size;
    return a.name.localeCompare(b.name);
  });

  return (
    <div className="flex flex-col h-full" style={{ background: "var(--bg-primary)" }}>
      <Header title="📄 프로젝트별 문서 통합" />
      <div className="flex-1 flex overflow-hidden">
        {/* ── 좌측: 문서 목록 ── */}
        <div className="w-full lg:w-1/2 xl:w-2/5 flex flex-col border-r" style={{ borderColor: "var(--border)" }}>
          {/* 필터 바 */}
          <div className="p-3 space-y-2" style={{ borderBottom: "1px solid var(--border)" }}>
            <div className="flex items-center gap-2 flex-wrap">
              <button
                onClick={() => setSelectedProject("all")}
                className="px-2.5 py-1 text-xs rounded-lg transition-colors"
                style={selectedProject === "all"
                  ? { background: "var(--accent)", color: "#fff" }
                  : { border: "1px solid var(--border)", color: "var(--text-secondary)", background: "transparent" }}
              >
                전체 {data?.total || 0}
              </button>
              {data?.projects?.map((p) => (
                <button
                  key={p.project}
                  onClick={() => setSelectedProject(p.project)}
                  className="px-2.5 py-1 text-xs rounded-lg transition-colors"
                  style={selectedProject === p.project
                    ? { background: "var(--accent)", color: "#fff" }
                    : { border: "1px solid var(--border)", color: "var(--text-secondary)", background: "transparent" }}
                >
                  {p.project} {p.total}
                </button>
              ))}
              <button
                onClick={() => fetchDocs(true)}
                className="ml-auto px-2 py-1 text-xs rounded-lg"
                style={{ background: "var(--bg-hover)", color: "var(--text-primary)" }}
                title="강제 재스캔"
              >
                🔄
              </button>
            </div>
            <div className="flex gap-2">
              <input
                type="text"
                placeholder="파일명 검색..."
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                className="flex-1 px-3 py-1.5 rounded-lg text-sm outline-none"
                style={{
                  background: "var(--bg-card)",
                  border: "1px solid var(--border)",
                  color: "var(--text-primary)",
                }}
              />
              <select
                value={sortBy}
                onChange={(e) => setSortBy(e.target.value as any)}
                className="px-2 py-1.5 rounded-lg text-xs outline-none"
                style={{
                  background: "var(--bg-card)",
                  border: "1px solid var(--border)",
                  color: "var(--text-secondary)",
                }}
              >
                <option value="modified">최신순</option>
                <option value="name">이름순</option>
                <option value="size">크기순</option>
              </select>
            </div>
          </div>

          {/* 파일 리스트 */}
          <div className="flex-1 overflow-auto p-2 space-y-1">
            {loading ? (
              <div className="text-center py-12" style={{ color: "var(--text-secondary)" }}>
                스캔 중...
              </div>
            ) : error ? (
              <div className="text-center py-12 text-red-400">{error}</div>
            ) : allFiles.length === 0 ? (
              <div className="text-center py-12" style={{ color: "var(--text-secondary)" }}>
                문서가 없습니다
              </div>
            ) : (
              allFiles.map((f, i) => {
                const isSelected = selectedFile?.path === f.path && selectedFile?.base_path === f.base_path;
                return (
                  <button
                    key={`${f.project}-${f.base_path}-${f.path}-${i}`}
                    onClick={() => openFile(f.project, f)}
                    className="w-full text-left px-3 py-2 rounded-lg transition-colors"
                    style={{
                      background: isSelected ? "var(--accent)" : "transparent",
                      color: isSelected ? "#fff" : "var(--text-primary)",
                    }}
                    onMouseEnter={(e) => {
                      if (!isSelected) (e.currentTarget as HTMLElement).style.background = "var(--bg-hover)";
                    }}
                    onMouseLeave={(e) => {
                      if (!isSelected) (e.currentTarget as HTMLElement).style.background = "transparent";
                    }}
                  >
                    <div className="flex items-center gap-2">
                      <span className={`text-xs px-1.5 py-0.5 rounded ${PROJECT_COLORS[f.project] || "bg-gray-700 text-gray-200"}`}>
                        {f.project}
                      </span>
                      <span className={`text-xs px-1.5 py-0.5 rounded ${TYPE_COLORS[f.type] || "bg-gray-700 text-gray-200"}`}>
                        {TYPE_LABELS[f.type] || f.type}
                      </span>
                      <span className="text-xs ml-auto" style={{ color: isSelected ? "rgba(255,255,255,0.7)" : "var(--text-secondary)" }}>
                        {formatSize(f.size)}
                      </span>
                    </div>
                    <div className="text-sm mt-1 truncate font-medium">{f.name}</div>
                    <div className="flex items-center gap-2 mt-0.5">
                      <span className="text-xs truncate" style={{ color: isSelected ? "rgba(255,255,255,0.6)" : "var(--text-secondary)" }}>
                        {f.label} / {f.path.includes("/") ? f.path.substring(0, f.path.lastIndexOf("/")) : ""}
                      </span>
                      <span className="text-xs ml-auto" style={{ color: isSelected ? "rgba(255,255,255,0.6)" : "var(--text-secondary)" }}>
                        {formatDate(f.modified)}
                      </span>
                    </div>
                  </button>
                );
              })
            )}
          </div>

          {/* 하단 상태바 */}
          {data && (
            <div className="px-3 py-2 text-xs flex justify-between" style={{ borderTop: "1px solid var(--border)", color: "var(--text-secondary)" }}>
              <span>{allFiles.length}개 문서</span>
              <span>스캔: {new Date((data.scanned_at || 0) * 1000).toLocaleTimeString("ko-KR")}</span>
            </div>
          )}
        </div>

        {/* ── 우측: 문서 뷰어 ── */}
        <div className="hidden lg:flex flex-1 flex-col overflow-hidden">
          {selectedFile ? (
            <>
              <div className="px-4 py-3 flex items-center gap-3" style={{ borderBottom: "1px solid var(--border)" }}>
                <span className={`text-xs px-1.5 py-0.5 rounded ${PROJECT_COLORS[(selectedFile as any).project || ""] || "bg-gray-700 text-gray-200"}`}>
                  {(selectedFile as any).project}
                </span>
                <span className="text-sm font-semibold" style={{ color: "var(--text-primary)" }}>
                  {selectedFile.name}
                </span>
                <span className="text-xs ml-auto" style={{ color: "var(--text-secondary)" }}>
                  {selectedFile.path} · {formatSize(selectedFile.size)}
                </span>
              </div>
              <div className="flex-1 overflow-auto p-4">
                {contentLoading ? (
                  <div className="text-center py-12" style={{ color: "var(--text-secondary)" }}>
                    로딩 중...
                  </div>
                ) : fileContent !== null ? (
                  selectedFile.name.endsWith(".md") ? (
                    <div
                      className="prose prose-invert max-w-none text-sm leading-relaxed"
                      style={{ color: "var(--text-primary)" }}
                      dangerouslySetInnerHTML={{ __html: renderMarkdown(fileContent) }}
                    />
                  ) : (
                    <pre
                      className="text-sm whitespace-pre-wrap break-words"
                      style={{ color: "var(--text-primary)" }}
                    >
                      {fileContent}
                    </pre>
                  )
                ) : null}
              </div>
            </>
          ) : (
            <div className="flex-1 flex items-center justify-center" style={{ color: "var(--text-secondary)" }}>
              <div className="text-center">
                <div className="text-4xl mb-3">📄</div>
                <p className="text-sm">좌측에서 문서를 선택하세요</p>
                <p className="text-xs mt-1">3개 서버의 문서를 한 곳에서 확인합니다</p>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
