"""Binary resin container tests for the cost parsers (#1660).

The .cxdlpv4 golden fixture is built byte by byte from the container
layout that two independent open-source implementations agree on:
UVtools (UVtools.Core/FileFormats/CrealityCXDLPv4File.cs) and
Danslicer (Danslicer.Core/IO/ChituWriter.cs), which writes the same
header and print parameters block for real HALOT printers. The
expected volume and print time are the values the fixture writes, and
the costed breakdown is hand-computed from the documented engine
rules, so the tests prove the offsets rather than the parser's own
assumptions.

The .lys verdict is grounded in real files: two Lychee 5.4.3 scenes
from the JRiggles/DELPHI-kbd project carry a container manifest with
only a version and a file index, an encoded scene payload no public
reader decodes, and no volume, print time or resin field anywhere in
the file. Lychee computes both values only when a slice is exported,
so a .lys lands in the explicit cannot-estimate state.
"""
import json
import struct
import zipfile
import io

from print_handy.core import cost, cost_parse


def _cxdlpv4_bytes(*, volume_ml=40.0, print_time=14400, layer_count=850,
                   version=4, magic=b"CXSW3DV2\x00", params_gap=200,
                   params_size=68):
    """A minimal .cxdlpv4 container, byte by byte per the layout."""
    out = bytearray()
    out += struct.pack(">I", 9)            # magic size, big-endian
    out += magic                           # "CXSW3DV2" plus a NUL
    out += struct.pack(">H", version)      # format version, big-endian
    model = b"HALOT-MAGE"
    out += struct.pack(">I", len(model) + 1)
    out += model + b"\x00"
    header_base = len(out)
    params_offset = header_base + 80 + params_gap
    fields = struct.pack("<HH", 7680, 4320)                    # resolution
    fields += struct.pack("<fffff", 228.0, 128.0, 230.0, 42.5, 0.05)
    fields += struct.pack("<IIIIII", 6, 0, 0, layer_count, 0, print_time)
    fields += struct.pack("<IIII", 1, params_offset, params_size, 1)
    fields += struct.pack("<HH", 255, 255)                     # light PWM
    fields += struct.pack("<III", 0, 0, 0)                     # no layer key
    assert len(fields) == 80
    out += fields
    out += b"\x00" * params_gap            # stand-in preview data
    params = struct.pack("<fffff", 6.0, 60.0, 6.0, 90.0, 150.0)
    params += struct.pack("<fff", volume_ml, 44.0, 0.0)        # volume at +20
    params += struct.pack("<ff", 1.0, 1.0)                     # light-off delays
    params += struct.pack("<I", 6)                             # bottom layers
    params += struct.pack("<ff", 2.5, 30.0)                    # exposures
    params += b"\x00" * 16                                     # padding
    assert len(params) == 68
    out += params
    out += b"\x00" * 256                   # stand-in layer data
    return bytes(out)


def _lys_mango_bytes():
    """A Lychee .lys mango container, framed like a real 5.4.3 scene."""
    manifest = json.dumps({
        "version": "3.1.0",
        "mangoFiles": {
            "scene.bin": {"size": 4, "offset": "0"},
            "preview.png": {"size": 8, "offset": "4"},
        },
    }).encode()
    prefix = struct.pack("<IIII", 4, len(manifest) + 8,
                         len(manifest) + 4, len(manifest))
    return prefix + manifest + b"SCENE" + b"PREVIEW!"


def _lys_zip_bytes():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("scene.bin", b"encoded scene payload")
        archive.writestr("preview.png", b"not a real png")
    return buffer.getvalue()


def test_cxdlpv4_parses_volume_and_print_time():
    usage = cost_parse.parse_cxdlpv4_bytes(_cxdlpv4_bytes())
    assert usage.sliced is True
    assert usage.volume_ml == 40.0
    assert usage.time_seconds == 14400
    assert usage.source == "ChiTuBox .cxdlpv4 container"


def test_cxdlpv4_from_dropped_file(tmp_path):
    path = tmp_path / "halot print.cxdlpv4"
    path.write_bytes(_cxdlpv4_bytes())
    usage = cost_parse.usage_from_file(path)
    assert usage.sliced is True
    assert usage.volume_ml == 40.0
    assert usage.time_seconds == 14400
    assert usage.source == "ChiTuBox .cxdlpv4 container"


