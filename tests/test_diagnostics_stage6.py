"""#390 Stage 6: the Discord action handlers' generic failure posts meet
the operator bar — what failed, the state/remedy note, and the stable
greppable PH_* marker, redacted. #467: the raw exception text is no
longer posted; it is classified for the marker and kept in the log."""
import importlib.util
import re
from pathlib import Path

PLUGIN_PATH = Path(__file__).parents[1] / "plugins/print-feedback/__init__.py"
SPEC = importlib.util.spec_from_file_location("print_feedback_stage6", PLUGIN_PATH)
assert SPEC is not None and SPEC.loader is not None
plugin = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(plugin)


def test_failure_post_carries_label_note_and_marker_without_raw_detail():
    post = plugin._failure_post(
        "Stop", RuntimeError("printer did not answer"), "Check the printer screen.",
        subsystem="printer")
    assert post.startswith("Stop didn't work.")
    assert "Check the printer screen." in post
    assert "printer did not answer" not in post  # raw detail demoted to the log (#467)
    assert "Reference: [PH_PRINTER_UNKNOWN](" in post


def test_failure_post_marker_reflects_the_classified_category():
    post = plugin._failure_post(
        "Next plate", ConnectionError("connection reset"), "Check the printer screen before retrying.",
        subsystem="printer")
    assert "Reference: [PH_PRINTER_TRANSIENT](" in post
    post = plugin._failure_post(
        "Color selection", RuntimeError("bad profile"), "No slicing, upload or print started.",
        subsystem="intake")
    assert "Reference: [PH_INTAKE_UNKNOWN](" in post


def test_failure_post_is_redacted():
    post = plugin._failure_post(
        "Stop", RuntimeError("auth failed token=sekrit123"), "Check the printer screen.",
        subsystem="printer")
    assert "sekrit123" not in post
    assert "auth failed" not in post  # the whole raw exception text stays out of chat (#467)


def test_every_generic_handler_post_uses_the_helper():
    from handler_source import handler_source  # the handlers moved into print_handy/bot/handlers (#1323)
    source = handler_source()
    # The eight audited generic posts (color selection, next plate x2,
    # stop, resume, print again, restart, alert buttons) all route
    # through _failure_post; no bare f-string failure post remains.
    assert len(re.findall(r"_failure_post\(", source)) >= 9  # helper + 8 call sites
    # No "failed: {exc}" f-string remains in the helper (the raw text is
    # classified, not posted, #467); the only "did not work: {exc}" left is
    # the curated DesignError post (a deliberate, operator-phrased error
    # type, in the same class as the NextPlateError refusals — audited).
    assert len(re.findall(r"failed: \{exc\}", source)) == 0
