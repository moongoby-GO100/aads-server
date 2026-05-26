"""docs/page.tsx 패치 — PDF/이미지/오피스 렌더링 추가 + 종류별 검색 강화."""
import re
import sys
from pathlib import Path

PATH = Path("/root/aads/aads-dashboard/src/app/docs/page.tsx")
text = PATH.read_text(encoding="utf-8")
original = text

# 1) DocFile 인터페이스에 encoding/mime_type/is_binary 필드 추가
old = '''interface DocFile {
  name: string;
  path: string;
  size: number;
  modified: number;
  type: string;
  base_path: string;
  label: string;
  full_path?: string;
  format?: string;
}'''
new = '''interface DocFile {
  name: string;
  path: string;
  size: number;
  modified: number;
  type: string;
  base_path: string;
  label: string;
  full_path?: string;
  format?: string;
}

interface DocContentResponse {
  content: string;
  encoding?: "text" | "base64";
  mime_type?: string;
  is_binary?: boolean;
}'''
assert old in text, "DocFile interface block not found"
text = text.replace(old, new, 1)

# 2) FORMAT_LABELS 확장
old = '''const FORMAT_LABELS: Record<string, string> = {
  markdown: "Markdown",
  html: "HTML",
  text: "텍스트",
  json: "JSON",
  yaml: "YAML",
  toml: "TOML",
  xml: "XML",
  csv: "CSV",
  python: "Python",
  shell: "Shell",
  sql: "SQL",
  javascript: "JavaScript",
  typescript: "TypeScript",
  css: "CSS",
  config: "설정파일",
  log: "로그",
  rst: "reStructuredText",
  other: "기타",
};'''
new = '''const FORMAT_LABELS: Record<string, string> = {
  markdown: "Markdown",
  html: "HTML",
  text: "텍스트",
  pdf: "PDF",
  image: "이미지",
  word: "Word",
  excel: "Excel",
  powerpoint: "PowerPoint",
  json: "JSON",
  yaml: "YAML",
  toml: "TOML",
  xml: "XML",
  csv: "CSV",
  python: "Python",
  shell: "Shell",
  sql: "SQL",
  javascript: "JavaScript",
  typescript: "TypeScript",
  css: "CSS",
  config: "설정파일",
  log: "로그",
  rst: "reStructuredText",
  other: "기타",
};'''
assert old in text, "FORMAT_LABELS block not found"
text = text.replace(old, new, 1)

# 3) FORMAT_COLORS 확장
old = '''const FORMAT_COLORS: Record<string, string> = {
  markdown: "bg-blue-800 text-blue-100",
  html: "bg-orange-800 text-orange-100",
  text: "bg-gray-600 text-gray-100",
  json: "bg-yellow-800 text-yellow-100",
  yaml: "bg-green-800 text-green-100",
  toml: "bg-teal-800 text-teal-100",
  xml: "bg-indigo-800 text-indigo-100",
  csv: "bg-emerald-800 text-emerald-100",
  python: "bg-sky-800 text-sky-100",
  shell: "bg-lime-800 text-lime-100",
  sql: "bg-violet-800 text-violet-100",
  javascript: "bg-amber-800 text-amber-100",
  typescript: "bg-blue-700 text-blue-100",
  css: "bg-pink-800 text-pink-100",
  config: "bg-slate-600 text-slate-100",
  log: "bg-red-800 text-red-100",
  rst: "bg-cyan-800 text-cyan-100",
  other: "bg-gray-700 text-gray-200",
};'''
new = '''const FORMAT_COLORS: Record<string, string> = {
  markdown: "bg-blue-800 text-blue-100",
  html: "bg-orange-800 text-orange-100",
  text: "bg-gray-600 text-gray-100",
  pdf: "bg-red-900 text-red-100",
  image: "bg-purple-800 text-purple-100",
  word: "bg-blue-900 text-blue-100",
  excel: "bg-green-900 text-green-100",
  powerpoint: "bg-orange-900 text-orange-100",
  json: "bg-yellow-800 text-yellow-100",
  yaml: "bg-green-800 text-green-100",
  toml: "bg-teal-800 text-teal-100",
  xml: "bg-indigo-800 text-indigo-100",
  csv: "bg-emerald-800 text-emerald-100",
  python: "bg-sky-800 text-sky-100",
  shell: "bg-lime-800 text-lime-100",
  sql: "bg-violet-800 text-violet-100",
  javascript: "bg-amber-800 text-amber-100",
  typescript: "bg-blue-700 text-blue-100",
  css: "bg-pink-800 text-pink-100",
  config: "bg-slate-600 text-slate-100",
  log: "bg-red-800 text-red-100",
  rst: "bg-cyan-800 text-cyan-100",
  other: "bg-gray-700 text-gray-200",
};'''
assert old in text, "FORMAT_COLORS block not found"
text = text.replace(old, new, 1)

