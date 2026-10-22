"""AAG S1 — 코드 의존 간선 추출 (Python import · TS import · 테스트 · 화면 route).

scan_aads.py 가 호출하는 순수 함수 모음이다. 파일을 직접 순회하지 않고 호출자가
넘긴 파일 목록과 `read(key) -> str` 콜백만 쓴다 — 그래서 저장소 밖(대시보드)
키도, 테스트의 가짜 트리도 같은 코드로 다룬다.

원격 호스트(contabo14, Python 3.8)로 scan_aads.py 와 함께 복사되어 실행된다.
3.9+ 전용 문법·API(`ast.unparse`, `str.removeprefix`, `dict | dict`, 런타임
`list[str]`)를 쓰지 않는다.

규칙 한 줄 요약.
- 프로젝트 내부 모듈 사이만 간선을 만든다. 외부 패키지는 건수만 센다.
- 해석하지 못한 내부 import 는 간선 없이 unresolved 로만 센다(추측 금지).
- 같은 (from, to, kind) 는 한 줄로 합친다. lineno 는 첫 위치.
"""

from __future__ import annotations

import ast
import json
import os
import re
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

FLAG_ENV = "AAG_SCAN_CODE_DEPS"
_OFF_VALUES = {"0", "false", "off", "no", "disabled"}
UNRESOLVED_SAMPLE_LIMIT = 20

TS_SOURCE_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
_TS_RESOLVE_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
_TS_JS_TO_TS = {".js": (".ts", ".tsx"), ".jsx": (".tsx",), ".mjs": (".mts",), ".cjs": (".cts",)}


def enabled(environ: Optional[Dict[str, str]] = None) -> bool:
    """AAG_SCAN_CODE_DEPS=0/false/off/no 이면 끈다. 미설정은 켜짐."""
    env = os.environ if environ is None else environ
    return str(env.get(FLAG_ENV, "1")).strip().lower() not in _OFF_VALUES


class EdgeSet:
    """(from, to, kind) 기준 중복 제거 + 삽입 순서 유지."""

    def __init__(self) -> None:
        self._seen: Dict[Tuple[str, str, str], int] = {}
        self.edges: List[Dict[str, Any]] = []

    def add(self, src: str, dst: str, kind: str, lineno: int = 0, **extra: Any) -> bool:
        if not src or not dst or src == dst:
            return False
        key = (src, dst, kind)
        if key in self._seen:
            return False
        self._seen[key] = len(self.edges)
        edge: Dict[str, Any] = {"from": src, "to": dst, "kind": kind}
        if lineno:
            edge["lineno"] = lineno
        edge.update(extra)
        self.edges.append(edge)
        return True


# ══════════════════════════════════════════════════════════════════════
# Python
# ══════════════════════════════════════════════════════════════════════


def is_test_path(rel: str, test_roots: Sequence[str]) -> bool:
    rel = rel.replace("\\", "/")
    name = rel.rsplit("/", 1)[-1]
    if name == "conftest.py" or name.startswith("test_") or name.endswith("_test.py"):
        return True
    for root in test_roots:
        root = root.strip("/")
        if root and (rel == root or rel.startswith(root + "/")):
            return True
    return False


