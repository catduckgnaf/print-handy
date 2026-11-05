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
PY_NODE = re.compile(r"^tests/.+\.py::[^:]+(?:::[^:]+)*$")
CORPUS_NODE = re.compile(r"^corpus:[A-Za-z0-9._-]+$")
REASON = re.compile(r"^[^\n]{8,240}$")


def _call_name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _keyword(call, name):
    return next((kw.value for kw in call.keywords if kw.arg == name), None)


def _is_strict_xfail(node):
    """True only for an xfail mark that passes strict=True.

    A bare pytest.mark.xfail, and one called without strict=True, follow pytest's
    default of strict=False (this repo does not set strict_xfail). A positional
    argument is the condition, never the strict flag, so it cannot make one strict.
    """
    if isinstance(node, ast.Attribute) and node.attr == "xfail":
        return False
    if not (isinstance(node, ast.Call) and _call_name(node.func) == "xfail"):
        return False
    strict = _keyword(node, "strict")
    return isinstance(strict, ast.Constant) and strict.value is True


def _walk_xfail(node, node_id, found, seen):
    """Record node_id wherever this tree writes a strict xfail, descending into marks."""
    if id(node) in seen or not isinstance(node, ast.AST):
        return
    seen.add(id(node))
    if _is_strict_xfail(node):
        found.append(node_id)
    for child in ast.iter_child_nodes(node):
        _walk_xfail(child, node_id, found, seen)


def _decorator_sites(decorators, node_id):
    found = []
    seen = set()
    for deco in decorators:
        _walk_xfail(deco, node_id, found, seen)
    return found


def python_sites(root):
    """Where strict xfails are written: decorating test node ids, else file:line."""
    sites = []
    for path in sorted((root / "tests").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        rel = path.relative_to(root).as_posix()

        def visit(node, class_name=""):
            if isinstance(node, ast.ClassDef):
                node_id = f"{rel}::{class_name}::{node.name}" if class_name else f"{rel}::{node.name}"
                sites.extend(_decorator_sites(node.decorator_list, node_id))
                for child in node.body:
                    visit(child, f"{class_name}::{node.name}" if class_name else node.name)
                return
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
                node_id = f"{rel}::{class_name}::{node.name}" if class_name else f"{rel}::{node.name}"
                sites.extend(_decorator_sites(node.decorator_list, node_id))
                decorated = {id(item) for deco in node.decorator_list for item in ast.walk(deco)}
            else:
                decorated = set()
            for child in ast.walk(node):
                if isinstance(child, ast.Call) and id(child) not in decorated and _is_strict_xfail(child):
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
