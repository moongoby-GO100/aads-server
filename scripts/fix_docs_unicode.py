#!/usr/bin/env python3
"""Fix unicode escapes in docs/page.tsx to use actual Korean characters."""

SRC = "/root/aads/aads-dashboard/src/app/docs/page.tsx"

with open(SRC) as f:
    content = f.read()

replacements = {
    '"\\ud14d\\uc2a4\\ud2b8"': '"텍스트"',
    '"\\uc124\\uc815\\ud30c\\uc77c"': '"설정파일"',
    '"\\ub85c\\uadf8"': '"로그"',
    '"\\uae30\\ud0c0"': '"기타"',
    '\\uc804\\uccb4 \\ud3ec\\ub9f7': '전체 포맷',
}

for old, new in replacements.items():
    if old in content:
        content = content.replace(old, new)
        print(f"OK: {old[:20]}... -> {new}")
    else:
        print(f"SKIP: {old[:20]}... not found")

with open(SRC, "w") as f:
    f.write(content)

print("DONE")
