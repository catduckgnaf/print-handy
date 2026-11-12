#!/usr/bin/env python3
"""Read-only PR metadata and changed Markdown destination checks; stdlib only."""
import argparse
import json
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit


def section(body, title):
    match = re.search(r"^## " + re.escape(title) + r"\s*$(.*?)(?=^## |\Z)", body, re.M | re.S)
    return match.group(1).strip() if match else ""


def meaningful(text):
    cleaned = text.strip().strip("- ")
    return len(cleaned) >= 15 and cleaned.casefold() not in {"not applicable", "none", "todo", "tbd"} and not re.fullmatch(r"(?:TODO|TBD|N/A)[.! ]*", cleaned, re.I)


_FIELD_LINE = re.compile(r"^[A-Za-z][A-Za-z ]*:")


def field(text, name):
    """A field's answer: the rest of its line, or the bullet list right under an empty one.

    A list under the field is read until the first line that is not a bullet,
    so an empty field never captures the next field's line.
    """
    match = re.search(r"^" + re.escape(name) + r":[ \t]*(.*)$", text, re.M | re.I)
    if not match:
        return ""
    value = match.group(1).strip()
    if value:
        return value
    bullets = []
    for line in text[match.end():].splitlines()[1:]:
        stripped = line.strip()
        if not stripped.startswith(("- ", "* ")) or _FIELD_LINE.match(stripped[2:].lstrip()):
            break
        bullets.append(stripped)
    return "\n".join(bullets)


# --- regression tests -------------------------------------------------------
# A bug fix carries a regression test that was seen failing without the fix:
# the detail names the test and the commit it fails on. A test never seen red
# proves nothing about the bug it claims to pin.

REGRESSION_KINDS = {"added", "not a bug fix", "not possible"}
_SHA = re.compile(r"\b[0-9a-f]{7,40}\b")
_ISSUE_REF = re.compile(r"(?:#\d+\b|https://github\.com/[^/\s]+/[^/\s]+/issues/\d+)")


def is_bug_fix(body):
    kinds = section(body, "Type of change")
    return bool(re.search(r"^\s*[-*]\s*\[[xX]\]\s*Bug fix\b", kinds, re.M))


def check_regression_test(body, testing, files):
    kind = field(testing, "Regression test").casefold()
    detail = field(testing, "Regression test detail")
    if kind not in REGRESSION_KINDS:
        return ["Regression test: must be 'added', 'not a bug fix' or 'not possible'."]
    errors = []
    if not meaningful(detail):
        errors.append("Regression test detail: name the regression test and the commit it fails on, "
                      "or say why this is not a bug fix, or why no automated test is possible.")
    if kind == "added":
        if not any(is_test(f) for f in files):
            errors.append("Regression test says added but no test file changed.")
        if not (_SHA.search(detail) and re.search(r"\bfail", detail, re.I)):
            errors.append("Regression test detail: name the test and the commit it fails on without the fix "
                          "(for example 'fails on 1a2b3c4, passes on this head').")
    elif kind == "not a bug fix" and is_bug_fix(body):
        errors.append("Regression test: this PR is marked Bug fix, so 'not a bug fix' does not apply. "
                      "Add a regression test, or say 'not possible' and why.")
    elif kind == "not possible" and not _ISSUE_REF.search(detail):
        errors.append("Regression test detail: when no automated regression test is possible, "
                      "link the issue that tracks the manual or live check.")
    return errors


# --- issue checklists -------------------------------------------------------
# An issue is finished when every checklist item in its body is ticked, or
# marked as moved to the issue that now owns it. The checkbox fields of the
# issue forms are the reporter's answers, not work items, so they are skipped
# (tests pin this list against .github/ISSUE_TEMPLATE).

FORM_CHECKBOX_SECTIONS = {"before submitting", "hardware checks you ran and that passed"}
_BOX = re.compile(r"^\s*[-*+]\s+\[ \]\s+(.*\S)", re.M)
_MOVED = re.compile(r"\b(?:moved|deferred|tracked)\s+(?:to|in)\s+"
                    r"(?:#\d+|https://github\.com/[^/\s]+/[^/\s]+/issues/\d+)", re.I)


def open_items(body):
    """The unticked checklist items of an issue body that nothing has taken over."""
    text = re.sub(r"<!--.*?-->", "", body or "", flags=re.S)
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    parts = re.split(r"^#{1,6} +(.+?)\s*$", text, flags=re.M)
    kept = [parts[0]] + [parts[i + 1] for i in range(1, len(parts) - 1, 2)
                          if parts[i].strip().casefold() not in FORM_CHECKBOX_SECTIONS]
    return [m.group(1).strip() for chunk in kept for m in _BOX.finditer(chunk)
            if not _MOVED.search(m.group(1))]


