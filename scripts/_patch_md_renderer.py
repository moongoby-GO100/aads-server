import shutil

path = '/root/aads/aads-dashboard/src/app/chat/MarkdownRenderer.tsx'
shutil.copy(path, path + '.bak_aads')

content = r'''"use client";
import React, { useState } from "react";

// AADS Markdown Renderer — extracted from page.tsx (Phase 2)

const isSafeUrl = (url: string) => {
  const u = url.trim().toLowerCase();
  return !u.startsWith("javascript:") && !u.startsWith("data:") && !u.startsWith("vbscript:");
};

function processInline(text: string, opts?: { linkColor?: string }): React.ReactNode {
  const _lc = opts?.linkColor || "var(--ct-accent)";
  // Split by inline code first
  const codeParts = text.split(/(`[^`\n]+`)/g);
  return codeParts.map((part, i) => {
    if (part.startsWith("`") && part.endsWith("`") && part.length > 2) {
      return (
        <code
          key={i}
          style={{
            background: "var(--ct-code)",
            padding: "2px 6px",
            borderRadius: "4px",
            fontFamily: "monospace",
            fontSize: "90%",
          }}
        >
          {part.slice(1, -1)}
        </code>
      );
    }
    // Split by images ![alt](url) first, then by links
    const imgSplitParts = part.split(/(!\[[^\]]*\]\([^)]+\))/g);
    return imgSplitParts.flatMap((imgPart: string, ii: number) => {
      const imgMatch = imgPart.match(/^!\[([^\]]*)\]\(([^)]+)\)$/);
      if (imgMatch && isSafeUrl(imgMatch[2])) {
        return (
          <img
            key={`${i}-img${ii}`}
            src={imgMatch[2]}
            alt={imgMatch[1]}
            style={{
              maxWidth: "100%",
              borderRadius: "8px",
              marginTop: "8px",
              marginBottom: "8px",
              display: "block",
            }}
          />
        );
      }
      // Split by links [text](url) — but not images ![alt](url)
      const linkParts = imgPart.split(/(?<!!)\[([^\]]+)\]\(([^)]+)\)/g);
      // linkParts: [before, text, url, after, text, url, ...]
      const withLinks: React.ReactNode[] = [];
      for (let li = 0; li < linkParts.length; li += 3) {
        const seg = linkParts[li] || "";
        if (seg) withLinks.push(<span key={`${i}-${ii}-l${li}`}>{seg}</span>);
        if (li + 2 < linkParts.length) {
          const linkUrl = linkParts[li + 2];
          withLinks.push(
            isSafeUrl(linkUrl) ? (
              <a
                key={`${i}-${ii}-a${li}`}
                href={linkUrl}
                target="_blank"
                rel="noopener noreferrer"
                style={{ color: _lc, textDecoration: "underline" }}
              >
                {linkParts[li + 1]}
              </a>
            ) : (
              <span key={`${i}-${ii}-a${li}`}>{linkParts[li + 1]}</span>
            )
          );
        }
      }
      // Now process bold + plain URLs within each text span
      return withLinks.map((node, wi) => {
        if (typeof node === "object" && node !== null && (node as any).type === "a") return node;
        const raw = typeof node === "string" ? node : ((node as any)?.props?.children ?? "");
        if (typeof raw !== "string" || !raw) return node;
        // Step 1: bold split
        const boldParts = raw.split(/(\*\*[^*\n]+\*\*)/g);
        return boldParts.flatMap((bp: string, j: number) => {
          if (bp.startsWith("**") && bp.endsWith("**")) {
            return <strong key={`${i}-${ii}-${wi}-${j}`}>{bp.slice(2, -2)}</strong>;
          }
          // Step 2: plain URL detection within non-bold text
          const urlRegex = /(https?:\/\/[^\s<>"')\]},;]+)/g;
          const urlParts = bp.split(urlRegex);
          return urlParts.map((up: string, k: number) => {
            if (up.match(/^https?:\/\//) && isSafeUrl(up)) {
              // Remove trailing punctuation that's likely not part of URL
              const cleaned = up.replace(/[.),:;!?]+$/, "");
              const trailing = up.slice(cleaned.length);
              return (
                <span key={`${i}-${ii}-${wi}-${j}-${k}`}>
                  <a
                    href={cleaned}
                    target="_blank"
                    rel="noopener noreferrer"
                    style={{ color: _lc, textDecoration: "underline", wordBreak: "break-all" }}
                  >
                    {cleaned}
                  </a>
                  {trailing}
                </span>
              );
            }
            return <span key={`${i}-${ii}-${wi}-${j}-${k}`}>{up}</span>;
          });
        });
      });
    });
  });
}

function InlineMd({ text, linkColor }: { text: string; linkColor?: string }) {
  const lines = text.split("\n");

  // Group consecutive lines starting with | into table blocks
  const blocks: { type: "lines" | "table"; rows: string[] }[] = [];
  let i = 0;
  while (i < lines.length) {
    if (lines[i].trimStart().startsWith("|")) {
      const tableRows: string[] = [];
      while (i < lines.length && lines[i].trimStart().startsWith("|")) {
        tableRows.push(lines[i]);
        i++;
      }
      blocks.push({ type: "table", rows: tableRows });
    } else {
      if (blocks.length === 0 || blocks[blocks.length - 1].type !== "lines") {
        blocks.push({ type: "lines", rows: [] });
      }
      blocks[blocks.length - 1].rows.push(lines[i]);
      i++;
    }
  }

  const parseTableCells = (row: string) =>
    row.split("|").slice(1, -1).map((c) => c.trim());

  const isSeparatorRow = (row: string) =>
    /^\|[\s:]*-{2,}[\s:]*(\|[\s:]*-{2,}[\s:]*)*\|?\s*$/.test(row.trim());

  const renderLine = (line: string, li: number, total: number) => {
    // Images: ![alt](url)
    if (line.trim().match(/^!\[([^\]]*)\]\(([^)]+)\)\s*$/)) {
      const m = line.trim().match(/^!\[([^\]]*)\]\(([^)]+)\)/);
      if (m && isSafeUrl(m[2])) {
        return (
          <img
            key={li}
            src={m[2]}
            alt={m[1]}
            style={{
              maxWidth: "100%",
              borderRadius: "8px",
              marginTop: "8px",
              marginBottom: "8px",
              display: "block",
            }}
          />
        );
      }
    }
    if (line.startsWith("### "))
      return (
        <div
          key={li}
          style={{ fontWeight: 700, fontSize: "14px", marginTop: "10px", marginBottom: "4px" }}
        >
          {processInline(line.slice(4), { linkColor })}
        </div>
      );
    if (line.startsWith("## "))
      return (
        <div
          key={li}
          style={{ fontWeight: 700, fontSize: "15px", marginTop: "12px", marginBottom: "6px" }}
        >
          {processInline(line.slice(3), { linkColor })}
        </div>
      );
    if (line.startsWith("# "))
      return (
        <div
          key={li}
          style={{ fontWeight: 700, fontSize: "17px", marginTop: "14px", marginBottom: "8px" }}
        >
          {processInline(line.slice(2), { linkColor })}
        </div>
      );
    if (line.match(/^[-*] /))
      return (
        <div key={li} style={{ paddingLeft: "16px", display: "flex", gap: "6px" }}>
          <span>•</span>
          <span>{processInline(line.slice(2), { linkColor })}</span>
        </div>
      );
    if (line.match(/^\d+\. /))
      return (
        <div key={li} style={{ paddingLeft: "16px" }}>
          {processInline(line, { linkColor })}
        </div>
      );
    return (
      <span key={li}>
        {processInline(line, { linkColor })}
        {li < total - 1 && <br />}
      </span>
    );
  };

  let lineIdx = 0;
  return (
    <>
      {blocks.map((block, bi) => {
        if (block.type === "table") {
          const rows = block.rows;
          // Determine header: first row is header if second row is separator
          const hasHeader = rows.length >= 2 && isSeparatorRow(rows[1]);
          const headerCells = hasHeader ? parseTableCells(rows[0]) : null;
          const dataRows = hasHeader ? rows.slice(2) : rows.filter((r) => !isSeparatorRow(r));
          lineIdx += rows.length;
          const cellStyle: React.CSSProperties = {
            padding: "6px 12px",
            border: "1px solid var(--ct-border)",
            textAlign: "left" as const,
          };
          return (
            <div key={`tbl-${bi}`} style={{ overflowX: "auto", margin: "8px 0" }}>
              <table
                style={{
                  borderCollapse: "collapse",
                  width: "100%",
                  fontSize: "13px",
                }}
              >
                {headerCells && (
                  <thead>
                    <tr>
                      {headerCells.map((cell, ci) => (
                        <th
                          key={ci}
                          style={{
                            ...cellStyle,
                            fontWeight: 700,
                            background: "var(--ct-code)",
                          }}
                        >
                          {processInline(cell, { linkColor })}
                        </th>
                      ))}
                    </tr>
                  </thead>
                )}
                <tbody>
                  {dataRows.map((row, ri) => (
                    <tr key={ri}>
                      {parseTableCells(row).map((cell, ci) => (
                        <td key={ci} style={cellStyle}>
                          {processInline(cell, { linkColor })}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          );
        }
        // Regular lines
        const rendered = block.rows.map((line) => {
          const result = renderLine(line, lineIdx, lines.length);
          lineIdx++;
          return result;
        });
        return <span key={`blk-${bi}`}>{rendered}</span>;
      })}
    </>
  );
}

function CopyableCodeBlock({ lang, code }: { lang: string; code: string }) {
  const [copied, setCopied] = useState(false);
  const handleCopy = () => {
    navigator.clipboard.writeText(code.trimEnd()).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    });
  };

  // HTML 코드블록 → iframe 미리보기 렌더링
  if (lang === "html") {
    return (
      <div style={{ marginTop: "12px", borderRadius: "12px", overflow: "hidden", border: "1px solid #2d3148" }}>
        <div style={{ background: "#1e2130", padding: "6px 14px", fontSize: "0.75rem", color: "#a855f7", fontWeight: 600, display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <span>🎨 미리보기</span>
          <button
            onClick={handleCopy}
            style={{
              padding: "2px 8px", fontSize: "11px", borderRadius: "4px", cursor: "pointer",
              border: "1px solid #2d3148", fontFamily: "sans-serif",
              background: copied ? "#22c55e" : "#1e2130",
              color: copied ? "#fff" : "#a855f7",
              transition: "all 0.2s",
            }}
          >
            {copied ? "복사됨" : "복사"}
          </button>
        </div>
        <iframe
          srcDoc={code}
          style={{ width: "100%", height: "540px", border: "none", background: "#fff" }}
          sandbox=""
          title="HTML Preview"
        />
      </div>
    );
  }

  return (
    <div style={{ position: "relative", margin: "8px 0" }}>
      <button
        onClick={handleCopy}
        style={{
          position: "absolute", top: 6, right: 6, padding: "2px 8px",
          fontSize: "11px", borderRadius: "4px", cursor: "pointer",
          border: "1px solid var(--ct-border)", fontFamily: "sans-serif",
          background: copied ? "#22c55e" : "var(--ct-card)",
          color: copied ? "#fff" : "var(--ct-text2)",
          transition: "all 0.2s",
        }}
      >
        {copied ? "복사됨" : "복사"}
      </button>
      <pre
        style={{
          background: "var(--ct-code)", padding: "12px", borderRadius: "8px",
          overflowX: "auto", fontSize: "12px", fontFamily: "monospace",
          whiteSpace: "pre-wrap", wordBreak: "break-word",
          border: "1px solid var(--ct-border)",
        }}
      >
        {lang && (
          <div style={{ color: "var(--ct-text2)", fontSize: "10px", marginBottom: "6px", fontFamily: "sans-serif" }}>
            {lang}
          </div>
        )}
        <code>{code}</code>
      </pre>
    </div>
  );
}

function MarkdownBlock({ text, linkColor }: { text: string; linkColor?: string }) {
  const parts = text.split(/(```[\s\S]*?```)/g);
  return (
    <div>
      {parts.map((part, i) => {
        if (part.startsWith("```")) {
          const firstNl = part.indexOf("\n");
          const lang = firstNl > 3 ? part.slice(3, firstNl).trim() : "";
          const code = firstNl >= 0 ? part.slice(firstNl + 1).replace(/```$/, "") : part.slice(3).replace(/```$/, "");
          return <CopyableCodeBlock key={i} lang={lang} code={code} />;
        }
        return <InlineMd key={i} text={part} linkColor={linkColor} />;
      })}
    </div>
  );
}



export { processInline, InlineMd, CopyableCodeBlock, MarkdownBlock };
'''

with open(path, 'w') as f:
    f.write(content)

print('File written.')
with open(path, 'r') as f:
    written = f.read()

checks = [
    ('line.trim().match', 'Change 1: line.trim() in renderLine'),
    ('imgSplitParts', 'Change 2: imgSplitParts variable'),
    ('imgPart.split', 'Change 2: imgPart used for link split'),
    ('i}-img${ii}', 'Change 2: img key with ii'),
    ('${i}-${ii}-l${li}', 'Change 2: link key with ii'),
]
all_ok = True
for needle, label in checks:
    if needle in written:
        print(f'  PASS: {label}')
    else:
        print(f'  FAIL: {label}')
        all_ok = False

if all_ok:
    print('All checks passed.')
else:
    print('Some checks failed.')
