#!/usr/bin/env python3
"""Dashboard document link fixes - apply patches to 3 files."""
import shutil
import sys
from pathlib import Path

DASH = Path("/root/aads/aads-dashboard")
FIXES = Path("/root/aads/aads-server/scripts")


def backup(path):
    bak = Path(str(path) + ".bak_aads")
    if not bak.exists():
        shutil.copy2(path, bak)


def find_function(lines, name):
    start = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(f"function {name}(") or stripped.startswith(f"function {name} ("):
            start = i
            break
    if start is None:
        raise ValueError(f"Function {name} not found")
    depth = 0
    for i in range(start, len(lines)):
        depth += lines[i].count("{") - lines[i].count("}")
        if depth == 0:
            return start, i
    raise ValueError(f"Could not find end of function {name}")


def find_deeplink_effect(lines):
    start = None
    for i, line in enumerate(lines):
        if "useEffect(" in line:
            block = "\n".join(lines[i:i + 25])
            if "openedDeepLinkRef.current === key" in block:
                start = i
                break
    if start is None:
        raise ValueError("Deep link useEffect not found")
    for i in range(start, len(lines)):
        if "}, [data, openFile]);" in lines[i]:
            return start, i
    raise ValueError("Could not find end of deep link useEffect")


def main():
    errors = []

    # 1. documentLinks.ts - full replacement
    try:
        target = DASH / "src/lib/documentLinks.ts"
        backup(target)
        src = FIXES / "_fix_doclinks.ts"
        content = src.read_text("utf-8")
        target.write_text(content, encoding="utf-8")
        print(f"OK documentLinks.ts ({len(content)} bytes)")
    except Exception as e:
        errors.append(f"documentLinks.ts: {e}")
        print(f"FAIL documentLinks.ts: {e}")

    # 2. MarkdownRenderer.tsx - replace FilePathChip function
    try:
        target = DASH / "src/app/chat/MarkdownRenderer.tsx"
        backup(target)
        lines = target.read_text("utf-8").splitlines()
        s, e = find_function(lines, "FilePathChip")
        new_chip = (FIXES / "_fix_chip.txt").read_text("utf-8").rstrip("\n")
        new_lines = lines[:s] + new_chip.splitlines() + lines[e + 1:]
        result = "\n".join(new_lines) + "\n"
        target.write_text(result, encoding="utf-8")
        print(f"OK MarkdownRenderer.tsx (replaced lines {s+1}-{e+1})")
    except Exception as e:
        errors.append(f"MarkdownRenderer.tsx: {e}")
        print(f"FAIL MarkdownRenderer.tsx: {e}")

    # 3. docs/page.tsx - replace deep link useEffect
    try:
        target = DASH / "src/app/docs/page.tsx"
        backup(target)
        lines = target.read_text("utf-8").splitlines()
        s, e = find_deeplink_effect(lines)
        new_effect = (FIXES / "_fix_deeplink.txt").read_text("utf-8").rstrip("\n")
        new_lines = lines[:s] + new_effect.splitlines() + lines[e + 1:]
        result = "\n".join(new_lines) + "\n"
        target.write_text(result, encoding="utf-8")
        print(f"OK page.tsx (replaced lines {s+1}-{e+1})")
    except Exception as e:
        errors.append(f"page.tsx: {e}")
        print(f"FAIL page.tsx: {e}")

    if errors:
        print(f"\nFAILED: {len(errors)} error(s)")
        for err in errors:
            print(f"  - {err}")
        sys.exit(1)
    print("\nAll 3 patches applied successfully!")


if __name__ == "__main__":
    main()
