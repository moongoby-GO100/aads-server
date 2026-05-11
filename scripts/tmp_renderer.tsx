"use client";
import React, { useState, useMemo } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import InlineChart from "@/components/chat/InlineChart";

const isSafeUrl = (url: string) => {
  const u = url.trim().toLowerCase();
  return !u.startsWith("javascript:") && !u.startsWith("data:") && !u.startsWith("vbscript:");
};

function CopyableCodeBlock({ lang, code }: { lang: string; code: string }) {
  const [copied, setCopied] = useState(false);
  const handleCopy = () => {
    navigator.clipboard.writeText(code.trimEnd()).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    });
  };

  if (lang === "html") {
    return (
      <div style={{ marginTop: "12px", borderRadius: "12px", overflow: "hidden", border: "1px solid #2d3148" }}>
        <div style={{
          background: "#1e2130", padding: "6px 14px", fontSize: "0.75rem",
          color: "#a855f7", fontWeight: 600, display: "flex",
          justifyContent: "space-between", alignItems: "center",
        }}>
          <span>🎨 미리보기</span>
          <button onClick={handleCopy} style={{
            padding: "2px 8px", fontSize: "11px", borderRadius: "4px",
            cursor: "pointer", border: "1px solid #2d3148", fontFamily: "sans-serif",
            background: copied ? "#22c55e" : "#1e2130",
            color: copied ? "#fff" : "#a855f7", transition: "all 0.2s",
          }}>
            {copied ? "복사됨" : "복사"}
          </button>
        </div>
        <iframe srcDoc={code} style={{ width: "100%", height: "540px", border: "none", background: "#fff" }}
          sandbox="" title="HTML Preview" />
      </div>
    );
  }

  return (
    <div style={{ position: "relative", margin: "8px 0" }}>
      <button onClick={handleCopy} style={{
        position: "absolute", top: 6, right: 6, padding: "2px 8px",
        fontSize: "11px", borderRadius: "4px", cursor: "pointer",
        border: "1px solid var(--ct-border)", fontFamily: "sans-serif",
        background: copied ? "#22c55e" : "var(--ct-card)",
        color: copied ? "#fff" : "var(--ct-text2)", transition: "all 0.2s",
      }}>
        {copied ? "복사됨" : "복사"}
      </button>
      <pre style={{
        background: "var(--ct-code)", padding: "12px", borderRadius: "8px",
        overflowX: "auto", fontSize: "12px", fontFamily: "monospace",
        whiteSpace: "pre-wrap", wordBreak: "break-word",
        border: "1px solid var(--ct-border)",
      }}>
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

function processInline(text: string, opts?: { linkColor?: string }): React.ReactNode {
  const _lc = opts?.linkColor || "var(--ct-accent)";
  const codeParts = text.split(/(`[^`\n]+`)/g);
  return codeParts.map((part, i) => {
    if (part.startsWith("`") && part.endsWith("`") && part.length > 2) {
      return (
        <code key={i} style={{
          background: "var(--ct-code)", padding: "2px 6px",
          borderRadius: "4px", fontFamily: "monospace", fontSize: "90%",
        }}>
          {part.slice(1, -1)}
        </code>
      );
    }
    const linkParts = part.split(/(?<!!)\[([^\]]+)\]\(([^)]+)\)/g);
    const withLinks: React.ReactNode[] = [];
    for (let li = 0; li < linkParts.length; li += 3) {
      const seg = linkParts[li] || "";
      if (seg) withLinks.push(<span key={`${i}-l${li}`}>{seg}</span>);
      if (li + 2 < linkParts.length) {
        const linkUrl = linkParts[li + 2];
        withLinks.push(
          isSafeUrl(linkUrl) ? (
            <a key={`${i}-a${li}`} href={linkUrl} target="_blank"
              rel="noopener noreferrer" style={{ color: _lc, textDecoration: "underline" }}>
              {linkParts[li + 1]}
            </a>
          ) : (
            <span key={`${i}-a${li}`}>{linkParts[li + 1]}</span>
          )
        );
      }
    }
    return withLinks.map((node, wi) => {
      if (typeof node === "object" && node !== null && (node as any).type === "a") return node;
      const raw = typeof node === "string" ? node : ((node as any)?.props?.children ?? "");
      if (typeof raw !== "string" || !raw) return node;
      const strikeParts = raw.split(/(~~[^~\n]+~~)/g);
      return strikeParts.flatMap((sp: string, si: number) => {
        if (sp.startsWith("~~") && sp.endsWith("~~")) {
          return <del key={`${i}-${wi}-s${si}`} style={{ textDecoration: "line-through", color: "var(--ct-text2)" }}>{sp.slice(2, -2)}</del>;
        }
        const boldParts = sp.split(/(\*\*[^*\n]+\*\*)/g);
        return boldParts.flatMap((bp: string, j: number) => {
          if (bp.startsWith("**") && bp.endsWith("**")) {
            return <strong key={`${i}-${wi}-${j}`}>{bp.slice(2, -2)}</strong>;
          }
          const italicParts = bp.split(/(?<!\*)\*([^*\n]+)\*(?!\*)/g);
          return italicParts.flatMap((ip: string, k: number) => {
            if (k % 2 === 1) {
              return <em key={`${i}-${wi}-${j}-i${k}`}>{ip}</em>;
            }
            const urlRegex = /(https?:\/\/[^\s<>"')\]},;]+)/g;
            const urlParts = ip.split(urlRegex);
            return urlParts.map((up: string, m: number) => {
              if (up.match(/^https?:\/\//) && isSafeUrl(up)) {
                const cleaned = up.replace(/[.),:;!?]+$/, "");
                const trailing = up.slice(cleaned.length);
                return (
                  <span key={`${i}-${wi}-${j}-${k}-${m}`}>
                    <a href={cleaned} target="_blank" rel="noopener noreferrer"
                      style={{ color: _lc, textDecoration: "underline", wordBreak: "break-all" as const }}>
                      {cleaned}
                    </a>
                    {trailing}
                  </span>
                );
              }
              return <span key={`${i}-${wi}-${j}-${k}-${m}`}>{up}</span>;
            });
          });
        });
      });
    });
  });
}

function InlineMd({ text, linkColor }: { text: string; linkColor?: string }) {
  return <MarkdownBlock text={text} linkColor={linkColor} />;
}

function MarkdownBlock({ text, linkColor }: { text: string; linkColor?: string }) {
  const _lc = linkColor || "var(--ct-accent)";

  const components = useMemo(() => ({
    pre: (props: any) => <>{props.children}</>,

    code: (props: any) => {
      const { className, children, node, ref: _ref, ...rest } = props;
      const match = /language-(\w+)/.exec(className || "");
      const codeStr = String(children).replace(/\n$/, "");

      if (match) {
        const lang = match[1];
        if (lang === "chart") return <InlineChart raw={codeStr} />;
        return <CopyableCodeBlock lang={lang} code={codeStr} />;
      }

      if (codeStr.includes("\n") && codeStr.length > 40) {
        return <CopyableCodeBlock lang="" code={codeStr} />;
      }

      return (
        <code style={{
          background: "var(--ct-code)", padding: "2px 6px",
          borderRadius: "4px", fontFamily: "monospace", fontSize: "90%",
        }} {...rest}>
          {children}
        </code>
      );
    },

    table: (props: any) => (
      <div style={{ overflowX: "auto" as const, margin: "8px 0" }}>
        <table style={{ borderCollapse: "collapse" as const, width: "100%", fontSize: "13px" }}>
          {props.children}
        </table>
      </div>
    ),
    thead: (props: any) => <thead>{props.children}</thead>,
    tbody: (props: any) => <tbody>{props.children}</tbody>,
    tr: (props: any) => <tr>{props.children}</tr>,
    th: (props: any) => (
      <th style={{
        padding: "6px 12px", border: "1px solid var(--ct-border)",
        fontWeight: 700, background: "var(--ct-code)",
        textAlign: props.style?.textAlign || "left",
      }}>
        {props.children}
      </th>
    ),
    td: (props: any) => (
      <td style={{
        padding: "6px 12px", border: "1px solid var(--ct-border)",
        textAlign: props.style?.textAlign || "left",
      }}>
        {props.children}
      </td>
    ),

    a: (props: any) => {
      if (!props.href || !isSafeUrl(props.href)) return <span>{props.children}</span>;
      return (
        <a href={props.href} target="_blank" rel="noopener noreferrer"
          style={{ color: _lc, textDecoration: "underline", wordBreak: "break-all" as const }}>
          {props.children}
        </a>
      );
    },

    img: (props: any) => {
      if (!props.src || !isSafeUrl(props.src)) return null;
      return (
        <img src={props.src} alt={props.alt || ""} style={{
          maxWidth: "100%", borderRadius: "8px",
          marginTop: "8px", marginBottom: "8px", display: "block" as const,
        }} />
      );
    },

    h1: (props: any) => <div style={{ fontWeight: 700, fontSize: "17px", marginTop: "14px", marginBottom: "8px" }}>{props.children}</div>,
    h2: (props: any) => <div style={{ fontWeight: 700, fontSize: "15px", marginTop: "12px", marginBottom: "6px" }}>{props.children}</div>,
    h3: (props: any) => <div style={{ fontWeight: 700, fontSize: "14px", marginTop: "10px", marginBottom: "4px" }}>{props.children}</div>,
    h4: (props: any) => <div style={{ fontWeight: 600, fontSize: "13px", marginTop: "8px", marginBottom: "4px" }}>{props.children}</div>,

    ul: (props: any) => <ul style={{ paddingLeft: "20px", margin: "4px 0", listStyleType: "disc" as const }}>{props.children}</ul>,
    ol: (props: any) => <ol style={{ paddingLeft: "20px", margin: "4px 0", listStyleType: "decimal" as const }}>{props.children}</ol>,
    li: (props: any) => <li style={{ marginBottom: "2px" }}>{props.children}</li>,

    blockquote: (props: any) => (
      <div style={{
        borderLeft: "3px solid var(--ct-accent)", paddingLeft: "12px",
        margin: "8px 0", color: "var(--ct-text2)", fontStyle: "italic" as const,
      }}>
        {props.children}
      </div>
    ),

    hr: () => <hr style={{ border: "none", borderTop: "1px solid var(--ct-border)", margin: "12px 0" }} />,
    p: (props: any) => <div style={{ margin: "4px 0" }}>{props.children}</div>,

    input: (props: any) => {
      if (props.type === "checkbox") {
        return <input type="checkbox" checked={props.checked} readOnly
          style={{ marginRight: "6px", accentColor: "var(--ct-accent)" }} />;
      }
      return <input type={props.type} />;
    },

    del: (props: any) => <del style={{ textDecoration: "line-through", color: "var(--ct-text2)" }}>{props.children}</del>,
    strong: (props: any) => <strong>{props.children}</strong>,
    em: (props: any) => <em>{props.children}</em>,
  }), [_lc]);

  return (
    <div>
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {text}
      </ReactMarkdown>
    </div>
  );
}

export { processInline, InlineMd, CopyableCodeBlock, MarkdownBlock };