def test_cxdlpv4_breakdown_matches_hand_computed_cost():
    # The fixture carries 40.0 mL and 14400 s (4 h). Hand computation
    # from the documented resin rules: material 40 mL at 35 per liter
    # is 1.40, plus the 10 percent resin waste allowance is 1.54.
    # Electricity (60 W print + 60 W wash/cure for 0.25 h) x 4 h at
    # 0.23 is 0.06; depreciation 4 x 320/4000 is 0.32; maintenance
    # 4 x 0.05 is 0.20; consumables film 0.12 + IPA 0.23 + PPE 0.12 +
    # screen wear 0.12 is 0.59. Repeatable 2.71; the 10 percent resin
    # failure allowance divides by 0.9, giving 3.01 (failure 0.30).
    # Labor (5 prep + 10 post minutes) at 20 per hour is 5.00.
    # Total 8.01.
    usage = cost_parse.parse_cxdlpv4_bytes(_cxdlpv4_bytes())
    profile = cost.PrinterCostProfile(
        name="Test resin", technology=cost.RESIN, purchase_price=320.0,
        lifetime_hours=4000.0, watts=60.0)
    workspace = cost.WorkspaceCost(electricity_rate_per_kwh=0.23,
                                   labor_rate_per_hour=20.0)
    job = cost.JobCostInput(
        lines=[cost.MaterialLine(volume_ml=usage.volume_ml,
                                 spool={"id": 9, "price": 35.0,
                                        "volume_ml": 1000.0})],
        print_hours=usage.hours, prep_minutes=5.0, post_minutes_per_part=10.0)
    job.grams_source = usage.source
    result = cost.compute(job, profile, workspace)
    amounts = {entry["key"]: entry["amount"] for entry in result.lines}
    assert amounts["material"] == 1.54
    assert amounts["electricity"] == 0.06
    assert amounts["depreciation"] == 0.32
    assert amounts["maintenance"] == 0.20
    assert amounts["consumables"] == 0.59
    assert amounts["failure"] == 0.30
    assert amounts["labor"] == 5.00
    assert result.cost_per_build == 8.01
    assert result.totals_detail["total_volume_ml"] == 40.0
    assert result.totals_detail["print_hours"] == 4.0
    assert result.provenance["grams_source"] == "ChiTuBox .cxdlpv4 container"


def test_cxdlpv4_bad_magic_is_cannot_estimate():
    usage = cost_parse.parse_cxdlpv4_bytes(_cxdlpv4_bytes(magic=b"CXSW3DV3\x00"))
    assert usage.sliced is False
    assert usage.volume_ml is None
    assert "magic" in usage.not_sliced_reason


def test_cxdlpv4_older_version_is_cannot_estimate():
    usage = cost_parse.parse_cxdlpv4_bytes(_cxdlpv4_bytes(version=3))
    assert usage.sliced is False
    assert usage.volume_ml is None
    assert "version 3" in usage.not_sliced_reason


def test_cxdlpv4_truncated_header_is_cannot_estimate():
    usage = cost_parse.parse_cxdlpv4_bytes(_cxdlpv4_bytes()[:40])
    assert usage.sliced is False
    assert usage.volume_ml is None
    assert "header" in usage.not_sliced_reason


def test_cxdlpv4_truncated_parameters_is_cannot_estimate(tmp_path):
    data = _cxdlpv4_bytes()
    path = tmp_path / "cut.cxdlpv4"
    path.write_bytes(data[:120])  # header intact, parameters block gone
    usage = cost_parse.usage_from_file(path)
    assert usage.sliced is False
    assert usage.volume_ml is None
    assert "parameters" in usage.not_sliced_reason


def test_cxdlpv4_zero_volume_is_cannot_estimate():
    usage = cost_parse.parse_cxdlpv4_bytes(_cxdlpv4_bytes(volume_ml=0.0))
    assert usage.sliced is False
    assert usage.volume_ml is None
    assert "no resin volume" in usage.not_sliced_reason


def test_cxdlpv4_encrypted_payload_is_cannot_estimate(tmp_path):
    # ChiTuBox's encrypted container saved under this extension: opaque
    # bytes with no readable header must never parse as a zero cost.
    path = tmp_path / "encrypted.cxdlpv4"
    path.write_bytes(bytes(range(256)) * 8)
    usage = cost_parse.usage_from_file(path)
    assert usage.sliced is False
    assert usage.volume_ml is None
    assert usage.time_seconds is None


def test_lys_mango_scene_is_cannot_estimate(tmp_path):
    path = tmp_path / "scene.lys"
    path.write_bytes(_lys_mango_bytes())
    usage = cost_parse.usage_from_file(path)
    assert usage.sliced is False
    assert usage.volume_ml is None
    assert usage.time_seconds is None
    assert usage.source == "Lychee .lys container"
    assert "saved scenes" in usage.not_sliced_reason


def test_lys_zip_scene_is_cannot_estimate():
    usage = cost_parse.parse_lys_bytes(_lys_zip_bytes())
    assert usage.sliced is False
    assert usage.volume_ml is None
    assert "saved scenes" in usage.not_sliced_reason


def test_lys_unreadable_bytes_are_cannot_estimate():
    usage = cost_parse.parse_lys_bytes(b"\x00\x01\x02 not a container")
    assert usage.sliced is False
    assert usage.volume_ml is None
    assert "not a readable" in usage.not_sliced_reason
