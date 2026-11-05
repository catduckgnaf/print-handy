#!/usr/bin/env python3
"""Fail when a strict xfail is missing from the register, or the register is stale.

A strict xfail is pytest.mark.xfail(strict=True) written in tests/**/*.py, or a
known_failure key in tests/corpus/cases/*/case.toml (the corpus flow applies
that key as a strict xfail). tests/strict_xfail_register.json lists the ones
that are allowed, each with an owner and an expiry date. The register is the
list; this script only reads it.
"""
import argparse
import ast
import json
import re
import sys
import tomllib
from datetime import date, datetime, timezone
from pathlib import Path

REGISTER = Path("tests/strict_xfail_register.json")
PY_NODE = re.compile(r"^tests/.+\.py::[^:]+(?:::[^:]+)?$")
CORPUS_NODE = re.compile(r"^corpus:[A-Za-z0-9._-]+$")
REASON = re.compile(r"^[^\n]{8,240}$")


def _call_name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _is_strict_xfail(node):
    """True for an xfail mark that passes strict=True, including a bare attribute."""
    call = node
    if isinstance(node, ast.Attribute) and node.attr == "xfail":
        return True
    if not (isinstance(node, ast.Call) and _call_name(node.func) == "xfail"):
        return False
    strict = next((kw.value for kw in call.keywords if kw.arg == "strict"), None)
    return isinstance(strict, ast.Constant) and strict.value is True


def _decorator_sites(decorators, node_id):
    found = []
    for deco in decorators:
        if isinstance(deco, ast.Call) and _call_name(deco.func) == "parametrize":
            for arg in deco.args[1:]:
                target = arg.value if isinstance(arg, ast.Starred) else arg
                if _is_strict_xfail(target):
                    found.append(node_id)
            continue
        if _is_strict_xfail(deco):
            found.append(node_id)
    return found


def python_sites(root):
    """Where strict xfails are written: decorating test node ids, else file:line."""
    sites = []
    for path in sorted((root / "tests").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        rel = path.relative_to(root).as_posix()

        def visit(node, class_name=""):
            if isinstance(node, ast.ClassDef):
                for child in node.body:
                    visit(child, node.name)
                return
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
                node_id = f"{rel}::{class_name}::{node.name}" if class_name else f"{rel}::{node.name}"
                sites.extend(_decorator_sites(node.decorator_list, node_id))
                owned = {id(deco) for deco in node.decorator_list}
                owned.update(id(deco.func) for deco in node.decorator_list if isinstance(deco, ast.Call))
            else:
                owned = set()
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.Call) and id(child) not in owned and _is_strict_xfail(child):
                    sites.append(f"{rel}:{child.lineno}")

        for child in tree.body:
            visit(child)
    return sites


def corpus_sites(root):
    """Case ids whose case.toml declares known_failure, which the flow marks strict."""
    sites = []
    cases = root / "tests" / "corpus" / "cases"
    if not cases.is_dir():
        return sites
    for path in sorted(cases.glob("*/case.toml")):
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        if data.get("known_failure"):
            sites.append(f"corpus:{path.parent.name}")
    return sites


def registered_nodes(root):
    """node_ids listed in the register. An unreadable register registers nothing."""
    path = root / REGISTER
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return set()
    return {entry.get("node_id") for entry in entries if isinstance(entry, dict)}


def load_register(root):
    path = root / REGISTER
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, [f"{REGISTER}: unreadable ({exc})"]
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        return None, [f"{REGISTER}: expected an object with an entries list"]
    return data, []


def _node_shape(node):
    return isinstance(node, str) and (PY_NODE.match(node) or CORPUS_NODE.match(node) or re.fullmatch(r"tests/.+\.py:\d+", node))


def check(root, today):
    errors = []
    data, errors = load_register(root)
    if data is None:
        return errors
    found = python_sites(root) + corpus_sites(root)
    seen = {}
    for index, entry in enumerate(data["entries"], start=1):
        where = f"{REGISTER} entries[{index}]"
        if not isinstance(entry, dict):
            errors.append(f"{where}: expected an object")
            continue
        node = entry.get("node_id")
        owner = entry.get("owner")
        expiry = entry.get("expiry")
        reason = entry.get("reason")
        if not _node_shape(node):
            errors.append(f"{where}: node_id must name a test, a tests file:line, or corpus:<case>")
            node = None
        elif node in seen:
            errors.append(f"{where}: duplicate node_id {node} (also entries[{seen[node]}])")
        else:
            seen[node] = index
        if not isinstance(owner, str) or not owner.strip():
            errors.append(f"{where}: owner is required")
        due = None
        if isinstance(expiry, str):
            try:
                due = datetime.strptime(expiry, "%Y-%m-%d").date()
            except ValueError:
                errors.append(f"{where}: expiry must be YYYY-MM-DD")
        else:
            errors.append(f"{where}: expiry must be YYYY-MM-DD")
        if due is not None and due < today:
            errors.append(f"{where}: expired on {expiry} ({node})")
        if not isinstance(reason, str) or not REASON.match(reason.strip()):
            errors.append(f"{where}: reason must be one line, 8 to 240 characters")
    for node in sorted(set(found)):
        if node not in seen:
            errors.append(f"strict xfail has no register entry: {node}")
    for node in sorted(seen):
        if node not in found:
            errors.append(f"register entry has no strict xfail: {node}")
    return errors


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--today", help="Override today's date (YYYY-MM-DD) for tests")
    args = parser.parse_args(argv)
    today = date.fromisoformat(args.today) if args.today else datetime.now(timezone.utc).date()
    errors = check(args.root, today)
    for error in errors:
        print(error)
    print(f"Strict xfail register: {len(errors)} problem(s).")
    return bool(errors)


if __name__ == "__main__":
    sys.exit(main())
