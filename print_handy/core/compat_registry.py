"""Versioned compatibility registry for 3MF conversion decisions (#1110 criterion 5).

Every compatibility decision the converter makes — translating a foreign slicer's keys and
values, falling back on an enumeration Orca does not accept, protecting the destination
machine's hardware-only keys, converting percentage line widths for a Bambu destination —
is a rule here, not an open-coded check at a call site. Each rule carries a unique id, its
decision kind, the source and destination slicer families it applies to with a version
applicability spec, the reason for the decision, and regression evidence: named tests
and/or a documentation section. No rule without evidence; no unexplained broad
substitution: a rule's parameters name exactly the keys, values or patterns it governs.

Version applicability specs are "any" or a comma-separated list of comparisons against a
dotted version (for example ">=2.4,<3.0"). An unknown version (None) matches only an
unrestricted ("any") spec: a rule written for known versions never silently governs a
source or destination whose version was not established. Phase A of the #1110 program
records the established source/destination versions in conversion provenance by reading
the same family/version fields this registry declares.

The converter consults this module: author_settings and orca_adapter build their live
tables from the rule parameters below, so the registry and the behavior cannot drift
(tests/pipeline/test_compat_registry.py asserts the parity).
"""
from dataclasses import dataclass, field
from types import MappingProxyType

REGISTRY_VERSION = 1

_DOC = "docs/PRINTER-PROFILES.md"
_FOREIGN = _DOC + "#projects-from-another-printer-or-slicer"
_REGISTRY_DOC = _DOC + "#versioned-compatibility-registry"
_EVIDENCE = _DOC + "#compatibility-rules-with-direct-regression-evidence"
_TA = "tests/pipeline/test_author_settings.py"
_TO = "tests/pipeline/test_orca_adapter.py"
_TS = "tests/pipeline/test_orca_sanitize.py"
_TF = "tests/pipeline/test_foreign_project_adoption.py"
_TP = "tests/pipeline/test_prusa_object_settings.py"


@dataclass(frozen=True)
class Rule:
    id: str
    kind: str
    source_family: str
    source_versions: str
    destination_family: str
    destination_versions: str
    reason: str
    evidence_tests: tuple = ()
    evidence_docs: tuple = ()
    parameters: dict = field(default_factory=dict)

    def __post_init__(self):
        # The converter builds its live tables from these parameters, so they are frozen
        # (read-only mappings, tuples) and never shared: no caller can change a rule.
        object.__setattr__(self, 'parameters', _freeze(self.parameters))


def _freeze(value):
    """A deeply read-only copy: dicts become read-only mappings, lists and tuples tuples."""
    if isinstance(value, (dict, MappingProxyType)):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set):
        return frozenset(value)
    return value


def parse_version_spec(spec):
    """Parse a version applicability spec into [(operator, version tuple)]; [] means any."""
    if spec == "any":
        return []
    out = []
    for part in spec.split(","):
        part = part.strip()
        for op in (">=", "<=", "==", ">", "<"):
            if part.startswith(op):
                out.append((op, _version_tuple(part[len(op):])))
                break
        else:
            raise ValueError(f"malformed version applicability spec: {spec!r}")
    return out


def _version_tuple(text):
    parts = []
    for piece in str(text).strip().split("."):
        digits = ""
        for char in piece:
            if char.isdigit():
                digits += char
            else:
                break
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def applies(spec, version):
    """True when `version` (dotted string or None) satisfies the applicability `spec`."""
    clauses = parse_version_spec(spec)
    if not clauses:
        return True
    if version is None:
        return False
    value = _version_tuple(version)
    for op, bound in clauses:
        width = max(len(value), len(bound))
        left, right = value + (0,) * (width - len(value)), bound + (0,) * (width - len(bound))
        if op == ">=" and not left >= right:
            return False
        if op == "<=" and not left <= right:
            return False
        if op == ">" and not left > right:
            return False
        if op == "<" and not left < right:
            return False
        if op == "==" and not left == right:
            return False
    return True


def rule_applies(rule, source_family=None, source_version=None,
                 destination_family=None, destination_version=None):
    """True when a rule governs the given source/destination pair (None family = not checked)."""
    if source_family is not None and rule.source_family not in ("any", source_family):
        return False
    if destination_family is not None and rule.destination_family not in ("any", destination_family):
        return False
    return (applies(rule.source_versions, source_version)
            and applies(rule.destination_versions, destination_version))


