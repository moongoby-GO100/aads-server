#!/usr/bin/env python3
"""Recognize top-level TRUNCATE statements for the release migration gate."""
import re
import sys
from pathlib import Path


def has_truncate(sql: str, in_body: bool = False) -> bool:
    # Analyze dollar bodies recursively; retain the legacy checks separately.
    def destructive(statement: str) -> bool:
        text = statement.strip()
        if re.match(r'^TRUNCATE\b', text, re.IGNORECASE):
            return True
        if in_body and re.match(r'^(?:BEGIN|THEN|ELSE|LOOP)\s+TRUNCATE\b', text, re.IGNORECASE):
            return True
        if re.match(r'^CREATE\s+(?:OR\s+REPLACE\s+)?(?:CONSTRAINT\s+)?TRIGGER\b', text, re.IGNORECASE):
            event = re.search(r'\b(?:BEFORE|AFTER|INSTEAD\s+OF)\b(.*?)\bON\b', text, re.IGNORECASE | re.DOTALL)
            if event:
                text = text[:event.start(1)] + re.sub(r'\bTRUNCATE\b', '', event.group(1), flags=re.IGNORECASE) + text[event.end(1):]
            return bool(re.search(r'\bTRUNCATE\b', text, re.IGNORECASE))
        return False
    i = 0
    statement = []
    while i < len(sql):
        if sql.startswith('--', i):
            end = sql.find('\n', i + 2)
            i = len(sql) if end < 0 else end
            statement.append(' ')
        elif sql.startswith('/*', i):
            i += 2
            depth = 1
            while i < len(sql) and depth:
                if sql.startswith('/*', i):
                    depth += 1
                    i += 2
                elif sql.startswith('*/', i):
                    depth -= 1
                    i += 2
                else:
                    i += 1
            statement.append(' ')
        elif sql[i] in "'\"":
            quote = sql[i]
            # Keep an identifier/string placeholder so a quoted first token
            # cannot make a subsequent TRUNCATE appear to start a statement.
            statement.append(' ? ')
            i += 1
            start = i
            while i < len(sql):
                if sql[i] == quote:
                    i += 1
                    if i < len(sql) and sql[i] == quote:
                        i += 1
                        continue
                    break
                if sql[i] == '\\' and quote == "'" and i + 1 < len(sql):
                    i += 2
                else:
                    i += 1
            if in_body and quote == "'" and re.search(r'\bTRUNCATE\b', sql[start:i], re.IGNORECASE):
                return True
        elif sql[i] == '$' and (tag := re.match(r'\$(?:[A-Za-z_][A-Za-z_0-9]*)?\$', sql[i:])):
            delimiter = tag.group()
            end = sql.find(delimiter, i + len(delimiter))
            body_end = len(sql) if end < 0 else end
            if has_truncate(sql[i + len(delimiter):body_end], in_body=True):
                return True
            i = len(sql) if end < 0 else end + len(delimiter)
            statement.append(' ? ')
        elif sql[i] == ';':
            if destructive(''.join(statement)):
                return True
            statement.clear()
            i += 1
        else:
            statement.append(sql[i])
            i += 1
    return destructive(''.join(statement))


if __name__ == '__main__':
    sql = Path(sys.argv[1]).read_text(encoding='utf-8')
    legacy = sys.argv[2]
    reason = legacy or ('TRUNCATE' if has_truncate(sql) else '')
    if reason:
        print(reason)