def check_closing(body, closing):
    """A PR may close an issue only when the issue's own checklist is complete."""
    if not closing:
        return []
    errors = []
    if not meaningful(field(section(body, "Related issues"), "Closure check")):
        errors.append("Closure check: this PR closes an issue; say how each closed issue's checklist "
                      "was reconciled and where each item was done.")
    for issue in closing:
        items = open_items(issue.get("body"))
        if items:
            shown = "; ".join(item[:90] for item in items[:5])
            more = f"; and {len(items) - 5} more" if len(items) > 5 else ""
            errors.append(f"Closes #{issue.get('number')}, which still has {len(items)} unticked checklist "
                          f"item(s): {shown}{more}. Tick each finished item on the issue with where it was "
                          "done, mark the rest 'moved to #N', or change Fixes to Related.")
    return errors


_VERIFICATION_PROMPTS = ("Checks run", "Checks not run and why")


def verification_answered(text):
    """Whether the verification section answers its template prompts (P2-43).

    The untouched template leaves the two bare prompt labels behind; joined,
    they are long enough to pass meaningful(), so an unfilled section read
    as evidence. Strip each prompt label and judge only what remains.
    """
    answered = []
    for line in text.splitlines():
        line = line.strip().lstrip("-").strip()
        for label in _VERIFICATION_PROMPTS:
            if line.casefold().startswith(label.casefold()):
                line = line[len(label):].lstrip(":").strip()
                break
        answered.append(line)
    return meaningful("\n".join(answered))


def is_doc(path):
    return path.endswith(".md") or path.startswith("docs/")


def is_test(path):
    parts = path.split("/")
    name = parts[-1]
    return "tests" in parts[:-1] or name.startswith("test_") or name.endswith("_test.py")


def check_pr(body, files, closing=()):
    body = re.sub(r"<!--.*?-->", "", body or "", flags=re.S)
    errors = []
    for title in ["Summary and user impact"]:
        if not meaningful(section(body, title)):
            errors.append(f"Fill '## {title}' with actual evidence or explain unrun checks.")
    if not verification_answered(section(body, "Verification and test results")):
        errors.append("Fill '## Verification and test results' with actual evidence or explain unrun checks.")
    docs = section(body, "Documentation impact")
    status = field(docs, "Documentation").casefold()
    if status not in {"updated", "not needed"}:
        errors.append("Documentation: must be 'updated' or 'not needed'.")
    if not meaningful(field(docs, "Reason")):
        errors.append("Reason: explain the documentation changes or why none are needed.")
    if status == "updated" and not any(is_doc(f) for f in files):
        errors.append("Documentation says updated but no documentation file changed.")
    wiki = field(docs, "Wiki").casefold()
    if wiki not in {"updated", "not applicable", "deferred"}:
        errors.append("Wiki: must be 'updated', 'not applicable', or 'deferred'.")
    if not meaningful(field(docs, "Wiki reason")):
        errors.append("Wiki reason: explain the wiki impact and versioned docs coverage.")
    if wiki == "updated" and not re.search(r"https://github\.com/[^/\s]+/[^/\s]+/wiki/[^\s]+", docs):
        errors.append("Link the updated wiki page in Documentation impact.")
    if wiki == "deferred" and not re.search(r"https://github\.com/[^/\s]+/[^/\s]+/issues/\d+", docs):
        errors.append("Link the tracking issue for deferred wiki work in Documentation impact.")
    testing = section(body, "Tests added and regression scoping")
    added = field(testing, "Tests added").casefold()
    if added not in {"added", "none"}:
        errors.append("Tests added: must be 'added' or 'none'.")
    if not meaningful(field(testing, "Tests added detail")):
        errors.append("Tests added detail: name the new or updated tests and what each pins, or explain why no test was added.")
    if added == "added" and not any(is_test(f) for f in files):
        errors.append("Tests added says added but no test file changed.")
    scoping = field(testing, "Regression scoping").casefold()
    if scoping not in {"checked", "not applicable"}:
        errors.append("Regression scoping: must be 'checked' or 'not applicable'.")
    if not meaningful(field(testing, "Regression scoping reason")):
        errors.append("Regression scoping reason: state how the change was scoped against the full suite and existing regression tests, what was left out and why, or why scoping does not apply.")
    errors += check_regression_test(body, testing, files)
    errors += check_closing(body, closing)
    surfaces = section(body, "Surfaces and variables")
    variables = field(surfaces, "Variables").casefold()
    if variables not in {"checked", "not applicable"}:
        errors.append("Variables: must be 'checked' or 'not applicable'.")
    if not meaningful(field(surfaces, "Variables reason")):
        errors.append("Variables reason: state which variables were checked per surface, or why this PR adds or changes none.")
    return errors


