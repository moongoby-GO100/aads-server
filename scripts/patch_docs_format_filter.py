#!/usr/bin/env python3
"""Patch docs/page.tsx: add format filter + format badge."""
import shutil
import sys

SRC = "/root/aads/aads-dashboard/src/app/docs/page.tsx"
BAK = SRC + ".bak_format"

shutil.copy2(SRC, BAK)
with open(SRC) as f:
    src = f.read()

if "FORMAT_LABELS" in src:
    print("SKIP: FORMAT_LABELS already exists")
    sys.exit(0)

changes = 0

# 1. Add format to DocFile interface
old1 = "  full_path?: string;\n}\n\ninterface ListedDocFile"
new1 = "  full_path?: string;\n  format?: string;\n}\n\ninterface ListedDocFile"
if old1 in src:
    src = src.replace(old1, new1, 1)
    changes += 1
    print("OK: DocFile.format added")
else:
    print("WARN: DocFile interface pattern not found")

# 2. Add FORMAT_LABELS, FORMAT_COLORS, detectFormat after TYPE_COLORS
INSERT_AFTER = '''  config: "bg-slate-700 text-slate-200",
};'''

FORMAT_BLOCK = '''

const FORMAT_LABELS: Record<string, string> = {
  markdown: "Markdown",
  html: "HTML",
  text: "\\ud14d\\uc2a4\\ud2b8",
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
  config: "\\uc124\\uc815\\ud30c\\uc77c",
  log: "\\ub85c\\uadf8",
  rst: "reStructuredText",
  other: "\\uae30\\ud0c0",
};

const FORMAT_COLORS: Record<string, string> = {
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
};

function detectFormat(name: string): string {
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

if INSERT_AFTER in src:
    src = src.replace(INSERT_AFTER, INSERT_AFTER + FORMAT_BLOCK, 1)
    changes += 1
    print("OK: FORMAT constants + detectFormat added")
else:
    print("WARN: TYPE_COLORS end pattern not found")

# 3. Add selectedFormat state
old3 = '  const [isDesktop, setIsDesktop] = useState(false);'
new3 = '  const [selectedFormat, setSelectedFormat] = useState<string>("all");\n  const [isDesktop, setIsDesktop] = useState(false);'
if old3 in src:
    src = src.replace(old3, new3, 1)
    changes += 1
    print("OK: selectedFormat state added")

# 4. Replace allFiles filter to add format counts + format filter
old4 = '  const allFiles = matchingFiles.filter((file) => selectedType === "all" || file.type === selectedType);'
new4 = '''  const formatCounts = matchingFiles.reduce<Record<string, number>>((acc, file) => {
    const fmt = file.format || detectFormat(file.name);
    acc[fmt] = (acc[fmt] || 0) + 1;
    return acc;
  }, {});

  const availableFormats = Object.keys(formatCounts).sort((a, b) => {
    const labelA = FORMAT_LABELS[a] || a;
    const labelB = FORMAT_LABELS[b] || b;
    return labelA.localeCompare(labelB, "ko");
  });

  const allFiles = matchingFiles
    .filter((file) => selectedType === "all" || file.type === selectedType)
    .filter((file) => selectedFormat === "all" || (file.format || detectFormat(file.name)) === selectedFormat);'''
if old4 in src:
    src = src.replace(old4, new4, 1)
    changes += 1
    print("OK: formatCounts + allFiles filter updated")

# 5. Add format filter UI row after type filter row
old5 = '''                {availableTypes.map((type) => (
                  <button
                    key={type}
                    onClick={() => setSelectedType(type)}
                    className="px-2.5 py-1 text-xs rounded-lg transition-colors"
                    style={
                      selectedType === type
                        ? { background: "var(--accent)", color: "#fff" }
                        : { border: "1px solid var(--border)", color: "var(--text-secondary)", background: "transparent" }
                    }
                  >
                    {TYPE_LABELS[type] || type} {typeCounts[type] || 0}
                  </button>
                ))}
              </div>'''

new5 = '''                {availableTypes.map((type) => (
                  <button
                    key={type}
                    onClick={() => setSelectedType(type)}
                    className="px-2.5 py-1 text-xs rounded-lg transition-colors"
                    style={
                      selectedType === type
                        ? { background: "var(--accent)", color: "#fff" }
                        : { border: "1px solid var(--border)", color: "var(--text-secondary)", background: "transparent" }
                    }
                  >
                    {TYPE_LABELS[type] || type} {typeCounts[type] || 0}
                  </button>
                ))}
              </div>

              <div className="flex gap-2 flex-wrap">
                <button
                  onClick={() => setSelectedFormat("all")}
                  className="px-2.5 py-1 text-xs rounded-lg transition-colors"
                  style={
                    selectedFormat === "all"
                      ? { background: "var(--accent)", color: "#fff" }
                      : { border: "1px solid var(--border)", color: "var(--text-secondary)", background: "transparent" }
                  }
                >
                  \\uc804\\uccb4 \\ud3ec\\ub9f7
                </button>
                {availableFormats.map((fmt) => (
                  <button
                    key={fmt}
                    onClick={() => setSelectedFormat(fmt)}
                    className="px-2.5 py-1 text-xs rounded-lg transition-colors"
                    style={
                      selectedFormat === fmt
                        ? { background: "var(--accent)", color: "#fff" }
                        : { border: "1px solid var(--border)", color: "var(--text-secondary)", background: "transparent" }
                    }
                  >
                    {FORMAT_LABELS[fmt] || fmt} {formatCounts[fmt] || 0}
                  </button>
                ))}
              </div>'''

if old5 in src:
    src = src.replace(old5, new5, 1)
    changes += 1
    print("OK: format filter UI added")
else:
    print("WARN: type filter pattern not found for UI insertion")

# 6. Add format badge in file list items (after type badge)
old6 = '''                        <span className={`text-xs px-1.5 py-0.5 rounded ${TYPE_COLORS[file.type] || "bg-gray-700 text-gray-200"}`}>
                          {TYPE_LABELS[file.type] || file.type}
                        </span>'''
new6 = '''                        <span className={`text-xs px-1.5 py-0.5 rounded ${TYPE_COLORS[file.type] || "bg-gray-700 text-gray-200"}`}>
                          {TYPE_LABELS[file.type] || file.type}
                        </span>
                        <span className={`text-xs px-1.5 py-0.5 rounded ${FORMAT_COLORS[file.format || detectFormat(file.name)] || "bg-gray-700 text-gray-200"}`}>
                          {FORMAT_LABELS[file.format || detectFormat(file.name)] || detectFormat(file.name)}
                        </span>'''
if old6 in src:
    src = src.replace(old6, new6, 1)
    changes += 1
    print("OK: format badge added to file list")

# 7. Add format badge in detail panel (after type badge)
old7 = '''                  <span className={`text-xs px-1.5 py-0.5 rounded ${TYPE_COLORS[selectedFile.type] || "bg-gray-700 text-gray-200"}`}>
                    {TYPE_LABELS[selectedFile.type] || selectedFile.type}
                  </span>'''
new7 = '''                  <span className={`text-xs px-1.5 py-0.5 rounded ${TYPE_COLORS[selectedFile.type] || "bg-gray-700 text-gray-200"}`}>
                    {TYPE_LABELS[selectedFile.type] || selectedFile.type}
                  </span>
                  <span className={`text-xs px-1.5 py-0.5 rounded ${FORMAT_COLORS[selectedFile.format || detectFormat(selectedFile.name)] || "bg-gray-700 text-gray-200"}`}>
                    {FORMAT_LABELS[selectedFile.format || detectFormat(selectedFile.name)] || detectFormat(selectedFile.name)}
                  </span>'''
if old7 in src:
    src = src.replace(old7, new7, 1)
    changes += 1
    print("OK: format badge added to detail panel")

# 8. JSON pretty-print in content rendering
old8 = '''                  ) : (
                    <pre className="text-sm whitespace-pre-wrap break-words" style={{ color: "var(--text-primary)" }}>
                      {fileContent}
                    </pre>
                  )'''
new8 = '''                  ) : selectedFile.name.endsWith(".json") ? (
                    <pre className="text-sm whitespace-pre-wrap break-words" style={{ color: "var(--text-primary)" }}>
                      {(() => { try { return JSON.stringify(JSON.parse(fileContent), null, 2); } catch { return fileContent; } })()}
                    </pre>
                  ) : (
                    <pre className="text-sm whitespace-pre-wrap break-words" style={{ color: "var(--text-primary)" }}>
                      {fileContent}
                    </pre>
                  )'''
if old8 in src:
    src = src.replace(old8, new8, 1)
    changes += 1
    print("OK: JSON pretty-print added")

with open(SRC, "w") as f:
    f.write(src)

print(f"\nDONE: {changes} patches applied to {SRC}")
