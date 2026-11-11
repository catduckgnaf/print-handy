"""No em dashes in the user docs (#1242, #1156; the ban is in docs/BRANDING.md).

#1212 cleared DOCKER.md and WEBUI.md, and dashes were still left in other
user pages, so the rule is checked here instead of by hand. The scope is
README.md and the user tree, docs/*.md; the developer records in
docs/history/ are kept as written.

The one exception is a table cell that holds nothing but an em dash, the
empty-cell placeholder of the settings tables. It is not prose.
"""

import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
EM_DASH = "—"
PLACEHOLDER_CELL = re.compile(r"\|\s*—\s*(?=\|)")


def user_docs():
    return [ROOT / "README.md"] + sorted((ROOT / "docs").glob("*.md"))


class NoEmDashInUserDocs(unittest.TestCase):
    def test_user_docs_have_no_prose_em_dash(self):
        found = []
        for path in user_docs():
            text = path.read_text(encoding="utf-8")
            for number, line in enumerate(text.splitlines(), 1):
                if line.lstrip().startswith("|"):
                    line = PLACEHOLDER_CELL.sub("|", line)
                if EM_DASH in line:
                    found.append(f"{path.relative_to(ROOT).as_posix()}:{number}")
        self.assertEqual(
            found, [],
            "Em dashes in user docs (BRANDING bans them; use a comma, colon, "
            "semicolon, parentheses or a new sentence): " + ", ".join(found))

    def test_placeholder_cell_is_the_only_exception(self):
        self.assertNotIn(EM_DASH, PLACEHOLDER_CELL.sub("|", "| a | — | b |"))
        self.assertIn(EM_DASH, PLACEHOLDER_CELL.sub("|", "| a — b |"))


if __name__ == "__main__":
    unittest.main()
