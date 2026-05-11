"""Phase 3: ChatArtifactPanel에 요약 카드(stats bar) 추가"""

TARGET = "/root/aads/aads-dashboard/src/app/chat/ChatArtifactPanel.tsx"

with open(TARGET, "r") as f:
    content = f.read()

if "artifact-summary-stats" in content:
    print("SKIP: 요약 카드 이미 존재")
    exit(0)

# 삽입 지점: 제목 div 닫힘 + 세션 뱃지 뒤, 본문 div 바로 앞
OLD = """                    {activeArtifact.session_id !== activeSession?.id && (
                      <span style={{ fontSize: "10px", color: "#888", marginLeft: "4px" }}>
                        (다른 세션)
                      </span>
                    )}
                    <div style={{ fontSize: "13px", lineHeight: "1.6" }}>"""

SUMMARY_CARD = """                    {activeArtifact.session_id !== activeSession?.id && (
                      <span style={{ fontSize: "10px", color: "#888", marginLeft: "4px" }}>
                        (다른 세션)
                      </span>
                    )}
                    {/* artifact-summary-stats */}
                    {(() => {
                      const c = displayArtifact.content || "";
                      const chars = c.length;
                      const words = c.trim().split(/\\s+/).filter(Boolean).length;
                      const lines = c.split("\\n").length;
                      const sections = (c.match(/^#{1,3}\\s+/gm) || []).length;
                      const tables = (c.match(/^\\|.+\\|$/gm) || []).length;
                      const codeBlocks = Math.floor(((c.match(/```/g) || []).length) / 2);
                      const typeLabel: Record<string, string> = {
                        report: "\\ud83d\\udcc4 \\ubcf4\\uace0\\uc11c",
                        code: "\\ud83d\\udcbb \\ucf54\\ub4dc",
                        chart: "\\ud83d\\udcca \\ucc28\\ud2b8",
                        full_response: "\\ud83d\\udcc4 \\ubcf4\\uace0\\uc11c",
                        table: "\\ud83d\\udcca \\ud45c",
                        image: "\\ud83d\\uddbc\\ufe0f \\uc774\\ubbf8\\uc9c0",
                        file: "\\ud83d\\udcce \\ud30c\\uc77c",
                      };
                      const label = typeLabel[displayArtifact.artifact_type] || "\\ud83d\\udcc4 \\ubb38\\uc11c";
                      if (chars < 10) return null;
                      const stats = [
                        { k: "\\uc720\\ud615", v: label },
                        { k: "\\uae00\\uc790", v: chars.toLocaleString() },
                        { k: "\\ub2e8\\uc5b4", v: words.toLocaleString() },
                        { k: "\\uc904", v: lines.toLocaleString() },
                      ];
                      if (sections > 0) stats.push({ k: "\\uc139\\uc158", v: String(sections) });
                      if (tables > 0) stats.push({ k: "\\ud45c", v: String(tables) });
                      if (codeBlocks > 0) stats.push({ k: "\\ucf54\\ub4dc\\ube14\\ub85d", v: String(codeBlocks) });
                      return (
                        <div style={{
                          display: "flex", alignItems: "center", gap: "12px",
                          padding: "4px 0", marginBottom: "8px",
                          borderBottom: "1px solid var(--ct-border)",
                          overflowX: "auto",
                        }}>
                          {stats.map((s, i) => (
                            <span key={i} style={{ display: "flex", alignItems: "center", gap: "3px", whiteSpace: "nowrap" }}>
                              <span style={{ color: "var(--ct-text-muted)", fontSize: "10px" }}>{s.k}</span>
                              <span style={{ color: "var(--ct-text)", fontSize: "11px", fontWeight: 500 }}>{s.v}</span>
                            </span>
                          ))}
                        </div>
                      );
                    })()}
                    <div style={{ fontSize: "13px", lineHeight: "1.6" }}>"""

if OLD not in content:
    print("ERROR: 삽입 지점을 찾을 수 없습니다")
    exit(1)

content = content.replace(OLD, SUMMARY_CARD, 1)

with open(TARGET, "w") as f:
    f.write(content)

print("OK: 요약 카드(stats bar) 추가 완료")
