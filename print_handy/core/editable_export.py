"""Editable destination 3MF export (#1525, tranche E).

Exports a source project as an editable destination .3mf: the geometry,
plates, palette and verified settings of the source, with stale slice
artifacts stripped (embedded plate G-code, slice records and checksums
of the old numbering), the source SHA-256 preserved in a provenance
member, and the write staged atomically through
``package_remap.write_package_atomically`` — the same writer the
remapper uses, so a failed export leaves no partial file.

The export never starts a print, sends a machine command or touches
spool inventory; it only reads the source and writes one new file.
A sliced-only source (toolpaths, no editable geometry) is refused with
the concrete reason instead of being dressed up as a project. A
non-identity filament mapping goes through ``package_remap`` on a
cleaned package, so every per-filament array is permuted atomically by
the established rules or the export refuses by name.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from pathlib import Path

from . import package_remap, remap_integrity, source_descriptor

PROVENANCE_MEMBER = "Metadata/print_handy_export.json"
_GCODE_MEMBER = re.compile(r"(?:^|/)plate_\d+\.gcode$", re.I)
_SLICE_RECORD = re.compile(r"Metadata/plate_\d+\.(?:json|gcode\.md5)$", re.I)
_ANY_GCODE = re.compile(r"\.gcode$", re.I)


class ExportRefused(RuntimeError):
    """An export that cannot honestly be made, with the reason."""


def _sha256(path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def stripped_members(names) -> list[str]:
    """Members an export drops: stale toolpaths, caches and slice records."""
    out = []
    for name in names:
        if _GCODE_MEMBER.search(name) or _ANY_GCODE.search(name) \
                or _SLICE_RECORD.search(name) or name == "Metadata/slice_info.config":
            out.append(name)
    return out


def _clean_payload(source: Path) -> tuple[dict, list[str]]:
    with zipfile.ZipFile(source) as archive:
        names = archive.namelist()
        drop = set(stripped_members(names))
        payload = {name: archive.read(name) for name in names if name not in drop}
    return payload, sorted(drop)


def export_editable(source, target, *, mapping=None, selected_plate_ids=None,
                    target_printer=None) -> dict:
    """Write the editable destination project for `source` at `target`.

    Returns the export report: source SHA-256, source kind and class,
    the members stripped, the mapping applied and the target path.
    Raises ExportRefused with a concrete reason when the source cannot
    honestly be exported.
    """
    source, target = Path(source), Path(target)
    descriptor = source_descriptor.describe(source)
    if descriptor["user_class"] == "sliced-only":
        raise ExportRefused(
            "this file is sliced-only: it carries toolpaths but no editable "
            "geometry, so there is no project to export. Slice the original "
            "model instead, or print the sliced file as it is.")
    if descriptor["user_class"] == "unsupported-corrupt":
        raise ExportRefused(
            "this file could not be read as a usable 3MF ("
            + "; ".join(descriptor["problems"] or ["unknown damage"]) + ").")
    if not descriptor["has_geometry"]:
        raise ExportRefused("this file carries no editable geometry to export.")
    check = remap_integrity.validate(source, mapping=mapping,
                                     selected_plate_ids=selected_plate_ids)
    if not check["ok"]:
        raise ExportRefused("the export was refused: " + "; ".join(check["problems"]))
    source_sha = _sha256(source)
    payload, dropped = _clean_payload(source)
    provenance = {"schema_version": 1, "exported_by": "print_handy editable_export",
                  "source_sha256": source_sha, "source_kind": descriptor["kind"],
                  "source_class": descriptor["user_class"],
                  "target_printer": target_printer,
                  "mapping": {str(k): v for k, v in (mapping or {}).items()},
                  "stripped_members": dropped}
    identity = not mapping or all(int(k) == int(v) for k, v in mapping.items())
    if identity:
        payload[PROVENANCE_MEMBER] = (
            json.dumps(provenance, indent=2, sort_keys=True) + "\n").encode()
        package_remap.write_package_atomically(target, payload, source=source)
    else:
        # Remap the cleaned package with the established remapper, then
        # stamp provenance into the result the same atomic way.
        import tempfile
        with tempfile.TemporaryDirectory(dir=target.parent) as staging:
            cleaned = Path(staging) / "cleaned.3mf"
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
                for name, data in payload.items():
                    archive.writestr(name, data)
            cleaned.write_bytes(buffer.getvalue())
            remapped = Path(staging) / "remapped.3mf"
            package_remap.remap_package(cleaned, remapped, mapping)
            with zipfile.ZipFile(remapped) as archive:
                final = {name: archive.read(name) for name in archive.namelist()}
            final[PROVENANCE_MEMBER] = (
                json.dumps(provenance, indent=2, sort_keys=True) + "\n").encode()
            package_remap.write_package_atomically(target, final, source=source)
    return {"target": str(target), "source_sha256": source_sha,
            "source_kind": descriptor["kind"],
            "source_class": descriptor["user_class"],
            "stripped_members": dropped,
            "mapping": provenance["mapping"],
            "target_printer": target_printer,
            "warnings": check["warnings"]}


def export_summary(report: dict) -> str:
    """One plain sentence about an export, for chat and page text."""
    text = (f"Exported an editable 3MF from the {report['source_kind']} source "
            f"(source SHA-256 {report['source_sha256'][:12]}…).")
    if report["stripped_members"]:
        text += (f" Stripped {len(report['stripped_members'])} stale slice "
                 "artifact(s): " + ", ".join(report["stripped_members"]) + ".")
    text += " Nothing was sliced, printed or charged to a spool."
    return text
