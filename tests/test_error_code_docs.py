"""#924: Reference codes are clickable links to the docs catalog.

Pins: (1) the code registry in `diagnostics` / `failure_report` and the
catalog in docs/DIAGNOSTICS.md match both ways, so a link can never
point at a section that does not exist; (2) chat posts and operator
messages carry the Markdown link while the raw PH_ token stays in the
text (log greps keep working); (3) a classified failure links its
specific signature code, not just the generic marker; (4) plain-text
surfaces (doctor rows, Discord startup exits) carry the code plus a
plain URL; (5) structured receipts keep their exact machine-readable
shape — no link text ever lands in a receipt.
"""
import re
from pathlib import Path

from print_handy import diagnostics, doctor
from print_handy.core import failure_report, slice_failure

ROOT = Path(__file__).resolve().parents[1]
DIAGNOSTICS_MD = (ROOT / "docs" / "DIAGNOSTICS.md").read_text(encoding="utf-8")
BASE = "https://catduckgnaf.github.io/print-handy-docs/diagnostics"


def _catalog_anchors():
    return set(re.findall(r"\{#(ph-[a-z0-9-]+)\}", DIAGNOSTICS_MD))


def _all_codes():
    return set(diagnostics.ERROR_CODES) | set(failure_report.SIGNATURE_CODES.values())


# ------------------------------------------------------------- registry/docs parity

def test_every_registered_code_has_a_catalog_section_at_its_anchor():
    anchors = _catalog_anchors()
    missing = [code for code in sorted(_all_codes())
               if diagnostics.anchor_for(code) not in anchors]
    assert missing == []


def test_every_catalog_section_is_a_registered_code():
    codes = {diagnostics.anchor_for(code) for code in _all_codes()}
    extra = sorted(_catalog_anchors() - codes)
    assert extra == []


def test_catalog_size_matches_the_inventory():
    # 24 marker codes + 12 signature codes (Phase 0 inventory for #924;
    # the 12th signature, gcode_3mf_package, arrived with #922).
    # ERROR_CODES holds both families once failure_report has registered
    # its signature codes (the import at the top of this module).
    marker_count = sum(len(c) for c in diagnostics.MARKER_CODE_CATEGORIES.values())
    assert marker_count == 24
    assert len(failure_report.SIGNATURE_CODES) == 12
    assert len(diagnostics.ERROR_CODES) == 36
    assert len(_catalog_anchors()) == 36


def test_every_emittable_marker_pair_is_registered():
    for subsystem, categories in diagnostics.MARKER_CODE_CATEGORIES.items():
        for category in categories:
            assert diagnostics.marker(subsystem, category) in diagnostics.ERROR_CODES


def test_signature_codes_follow_the_stage_rule():
    for sig_id, code in failure_report.SIGNATURE_CODES.items():
        stage = failure_report.from_signature(sig_id).stage
        assert code == f"PH_{stage.upper().replace(' ', '_')}_{sig_id.upper()}"
    assert failure_report.SIGNATURE_CODES["bad_3mf_structure"] == "PH_PARSE_BAD_3MF_STRUCTURE"


def test_base_url_is_the_repo_documented_site():
    assert diagnostics.DOCS_BASE_URL == "https://catduckgnaf.github.io/print-handy-docs/"
    assert diagnostics.DIAGNOSTICS_URL == BASE


# ------------------------------------------------------------- link building

def test_anchor_slug_rule():
    assert diagnostics.anchor_for("PH_INTAKE_CONFIGURATION") == "ph-intake-configuration"
    assert diagnostics.anchor_for("PH_PARSE_BAD_3MF_STRUCTURE") == "ph-parse-bad-3mf-structure"


def test_docs_url_for_registered_and_unregistered_codes():
    assert diagnostics.docs_url_for("PH_INTAKE_CONFIGURATION") == \
        BASE + "#ph-intake-configuration"
    # A pair no boundary emits still links somewhere real: the general section.
    assert diagnostics.docs_url_for("PH_GENERAL_UNKNOWN") == BASE + "#reference-codes"


