"""Repository attributes that tests depend on (#1017)."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_design_fixtures_are_byte_exact_on_every_checkout():
    """SHA256SUMS.txt and the ZIP-member checks hash committed bytes; a Windows
    checkout with core.autocrlf=true would rewrite the STEP, Python and Markdown
    fixtures' line endings unless the corpus is marked -text."""
    rules = [line.split() for line in (ROOT / ".gitattributes").read_text().splitlines()
             if line.strip() and not line.lstrip().startswith("#")]
    assert ["tests/corpus/design/**", "-text"] in rules
