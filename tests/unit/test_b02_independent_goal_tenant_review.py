"""Blocking independent review for commit 1db5bade85775ae596769959105aa4a829f69b25."""

import ast
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[2]
REVIEW_SHA = "1db5bade85775ae596769959105aa4a829f69b25"


def test_every_goal_http_endpoint_requires_tenant_context():
    source = subprocess.check_output(
        ["git", "show", f"{REVIEW_SHA}:app/routers/goals.py"],
        cwd=ROOT,
        text=True,
    )
    tree = ast.parse(source)
    unscoped = []
    for node in tree.body:
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        is_goal_route = any(
            isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Attribute)
            and isinstance(decorator.func.value, ast.Name)
            and decorator.func.value.id == "router"
            and decorator.args
            and isinstance(decorator.args[0], ast.Constant)
            and str(decorator.args[0].value).startswith("/goals")
            for decorator in node.decorator_list
        )
        argument_names = {arg.arg for arg in (*node.args.args, *node.args.kwonlyargs)}
        if is_goal_route and "tenant" not in argument_names:
            unscoped.append(node.name)

    assert unscoped == [], (
        "commit 1db5bade is rejected: goal endpoints without tenant dependency: "
        + ", ".join(unscoped)
    )