# 4) detectFormat 함수 확장
old = '''function detectFormat(name: string): string {
  const ext = name.includes(".") ? name.split(".").pop()?.toLowerCase() || "" : "";
  const map: Record<string, string> = {
    md: "markdown", txt: "text", html: "html", htm: "html", rst: "rst",
    json: "json", yaml: "yaml", yml: "yaml", toml: "toml", xml: "xml", csv: "csv",
    py: "python", sh: "shell", sql: "sql",
    js: "javascript", ts: "typescript", tsx: "typescript", jsx: "javascript",
    css: "css", ini: "config", cfg: "config", conf: "config", log: "log",
  };
  return map[ext] || "other";
}'''
new = '''function detectFormat(name: string): string {
  const ext = name.includes(".") ? name.split(".").pop()?.toLowerCase() || "" : "";
  const map: Record<string, string> = {
    md: "markdown", txt: "text", html: "html", htm: "html", rst: "rst", pdf: "pdf",
    json: "json", yaml: "yaml", yml: "yaml", toml: "toml", xml: "xml", csv: "csv",
    py: "python", sh: "shell", sql: "sql",
    js: "javascript", ts: "typescript", tsx: "typescript", jsx: "javascript",
    css: "css", ini: "config", cfg: "config", conf: "config", log: "log",
    png: "image", jpg: "image", jpeg: "image", gif: "image",
    svg: "image", webp: "image", bmp: "image", ico: "image",
    docx: "word", odt: "word",
    xlsx: "excel", ods: "excel",
    pptx: "powerpoint", odp: "powerpoint",
  };
  return map[ext] || "other";
}'''
assert old in text, "detectFormat function not found"
text = text.replace(old, new, 1)

# 5) state 추가: 바이너리 응답 보관용
old = '''  const [fileContent, setFileContent] = useState<string | null>(null);
  const [contentLoading, setContentLoading] = useState(false);'''
new = '''  const [fileContent, setFileContent] = useState<string | null>(null);
  const [fileMeta, setFileMeta] = useState<{ encoding: string; mime_type: string; is_binary: boolean } | null>(null);
  const [contentLoading, setContentLoading] = useState(false);'''
assert old in text, "fileContent state declaration not found"
text = text.replace(old, new, 1)

# 6) openFile 핸들러: meta 저장
old = '''  const openFile = async (project: string, file: ListedDocFile) => {
    setSelectedFile(file);
    setContentLoading(true);
    setFileContent(null);
    try {
      const r = await api.getProjectDocContent(project, file.base_path, file.path);
      setFileContent(r.content || "");
    } catch (e: unknown) {
      const message = e instanceof Error ? e.message : "알 수 없는 오류";
      setFileContent(`⚠️ 파일을 불러올 수 없습니다: ${message}`);
    } finally {
      setContentLoading(false);
    }
  };'''
new = '''  const openFile = async (project: string, file: ListedDocFile) => {
    setSelectedFile(file);
    setContentLoading(true);
    setFileContent(null);
    setFileMeta(null);
    try {
      const r = (await api.getProjectDocContent(project, file.base_path, file.path)) as DocContentResponse;
      setFileContent(r.content || "");
      setFileMeta({
        encoding: r.encoding || "text",
        mime_type: r.mime_type || "text/plain",
        is_binary: !!r.is_binary,
      });
    } catch (e: unknown) {
      const message = e instanceof Error ? e.message : "알 수 없는 오류";
      setFileContent(`⚠️ 파일을 불러올 수 없습니다: ${message}`);
      setFileMeta(null);
    } finally {
      setContentLoading(false);
    }
  };'''
assert old in text, "openFile handler not found"
text = text.replace(old, new, 1)

# 7) 렌더링 분기 확장 — html/json/기타 분기를 image/pdf/office/html/json/text 로 확장
old = '''                ) : fileContent !== null ? (
                  selectedFile.name.endsWith(".md") ? (
                    <div
                      className="prose prose-invert max-w-none text-sm leading-relaxed"
                      style={{ color: "var(--text-primary)" }}
                      dangerouslySetInnerHTML={{ __html: renderMarkdown(fileContent) }}
                    />
                  ) : selectedFile.name.endsWith(".html") || selectedFile.name.endsWith(".htm") ? (
                    <iframe
                      srcDoc={fileContent}
                      className="w-full border-0 rounded-lg"
                      style={{ background: "#fff", height: "calc(100vh - 200px)" }}
                      title={selectedFile.name}
                    />
                  ) : selectedFile.name.endsWith(".json") ? (
                    <pre className="text-sm whitespace-pre-wrap break-words" style={{ color: "var(--text-primary)" }}>
                      {(() => { try { return JSON.stringify(JSON.parse(fileContent), null, 2); } catch { return fileContent; } })()}
                    </pre>
                  ) : (
                    <pre className="text-sm whitespace-pre-wrap break-words" style={{ color: "var(--text-primary)" }}>
                      {fileContent}
                    </pre>
                  )'''
