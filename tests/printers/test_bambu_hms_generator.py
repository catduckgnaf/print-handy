"""Generator merge rules for scripts/bambu-hms.py (the shipped table's builder)."""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _generator():
    spec = importlib.util.spec_from_file_location("bambu_hms_gen", ROOT / "scripts" / "bambu-hms.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


KEY = "0300020000010008"


def _files(text_by_prefix):
    """merge() consumes Bambu Studio's per-prefix entry lists."""
    return {prefix: [{"ecode": KEY, "intro": text}]
            for prefix, text in text_by_prefix.items()}


def test_tie_break_is_restricted_to_the_most_frequent_texts():
    """P2-102: the lowest sorted prefix overall may carry a minority text.

    Prefixes 0300/0500/0600/0700/0800 give A once, B twice and C twice. The
    common text must be B or C — the tie breaks to B, from the lowest prefix
    carrying a most-frequent text — never to A just because 0300 sorts first.
    """
    table, overrides = _generator().merge(_files({
        "0300": "Minority text.",
        "0500": "Frequent one.",
        "0600": "Frequent two.",
        "0700": "Frequent one.",
        "0800": "Frequent two.",
    }))
    assert table[KEY] == "Frequent one."
    assert overrides["0300"][KEY] == "Minority text."
    assert overrides["0600"][KEY] == "Frequent two."
    assert "0500" not in overrides and "0700" not in overrides


def test_clear_majority_and_unanimity_are_unchanged():
    table, overrides = _generator().merge(_files({
        "0300": "Same.", "0500": "Same.", "0600": "Odd.",
    }))
    assert table[KEY] == "Same."
    assert overrides == {"0600": {KEY: "Odd."}}
