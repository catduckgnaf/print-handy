"""Hostile-model security corpus for intake (deep-dive P1-4).

Crafted, untrusted model files through the real intake entry points
(print_handy.core.intake_resources.check_source and the parsers that call it).
Fixtures are generated in-test at modest real sizes: limits are made to trip
on declared sizes and ratios via the same patch.object harness
tests/test_intake_resources.py uses, never on gigabytes of real data.
This file deliberately does not duplicate that file's size-budget cases.

Documented behavior under test (docs/INTAKE-LIMITS.md and the code):
- XML document type declarations are refused, so XXE and entity-expansion
  payloads never reach a parser that could resolve them.
- ZIP expanded sizes are checked from the directory before any member opens.
- A nested 3MF is not opened: intake reads the outer archive only.
- Part names are sanitized by geometry_layout._safe_name before any file is
  written, so an arranged export stays inside its job folder.
- A source thumbnail that is not a decodable image is skipped, never sent
  and never an exception (prepared_intake._send_source_thumbnail).
- A binary STL whose header count disagrees with its byte length is not a
  readable STL; a 3MF triangle naming a vertex that does not exist is
  refused by the geometry loader.
"""
import asyncio
import io
import re
import struct
import time
import zipfile
from pathlib import Path
from unittest.mock import patch
from xml.etree.ElementTree import ParseError

import pytest
from PIL import Image

from print_handy.core import geometry_layout, intake_resources as limits, model_job
from print_handy.core import prepared_intake, source_colors

NS = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
TRIANGLE = ('<vertices><vertex x="0" y="0" z="0"/><vertex x="10" y="0" z="0"/>'
            '<vertex x="0" y="10" z="0"/></vertices>'
            '<triangles><triangle v1="0" v2="1" v3="2"/></triangles>')


def model_xml(objects, items=None):
    """objects: [(id, name, mesh_xml)]; a minimal valid 3MF model part."""
    objs = "".join(
        f'<object id="{oid}" name="{name}"><mesh>{mesh}</mesh></object>'
        for oid, name, mesh in objects)
    items = items if items is not None else [oid for oid, _, _ in objects]
    build = "".join(f'<item objectid="{oid}"/>' for oid in items)
    return (f'<model xmlns="{NS}"><resources>{objs}</resources>'
            f'<build>{build}</build></model>')


def write_3mf(path, model, extra=None):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("3D/3dmodel.model", model)
        for name, data in (extra or {}).items():
            archive.writestr(name, data)
    return path


def simple_3mf(path, name="part"):
    return write_3mf(path, model_xml([("1", name, TRIANGLE)]))


# --------------------------------------------------------------------- (a) XML attacks

SENTINEL = "HOSTILE_SENTINEL_CONTENT"


def test_external_entity_payload_is_refused_by_every_intake_entry_point(tmp_path):
    secret = tmp_path / "sentinel.txt"
    secret.write_text(SENTINEL)
    payload = (
        '<?xml version="1.0"?>'
        f'<!DOCTYPE model [<!ENTITY xxe SYSTEM "file://{secret}">]>'
        + model_xml([("1", "&xxe;", TRIANGLE)]))
    source = write_3mf(tmp_path / "xxe.3mf", payload)
    assert source.stat().st_size < 4096  # the refusal is about the shape, not the size
    started = time.monotonic()
    for entry in (limits.check_source, geometry_layout.load_parts,
                  source_colors.inventory, model_job.audit):
        with pytest.raises(limits.IntakeLimitExceeded, match="document type") as caught:
            entry(source)
        assert SENTINEL not in str(caught.value)
    assert time.monotonic() - started < 10, "XXE refusal must be immediate, not an expansion"


def test_billion_laughs_payload_is_refused_before_any_expansion(tmp_path):
    entities = ['<!ENTITY lol "lollollollollollollollollol">']
    for level in range(2, 8):
        entities.append(
            f'<!ENTITY lol{level} "' + f"&lol{level - 1};" * 10 + '">')
    payload = ('<?xml version="1.0"?><!DOCTYPE model [' + "".join(entities) + "]>"
               + model_xml([("1", "&lol7;", TRIANGLE)]))
    source = write_3mf(tmp_path / "laughs.3mf", payload)
    expanded_claim = len("lollollollollollollollollol") * 10 ** 6
    assert source.stat().st_size < 4096 and expanded_claim > source.stat().st_size * 1000
    started = time.monotonic()
    with pytest.raises(limits.IntakeLimitExceeded, match="document type"):
        geometry_layout.load_parts(source)
    assert time.monotonic() - started < 10


