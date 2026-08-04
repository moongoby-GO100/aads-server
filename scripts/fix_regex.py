#!/usr/bin/env python3
FILE = "/root/aads/aads-dashboard/src/app/chat/MarkdownRenderer.tsx"
with open(FILE, "r") as f:
    lines = f.readlines()

for i, line in enumerate(lines):
    if "_FILE_PATH_RE" in line and "\\\\" in line:
        # Fix: replace \.\\/ with \.\/ (remove extra backslash)
        fixed = line.replace('\\.\\\\/', '\\.\\/')
        print(f"Line {i+1} BEFORE: {line.rstrip()}")
        print(f"Line {i+1} AFTER:  {fixed.rstrip()}")
        lines[i] = fixed

with open(FILE, "w") as f:
    f.writelines(lines)
print("DONE")
