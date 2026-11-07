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


def field(text, name):
    match = re.search(r"^" + re.escape(name) + r":[ \t]*(.+)$", text, re.M | re.I)
    return match.group(1).strip() if match else ""


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


def check_pr(body, files):
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
    surfaces = section(body, "Surfaces and variables")
    variables = field(surfaces, "Variables").casefold()
    if variables not in {"checked", "not applicable"}:
        errors.append("Variables: must be 'checked' or 'not applicable'.")
    if not meaningful(field(surfaces, "Variables reason")):
        errors.append("Variables reason: state which variables were checked per surface, or why this PR adds or changes none.")
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
    errors = check_pr(data["body"], data["files"]) + check_links(args.root, data["files"])
    for error in errors:
        print(error)
    print(f"Contribution policy: {len(errors)} problem(s); {len(data['files'])} changed files inspected.")
    return bool(errors)


if __name__ == "__main__":
    raise SystemExit(main())
