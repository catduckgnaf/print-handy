"""Bambu HMS / print_error translation (#306): offline, synthetic payloads only.

Codes used below are real entries in the generated table (scripts/bambu-hms.py, from Bambu
Studio at the commit in hms_data.json's _provenance block), chosen for the case they pin:
- HMS 0300020000010008 (attr 0x03000200, code 0x00010008): listed with text everywhere.
- HMS 0C00020000010001: text differs per model — 094 says "horizontal laser", the common
  text says "height measuring laser".
- HMS 0300180000000000 (attr 0x03001800, code 0): listed with EMPTY text — not a fault.
- print_error 0x07FFC010 (decimal 134209808): listed with text.
- print_error 0x05008030: listed with empty text.
"""
from print_handy import printers, settings
from print_handy.printers.bambulab import hms

RECORD = {"bed_mm": [256, 256], "max_print_height_mm": 256,
          "nozzle": {"diameter_mm": 0.4, "type": "stainless_steel"}}

KNOWN_HMS = {"attr": 0x03000200, "code": 0x00010008}
DIFFERING_HMS = {"attr": 0x0C000200, "code": 0x00010001}
EMPTY_HMS = {"attr": 0x03001800, "code": 0}
UNKNOWN_HMS = {"attr": 0x0BADF00D, "code": 0x00009999}


def adapter(serial=None):
    env = {"PRINT_HANDY__PRINTER__MODEL": "bambulab.a1"}
    if serial:
        env["PRINT_HANDY__BAMBU__LAN_HOST"] = "192.168.1.50"
        env["PRINT_HANDY__BAMBU__LAN_SERIAL"] = serial
    return printers.get("bambulab.a1", settings.load(local=False, environ=env))


def problems_for(payload, serial=None):
    return adapter(serial).readiness_problems(RECORD, {"print": payload})


# ---------------------------------------------------------------- key spaces

def test_key_spaces_never_mix():
    # A 16-hex HMS key does not resolve as a print_error, and an 8-hex error code
    # does not resolve as an HMS code.
    assert hms.lookup_error("0300020000010008").listed is False
    assert hms.lookup_hms("07FFC010").listed is False
    # The same 8 hex digits mean different things in each space and stay separate:
    # as an error code 05008030 is listed-but-empty; it is not an HMS key at all.
    assert hms.lookup_error(0x05008030).listed is True
    assert hms.lookup_hms("05008030").listed is False


def test_hms_key_is_attr_then_code_sixteen_hex():
    assert hms.hms_key(0x03000200, 0x00010008) == "0300020000010008"
    assert hms.hms_key("03000200", "00010008") == "0300020000010008"
    assert hms.hms_key(0x03000200, None) is None


# ---------------------------------------------------------------- lookup rules

def test_known_hms_code_decodes():
    look = hms.decode_entry(KNOWN_HMS)
    assert look.listed is True and look.is_fault is True
    assert "nozzle temperature is abnormal" in look.text


def test_per_model_override_wins():
    common = hms.lookup_hms("0C00020000010001")
    override = hms.lookup_hms("0C00020000010001", "094")
    assert "height measuring laser" in common.text
    assert "horizontal laser" in override.text and override.text != common.text


def test_listed_but_empty_is_listed_and_not_a_fault():
    look = hms.decode_entry(EMPTY_HMS)
    assert look.listed is True and look.text == "" and look.is_fault is False


def test_unlisted_is_unknown_and_still_a_fault():
    look = hms.decode_entry(UNKNOWN_HMS)
    assert look.listed is False and look.text is None and look.is_fault is True


def test_print_error_decodes_in_its_own_space():
    look = hms.lookup_error(0x07FFC010)
    assert look.listed is True and "Insert the filament" in look.text
    empty = hms.lookup_error(0x05008030)
    assert empty.listed is True and empty.text == "" and empty.is_fault is False


# ---------------------------------------------------------------- adapter surface