def test_undefined_entity_without_a_doctype_is_a_parse_refusal_not_a_crash(tmp_path):
    source = write_3mf(tmp_path / "undef.3mf", model_xml([("1", "&nosuchentity;", TRIANGLE)]))
    for entry in (limits.check_source, geometry_layout.load_parts):
        with pytest.raises(ParseError, match="undefined entity"):
            entry(source)


# --------------------------------------------------------------------- (b) zip bombs / nesting

def test_high_ratio_deflate_bomb_trips_the_expanded_size_refusal(tmp_path):
    source = tmp_path / "bomb.3mf"
    with zipfile.ZipFile(source, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("3D/3dmodel.model", model_xml([("1", "part", TRIANGLE)]))
        archive.writestr("payload.bin", b"\x00" * 2_000_000)
    real = source.stat().st_size
    assert real < 16 * 1024, f"bomb must stay small on disk, got {real}"
    started = time.monotonic()
    with patch.object(limits, "MAX_EXPANDED_BYTES", 1_000_000):
        with pytest.raises(limits.IntakeLimitExceeded, match="expanded archive"):
            limits.check_source(source)
        with pytest.raises(limits.IntakeLimitExceeded, match="expanded archive"):
            geometry_layout.load_parts(source)
    assert time.monotonic() - started < 10


def test_nested_3mf_is_never_opened_and_the_outer_limits_still_apply(tmp_path):
    inner = simple_3mf(tmp_path / "inner.3mf", name="inner_part")
    outer = write_3mf(tmp_path / "outer.3mf", model_xml([("1", "outer_part", TRIANGLE)]),
                      extra={"nested/inner.3mf": inner.read_bytes()})
    # The outer archive parses as exactly its own one part: the nested
    # archive's model is not read, merged, or counted as a part.
    parts, _, _, _ = geometry_layout.load_parts(outer)
    assert [p["node"] for p in parts] == ["outer_part"]
    # The nested member still counts toward the outer expanded total.
    with patch.object(limits, "MAX_EXPANDED_BYTES", 10):
        with pytest.raises(limits.IntakeLimitExceeded, match="expanded archive"):
            limits.check_source(outer)


# --------------------------------------------------------------------- (c) hostile part names

HOSTILE_NAMES = ["../../escape", "/abs/path/part", "a/b\\c:d", "..", "x" * 300]


def test_hostile_part_names_are_sanitized_by_intake(tmp_path):
    objects = [(str(i), name, TRIANGLE) for i, name in enumerate(HOSTILE_NAMES, 1)]
    source = write_3mf(tmp_path / "hostile.3mf", model_xml(objects))
    parts, _, _, _ = geometry_layout.load_parts(source)
    assert len(parts) == len(HOSTILE_NAMES)
    for idx, part in enumerate(parts, 1):
        safe = part["id"]
        assert re.fullmatch(r"\d{2}_[A-Za-z0-9_-]+", safe), safe
        assert len(safe) <= 78  # two digits, underscore, at most 75 kept characters
        assert "/" not in safe and "\\" not in safe and ".." not in safe
        assert not Path(safe).is_absolute()
        assert part["node"] in HOSTILE_NAMES  # the original label is data, never a path


def test_hostile_names_from_model_settings_are_sanitized_too(tmp_path):
    config = ('<config><object id="1"><metadata key="name" value="../../settings_escape"/>'
              "</object></config>")
    source = write_3mf(tmp_path / "settings.3mf", model_xml([("1", "safe_name", TRIANGLE)]),
                       extra={"Metadata/model_settings.config": config})
    parts, _, _, _ = geometry_layout.load_parts(source)
    assert parts[0]["node"] == "../../settings_escape"
    assert parts[0]["id"] == "01_settings_escape"


def test_arranged_export_of_hostile_names_stays_inside_the_job_folder(tmp_path):
    objects = [(str(i), name, TRIANGLE) for i, name in enumerate(HOSTILE_NAMES, 1)]
    source = write_3mf(tmp_path / "hostile.3mf", model_xml(objects))
    job = tmp_path / "job"
    outside_before = {p for p in tmp_path.rglob("*") if p.is_file()}
    report = geometry_layout.arrange_and_export(source, job)
    assert report["result"] == "geometry_fits_nominal_bed"
    written = [p for p in job.rglob("*") if p.is_file()]
    assert written, "the export must actually write files for this test to mean anything"
    for path in written:
        assert path.resolve().is_relative_to(job.resolve()), path
    # Nothing escaped sideways into the parent either: the only new files
    # anywhere under tmp_path are the ones inside the job folder.
    outside_after = {p for p in tmp_path.rglob("*")
                     if p.is_file() and not p.is_relative_to(job)}
    assert outside_after == outside_before


def test_control_character_in_a_part_name_is_a_parse_refusal(tmp_path):
    # A raw control byte cannot be well-formed XML 1.0, so this name shape is
    # refused at parse time instead of reaching any file writer.
    model = model_xml([("1", "bad\x01name", TRIANGLE)])
    source = write_3mf(tmp_path / "ctrl.3mf", model)
    with pytest.raises(ParseError):
        geometry_layout.load_parts(source)


# --------------------------------------------------------------------- (d) thumbnail payloads

class _FakeAdapter:
    def __init__(self):
        self.sent = []

    async def send(self, **kwargs):
        self.sent.append(kwargs)


class _Source:
    chat_id = "1"
    thread_id = None


def run_thumbnail(tmp_path, payload, member="Metadata/plate_1.png"):
    source = write_3mf(tmp_path / "thumb.3mf", model_xml([("1", "part", TRIANGLE)]),
                       extra={member: payload})
    adapter = _FakeAdapter()
    target = tmp_path / "thumb_out.png"
    asyncio.run(prepared_intake._send_source_thumbnail(adapter, _Source(), source, target))
    return adapter, target


def test_thumbnail_that_is_plain_text_is_skipped_not_sent(tmp_path):
    adapter, target = run_thumbnail(tmp_path, b"this is not an image, just hostile text" * 2)
    assert adapter.sent == [] and not target.exists()


def test_thumbnail_with_a_png_header_but_no_image_data_is_skipped(tmp_path):
    adapter, target = run_thumbnail(tmp_path, b"\x89PNG\r\n\x1a\n" + b"garbage" * 16,
                                    member="Metadata/thumbnail.png")
    assert adapter.sent == [] and not target.exists()


def test_a_real_thumbnail_still_sends_so_the_skip_is_selective(tmp_path):
    buffer = io.BytesIO()
    Image.new("RGB", (64, 64), (200, 30, 30)).save(buffer, format="PNG")
    adapter, target = run_thumbnail(tmp_path, buffer.getvalue())
    assert len(adapter.sent) == 1 and target.exists()


def test_gcode_thumbnail_block_that_is_not_a_png_is_ignored():
    from print_handy.core import plate_thumbnail
    import base64
    data = base64.b64encode(b"hello, not a png").decode()
    text = f"; thumbnail begin 10x10 {len(data)}\n; {data}\n; thumbnail end\nG90\n"
    assert plate_thumbnail.embedded_thumbnails(text) == []


# --------------------------------------------------------------------- (e) declared vs actual counts

def binary_stl(declared, records):
    return b"\x00" * 80 + struct.pack("<I", declared) + b"\x00" * 50 * records


@pytest.mark.parametrize("declared,records", [(5, 1), (1, 2), (0, 1)])
def test_stl_header_count_disagreeing_with_the_file_is_not_readable(tmp_path, declared, records):
    source = tmp_path / "mismatch.stl"
    source.write_bytes(binary_stl(declared, records))
    # check_source only trusts the header when the byte length agrees with
    # it, so a lying header is not a limit refusal here...
    limits.check_source(source)
    # ...and the reader refuses the file by name instead of trusting either number.
    with pytest.raises(RuntimeError, match="is not a readable STL"):
        geometry_layout.load_parts(source)


def test_stl_header_count_agreeing_with_the_file_is_checked_against_the_limit(tmp_path):
    source = tmp_path / "honest.stl"
    # One real, readable triangle record whose header honestly declares it.
    record = struct.pack("<12fH", 0, 0, 1, 0, 0, 0, 10, 0, 0, 0, 10, 0, 0)
    source.write_bytes(b"\x00" * 80 + struct.pack("<I", 1) + record)
    parts, _, _, _ = geometry_layout.load_parts(source)
    assert len(parts) == 1 and len(parts[0]["triangles"]) == 1
    with patch.object(limits, "MAX_TRIANGLES", 0):
        with pytest.raises(limits.IntakeLimitExceeded, match="triangle"):
            limits.check_source(source)


def test_3mf_triangle_naming_a_missing_vertex_is_refused_by_the_loader(tmp_path):
    mesh = TRIANGLE.replace('v3="2"', 'v3="99"')
    source = write_3mf(tmp_path / "badindex.3mf", model_xml([("1", "part", mesh)]))
    limits.check_source(source)  # counts agree; the index itself is the lie
    with pytest.raises(IndexError, match="vertex index out of range"):
        geometry_layout.load_parts(source)