new = '''                ) : fileContent !== null ? (
                  (() => {
                    const fmt = selectedFile.format || detectFormat(selectedFile.name);
                    const lowerName = selectedFile.name.toLowerCase();
                    const isBinary = fileMeta?.is_binary || fileMeta?.encoding === "base64";
                    const mime = fileMeta?.mime_type || "application/octet-stream";

                    // 이미지: data URL로 직접 표시
                    if (fmt === "image") {
                      const src = isBinary
                        ? `data:${mime};base64,${fileContent}`
                        : `data:${mime || "image/svg+xml"};utf8,${encodeURIComponent(fileContent)}`;
                      return (
                        <div className="flex items-center justify-center bg-gray-900/40 rounded-lg p-4">
                          {/* eslint-disable-next-line @next/next/no-img-element */}
                          <img
                            src={src}
                            alt={selectedFile.name}
                            className="max-w-full max-h-[calc(100vh-220px)] object-contain rounded"
                          />
                        </div>
                      );
                    }

                    // PDF: base64 data URL로 iframe 표시
                    if (fmt === "pdf") {
                      const src = `data:application/pdf;base64,${fileContent}`;
                      return (
                        <iframe
                          src={src}
                          className="w-full border-0 rounded-lg bg-white"
                          style={{ height: "calc(100vh - 200px)" }}
                          title={selectedFile.name}
                        />
                      );
                    }

                    // 오피스 문서: 미리보기 불가, 다운로드 안내
                    if (fmt === "word" || fmt === "excel" || fmt === "powerpoint") {
                      const downloadHref = `data:${mime};base64,${fileContent}`;
                      const officeLabel = fmt === "word" ? "Word" : fmt === "excel" ? "Excel" : "PowerPoint";
                      return (
                        <div className="flex flex-col items-center justify-center py-16 text-center">
                          <div className="text-5xl mb-4">📎</div>
                          <p className="text-base font-semibold" style={{ color: "var(--text-primary)" }}>
                            {officeLabel} 문서는 브라우저에서 미리보기를 지원하지 않습니다
                          </p>
                          <p className="text-sm mt-2" style={{ color: "var(--text-secondary)" }}>
                            {selectedFile.name} · {formatSize(selectedFile.size)}
                          </p>
                          <a
                            href={downloadHref}
                            download={selectedFile.name}
                            className="mt-5 px-4 py-2 rounded-lg text-sm font-medium"
                            style={{ background: "var(--accent)", color: "#fff" }}
                          >
                            ⬇️ 다운로드
                          </a>
                        </div>
                      );
                    }

                    // Markdown
                    if (lowerName.endsWith(".md")) {
                      return (
                        <div
                          className="prose prose-invert max-w-none text-sm leading-relaxed"
                          style={{ color: "var(--text-primary)" }}
                          dangerouslySetInnerHTML={{ __html: renderMarkdown(fileContent) }}
                        />
                      );
                    }

                    // HTML
                    if (lowerName.endsWith(".html") || lowerName.endsWith(".htm")) {
                      return (
                        <iframe
                          srcDoc={fileContent}
                          className="w-full border-0 rounded-lg"
                          style={{ background: "#fff", height: "calc(100vh - 200px)" }}
                          title={selectedFile.name}
                          sandbox="allow-same-origin allow-popups"
                        />
                      );
                    }

                    // JSON
                    if (lowerName.endsWith(".json")) {
                      return (
                        <pre className="text-sm whitespace-pre-wrap break-words" style={{ color: "var(--text-primary)" }}>
                          {(() => { try { return JSON.stringify(JSON.parse(fileContent), null, 2); } catch { return fileContent; } })()}
                        </pre>
                      );
                    }

                    // 기본: 텍스트
                    return (
                      <pre className="text-sm whitespace-pre-wrap break-words" style={{ color: "var(--text-primary)" }}>
                        {fileContent}
                      </pre>
                    );
                  })()'''
assert old in text, "rendering branch not found"
text = text.replace(old, new, 1)

if text == original:
    print("ERROR: no change applied", file=sys.stderr)
    sys.exit(1)

PATH.write_text(text, encoding="utf-8")
print(f"OK — patched {PATH} ({len(text)} bytes)")