def test_readiness_shows_decoded_hms_text_and_code():
    problems = problems_for({"gcode_state": "IDLE", "hms": [KNOWN_HMS]})
    assert any("nozzle temperature is abnormal" in p and "0300020000010008" in p for p in problems)


def test_readiness_uses_the_configured_printers_model_text():
    problems = problems_for({"gcode_state": "IDLE", "hms": [DIFFERING_HMS]}, serial="09400A000000000")
    assert any("horizontal laser" in p for p in problems)


def test_readiness_listed_but_empty_hms_does_not_block():
    assert problems_for({"gcode_state": "IDLE", "hms": [EMPTY_HMS]}) == []


def test_readiness_unknown_hms_keeps_count_wording_and_raw_code_and_blocks():
    problems = problems_for({"gcode_state": "IDLE", "hms": [UNKNOWN_HMS]})
    assert any("1 active HMS error(s)" in p and "0BADF00D00009999" in p for p in problems)


def test_readiness_known_print_error_decodes_and_blocks():
    problems = problems_for({"gcode_state": "IDLE", "print_error": 0x07FFC010})
    assert any("Insert the filament" in p and "print error" in p for p in problems)


def test_readiness_unknown_print_error_keeps_todays_wording():
    problems = problems_for({"gcode_state": "IDLE", "print_error": 503})
    assert problems == ["printer reports a print error (code 503)"]


def test_status_output_carries_decoded_hms():
    a = adapter()
    out = a.normalize_status({"print": {"gcode_state": "IDLE", "hms": [KNOWN_HMS, EMPTY_HMS, UNKNOWN_HMS],
                                         "print_error": 0x07FFC010}})
    by_code = {e["code"]: e for e in out["hms_errors"]}
    assert "nozzle temperature is abnormal" in by_code["0300020000010008"]["description"]
    assert by_code["0300180000000000"]["listed"] is True
    assert by_code["0300180000000000"]["description"] == ""
    assert by_code["0BADF00D00009999"]["listed"] is False
    assert by_code["0BADF00D00009999"]["description"] is None
    assert "Insert the filament" in out["print_error_text"]


def test_generated_table_provenance_is_recorded():
    import json
    from pathlib import Path
    data = json.loads((Path(hms.__file__).parent / "hms_data.json").read_text())
    prov = data["_provenance"]
    assert "BambuStudio" in prov["source"] and len(prov["bambu_studio_commit"]) == 40
    assert len(data["hms"]) > 5000 and len(data["error"]) > 1000


# ---------------------------------------------------------------- H3C (32X) initial support
# The H3C's serial prefix is 32X (facts.H3C, from Bambu Studio's device definition
# for the model). Its per-model override sections arrive with the HMS table
# regenerated from Bambu Studio 02.09.01.70; the shipped table on main does not
# carry them yet, so the resolution path is pinned here against a small SYNTHETIC
# table in the same generated shape — not against the regenerated table's data.

_SYNTHETIC_32X = {
    "_provenance": {"prefixes": ["32X"]},
    "hms": {"0300020000010001": "synthetic common HMS text"},
    "hms_by_model": {"32X": {"0300020000010001": "synthetic H3C HMS text"}},
    "error": {"03008001": "synthetic common error text"},
    "error_by_model": {"32X": {"03008001": "synthetic H3C error text"}},
}


def test_h3c_prefix_resolves_its_own_texts(monkeypatch):
    monkeypatch.setattr(hms, "_DATA", _SYNTHETIC_32X)
    assert hms.prefixes() == ("32X",)
    assert hms.lookup_hms("0300020000010001", "32X").text == "synthetic H3C HMS text"
    assert hms.lookup_hms("0300020000010001").text == "synthetic common HMS text"
    assert hms.lookup_error(0x03008001, "32X").text == "synthetic H3C error text"
    assert hms.lookup_error(0x03008001).text == "synthetic common error text"


