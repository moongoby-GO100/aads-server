#!/usr/bin/env python3
"""Patch page.tsx: replace tools_called simple list with collapsible rich preview UI."""

import re

FILE = "/root/aads/aads-dashboard/src/app/chat/page.tsx"

OLD = r"""{msg.tools_called && Array.isArray(msg.tools_called) && msg.tools_called.length > 0 && (
                <div style={{marginBottom: '8px', padding: '8px', borderRadius: '8px', background: 'rgba(255,255,255,0.05)', fontSize: '0.85em'}}>
                  {msg.tools_called.map((ev: any, i: number) => (
                    <div key={i} style={{padding: '2px 0', display: 'flex', alignItems: 'center', gap: '6px'}}>
                      {ev.type === 'tool_use' && (
                        <>
                          <span>🔧</span>
                          <span style={{fontWeight: 600}}>{ev.tool_name}</span>
                          <span style={{opacity: 0.6, fontSize: '0.9em'}}>호출</span>
                        </>
                      )}
                      {ev.type === 'tool_result' && (
                        <>
                          <span>✅</span>
                          <span style={{fontWeight: 600}}>{ev.tool_name}</span>
                          <span style={{opacity: 0.6, fontSize: '0.9em'}}>완료</span>
                        </>
                      )}
                      {ev.type === 'thinking' && (
                        <>
                          <span>💭</span>
                          <span style={{opacity: 0.7}}>{typeof ev.content === 'string' ? ev.content.slice(0, 100) : ''}</span>
                        </>
                      )}
                    </div>
                  ))}
                </div>
              )}"""

NEW = r"""{msg.tools_called && Array.isArray(msg.tools_called) && msg.tools_called.length > 0 && (() => {
                const toolIcons: Record<string, string> = {
                  read_remote_file: "📄", read_github_file: "📄", list_remote_dir: "📁",
                  write_remote_file: "✏️", patch_remote_file: "✏️",
                  run_remote_command: "⚡", query_database: "🗄️", query_project_database: "🗄️",
                  web_search: "🔍", web_search_brave: "🔍", search_naver: "🔍", search_kakao: "🔍",
                  jina_read: "🌐", crawl4ai_fetch: "🌐", deep_crawl: "🌐", deep_research: "🔬",
                  health_check: "💊", get_all_service_status: "📊",
                  pipeline_c_start: "🚀", delegate_to_agent: "🤖",
                  save_note: "📝", recall_notes: "🧠", generate_image: "🎨",
                  send_telegram: "📨", fact_check: "🔎", evaluate_alerts: "🔔",
                };
                const getIcon = (name: string) => toolIcons[name] || "🔧";
                const getParam = (inp: any) => {
                  if (!inp || typeof inp !== 'object') return '';
                  const v = inp.path || inp.query || inp.url || inp.command || inp.file_path || inp.task || inp.project
                    || (Object.values(inp).filter((x: unknown) => typeof x === 'string')[0] as string) || '';
                  return String(v).slice(0, 80);
                };
                const toolUseCount = msg.tools_called!.filter((e: any) => e.type === 'tool_use').length;
                const lastEvent = [...msg.tools_called!].reverse().find((e: any) => e.type === 'tool_use' || e.type === 'tool_result');
                return (
                  <details style={{marginBottom: '8px'}}>
                    <summary style={{
                      cursor: 'pointer', fontSize: '12px', padding: '6px 10px',
                      borderRadius: '8px', background: 'rgba(108,99,255,0.06)',
                      border: '1px solid rgba(108,99,255,0.2)',
                      display: 'flex', alignItems: 'center', gap: '6px',
                      listStyle: 'none', userSelect: 'none' as const,
                    }}>
                      <span style={{fontSize: '10px', opacity: 0.6, transition: 'transform 0.2s'}}>▶</span>
                      <span style={{fontWeight: 500, color: 'var(--ct-accent)'}}>도구 {toolUseCount}개 사용</span>
                      {lastEvent && (
                        <span style={{opacity: 0.6, fontSize: '11px', marginLeft: '4px'}}>
                          — {lastEvent.type === 'tool_result' ? '✅' : getIcon(lastEvent.tool_name)} {lastEvent.tool_name}
                        </span>
                      )}
                    </summary>
                    <div style={{
                      padding: '8px 10px', marginTop: '4px',
                      borderRadius: '8px', background: 'rgba(108,99,255,0.06)',
                      border: '1px solid rgba(108,99,255,0.2)',
                      fontSize: '12px', maxHeight: '240px', overflowY: 'auto',
                    }}>
                      {msg.tools_called!.map((ev: any, i: number) => (
                        <div key={i} style={{marginBottom: '4px'}}>
                          {ev.type === 'tool_use' && (
                            <>
                              <div style={{display: 'flex', alignItems: 'center', gap: '5px', color: 'var(--ct-accent)'}}>
                                <span>{getIcon(ev.tool_name)}</span>
                                <span style={{fontWeight: 500}}>{ev.tool_name} 실행</span>
                              </div>
                              {ev.tool_input && getParam(ev.tool_input) && (
                                <div style={{color: '#888', fontSize: '11px', marginLeft: '18px', fontFamily: 'monospace', wordBreak: 'break-all' as const}}>
                                  {getParam(ev.tool_input)}
                                </div>
                              )}
                            </>
                          )}
                          {ev.type === 'tool_result' && (
                            <>
                              <div style={{display: 'flex', alignItems: 'center', gap: '5px', color: '#4ade80'}}>
                                <span>✅</span>
                                <span style={{fontWeight: 500}}>{ev.tool_name} 완료</span>
                              </div>
                              {ev.content && (
                                <div style={{color: '#888', fontSize: '11px', marginLeft: '18px', fontFamily: 'monospace', wordBreak: 'break-all' as const}}>
                                  {String(ev.content).slice(0, 120).replace(/\n/g, ' ')}
                                </div>
                              )}
                            </>
                          )}
                          {ev.type === 'thinking' && (
                            <div style={{display: 'flex', alignItems: 'center', gap: '5px'}}>
                              <span>💭</span>
                              <span style={{opacity: 0.7, fontSize: '11px'}}>{typeof ev.content === 'string' ? ev.content.slice(0, 100) : ''}</span>
                            </div>
                          )}
                        </div>
                      ))}
                    </div>
                  </details>
                );
              })()}</details>"""

# 실제로는 마지막 </details> 제거 (잘못 붙음)
NEW = NEW.rstrip("</details>")

with open(FILE, "r") as f:
    content = f.read()

if OLD not in content:
    print("ERROR: old string not found in file!")
    import sys; sys.exit(1)

count = content.count(OLD)
if count != 1:
    print(f"ERROR: old string found {count} times, expected 1")
    import sys; sys.exit(1)

content = content.replace(OLD, NEW)

with open(FILE, "w") as f:
    f.write(content)

print(f"OK: patched tools_called UI in {FILE}")