class PyIndex:
    """점(.) 경로 → 파일 키. import root 별로 같은 파일이 여러 이름을 가질 수 있다."""

    def __init__(self, files: Iterable[str], import_roots: Sequence[str]) -> None:
        self.files: Set[str] = set(files)
        self.by_dotted: Dict[str, str] = {}
        self.namespace_dirs: Set[str] = set()
        self.top_levels: Set[str] = set()
        roots = [r.strip("/") for r in (import_roots or [""])]
        for rel in sorted(self.files):
            for root in roots:
                if root:
                    if not rel.startswith(root + "/"):
                        continue
                    sub = rel[len(root) + 1:]
                else:
                    sub = rel
                if not sub.endswith(".py"):
                    continue
                parts = sub[:-3].split("/")
                if parts[-1] == "__init__":
                    parts = parts[:-1]
                if not parts:
                    continue
                dotted = ".".join(parts)
                self.by_dotted.setdefault(dotted, rel)
                self.top_levels.add(parts[0])
                for i in range(1, len(parts)):
                    self.namespace_dirs.add(".".join(parts[:i]))

    def module(self, dotted: str) -> Optional[str]:
        return self.by_dotted.get(dotted)

    def is_project_name(self, dotted: str) -> bool:
        return dotted.split(".", 1)[0] in self.top_levels

    def deepest(self, dotted: str) -> Optional[str]:
        """a.b.c 가 없으면 a.b, a 순으로 가장 깊은 실재 모듈."""
        parts = dotted.split(".")
        while parts:
            hit = self.by_dotted.get(".".join(parts))
            if hit:
                return hit
            parts.pop()
        return None


def _relative_target(index: PyIndex, from_rel: str, level: int, module: str) -> Optional[str]:
    base = os.path.dirname(from_rel)
    for _ in range(level - 1):
        if not base:
            return None
        base = os.path.dirname(base)
    parts = [p for p in module.split(".") if p] if module else []
    joined = "/".join([p for p in [base] + parts if p])
    cands = (joined + ".py", joined + "/__init__.py") if parts else (joined + "/__init__.py",)
    for cand in cands:
        if cand in index.files:
            return cand
    return None


def _relative_known(from_rel: str, level: int) -> bool:
    base = os.path.dirname(from_rel)
    for _ in range(level - 1):
        if not base:
            return False
        base = os.path.dirname(base)
    return True


