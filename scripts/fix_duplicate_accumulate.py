"""Remove duplicate _accumulate_experience definition from self_evaluator.py."""

path = "app/services/self_evaluator.py"
with open(path, "r") as f:
    content = f.read()

lines = content.split("\n")

# Find all lines with 'async def _accumulate_experience('
defs = [i for i, line in enumerate(lines) if line.strip().startswith("async def _accumulate_experience(")]
print(f"Found {len(defs)} definitions at lines: {[d+1 for d in defs]}")

if len(defs) == 2:
    # Remove second definition: from the blank line before it to the end of the function
    # Second def starts at defs[1], find blank line before it
    start = defs[1]
    # Go back to find blank lines before the second def
    while start > 0 and lines[start - 1].strip() == "":
        start -= 1
    # Keep one blank line after first def's last line
    start += 1

    # Find end of second definition: next top-level def/class or EOF
    end = len(lines)
    for i in range(defs[1] + 1, len(lines)):
        stripped = lines[i].strip()
        # Top-level definition (not indented)
        if stripped and not lines[i].startswith(" ") and not lines[i].startswith("\t"):
            if stripped.startswith("def ") or stripped.startswith("async def ") or stripped.startswith("class "):
                # Go back to include blank lines before next def
                end = i
                while end > defs[1] and lines[end - 1].strip() == "":
                    end -= 1
                end += 1  # keep one blank line
                break

    print(f"Removing lines {start+1} to {end} (0-indexed: {start}-{end-1})")
    new_lines = lines[:start] + lines[end:]

    with open(path, "w") as f:
        f.write("\n".join(new_lines))

    print(f"Done. {len(lines)} -> {len(new_lines)} lines ({len(lines)-len(new_lines)} removed)")
else:
    print("Expected exactly 2 definitions, skipping.")