def test_markdown_reference_link_keeps_the_raw_token():
    line = diagnostics.reference_line("PH_SLICING_CONFIGURATION")
    assert line == f"Reference: [PH_SLICING_CONFIGURATION]({BASE}#ph-slicing-configuration)"
    assert "PH_SLICING_CONFIGURATION" in line  # grep target survives in raw text


# ------------------------------------------------------------- chat surfaces

def test_plain_post_ends_with_the_markdown_link():
    post = diagnostics.plain_post(
        "Slicing that model did not work.", subsystem="slicing",
        exc=RuntimeError("raw detail"))
    assert post.rstrip().endswith(
        f"Reference: [PH_SLICING_UNKNOWN]({BASE}#ph-slicing-unknown)")
    assert "raw detail" not in post


def test_operator_message_ends_with_the_markdown_link():
    text = diagnostics.operator_message(
        "Spool tracking was skipped", subsystem="spoolman",
        exc=ConnectionRefusedError("refused"))
    assert f"Reference: [PH_SPOOLMAN_TRANSIENT]({BASE}#ph-spoolman-transient)" in text


def test_unregistered_pair_falls_back_to_the_reference_codes_anchor():
    post = diagnostics.plain_post(
        "That did not work.", subsystem="delivery",
        category=diagnostics.Category.CONFIGURATION)
    assert post.rstrip().endswith(f"Reference: [PH_DELIVERY_CONFIGURATION]({BASE}#reference-codes)")


def test_failure_post_links_the_specific_signature_code():
    exc = slice_failure.SliceFailure("x", kind="engine", signature="bad_3mf_structure")
    post = failure_report.post(exc, "parse", subsystem="intake")
    assert f"Reference: [PH_PARSE_BAD_3MF_STRUCTURE]({BASE}#ph-parse-bad-3mf-structure)" in post
    assert "PH_INTAKE_UNKNOWN" not in post


def test_failure_post_without_a_signature_links_the_generic_marker():
    post = failure_report.post(RuntimeError("mystery"), "intake", subsystem="intake")
    assert f"Reference: [PH_INTAKE_UNKNOWN]({BASE}#ph-intake-unknown)" in post


def test_slice_failure_post_links_its_signature_code():
    exc = slice_failure.SliceFailure("x", kind="engine", signature="out_of_memory")
    post = slice_failure.post(exc)
    assert f"Reference: [PH_SLICE_OUT_OF_MEMORY]({BASE}#ph-slice-out-of-memory)" in post


# ------------------------------------------------------------- plain-text surfaces

def test_plain_reference_tail_for_terminal_surfaces():
    tail = diagnostics.plain_reference_tail("PH_DISCORD_CONFIGURATION")
    assert tail == f"Reference: PH_DISCORD_CONFIGURATION ({BASE}#ph-discord-configuration)"


def test_doctor_logging_row_carries_code_and_plain_url(tmp_path):
    data = {"logging": {"file": str(tmp_path / "missing-dir" / "print-handy.log")}}
    try:
        doctor.check_logging(data)
    except RuntimeError as exc:
        text = str(exc)
    else:  # the row must fail loudly: the folder does not exist
        raise AssertionError("check_logging did not raise for a missing folder")
    assert "[PH_LOGGING_CONFIGURATION]" in text
    assert f"({BASE}#ph-logging-configuration)" in text


# ------------------------------------------------------------- preservation

def test_marker_tokens_are_unchanged():
    assert diagnostics.marker("spoolman", diagnostics.Category.TRANSIENT) == "PH_SPOOLMAN_TRANSIENT"
    assert diagnostics.marker("intake", diagnostics.Category.CONFIGURATION) == \
        "PH_INTAKE_CONFIGURATION"


def test_receipt_record_shape_is_unchanged():
    exc = slice_failure.SliceFailure("x", kind="engine", signature="model_unreadable")
    record = slice_failure.report(exc).as_dict()
    assert record["signature"] == "model_unreadable"
    assert set(record) == {"stage", "cause", "recovery", "submitted", "signature"}
    assert "http" not in str(record) and "PH_" not in str(record)
