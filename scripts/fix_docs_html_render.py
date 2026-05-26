#!/usr/bin/env python3
"""Fix: Render .html files in docs page via iframe instead of plain text."""
import shutil

PAGE = '/root/aads/aads-dashboard/src/app/docs/page.tsx'
shutil.copy2(PAGE, PAGE + '.bak_aads')

with open(PAGE) as f:
    c = f.read()

# Replace the <pre> fallback to add an HTML iframe branch before it
old = '''                  ) : (
                    <pre className="text-sm whitespace-pre-wrap break-words" style={{ color: "var(--text-primary)" }}>
                      {fileContent}
                    </pre>
                  )'''

new = '''                  ) : selectedFile.name.endsWith(".html") || selectedFile.name.endsWith(".htm") ? (
                    <iframe
                      srcDoc={fileContent}
                      className="w-full border-0 rounded-lg"
                      style={{ background: "#fff", height: "calc(100vh - 200px)" }}
                      title={selectedFile.name}
                    />
                  ) : (
                    <pre className="text-sm whitespace-pre-wrap break-words" style={{ color: "var(--text-primary)" }}>
                      {fileContent}
                    </pre>
                  )'''

if old in c:
    c = c.replace(old, new, 1)
    with open(PAGE, 'w') as f:
        f.write(c)
    print('OK: HTML iframe rendering added to docs page')
else:
    print('ERR: pattern not found')
    # Debug: show what's around the pre tag
    idx = c.find('<pre className="text-sm whitespace-pre-wrap')
    if idx >= 0:
        print(f'Found <pre> at position {idx}')
        print(repr(c[max(0,idx-100):idx+200]))
    else:
        print('No <pre> tag found at all')