_PRUSA_KEYS = {
    'layer_height': 'layer_height', 'first_layer_height': 'initial_layer_print_height',
    'perimeters': 'wall_loops', 'fill_density': 'sparse_infill_density',
    'fill_pattern': 'sparse_infill_pattern', 'fill_angle': 'infill_direction',
    'top_fill_pattern': 'top_surface_pattern', 'bottom_fill_pattern': 'bottom_surface_pattern',
    'top_solid_layers': 'top_shell_layers', 'bottom_solid_layers': 'bottom_shell_layers',
    'top_solid_min_thickness': 'top_shell_thickness', 'bottom_solid_min_thickness': 'bottom_shell_thickness',
    'support_material': 'enable_support', 'support_material_threshold': 'support_threshold_angle',
    'support_material_buildplate_only': 'support_on_build_plate_only', 'seam_position': 'seam_position',
    'elefant_foot_compensation': 'elefant_foot_compensation', 'ironing_flowrate': 'ironing_flow',
    'ironing_spacing': 'ironing_spacing', 'brim_width': 'brim_width', 'brim_separation': 'brim_object_gap',
    'external_perimeter_speed': 'outer_wall_speed', 'perimeter_speed': 'inner_wall_speed',
    'infill_speed': 'sparse_infill_speed', 'solid_infill_speed': 'internal_solid_infill_speed',
    'top_solid_infill_speed': 'top_surface_speed', 'gap_fill_speed': 'gap_infill_speed',
    'support_material_speed': 'support_speed', 'support_material_interface_speed': 'support_interface_speed',
    'bridge_speed': 'bridge_speed', 'first_layer_speed': 'initial_layer_speed', 'travel_speed': 'travel_speed',
    'ironing_speed': 'ironing_speed', 'small_perimeter_speed': 'small_perimeter_speed',
}
_PRUSA_VALUES = {'rectilinear': 'zig-zag', 'stars': 'tri-hexagon', 'monotoniclines': 'monotonicline', 'rear': 'back'}
_INFILL = ('concentric', 'zig-zag', 'grid', 'line', 'cubic', 'triangles', 'tri-hexagon', 'gyroid', 'honeycomb',
           'adaptivecubic', 'alignedrectilinear', '3dhoneycomb', 'hilbertcurve', 'archimedeanchords', 'octagramspiral',
           'supportcubic', 'lightning', 'crosshatch', 'quartercubic', 'zigzag', 'crosszag', 'lockedzag', '2dlattice',
           '2dhoneycomb', 'tpmsd')
_SURFACE = ('concentric', 'zig-zag', 'monotonic', 'monotonicline', 'alignedrectilinear', 'hilbertcurve',
            'archimedeanchords', 'octagramspiral')
_ORCA_ENUMS = {
    'sparse_infill_pattern': _INFILL, 'top_surface_pattern': _SURFACE, 'bottom_surface_pattern': _SURFACE,
    'ironing_pattern': ('concentric', 'zig-zag'),
    'ironing_type': ('no ironing', 'top', 'topmost', 'solid'),
    'support_type': ('normal(auto)', 'tree(auto)', 'normal(manual)', 'tree(manual)'),
    'support_style': ('default', 'grid', 'snug', 'organic', 'tree_slim', 'tree_strong', 'tree_hybrid'),
    'seam_position': ('nearest', 'aligned', 'aligned_back', 'back', 'random'),
    'wall_sequence': ('inner wall/outer wall', 'outer wall/inner wall', 'inner-outer-inner wall'),
    'wall_generator': ('classic', 'arachne'),
    'gap_fill_target': ('everywhere', 'topbottom', 'nowhere'),
}
_PROCESS_KEYS = ('layer_height', 'initial_layer_print_height', 'wall_loops', 'sparse_infill_density',
                 'sparse_infill_pattern', 'brim_type', 'brim_width', 'brim_object_gap', 'enable_support',
                 'support_type', 'support_style', 'support_threshold_angle', 'support_critical_regions_only',
                 'support_remove_small_overhang', 'support_on_build_plate_only', 'support_object_xy_distance',
                 'top_shell_layers',
                 'bottom_shell_layers', 'top_shell_thickness', 'bottom_shell_thickness', 'gap_fill_target',
                 'filter_out_gap_fill', 'wall_generator', 'seam_position', 'wall_sequence', 'infill_direction',
                 'top_surface_pattern', 'bottom_surface_pattern', 'ironing_type', 'ironing_pattern', 'ironing_flow',
                 'ironing_spacing', 'elefant_foot_compensation')