# --- evidence for a met criterion (#1322) -------------------------------------
# A criterion claimed as met cites the test that proves it, by path, and that
# test drives the production entry point. The check below can see the claim and
# the citation; whether the cited test reaches the entry point is review work.
# A claim is a ticked box in the PR's "Scope and acceptance criteria" section,
# or a table row with a cell that reads "Met" there or in a changed Markdown file.
# A criterion no automated test can prove says "evidence:" and names what does.

_TEST_REF = re.compile(r"(?<![\w/.-])(tests/[\w./-]+?\.py)\b")
_EVIDENCE = re.compile(r"\bevidence:\s*\S{3,}", re.I)
_TICKED = re.compile(r"^\s*[-*+]\s+\[[xX]\]\s+(.*\S)", re.M)


def met_claims(text):
    """The lines of text that claim a criterion is met: ticked boxes and "Met" table rows."""
    text = re.sub(r"```.*?```", "", text or "", flags=re.S)
    claims = [m.group(0).strip() for m in _TICKED.finditer(text)]
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("|") and any(cell.strip().casefold() == "met" for cell in stripped.strip("|").split("|")):
            claims.append(stripped)
    return claims


def _claim_errors(root, where, claim):
    refs = _TEST_REF.findall(claim)
    shown = claim if len(claim) <= 90 else claim[:87] + "..."
    if not refs and not _EVIDENCE.search(claim):
        return [f"{where}: a met criterion cites no test: {shown!r}. Name the test that drives the production "
                "entry point (tests/test_x.py::test_y), or say 'evidence:' and what proves it."]
    return [f"{where}: cites {ref}, which is not in this tree." for ref in refs
            if root is not None and not (root / ref).is_file()]


def check_evidence(root, body, files):
    """Every met-criterion claim in the PR body and in changed Markdown cites its evidence."""
    root = root.resolve() if root is not None else None
    body = re.sub(r"<!--.*?-->", "", body or "", flags=re.S)
    errors = []
    for claim in met_claims(section(body, "Scope and acceptance criteria")):
        errors += _claim_errors(root, "Scope and acceptance criteria", claim)
    for name in files:
        if not name.endswith(".md") or root is None or not (root / name).is_file():
            continue
        text = re.sub(r"<!--.*?-->", "", (root / name).read_text(encoding="utf-8"), flags=re.S)
        for claim in met_claims(text):
            if claim.startswith("|"):  # a ticked box in a doc is a task list, not a criterion table
                errors += _claim_errors(root, name, claim)
    return errors


def check_links(root, files):
    """Check local file destinations, not remote URLs or heading anchors."""
    errors = []
    root = root.resolve()
    for name in files:
        if not name.endswith(".md"):
            continue
        path = root / name
        if not path.is_file():  # Deleted Markdown has no remaining links to inspect.
            continue
        text = re.sub(r"```.*?```", "", path.read_text(), flags=re.S)
        links = re.findall(r"!?\[[^\]]*\]\(([^)]+)\)", text)
        links += re.findall(r"^\[[^\]]+\]:\s*(\S+)", text, re.M)
        for raw in links:
            target = raw.split()[0].strip("<>")
            parsed = urlsplit(target)
            if parsed.scheme or parsed.netloc or not parsed.path:
                continue
            dest = (root / unquote(parsed.path).lstrip("/") if parsed.path.startswith("/") else path.parent / unquote(parsed.path)).resolve()
            if not dest.is_relative_to(root) or not dest.exists():
                errors.append(f"{name}: missing or outside-repository destination: {target}")
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metadata", type=Path, required=True, help="JSON body/files from GitHub API, never shell code")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    data = json.loads(args.metadata.read_text())
    errors = (check_pr(data["body"], data["files"], data.get("closing_issues", ()))
              + check_evidence(args.root, data["body"], data["files"])
              + check_links(args.root, data["files"]))
    for error in errors:
        print(error)
    print(f"Contribution policy: {len(errors)} problem(s); {len(data['files'])} changed files inspected.")
    return bool(errors)


if __name__ == "__main__":
    raise SystemExit(main())