def extract_py_imports(source: str) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """AST 로 import 문을 뽑는다. → ([{level, module, names, lineno}], 파싱오류)

    `importlib.import_module("lit")` / `__import__("lit")` 의 상수 문자열도 함께.
    함수 안·try/except·TYPE_CHECKING 안의 import 도 모두 본다(ast.walk).
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError) as exc:
        return [], "%s: %s" % (type(exc).__name__, exc)
    out: List[Dict[str, Any]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.append({"level": 0, "module": alias.name, "names": [],
                            "lineno": node.lineno})
        elif isinstance(node, ast.ImportFrom):
            out.append({"level": node.level or 0, "module": node.module or "",
                        "names": [a.name for a in node.names if a.name != "*"],
                        "lineno": node.lineno})
        elif isinstance(node, ast.Call) and node.args:
            func = node.func
            callee = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            first = node.args[0]
            if (callee in ("import_module", "__import__")
                    and isinstance(first, ast.Constant) and isinstance(first.value, str)
                    and first.value and not first.value.startswith(".")):
                out.append({"level": 0, "module": first.value, "names": [],
                            "lineno": node.lineno, "dynamic": True})
    out.sort(key=lambda i: i["lineno"])
    return out, None


def resolve_py_imports(
    from_rel: str, imports: Sequence[Dict[str, Any]], index: PyIndex,
) -> Tuple[List[Tuple[str, int]], Dict[str, int], List[Dict[str, Any]]]:
    """→ (내부 대상 [(파일키, lineno)], 외부 top-level 별 건수, 해석 실패 목록)"""
    targets: List[Tuple[str, int]] = []
    external: Dict[str, int] = {}
    unresolved: List[Dict[str, Any]] = []

    def fail(imp: Dict[str, Any], why: str) -> None:
        spec = "." * imp["level"] + imp["module"]
        unresolved.append({"file": from_rel, "lineno": imp["lineno"], "import": spec,
                           "reason": why})

    for imp in imports:
        level, module, names, lineno = imp["level"], imp["module"], imp["names"], imp["lineno"]
        if level:
            if not _relative_known(from_rel, level):
                fail(imp, "relative_beyond_root")
                continue
            base_hit = _relative_target(index, from_rel, level, module)
            got = False
            if base_hit:
                targets.append((base_hit, lineno))
                got = True
            for name in names:
                sub = _relative_target(index, from_rel, level, (module + "." + name) if module else name)
                if sub:
                    targets.append((sub, lineno))
                    got = True
            if not got:
                if not module and not names:
                    continue
                # `from . import x` 의 x 가 모듈이 아니라 패키지 __init__ 의 속성인 경우는
                # base_hit(__init__.py) 로 이미 잡힌다. 둘 다 없으면 진짜 실패.
                fail(imp, "relative_not_found")
            continue

        if not module:
            continue
        if not index.is_project_name(module):
            top = module.split(".", 1)[0]
            external[top] = external.get(top, 0) + 1
            continue
        full_hit = index.module(module)
        in_namespace = module in index.namespace_dirs
        if names:
            # from M import a, b — M 은 정확히 있어야 한다. 상위 패키지로 대신 잇지 않는다.
            if full_hit:
                targets.append((full_hit, lineno))
            sub_hits = [index.module(module + "." + n) for n in names]
            for sub in sub_hits:
                if sub:
                    targets.append((sub, lineno))
            if not full_hit and not (in_namespace and any(sub_hits)):
                fail(imp, "module_not_found")
        else:
            # import a.b.c — a, a.b 패키지도 실제로 import 되므로 가장 깊은 실재 모듈로 잇는다.
            hit = index.deepest(module)
            if hit:
                targets.append((hit, lineno))
            if not full_hit and not in_namespace:
                fail(imp, "submodule_not_found" if hit else "module_not_found")
    return targets, external, unresolved


def build_py_deps(
    py_files: Sequence[str],
    read: Callable[[str], str],
    import_roots: Sequence[str],
    test_roots: Sequence[str],
) -> Dict[str, Any]:
    """Python import / tests 간선과 집계.

    반환: {edges, imports, tests, test_files, linked_tests, unresolved, external,
           parse_failed, files_scanned}
    """
    index = PyIndex(py_files, import_roots)
    edges = EdgeSet()
    external_total: Dict[str, int] = {}
    unresolved: List[Dict[str, Any]] = []
    parse_failed: List[str] = []
    test_files = {rel for rel in index.files if is_test_path(rel, test_roots)}
    linked: Set[str] = set()
    n_imports = 0
    n_tests = 0
    for rel in sorted(index.files):
        imports, err = extract_py_imports(read(rel))
        if err:
            parse_failed.append(rel)
            continue
        targets, external, bad = resolve_py_imports(rel, imports, index)
        for name, count in external.items():
            external_total[name] = external_total.get(name, 0) + count
        unresolved.extend(bad)
        is_test = rel in test_files
        for dst, lineno in targets:
            if dst == rel:
                continue
            if is_test:
                if dst in test_files:
                    continue
                if edges.add(rel, dst, "tests", lineno):
                    n_tests += 1
                linked.add(rel)
            elif edges.add(rel, dst, "imports", lineno, lang="py"):
                n_imports += 1
    return {
        "edges": edges.edges,
        "imports": n_imports,
        "tests": n_tests,
        "test_files": sorted(test_files),
        "linked_tests": sorted(linked),
        "unresolved": unresolved,
        "external": external_total,
        "parse_failed": sorted(parse_failed),
        "files_scanned": len(index.files),
    }


# ══════════════════════════════════════════════════════════════════════
# TypeScript / Next.js
# ══════════════════════════════════════════════════════════════════════

_TS_FROM_RE = re.compile(
    r"(?:^|[;\n])[ \t]*(?:import|export)\s+(?:type\s+)?[\w$*{},\s]*?\bfrom\s*['\"]([^'\"\n]+)['\"]"
)
_TS_BARE_RE = re.compile(r"(?:^|[;\n])[ \t]*import\s*['\"]([^'\"\n]+)['\"]")
_TS_DYNAMIC_RE = re.compile(r"\b(?:import|require)\s*\(\s*['\"]([^'\"\n]+)['\"]\s*\)")
_TS_ASSET_SUFFIXES = (".css", ".scss", ".sass", ".less", ".json", ".svg", ".png", ".jpg",
                      ".jpeg", ".gif", ".webp", ".ico", ".woff", ".woff2", ".md", ".mdx")


def extract_ts_imports(text: str) -> List[Tuple[str, int]]:
    """→ [(지정자, lineno)] 소스 순서. 주석 처리된 줄 시작 import 는 패턴상 걸리지 않는다."""
    found: Dict[Tuple[str, int], None] = {}
    for regex in (_TS_FROM_RE, _TS_BARE_RE, _TS_DYNAMIC_RE):
        for m in regex.finditer(text):
            spec = m.group(1)
            lineno = text.count("\n", 0, m.start(1)) + 1
            found.setdefault((spec, lineno), None)
    return sorted(found, key=lambda t: t[1])


def strip_jsonc(text: str) -> str:
    """tsconfig.json 의 주석과 후행 쉼표를 지운다. 문자열 안은 건드리지 않는다."""
    out: List[str] = []
    i, n = 0, len(text)
    in_str = False
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
        elif ch == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
        elif ch == "/" and i + 1 < n and text[i + 1] == "*":
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
        else:
            out.append(ch)
            i += 1
    cleaned = "".join(out)
    return re.sub(r",(\s*[}\]])", r"\1", cleaned)


def parse_tsconfig(text: str) -> Dict[str, Any]:
    try:
        data = json.loads(strip_jsonc(text))
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def load_tsconfig_paths(
    tsconfig_path: str, read: Callable[[str], str],
) -> Tuple[Dict[str, List[str]], str]:
    """tsconfig 의 compilerOptions.paths → ({패턴: [절대 대상 디렉터리/패턴]}, baseUrl 절대경로)

    `extends`(상대 경로 한 단계)를 따라 paths/baseUrl 이 없으면 부모 값을 쓴다.
    대상은 tsconfig 디렉터리(baseUrl 이 있으면 baseUrl) 기준 정규화된 posix 경로다.
    """
    seen: Set[str] = set()
    paths_raw: Dict[str, Any] = {}
    base_url = ""
    paths_dir = ""
    cur = tsconfig_path
    first_dir = os.path.dirname(tsconfig_path)
    while cur and cur not in seen:
        seen.add(cur)
        cfg = parse_tsconfig(read(cur))
        if not cfg:
            break
        opts = cfg.get("compilerOptions") or {}
        cur_dir = os.path.dirname(cur)
        if not base_url and isinstance(opts.get("baseUrl"), str):
            base_url = os.path.normpath(os.path.join(cur_dir, opts["baseUrl"])).replace(os.sep, "/")
        if not paths_raw and isinstance(opts.get("paths"), dict):
            paths_raw = opts["paths"]
            paths_dir = cur_dir
        ext = cfg.get("extends")
        if isinstance(ext, str) and ext.startswith("."):
            nxt = os.path.normpath(os.path.join(cur_dir, ext)).replace(os.sep, "/")
            cur = nxt if nxt.endswith(".json") else nxt + ".json"
        else:
            cur = ""
    resolved: Dict[str, List[str]] = {}
    anchor = base_url or paths_dir or first_dir
    for pattern, targets in paths_raw.items():
        if not isinstance(targets, list):
            continue
        outs = []
        for tgt in targets:
            if isinstance(tgt, str):
                outs.append(os.path.normpath(os.path.join(anchor, tgt)).replace(os.sep, "/"))
        if outs:
            resolved[pattern] = outs
    return resolved, base_url


def _alias_candidates(spec: str, paths: Dict[str, List[str]]) -> Optional[List[str]]:
    """paths 패턴에 걸리면 후보 절대 경로(확장자 전), 안 걸리면 None. 가장 긴 접두 우선."""
    best: Optional[Tuple[int, List[str]]] = None
    for pattern, targets in paths.items():
        if "*" in pattern:
            head, _, tail = pattern.partition("*")
            if spec.startswith(head) and spec.endswith(tail) and len(spec) >= len(head) + len(tail):
                star = spec[len(head):len(spec) - len(tail)] if tail else spec[len(head):]
                cands = [t.replace("*", star, 1) for t in targets]
                if best is None or len(head) > best[0]:
                    best = (len(head), cands)
        elif spec == pattern:
            if best is None or len(pattern) > best[0]:
                best = (len(pattern), list(targets))
    return best[1] if best else None


def _ts_file_for(base: str, known: Set[str]) -> Optional[str]:
    base = base.rstrip("/")
    if base in known:
        return base
    stem, ext = os.path.splitext(base)
    if ext in _TS_JS_TO_TS:
        for alt in _TS_JS_TO_TS[ext]:
            if stem + alt in known:
                return stem + alt
    for suf in _TS_RESOLVE_SUFFIXES:
        if base + suf in known:
            return base + suf
    for suf in _TS_RESOLVE_SUFFIXES:
        if base + "/index" + suf in known:
            return base + "/index" + suf
    return None


def _external_package(spec: str) -> str:
    if spec.startswith("@"):
        return "/".join(spec.split("/")[:2])
    return spec.split("/", 1)[0]


def resolve_ts_import(
    spec: str,
    from_abs: str,
    known_abs: Set[str],
    paths: Dict[str, List[str]],
    base_url: str,
) -> Tuple[str, Optional[str]]:
    """→ (분류, 절대경로|None). 분류: internal | external | unresolved | asset"""
    if spec.startswith("node:"):
        return "external", None
    if spec.startswith("."):
        base = os.path.normpath(os.path.join(os.path.dirname(from_abs), spec)).replace(os.sep, "/")
        hit = _ts_file_for(base, known_abs)
        if hit:
            return "internal", hit
        return ("asset", None) if spec.lower().endswith(_TS_ASSET_SUFFIXES) else ("unresolved", None)
    cands = _alias_candidates(spec, paths)
    if cands is None and base_url:
        guess = os.path.normpath(os.path.join(base_url, spec)).replace(os.sep, "/")
        if _ts_file_for(guess, known_abs):
            cands = [guess]
    if cands is not None:
        for cand in cands:
            hit = _ts_file_for(cand, known_abs)
            if hit:
                return "internal", hit
        return ("asset", None) if spec.lower().endswith(_TS_ASSET_SUFFIXES) else ("unresolved", None)
    return "external", None


def find_tsconfig(start_dir: str, exists: Callable[[str], bool], stop_at: str = "") -> str:
    """start_dir 에서 위로 올라가며 tsconfig.json 을 찾는다. 없으면 빈 문자열."""
    cur = start_dir.rstrip("/") or "/"
    for _ in range(6):
        cand = (cur.rstrip("/") + "/tsconfig.json")
        if exists(cand):
            return cand
        if cur == "/" or (stop_at and cur == stop_at.rstrip("/")):
            break
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return ""


def build_ts_deps(
    fe_keys: Sequence[str],
    abs_of: Callable[[str], str],
    key_of: Callable[[str], str],
    read: Callable[[str], str],
    tsconfig_path: str,
    extra_aliases: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """TS/TSX import 간선. 저장소(프런트 root) 내부 상대·alias import 만 간선이 된다.

    fe_keys: 노드 키(상대 또는 절대). abs_of: 키→절대경로. key_of: 절대경로→키.
    extra_aliases: rules 의 path_aliases(`"@/": "dir/"`). tsconfig paths 가 우선한다.
    """
    keys = list(fe_keys)
    abs_to_key = {abs_of(k).replace(os.sep, "/"): k for k in keys}
    known_abs = set(abs_to_key)
    paths: Dict[str, List[str]] = {}
    base_url = ""
    if tsconfig_path:
        paths, base_url = load_tsconfig_paths(tsconfig_path, read)
    for prefix, target in (extra_aliases or {}).items():
        pattern = prefix + "*" if not prefix.endswith("*") else prefix
        if pattern in paths:
            continue
        tgt_abs = abs_of(target.rstrip("/")).replace(os.sep, "/") + "/*"
        paths[pattern] = [tgt_abs]

    edges = EdgeSet()
    external: Dict[str, int] = {}
    unresolved: List[Dict[str, Any]] = []
    assets = 0
    for key in sorted(keys):
        text = read(key)
        if not text:
            continue
        from_abs = abs_of(key).replace(os.sep, "/")
        for spec, lineno in extract_ts_imports(text):
            status, hit = resolve_ts_import(spec, from_abs, known_abs, paths, base_url)
            if status == "internal" and hit:
                edges.add(key, abs_to_key.get(hit) or key_of(hit), "imports", lineno, lang="ts")
            elif status == "external":
                pkg = _external_package(spec)
                external[pkg] = external.get(pkg, 0) + 1
            elif status == "asset":
                assets += 1
            else:
                unresolved.append({"file": key, "lineno": lineno, "import": spec,
                                   "reason": "module_not_found"})
    return {
        "edges": edges.edges,
        "imports": len(edges.edges),
        "unresolved": unresolved,
        "external": external,
        "assets_skipped": assets,
        "tsconfig": tsconfig_path,
        "alias_patterns": sorted(paths),
    }


# ══════════════════════════════════════════════════════════════════════
# Next.js 화면 노드 속성
# ══════════════════════════════════════════════════════════════════════

_SPECIAL_STEMS = {"loading", "error", "not-found", "template", "default", "global-error"}
_PAGE_EXTS = (".tsx", ".ts", ".jsx", ".js")


def _route_segment(seg: str) -> Optional[str]:
    """None 이면 URL 에 나타나지 않는 세그먼트."""
    if seg.startswith("(") and seg.endswith(")"):
        return None
    if seg.startswith("@"):
        return None
    m = re.match(r"^\[\[\.\.\.(.+)\]\]$", seg)
    if m:
        return "{%s*}" % m.group(1)
    m = re.match(r"^\[\.\.\.(.+)\]$", seg)
    if m:
        return "{%s*}" % m.group(1)
    m = re.match(r"^\[(.+)\]$", seg)
    if m:
        return "{%s}" % m.group(1)
    return seg


def classify_next_file(key: str) -> Dict[str, Any]:
    """프런트 파일 → {role, route?}. role: page|layout|route_handler|special|middleware|component|module

    App Router(`app/**/page.tsx`)와 Pages Router(`pages/**`) 모두 본다. 라우트 그룹 `(g)`·
    병렬 슬롯 `@s` 는 URL 에서 빠지고 동적 세그먼트 `[id]` 는 `{id}` 로 적는다.
    """
    parts = key.replace("\\", "/").split("/")
    name = parts[-1]
    stem, ext = os.path.splitext(name)
    dirs = parts[:-1]
    if ext not in _PAGE_EXTS:
        return {"role": "module"}

    def url_of(segs: Sequence[str]) -> str:
        out = [s for s in (_route_segment(x) for x in segs) if s]
        return "/" + "/".join(out)

    if "app" in dirs:
        idx = len(dirs) - 1 - dirs[::-1].index("app")
        tail = dirs[idx + 1:]
        if stem == "page":
            return {"role": "page", "route": url_of(tail)}
        if stem == "layout":
            return {"role": "layout", "route": url_of(tail)}
        if stem == "route":
            return {"role": "route_handler", "route": url_of(tail)}
        if stem in _SPECIAL_STEMS:
            return {"role": "special", "route": url_of(tail)}
    if "pages" in dirs:
        idx = len(dirs) - 1 - dirs[::-1].index("pages")
        tail = dirs[idx + 1:]
        if stem.startswith("_"):
            return {"role": "special"}
        segs = list(tail) if stem == "index" else list(tail) + [stem]
        if tail and tail[0] == "api":
            return {"role": "route_handler", "route": url_of(segs)}
        return {"role": "page", "route": url_of(segs)}
    if stem == "middleware" and len(dirs) <= 2:
        return {"role": "middleware"}
    return {"role": "component" if ext in (".tsx", ".jsx") else "module"}
