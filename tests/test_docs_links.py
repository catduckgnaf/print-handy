"""Every relative link and heading anchor in README.md and docs/ resolves (#1196, #1156).

A heading rename (#1077 stripped issue numbers from headings) silently killed
fourteen in-page anchors, and a link whose display text was swallowed by the
anchor builder (#1173, closed PR #1176) went unnoticed. This offline test
fails on:

* a relative link whose file or folder does not exist;
* a link to ``file.md#anchor`` (or ``#anchor`` inside the same page) where no
  heading of that page, and no explicit ``<a id>``, produces the anchor;
* a link with empty display text or a control character in it.

External URLs, ``mailto:`` links and anchors into non-Markdown files (line
anchors such as ``#L10``) are not checked: they need the network or the
forge's own renderer. Anchors use the same slug rules as
``scripts/check_release_surfaces.py``, which the release gate also uses.
"""

import pathlib
import re
import runpy
import unittest
from urllib.parse import unquote

ROOT = pathlib.Path(__file__).resolve().parents[1]
heading_anchors = runpy.run_path(str(ROOT / "scripts" / "check_release_surfaces.py"))["heading_anchors"]

FENCE = re.compile(r"^(```|~~~).*?^\1[ \t]*$", re.S | re.M)
INLINE_CODE = re.compile(r"`[^`\n]*`")
INLINE_LINK = re.compile(r"(?<!\!)\[((?:[^\[\]]|\[[^\]]*\])*)\]\(\s*<?([^)\s>]*)>?(?:\s+\"[^\"]*\")?\s*\)")
IMAGE_LINK = re.compile(r"!\[[^\]]*\]\(\s*<?([^)\s>]*)>?(?:\s+\"[^\"]*\")?\s*\)")
REFERENCE_DEF = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*<?(\S+?)>?(?:\s+.*)?$", re.M)
EXPLICIT_ANCHOR = re.compile(r"<a\s+[^>]*(?:id|name)=[\"']([^\"']+)[\"']", re.I)
EXTERNAL = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def markdown_files():
    files = [ROOT / "README.md"]
    files += sorted((ROOT / "docs").rglob("*.md"))
    return files


def prose(text):
    """The page text without code, where link-looking text is not a link."""
    blank = lambda match: re.sub(r"[^\n]", "c", match.group(0))  # keeps offsets and non-empty link text
    return INLINE_CODE.sub(blank, FENCE.sub(blank, text))


def anchors_of(path, cache={}):
    if path not in cache:
        text = path.read_text(encoding="utf-8")
        cache[path] = heading_anchors(path) | {a.lower() for a in EXPLICIT_ANCHOR.findall(text)}
    return cache[path]


def link_problems(page):
    """Problems for one Markdown page, as 'page:line: message' strings."""
    problems = []
    raw = page.read_text(encoding="utf-8")
    text = prose(raw)
    rel = page.relative_to(ROOT).as_posix()
    links = [(m.group(1), m.group(2), m.start()) for m in INLINE_LINK.finditer(text)]
    links += [(None, m.group(1), m.start()) for m in IMAGE_LINK.finditer(text)]
    links += [(None, m.group(1), m.start()) for m in REFERENCE_DEF.finditer(text)]
    for label, target, offset in links:
        line = text.count("\n", 0, offset) + 1
        where = f"{rel}:{line}"
        if label is not None and (not label.strip() or CONTROL.search(label)):
            problems.append(f"{where}: link text is empty or holds a control character: [{label!r}]({target})")
        if not target or EXTERNAL.match(target) or target.startswith("//"):
            continue
        path_part, _, anchor = target.partition("#")
        path_part = unquote(path_part.partition("?")[0])
        if path_part.startswith("/"):
            resolved = (ROOT / path_part.lstrip("/")).resolve()
        elif path_part:
            resolved = (page.parent / path_part).resolve()
        else:
            resolved = page
        if path_part and not resolved.exists():
            problems.append(f"{where}: link target does not exist: {target}")
            continue
        if anchor and resolved.is_file() and resolved.suffix == ".md":
            if unquote(anchor).lower() not in anchors_of(resolved):
                problems.append(f"{where}: heading anchor does not exist: {target}")
    return problems


class DocsLinkContract(unittest.TestCase):
    def test_every_relative_link_and_heading_anchor_resolves(self):
        problems = []
        for page in markdown_files():
            problems += link_problems(page)
        self.assertEqual(problems, [], "\n" + "\n".join(problems))

    def test_the_checker_catches_each_class_it_exists_for(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            global ROOT
            saved, ROOT = ROOT, pathlib.Path(directory).resolve()
            try:
                (ROOT / "docs").mkdir()
                (ROOT / "docs" / "b.md").write_text("# Real heading\n\n## Dup\n\n## Dup\n", encoding="utf-8")
                page = ROOT / "docs" / "a.md"
                page.write_text(
                    "# A\n\n"
                    "[ok](b.md#real-heading) [ok2](b.md#dup-1) [self](#a)\n"
                    "[gone](missing.md) [dead](b.md#renamed) [selfdead](#nope) [ext](https://x.example/#y)\n"
                    "[](b.md) [bad\x01](b.md) `[code](missing.md)`\n"
                    "```\n[fenced](missing.md)\n```\n",
                    encoding="utf-8")
                found = link_problems(page)
            finally:
                ROOT = saved
        joined = "\n".join(found)
        self.assertEqual(len(found), 5, joined)
        for needle in ("missing.md", "b.md#renamed", "#nope", "empty or holds a control character"):
            self.assertIn(needle, joined)


if __name__ == "__main__":
    unittest.main()
