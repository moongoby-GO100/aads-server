#!/usr/bin/env python3
"""Patch MarkdownRenderer.tsx to make file paths clickable with copy-to-clipboard."""

FILE = "/root/aads/aads-dashboard/src/app/chat/MarkdownRenderer.tsx"

with open(FILE, "r") as f:
    content = f.read()

# 1. Add file path detection helpers after the imports/constants area
# Find insertion point: before createMarkdownComponents
HELPER_CODE = '''
// ── File path detection ──
const _FILE_EXT_RE = /\\.(py|tsx?|jsx?|json|md|ya?ml|sh|sql|css|html|txt|conf|cfg|ini|toml|log|csv)$/i;
const _FILE_PATH_RE = /^(\\/root\\/|app\\/|src\\/|\\.\\/|\\.\\.\\\/|scripts\\/|tests\\/|docs\\/|components\\/|services\\/|routers\\/|public\\/)/;
const _IMAGE_EXT_RE = /\\.(png|jpe?g|gif|svg|webp|ico|bmp)$/i;
function _isFilePath(t: string) { const s = t.trim(); return _FILE_EXT_RE.test(s) || _FILE_PATH_RE.test(s); }
function _isImagePath(t: string) { return _IMAGE_EXT_RE.test(t.trim()); }

function FilePathChip({ text, children }: { text: string; children: React.ReactNode }) {
  const [copied, setCopied] = React.useState(false);
  const isImg = _isImagePath(text);
  return (
    <code
      onClick={(e) => {
        e.preventDefault();
        e.stopPropagation();
        navigator.clipboard?.writeText(text).then(() => {
          setCopied(true);
          setTimeout(() => setCopied(false), 1500);
        });
      }}
      title={copied ? "\\u2705 \\ubcf5\\uc0ac\\ub428" : "\\ud074\\ub9ad\\ud558\\uc5ec \\ubcf5\\uc0ac"}
      style={{
        background: copied ? "rgba(34,197,94,0.15)" : "rgba(108,99,255,0.12)",
        padding: "2px 6px",
        borderRadius: "4px",
        fontFamily: "monospace",
        fontSize: "90%",
        cursor: "pointer",
        color: copied ? "#22c55e" : "var(--ct-accent)",
        borderBottom: copied ? "1px solid #22c55e" : "1px dashed var(--ct-accent)",
        transition: "all 0.2s",
        userSelect: "none",
      }}
    >
      {isImg ? "\\ud83d\\uddbc" : "\\ud83d\\udcc4"} {copied ? "\\ubcf5\\uc0ac\\ub428" : children}
    </code>
  );
}

'''

# Insert before createMarkdownComponents function
INSERT_ANCHOR = "const createMarkdownComponents = (linkColor?: string, inlineMode = false): Components => ({"
if INSERT_ANCHOR in content:
    content = content.replace(INSERT_ANCHOR, HELPER_CODE + INSERT_ANCHOR, 1)
    print("OK: file path helpers inserted")
else:
    print("WARN: createMarkdownComponents anchor not found")

# 2. Modify inline code rendering to detect file paths
OLD_INLINE = """    if (isInline) {
      return (
        <code
          {...rest}
          style={{
            background: "var(--ct-code)",
            padding: "2px 6px",
            borderRadius: "4px",
            fontFamily: "monospace",
            fontSize: "90%",
          }}
        >
          {children}
        </code>
      );
    }"""

NEW_INLINE = """    if (isInline) {
      const _codeText = String(children ?? "").trim();
      if (_isFilePath(_codeText)) {
        return <FilePathChip text={_codeText}>{children}</FilePathChip>;
      }
      return (
        <code
          {...rest}
          style={{
            background: "var(--ct-code)",
            padding: "2px 6px",
            borderRadius: "4px",
            fontFamily: "monospace",
            fontSize: "90%",
          }}
        >
          {children}
        </code>
      );
    }"""

if OLD_INLINE in content:
    content = content.replace(OLD_INLINE, NEW_INLINE, 1)
    print("OK: inline code file path detection added")
else:
    print("WARN: inline code pattern not found")

with open(FILE, "w") as f:
    f.write(content)

print("DONE: MarkdownRenderer patched")