def test_adapter_accepts_a_32x_serial_and_readiness_uses_h3c_text(monkeypatch):
    monkeypatch.setattr(hms, "_DATA", _SYNTHETIC_32X)
    a = adapter(serial="32X00A000000000")
    assert a._hms_prefix() == "32X"
    problems = a.readiness_problems(RECORD, {"print": {"gcode_state": "IDLE",
                                                       "hms": [{"attr": 0x03000200, "code": 0x00010001}]}})
    assert any("synthetic H3C HMS text" in p for p in problems)
# ------------------------------------------------------- content integrity
# The lookup tests above pin mechanics on a handful of codes. The tests
# below pin the generated table's CONTENT across regenerations (the
# 2026-11-07 testing deep dive): a regeneration that silently drops
# codes, empties a model's overrides, or unlists a known code must fail
# here even though every mechanics test still passes. Floors and
# canaries are recorded from the Bambu Studio 2.8-generation table
# (5,289 HMS codes, 1,082 print-error codes); a deliberate larger
# regeneration (the 2.9 line's table is bigger) passes them, and a
# shrink below them is exactly the signal they exist for.

GENERATION_FLOORS = {"hms": 5289, "error": 1082}

# One canary per provenance prefix: a code that prefix's own override
# table carries with non-empty text, recorded from the current table.
CANARY_BY_PREFIX = {
    "093": "0300020000010006",
    "094": "0300020000010001",
    "20P": "0300020000010001",
    "22E": "0300020000010006",
    "239": "0300020000010001",
    "26A": "0300020000010006",
    "31B": "0300020000010001",
}
COMMON_CANARY = "0300010000010001"


def _table():
    import json
    from pathlib import Path
    return json.loads((Path(hms.__file__).parent / "hms_data.json").read_text())


def test_table_counts_do_not_shrink_below_the_recorded_generation():
    data = _table()
    for space, floor in GENERATION_FLOORS.items():
        assert len(data[space]) >= floor, (
            f"{space} table shrank to {len(data[space])} codes, below the "
            f"recorded generation floor of {floor}: a regeneration lost "
            "codes, or the floor needs a deliberate re-record with the "
            "upstream range that shrank it")


def test_every_provenance_prefix_keeps_nonempty_overrides():
    data = _table()
    for prefix in data["_provenance"]["prefixes"]:
        assert data["hms_by_model"].get(prefix), (
            f"prefix {prefix} is recorded in provenance but its per-model "
            "HMS overrides are empty or missing")
        assert data["error_by_model"].get(prefix), (
            f"prefix {prefix} is recorded in provenance but its per-model "
            "print-error overrides are empty or missing")


def test_canary_codes_stay_listed_and_decode_nonempty():
    for prefix, key in CANARY_BY_PREFIX.items():
        look = hms.lookup_hms(key, prefix)
        assert look.listed, f"canary {key} for prefix {prefix} became unlisted"
        assert look.text, f"canary {key} for prefix {prefix} decodes to empty text"
        # The canary is a real code in the common space too: unlisting
        # it there is the same loss, seen from the other side.
        assert hms.lookup_hms(key).listed, f"canary {key} became unlisted in the common table"
    common = hms.lookup_hms(COMMON_CANARY)
    assert common.listed and common.text


def test_unknown_prefix_falls_back_to_the_common_text():
    # A device prefix with no recorded overrides resolves through the
    # common text. The prefix here is deliberately synthetic ("ZZZ" is
    # not a Bambu device prefix): this pins the fallback MECHANISM for
    # any not-yet-known device, without prejudging how any specific
    # future prefix (a new model's own prefix) gets its overrides.
    common = hms.lookup_hms(COMMON_CANARY)
    fallback = hms.lookup_hms(COMMON_CANARY, "ZZZ")
    assert fallback.listed and fallback.text == common.text and fallback.text
    err = hms.lookup_error(0x07FFC010, "ZZZ")
    assert err.listed and err.text == hms.lookup_error(0x07FFC010).text