_FLEX_PROFILE = {
    'wall_generator': 'arachne', 'wall_loops': '2',
    'sparse_infill_density': '15%', 'sparse_infill_pattern': 'line',
    'initial_layer_print_height': '0.2', 'initial_layer_speed': '20', 'initial_layer_infill_speed': '20',
    'outer_wall_speed': '70', 'inner_wall_speed': '130', 'sparse_infill_speed': '160',
    'brim_type': 'no_brim', 'brim_width': '0', 'enable_support': '0', 'raft_layers': '0',
    'ironing_type': 'no ironing', 'fuzzy_skin': 'none',
}
_FOREIGN_PATTERN = (r'.*_(?:speed|acceleration|jerk|gcode|temp|temperature)(?:_initial_layer(?:_single)?|_range_(?:low|high))?'
                    r'|(?:machine|printer|printable|bed|nozzle|extruder|retract|retraction|z_hop|print_host|printhost)_.*'
                    r'|curr_bed_type|default_bed_type|host_type|post_process|z_hop|wipe|wipe_distance|travel_slope'
                    r'|slicing_pipeline_plugin|plugins')
_ORCA = ">=2.4,<3.0"  # the OrcaSlicer config names Print Handy targets (2.4/2.5 era)

RULES = (
    Rule("author-intent-allowlist", "allowlist", "any", "any", "orca", _ORCA,
         "Only settings that describe the model, not the source machine, may ride over the destination "
         "template; the allowlist names each such key explicitly so no other source key is substituted.",
         (_TA + "::test_more_author_intent_is_kept_over_the_template",
          _TF + "::test_derived_plate_reaches_orca_with_the_job_profile_not_the_foreign_one"),
         (_FOREIGN, _REGISTRY_DOC), {"keys": _PROCESS_KEYS}),
    Rule("prusa-key-translation", "key_translation", "prusaslicer", "any", "orca", _ORCA,
         "PrusaSlicer names the kept author settings differently from Orca; each Prusa key maps to exactly "
         "one Orca key, and an unmapped key is ignored rather than guessed. perimeter_generator is "
         "deliberately unmapped: PrusaSlicer defaults to Arachne, and mapping it would select the "
         "print-in-place flex profile for every PrusaSlicer project.",
         (_TA + "::test_prusa_keys_translate_to_orca_names_and_values",
          _TP + "::test_prusa_model_config_parses_objects_and_volumes"),
         (_FOREIGN, _REGISTRY_DOC), {"mapping": _PRUSA_KEYS}),
    Rule("prusa-value-translation", "value_translation", "prusaslicer", "any", "orca", _ORCA,
         "Four PrusaSlicer enumeration spellings differ from Orca's spelling for the same value; any other "
         "value passes through unchanged to the Orca enumeration check instead of being rewritten.",
         (_TA + "::test_prusa_keys_translate_to_orca_names_and_values",), (_FOREIGN,),
         {"mapping": _PRUSA_VALUES}),
    Rule("prusa-support-threshold-zero-drop", "value_drop", "prusaslicer", "any", "orca", _ORCA,
         "A PrusaSlicer support threshold of 0 means 'detect automatically', not a zero-degree angle, so "
         "the translated key is dropped rather than carried as an angle.",
         (_TA + "::test_prusa_support_threshold_zero_means_auto_and_is_not_an_angle",), (_FOREIGN,),
         {"key": "support_threshold_angle", "sentinel": "0"}),
    Rule("prusa-ironing-translation", "value_translation", "prusaslicer", "any", "orca", _ORCA,
         "PrusaSlicer carries ironing as a separate on/off flag plus an optional type; Orca carries one "
         "ironing_type enumeration, so the flag becomes 'no ironing' when off and the named type (default "
         "'top') when on.",
         (_TA + "::test_prusa_keys_translate_to_orca_names_and_values",
          _TA + "::test_prusa_settings_reach_the_slice_profile"), (_FOREIGN,),
         {"flag_key": "ironing", "type_key": "ironing_type", "off_value": "no ironing", "default_on_value": "top"}),
    Rule("prusa-support-style-translation", "value_translation", "prusaslicer", "any", "orca", _ORCA,
         "PrusaSlicer's support_material_style and support_material_auto together name Orca's support_type "
         "and support_style; a style Orca has no name for (outside grid, snug, organic) becomes 'default' "
         "rather than an invented style.",
         (_TA + "::test_prusa_keys_translate_to_orca_names_and_values",), (_FOREIGN,),
         {"style_key": "support_material_style", "auto_key": "support_material_auto",
          "carried_styles": ("grid", "snug", "organic"), "fallback_style": "default", "tree_style": "organic"}),
    Rule("prusa-wall-sequence-translation", "value_translation", "prusaslicer", "any", "orca", _ORCA,
         "PrusaSlicer's external_perimeters_first flag is Orca's wall_sequence enumeration; the flag's two "
         "states map to the two wall orders, nothing else.",
         (_TA + "::test_prusa_keys_translate_to_orca_names_and_values",), (_FOREIGN,),
         {"flag_key": "external_perimeters_first", "key": "wall_sequence",
          "on_value": "outer wall/inner wall", "off_value": "inner wall/outer wall"}),
    Rule("orca-enum-fallback", "enum_fallback", "any", "any", "orca", _ORCA,
         "An author enumeration value Orca's CLI does not accept (another slicer's spelling or a newer "
         "slicer's option) would fail or mis-slice; it is replaced by the destination template's value, or "
         "dropped to Orca's default when the template sets none, and the replacement is named in a "
         "conversion note. The accepted sets are the OrcaSlicer 2.4/2.5 config names, per key.",
         (_TA + "::test_unknown_author_enum_falls_back_to_the_template",
          _TA + "::test_valid_author_enums_pass_unchanged",
          _TA + "::test_bad_enum_never_reaches_the_slice_profile"),
         (_EVIDENCE, _REGISTRY_DOC), {"enums": _ORCA_ENUMS}),
    Rule("designer-lowered-speed-carry", "speed_carry", "any", "any", "orca", _ORCA,
         "Speeds belong to the destination template; a designer who set a speed strictly lower than the "
         "template's made a deliberate choice for that model, so only that lowered value is kept. Faster "
         "author speeds, percentages and 0 ('use the default') are never treated as speeds.",
         (_TA + "::test_only_speeds_below_the_profile_are_kept",
          _TA + "::test_lowered_speeds_reach_the_slice_profile"), (_FOREIGN,),
         {"key_suffix": "_speed", "comparison": "strictly_lower", "positive_plain_numbers_only": True}),
    Rule("organic-tree-hybrid-conversion", "support_style_conversion", "any", "any", "orca", _ORCA,
         "Organic tree supports fail with variable layer height and with a zero top Z distance (a soluble "
         "or zero-gap interface); Tree Hybrid handles both, so organic becomes tree_hybrid only under one "
         "of those two named conditions.",
         (_TA + "::test_organic_trees_become_tree_hybrid_with_zero_top_gap_or_variable_layers",
          _TA + "::test_organic_with_variable_layer_height_slices_as_tree_hybrid"), (_FOREIGN,),
         {"from_style": "organic", "to_style": "tree_hybrid", "gap_key": "support_top_z_distance",
          "gap_default": "0.2", "triggers": ("zero_top_z_distance", "variable_layer_height")}),
    Rule("painted-supports-enable-generation", "support_enable", "any", "any", "orca", _ORCA,
         "Painted support enforcers do nothing while support generation is off, so a source with enforcer "
         "painting slices with supports on — unless the operator decided supports for the job, whose "
         "decision wins. Blocker-only painting changes nothing.",
         (_TA + "::test_painted_support_enforcers_are_detected",
          _TA + "::test_painted_supports_turn_generation_on_unless_the_operator_decided"), (_FOREIGN,),
         {"key": "enable_support", "on_value": "1", "enforcer_state": 1}),
    Rule("arachne-flex-profile", "profile_substitution", "any", "any", "orca", _ORCA,
         "The named flex profile (Arachne, 2 walls, 15% line infill, no brim/supports/ironing/fuzzy skin) keeps "
         "moving gaps from fusing, per the AD5X Flexy Ring guide. Since 2.3.2 it is the recovery when Orca "
         "rejects a slice of a thin print-in-place model, not an up-front swap for any Arachne project "
         "(author_settings.FLEX_ON_ARACHNE, #1373). The substitution is this exact profile, no other keys.",
         ("tests/core/test_flex_print_slicing.py::test_flex_profile_follows_the_guide",
          _TO + "::test_flex_retry_preserves_explicit_supports_and_brim_policy"), (_FOREIGN,),
         {"profile": _FLEX_PROFILE, "trigger_key": "wall_generator", "trigger_value": "arachne"}),
    Rule("gap-fill-default", "default_value", "any", "any", "orca", _ORCA,
         "Gap fill falls back to 'everywhere', never to a template's 'none', which is not an Orca value; "
         "the fallback feeds the enumeration check like any other value.",
         (_TA + "::test_bad_enum_never_reaches_the_slice_profile",), (_FOREIGN,),
         {"key": "gap_fill_target", "default": "everywhere"}),
    Rule("imported-auto-sentinel-normalization", "sentinel_normalization", "any", "any", "orca", _ORCA,
         "Orca writes -1 for two 'automatic' values that its own CLI then rejects on an imported project; "
         "exactly those two named keys are normalized from -1 to 0, and no other negative value is touched. "
         "It runs on every adopted project copy, which is Orca-format by then (a PrusaSlicer plate is "
         "translated and seeded first), so it applies to any source.",
         (_TS + "::test_only_known_rejected_auto_sentinels_are_normalized",
          _TS + "::test_unknown_negative_and_unrejected_values_are_not_normalized"), (_REGISTRY_DOC,),
         {"sentinels": {'raft_first_layer_expansion': '-1', 'tree_support_wall_count': '-1'},
          "replacement": "0"}),
    Rule("imported-template-override", "template_override", "any", "any", "orca", _ORCA,
         "On an imported project copy, the destination template — not the source — owns per-filament motion "
         "values and the named motion/bed keys: each listed key takes the template's value exactly, because "
         "the source values describe another machine's hardware and motion limits.",
         (_TS + "::test_only_known_rejected_auto_sentinels_are_normalized",
          _TF + "::test_derived_plate_reaches_orca_with_the_job_profile_not_the_foreign_one"), (_FOREIGN,),
         {"template_value_keys": ("filament_z_hop_types", "filament_wipe", "filament_wipe_distance"),
          "per_slot_prefix": "filament_", "per_slot_exclude_suffix": "_settings_id",
          "own_template_keys": ("z_hop", "z_hop_types", "travel_slope", "wipe", "wipe_distance",
                                "retract_lift_above", "retract_lift_below", "enable_arc_fitting", "brim_type")}),
    Rule("settings-id-validation", "settings_id_validation", "any", "any", "orca", _ORCA,
         "An imported project's settings ids name presets the destination does not have; every "
         "*_settings_id takes the destination template's value, and a filament_settings_id entry survives "
         "only when the destination's allowlist accepts it for the job's slot count.",
         (_TF + "::test_derived_plate_reaches_orca_with_the_job_profile_not_the_foreign_one",), (_FOREIGN,),
         {"key_suffix": "_settings_id", "filament_key": "filament_settings_id"}),
    Rule("wipe-tower-bed-clamp", "value_clamp", "any", "any", "orca", _ORCA,
         "The prime tower must stay inside the destination bed: wipe_tower_x/y are clamped to the bed "
         "extent minus the job's effective tower width (the configured size, raised for a larger nozzle), "
         "per plate entry.",
         (_TA + "::test_tower_clamp_uses_the_effective_width_not_the_template_width",
          _TA + "::test_every_plates_tower_stays_inside_the_bed"), (_FOREIGN,),
         {"keys": ("wipe_tower_x", "wipe_tower_y"), "area_key": "printable_area", "width_key": "prime_tower_width"}),
    Rule("foreign-machine-key-removal", "hardware_key_removal", "any", "any", "orca", _ORCA,
         "On a copy Orca reads directly, a key that describes the source machine, its motion, temperature, "
         "bed or G-code — and that the job's own profile does not set and the author-intent allowlist does "
         "not keep — is removed so Orca falls back to its default, the same value a sliced STL gets. The "
         "pattern names the key families exactly; multi-extruder variant bookkeeping is exempt because the "
         "printer and filament halves must agree, and preset bookkeeping is never rewritten.",
         (_TF + "::test_derived_plate_reaches_orca_with_the_job_profile_not_the_foreign_one",
          _TF + "::test_plugin_keys_are_stripped_from_an_imported_project",
          "tests/pipeline/test_port_contracts.py::test_shipped_protected_baseline_matches_the_orca_machine_template"),
         (_EVIDENCE, _REGISTRY_DOC),
         {"pattern": _FOREIGN_PATTERN,
          "variant_pattern": r'.*_variant(?:_list)?|.*_extruder_id|extruder_type|nozzle_volume_type',
          "preset_meta": ('name', 'from', 'type', 'instantiation', 'setting_id', 'version', 'inherits',
                          'inherits_group', 'different_settings_to_system', 'compatible_printers',
                          'compatible_printers_condition', 'compatible_prints', 'compatible_prints_condition'),
          "structural_meta": ('name', 'extruder', 'matrix')}),
    Rule("bed-type-replacement", "value_replacement", "any", "any", "orca", _ORCA,
         "The bed type becomes the job's own; when the job sets none it becomes Orca's default Cool Plate, "
         "never a source project's plate (a Supertack or textured plate the destination may not have).",
         (_TF + "::test_derived_plate_reaches_orca_with_the_job_profile_not_the_foreign_one",), (_FOREIGN,),
         {"key": "curr_bed_type", "default_bed_type": "Cool Plate"}),
    Rule("preset-bookkeeping-clearing", "bookkeeping_clearing", "any", "any", "orca", _ORCA,
         "inherits, inherits_group and different_settings_to_system are cleared on an adopted copy so no "
         "source setting is re-applied as a 'user change' over the job's profile.",
         (_TF + "::test_derived_plate_reaches_orca_with_the_job_profile_not_the_foreign_one",), (_FOREIGN,),
         {"keys": ("inherits", "inherits_group", "different_settings_to_system"), "replacement": ""}),
    Rule("override-restriction", "override_restriction", "any", "any", "orca", _ORCA,
         "Per-object, per-part and height-range overrides on an adopted copy survive only as kept "
         "author-intent keys whose values Orca accepts; a plate keeps no foreign machine or bed setting at "
         "all. Structural metadata (name, extruder, matrix) and source_ bookkeeping are not overrides.",
         (_TF + "::test_adopt_keeps_per_filament_list_shape_and_layer_range_intent",
          _TF + "::test_plugin_keys_are_stripped_from_plate_overrides"), (_FOREIGN,),
         {"structural_meta": ('name', 'extruder', 'matrix'), "exempt_prefix": "source_"}),
    Rule("orca-percent-line-width-to-bambu-mm", "unit_conversion", "any", "any", "bambu_studio", "any",
         "Bambu Studio reads Orca's percentage line widths as absolute millimeters. Orca percentages are "
         "relative to the destination machine's nozzle diameter, so each *_line_width value ending in % is "
         "converted with that diameter; the conversion fails closed when the diameter cannot be read, so "
         "no percentage width reaches Bambu unconverted. The converted values are the job's own "
         "Orca-named process (the destination templates plus carried author keys), whatever slicer made "
         "the source project, so the rule applies to any source.",
         (_TO + "::test_bambu_fallback_converts_percent_line_widths",), (_EVIDENCE, _REGISTRY_DOC),
         {"key_suffix": "_line_width", "percent_marker": "%", "basis": "destination_nozzle_diameter"}),
    Rule("orca-range-error-clamp", "value_clamp", "any", "any", "orca", _ORCA,
         "Post-failure recovery only: a key Orca itself names in a range error is clamped to the bounds "
         "Orca stated in that error. No key absent from the log is touched and nothing is pre-normalized. "
         "The error comes from the destination Orca on the job's own private project copy, so the rule "
         "applies to any source and its version range is the destination's.",
         (_TS + "::test_range_recovery_clamps_only_named_keys", _TS + "::test_list_values_clamped"),
         (_REGISTRY_DOC,), {"source": "orca_range_error_log"}),
    Rule("support-filament-designation", "designation_carry", "any", "any", "orca", _ORCA,
         "A filament the author designated for supports (and optionally the interface) prints no part, so "
         "it is asked about and loaded like any source color and the process keys point at its tool; a "
         "designation of 0 (the object's own filament) is not a designation.",
         (_TF + "::test_dedicated_support_filament_never_points_past_the_plates_one_filament",
          _TA + "::test_painted_supports_turn_generation_on_unless_the_operator_decided"), (_FOREIGN,),
         {"keys": ("support_filament", "support_interface_filament"),
          "prusa_keys": ("support_material_extruder", "support_material_interface_extruder"),
          "no_designation_value": 0}),
    Rule("prusa-plate-adoption-seeding", "profile_seeding", "prusaslicer", "any", "orca", _ORCA,
         "A translated PrusaSlicer plate carries none of the machine half a Bambu project would name "
         "(printer model, start G-code, bed type), so the adopted config is seeded from the job's profile "
         "for every non-bookkeeping key it lacks, and both Prusa members are removed from the copy.",
         (_TP + "::test_derived_plate_from_a_prusa_project_is_rewritten",), (_FOREIGN,),
         {"seed_excludes": "preset_meta"}),
    Rule("mmu-segmentation-paint-translation", "paint_translation", "prusaslicer", "any", "orca", _ORCA,
         "PrusaSlicer's slic3rpe:mmu_segmentation triangle attribute carries the same TriangleSelector "
         "encoding and state numbering as Orca's paint_color (the corpus prusa-mmu-project fixture's "
         "\"8\" is state 2 in both), and the Orca destination reads paint_color, so a remap decodes the "
         "Prusa states, remaps them through the same paint table, writes them as paint_color and drops "
         "the Prusa attribute. A state with no slot choice refuses, exactly as an uncovered paint_color "
         "state does; the states are never carried verbatim (they would print in the base slot).",
         ("tests/pipeline/test_remap_exhaustive.py::test_mmu_segmentation_states_are_decoded_per_part",
          "tests/pipeline/test_remap_exhaustive.py::test_mmu_segmentation_export_translates_paint_to_the_chosen_tools"),
         ("docs/history/3MF-REMAP-INVENTORY.md#prusa-mmu-segmentation-paint-translation",),
         {"source_attribute": "slic3rpe:mmu_segmentation", "destination_attribute": "paint_color",
          "encoding": "triangleselector"}),
    Rule("per-filament-array-permutation", "array_permutation", "any", "any", "orca", _ORCA,
         "A filament remap permutes the project palette and every per-filament array by the same "
         "mapping, so destination filament M carries source filament N's entries wherever the mapping "
         "sends N to M; a one-filament collapse keeps the used source filament's entries (not entry "
         "1's), because a plate that uses filament 2 must not inherit filament 1's temperatures, "
         "density or profile id. An array whose length matches neither the palette nor a known shape "
         "refuses the remap. In a PrusaSlicer Slic3r_PE.config (#1155) the mapping must cover the whole "
         "extruder palette or the remap refuses; every per-filament list is then permuted, the role "
         "extruders (perimeter, infill, solid infill, support, support interface, wipe tower) are "
         "remapped with 0 kept, an absent role extruder that defaults to 1 is written as filament 1's "
         "new number, the wiping-volumes matrix is permuted on both axes and the load/unload vector by "
         "extruder pair, and per-extruder printer lists stay with their physical extruder.",
         ("tests/pipeline/test_remap_exhaustive.py::test_generic_remap_permutates_every_reference_and_preserves_the_rest",
          "tests/pipeline/test_remap_exhaustive.py::test_declare_one_filament_keeps_the_used_filaments_entries",
          "tests/pipeline/test_remap_package_prusa_1155.py::test_a_prusa_package_remaps_palette_settings_roles_and_matrix",
          "tests/pipeline/test_remap_package_prusa_1155.py::test_a_mapping_that_does_not_cover_the_prusa_palette_refuses"),
         ("docs/history/3MF-REMAP-INVENTORY.md#palette-per-filament-arrays-and-purge-references",),
         {"order": "destination_order_of_mapped_sources"}),
    Rule("flush-matrix-permutation", "purge_permutation", "any", "any", "orca", _ORCA,
         "flush_volumes_matrix is the palette-count-squared row-major matrix of purge volumes between "
         "ordered filament pairs (the shape orca_adapter.ifs_purge_settings generates), so a remap "
         "permutes its rows and columns by the same mapping and the diagonal stays zero; a matrix "
         "whose size is not the palette count squared refuses.",
         ("tests/pipeline/test_remap_exhaustive.py::test_generic_remap_permutates_every_reference_and_preserves_the_rest",),
         ("docs/history/3MF-REMAP-INVENTORY.md#palette-per-filament-arrays-and-purge-references",),
         {"key": "flush_volumes_matrix", "layout": "row_major_ordered_pairs"}),
    Rule("flush-vector-unestablished-refusal", "refusal", "any", "any", "orca", _ORCA,
         "The layout semantics of flush_volumes_vector are not established from this repository (only "
         "\"two entries per filament\" in one synthetic fixture), so no honest permutation of it "
         "exists: a remap that would have to permute the vector refuses and names the key. The "
         "identity mapping needs no permutation and passes it through, and so does a vector whose "
         "entries are all equal (the slicer default, a run of 140s), since every reordering of it is "
         "the same list whatever its layout (#1155); a one-filament collapse keeps the used "
         "filament's own pair, the one slice of the layout the fixture does establish.",
         ("tests/pipeline/test_remap_exhaustive.py::test_generic_remap_refuses_the_unestablished_flush_vector",
          "tests/pipeline/test_remap_package_bambu_1155.py::test_a_non_uniform_flush_vector_still_refuses_by_name",
          "tests/pipeline/test_remap_package_bambu_1155.py::test_a_realistic_bambu_project_remaps_completely",
          "tests/pipeline/test_remap_exhaustive.py::test_declare_one_filament_keeps_the_used_filaments_entries"),
         ("docs/history/3MF-REMAP-INVENTORY.md#palette-per-filament-arrays-and-purge-references",),
         {"key": "flush_volumes_vector", "on_nonidentity_remap": "refuse",
          "on_uniform_vector": "pass_through"}),
    Rule("support-designation-remap", "designation_remap", "any", "any", "orca", _ORCA,
         "Inside a remapped package, a support designation (support_filament / "
         "support_interface_filament, and the Prusa support_material_extruder pair) is rewritten "
         "through the filament mapping like any other reference. A designation whose filament the "
         "remap removes falls back to 0 — the object's own filament, which on the remapped plate is "
         "the mapped filament — and the fallback is named in the remap report; a designation never "
         "survives pointing past the new palette.",
         ("tests/pipeline/test_remap_exhaustive.py::test_generic_remap_permutates_every_reference_and_preserves_the_rest",
          "tests/pipeline/test_remap_exhaustive.py::test_declare_one_filament_keeps_the_used_filaments_entries"),
         ("docs/history/3MF-REMAP-INVENTORY.md#palette-per-filament-arrays-and-purge-references",),
         {"keys": ("support_filament", "support_interface_filament"),
          "prusa_keys": ("support_material_extruder", "support_material_interface_extruder"),
          "removed_filament_fallback": 0}),
    Rule("height-range-extruder-remap", "range_remap", "any", "any", "orca", _ORCA,
         "A height range's opt_key=\"extruder\" option is a filament reference keyed by object id. A "
         "remap that knows the mapping translates the value (and the object id, when the remap "
         "renumbers objects — package remaps preserve ids unless given an explicit object map) and "
         "refuses a value or object it does not know. At slice-time adoption, where the plate was "
         "already renumbered by its export, a value past the job's filament count clamps to the "
         "job's last filament, the _prusa_model_settings precedent; the option and the range's other "
         "intent are preserved, never carried verbatim past the palette.",
         ("tests/pipeline/test_remap_exhaustive.py::test_generic_remap_permutates_every_reference_and_preserves_the_rest",
          _TF + "::test_adopt_keeps_per_filament_list_shape_and_layer_range_intent"),
         ("docs/history/3MF-REMAP-INVENTORY.md#height-ranges-and-object-ids",),
         {"member": "Metadata/layer_config_ranges.xml", "option_key": "extruder",
          "adopt_behavior": "clamp_to_job_filament_count"}),
    Rule("remap-member-classification", "member_classification", "any", "any", "orca", _ORCA,
         "Every member of a package being remapped is classified before any byte is written: a member "
         "that can carry a filament reference is rewritten by the mapping, a member with no known "
         "filament reference (thumbnails, relationship parts, vendor records, Auxiliaries/) is "
         "preserved byte-for-byte, embedded plate G-code refuses (bare G-code is never rewritten and "
         "its tool commands would contradict the new numbering), and any member with no classification "
         "refuses the remap by name rather than be carried stale or dropped silently. The standard "
         "Bambu Studio / OrcaSlicer members are classified (#1155): the previous slice's plate records "
         "(plate_N.json, plate_N.gcode.md5) are kept under the identity mapping and dropped by name "
         "under any other, since they describe the old numbering and the next slice rewrites them; "
         "the object-keyed records with no filament reference (cut_information.xml, "
         "layer_heights_profile.txt, brim_ear_points.txt) are preserved, and refuse by name under an "
         "object map.",
         ("tests/pipeline/test_remap_exhaustive.py::test_generic_remap_refuses_unclassified_and_embedded_gcode_members",
          "tests/pipeline/test_remap_exhaustive.py::test_production_plate_preserves_auxiliaries_members",
          "tests/pipeline/test_remap_package_bambu_1155.py::test_a_realistic_bambu_project_remaps_completely",
          "tests/pipeline/test_remap_package_bambu_1155.py::test_an_object_map_refuses_the_object_keyed_cut_record_by_name"),
         ("docs/history/3MF-REMAP-INVENTORY.md#member-classification-and-the-object-id-policy",),
         {"on_unclassified": "refuse", "on_embedded_gcode": "refuse",
          "on_slice_record_nonidentity": "drop_named", "on_object_keyed_object_map": "refuse"}),
)

_BY_ID = {rule.id: rule for rule in RULES}


def get_rule(rule_id):
    """The rule with `rule_id`; KeyError names the missing id (no silent fallback)."""
    return _BY_ID[rule_id]


def rules_for(kind=None, source_family=None, destination_family=None):
    """Rules filtered by kind and/or slicer families, in registry order."""
    return tuple(rule for rule in RULES
                 if (kind is None or rule.kind == kind)
                 and (source_family is None or rule.source_family in ("any", source_family))
                 and (destination_family is None or rule.destination_family in ("any", destination_family)))


def provenance_version_fields():
    """The version-pair fields Phase A fills in conversion provenance, from this registry's shape.

    Phase A reads source/destination slicer versions from the source project and the
    destination templates and evaluates each rule with rule_applies(); this helper only
    names the fields so both phases agree on them.
    """
    return ("source_slicer_version", "destination_slicer_version",
            "source_profile_version", "destination_profile_version")
