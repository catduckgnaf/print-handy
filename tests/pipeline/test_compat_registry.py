"""Versioned compatibility registry (#1110 criterion 5): integrity and converter parity.

The registry is the single source for the compatibility decisions the 3MF converter makes.
These tests pin that every rule is complete and evidenced, that every evidence reference
resolves to a real test or doc section, and that the converter's live tables and outcomes
match the registry (no drift between the registry and the code that consults it).
"""
import ast
import re
from pathlib import Path

import pytest

from print_handy.core import author_settings, compat_registry, orca_adapter

ROOT = Path(__file__).resolve().parents[2]

REQUIRED_FIELDS = ("id", "kind", "source_family", "source_versions", "destination_family",
                   "destination_versions", "reason", "evidence_tests", "evidence_docs")


def test_registry_has_a_version_and_unique_ids():
    assert isinstance(compat_registry.REGISTRY_VERSION, int) and compat_registry.REGISTRY_VERSION >= 1
    ids = [rule.id for rule in compat_registry.RULES]
    assert len(ids) == len(set(ids)), "rule ids must be unique"
    assert len(ids) >= 15, "the registry must cover the decisions the converter actually makes"


def test_every_rule_carries_the_required_fields():
    for rule in compat_registry.RULES:
        for field in REQUIRED_FIELDS:
            value = getattr(rule, field)
            assert value not in (None, "", ()), f"rule {rule.id} is missing {field}"
        assert rule.reason.strip(), f"rule {rule.id} has no reason"
        assert rule.evidence_tests or rule.evidence_docs, f"rule {rule.id} has no evidence"


def test_version_applicability_specs_parse():
    for rule in compat_registry.RULES:
        for spec in (rule.source_versions, rule.destination_versions):
            compat_registry.parse_version_spec(spec)  # raises on a malformed spec
    assert compat_registry.applies("any", None)
    assert compat_registry.applies(">=2.4,<3.0", "2.5.0")
    assert not compat_registry.applies(">=2.4,<3.0", "2.3.0")
    assert not compat_registry.applies(">=2.4", None), "an unknown version matches only unrestricted rules"


def _test_names(path):
    tree = ast.parse(Path(path).read_text())
    return {node.name for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))} | {
        node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)}


def test_every_evidence_reference_resolves():
    for rule in compat_registry.RULES:
        for ref in rule.evidence_tests:
            path, _, name = ref.partition("::")
            full = ROOT / path
            assert full.is_file(), f"rule {rule.id} cites missing test file {path}"
            assert name in _test_names(full), f"rule {rule.id} cites missing test {ref}"
        for ref in rule.evidence_docs:
            path, _, anchor = ref.partition("#")
            headings = {re.sub(r"[^a-z0-9]+", "-", line.lstrip("# ").lower()).strip("-")
                        for line in (ROOT / path).read_text().splitlines() if line.startswith("#")}
            assert anchor in headings, f"rule {rule.id} cites missing doc anchor {ref}"


def test_converter_tables_are_the_registry_tables():
    prusa = compat_registry.get_rule("prusa-key-translation")
    assert dict(prusa.parameters["mapping"]) == author_settings.PRUSA_KEYS
    values = compat_registry.get_rule("prusa-value-translation")
    assert dict(values.parameters["mapping"]) == author_settings._PRUSA_VALUES
    enums = compat_registry.get_rule("orca-enum-fallback")
    assert {k: set(v) for k, v in enums.parameters["enums"].items()} == \
        {k: set(v) for k, v in author_settings.ORCA_ENUMS.items()}
    assert tuple(compat_registry.get_rule("author-intent-allowlist").parameters["keys"]) == orca_adapter.PROCESS_KEYS
    assert dict(compat_registry.get_rule("arachne-flex-profile").parameters["profile"]) == orca_adapter.FLEX_PROFILE
    assert dict(compat_registry.get_rule("imported-auto-sentinel-normalization").parameters["sentinels"]) == \
        orca_adapter.IMPORTED_AUTO_SENTINELS
    foreign = compat_registry.get_rule("foreign-machine-key-removal")
    assert foreign.parameters["pattern"] == orca_adapter._FOREIGN_KEY.pattern
    assert foreign.parameters["variant_pattern"] == orca_adapter._VARIANT_KEY.pattern
    assert set(foreign.parameters["preset_meta"]) == set(orca_adapter._PRESET_META)
    assert compat_registry.get_rule("bed-type-replacement").parameters["default_bed_type"] == \
        orca_adapter.ORCA_DEFAULT_BED_TYPE


def test_percent_line_width_parity_with_registry():
    rule = compat_registry.get_rule("orca-percent-line-width-to-bambu-mm")
    assert rule.source_family == "orca" and rule.destination_family == "bambu_studio"
    out = orca_adapter.convert_percent_line_widths(
        {"inner_wall_line_width": "110%", "outer_wall_line_width": "100%", "layer_height": "0.2"}, 0.4)
    assert out == {"inner_wall_line_width": "0.44", "outer_wall_line_width": "0.4", "layer_height": "0.2"}


def test_enum_fallback_parity_with_registry():
    template = {"sparse_infill_pattern": "grid"}
    carried, notes = author_settings.carry({"sparse_infill_pattern": "voronoi"}, template,
                                           orca_adapter.PROCESS_KEYS)
    assert carried["sparse_infill_pattern"] == "grid" and notes
    assert author_settings.invalid_enum("sparse_infill_pattern", "gyroid") is False


def test_hardware_only_key_protection_parity_with_registry(tmp_path):
    import json, zipfile
    from tests.corpus.make_models import box, write_3mf
    src = tmp_path / "foreign.3mf"
    write_3mf(src, [("Base", 1, box(20, 20, 10), (60, 60))], [("Plate 1", [0])],
              [("#FFFFFF", "PLA")], project_extra={
                  "printer_model": "Other", "machine_start_gcode": "FOREIGN",
                  "wall_loops": "3", "inherits": "Foreign"})
    job = {"wall_loops": "2", "curr_bed_type": "Cool Plate"}
    orca_adapter.adopt_job_settings(src, job)
    with zipfile.ZipFile(src) as archive:
        project = json.loads(archive.read("Metadata/project_settings.config"))
    assert "machine_start_gcode" not in project and "printer_model" not in project
    assert project["wall_loops"] == "2" and project["inherits"] == ""


def test_registry_lookup_refuses_an_unknown_id():
    with pytest.raises(KeyError):
        compat_registry.get_rule("no-such-rule")
