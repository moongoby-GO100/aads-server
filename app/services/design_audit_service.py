"""Read-only code scanner for Open Design Hub Phase 0."""
from __future__ import annotations

import html
import re
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable


ALLOWED_PROJECT_ROOTS: dict[str, Path] = {
    "AADS": Path("/root/aads/aads-dashboard"),
    "AADS_SERVER": Path("/root/aads/aads-server"),
    "GO100": Path("/root/kis-autotrade-v4/go100"),
    "KIS": Path("/root/kis-autotrade-v4"),
    "SF": Path("/data/shortflow"),
    "NTV2": Path("/var/www/newtalk"),
}

DEFAULT_INCLUDE_SUFFIXES = {
    ".css",
    ".html",
    ".js",
    ".jsx",
    ".mdx",
    ".ts",
    ".tsx",
    ".vue",
}

DEFAULT_EXCLUDED_DIRS = {
    ".git",
    ".next",
    ".venv",
    "__pycache__",
    "dist",
    "node_modules",
    "venv",
}

HEX_COLOR_RE = re.compile(r"(?<![\w-])#(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})(?![\w-])")
RGB_COLOR_RE = re.compile(r"\brgba?\(\s*[^)]{3,120}\)", re.IGNORECASE)
TAILWIND_ARBITRARY_COLOR_RE = re.compile(
    r"\b[a-z][\w:-]*-\[(?:#[0-9a-fA-F]{3,8}|rgba?\([^)]+\)|hsl[a]?\([^)]+\)|var\([^)]+\))\]"
)
BUTTON_CLASS_RE = re.compile(
    r"<button\b[^>]*\bclass(?:Name)?=(?P<quote>[\"'])(?P<class>.*?)(?P=quote)",
    re.IGNORECASE | re.DOTALL,
)
EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "]"
)


@dataclass(frozen=True)
class DesignFinding:
    kind: str
    value: str
    file_path: str
    line: int
    column: int
    context: str


@dataclass(frozen=True)
class ButtonClassPattern:
    classes: str
    count: int
    files: list[str]


def _line_column(content: str, index: int) -> tuple[int, int]:
    line = content.count("\n", 0, index) + 1
    last_newline = content.rfind("\n", 0, index)
    column = index + 1 if last_newline < 0 else index - last_newline
    return line, column


def _line_context(content: str, index: int, limit: int = 160) -> str:
    start = content.rfind("\n", 0, index) + 1
    end = content.find("\n", index)
    if end < 0:
        end = len(content)
    return content[start:end].strip()[:limit]


def _relative_display_path(path: Path, root: Path | None = None) -> str:
    try:
        return path.relative_to(root).as_posix() if root else path.as_posix()
    except ValueError:
        return path.as_posix()


def _iter_matches(kind: str, pattern: re.Pattern[str], content: str, file_path: str) -> Iterable[DesignFinding]:
    for match in pattern.finditer(content):
        line, column = _line_column(content, match.start())
        yield DesignFinding(
            kind=kind,
            value=html.unescape(match.group(0)),
            file_path=file_path,
            line=line,
            column=column,
            context=_line_context(content, match.start()),
        )


def audit_source_text(content: str, file_path: str = "<memory>") -> list[DesignFinding]:
    """Scan a single source string for tokenization and icon-quality candidates."""
    findings: list[DesignFinding] = []
    findings.extend(_iter_matches("raw_hex_color", HEX_COLOR_RE, content, file_path))
    findings.extend(_iter_matches("raw_rgb_color", RGB_COLOR_RE, content, file_path))
    findings.extend(_iter_matches("tailwind_arbitrary_color", TAILWIND_ARBITRARY_COLOR_RE, content, file_path))
    findings.extend(_iter_matches("emoji_icon", EMOJI_RE, content, file_path))
    return findings


def extract_button_class_patterns(content: str, file_path: str = "<memory>") -> list[ButtonClassPattern]:
    """Return normalized button class patterns found in JSX/HTML source."""
    counts: Counter[str] = Counter()
    files_by_class: dict[str, set[str]] = {}
    for match in BUTTON_CLASS_RE.finditer(content):
        classes = " ".join(html.unescape(match.group("class")).split())
        if not classes:
            continue
        counts[classes] += 1
        files_by_class.setdefault(classes, set()).add(file_path)
    return [
        ButtonClassPattern(classes=classes, count=count, files=sorted(files_by_class.get(classes, set())))
        for classes, count in counts.most_common()
    ]


def resolve_allowed_project_path(project_key: str, requested_path: str | None = None) -> Path:
    """Resolve a scan root under a known project directory."""
    normalized_key = project_key.upper()
    if normalized_key not in ALLOWED_PROJECT_ROOTS:
        allowed = ", ".join(sorted(ALLOWED_PROJECT_ROOTS))
        raise ValueError(f"unsupported project_key '{project_key}'. allowed: {allowed}")

    root = ALLOWED_PROJECT_ROOTS[normalized_key].resolve()
    candidate = root if not requested_path else (root / requested_path).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError("requested path escapes the allowed project root")
    return candidate


def iter_source_files(root: Path, max_files: int = 200) -> Iterable[Path]:
    """Yield source-like files while skipping generated and dependency directories."""
    if not root.exists():
        return
    if root.is_file():
        if root.suffix in DEFAULT_INCLUDE_SUFFIXES:
            yield root
        return
    yielded = 0
    for path in root.rglob("*"):
        if yielded >= max_files:
            break
        if not path.is_file():
            continue
        if any(part in DEFAULT_EXCLUDED_DIRS for part in path.parts):
            continue
        if path.suffix not in DEFAULT_INCLUDE_SUFFIXES:
            continue
        yielded += 1
        yield path


def audit_project_preview(
    project_key: str,
    requested_path: str | None = None,
    max_files: int = 80,
) -> dict:
    """Run a bounded read-only design audit preview for an allowlisted project path."""
    root = resolve_allowed_project_path(project_key, requested_path)
    findings: list[DesignFinding] = []
    button_counter: Counter[str] = Counter()
    button_files: dict[str, set[str]] = {}
    scanned_files = 0

    for source_path in iter_source_files(root, max_files=max_files):
        try:
            content = source_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        display_path = _relative_display_path(source_path, root if root.is_dir() else root.parent)
        scanned_files += 1
        findings.extend(audit_source_text(content, display_path))
        for pattern in extract_button_class_patterns(content, display_path):
            button_counter[pattern.classes] += pattern.count
            button_files.setdefault(pattern.classes, set()).update(pattern.files)

    findings_by_kind = Counter(item.kind for item in findings)
    repeated_button_patterns = [
        asdict(ButtonClassPattern(classes=classes, count=count, files=sorted(button_files.get(classes, set()))))
        for classes, count in button_counter.most_common(20)
        if count > 1
    ]

    return {
        "project_key": project_key.upper(),
        "root": root.as_posix(),
        "scanned_files": scanned_files,
        "max_files": max_files,
        "summary": {
            "total_findings": len(findings),
            "by_kind": dict(sorted(findings_by_kind.items())),
            "repeated_button_patterns": len(repeated_button_patterns),
        },
        "findings": [asdict(item) for item in findings[:200]],
        "button_patterns": repeated_button_patterns,
        "read_only": True,
    }
