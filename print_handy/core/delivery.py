"""What happens to a validated slice: the post-slice choices offered by the printer's vendor profile.

Offline and printer-neutral. Nothing here talks to Discord or a printer: prepared_intake asks the
question and sends the messages this module builds. Decisions stay separate, as issue #123 asks:

  slicer          [printer] slicer, chosen before slicing (orca_adapter.slicer_choice)
  delivery        discord / bambu_handoff, any number, each at most once per slice
  print           printer, only through the per-job bed-clear Start button (print_next.start)

A delivery never grants, consumes or implies print authorization. Every delivered file is first bound
to the exact JOB.json and the SHA256 recorded when the slice returned; a changed or unknown file is
refused, not sent.
"""
from __future__ import annotations

import hashlib
import logging
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path

from .. import printers, settings
from ..printers.base import start_code_problems
from . import plate_thumbnail

logger = logging.getLogger(__name__)
PRINTER, DISCORD, BAMBU_HANDOFF = "printer", "discord", "bambu_handoff"
LABELS = {
    DISCORD: "Send the files here",
    BAMBU_HANDOFF: "Package for Bambu (no upload)",
}
# "don't print" keeps link_intake.ask from adding its own Cancel, which would discard the slice receipt.
DONE_LABEL = "Done, don't print"
# Already-compressed formats gain nothing from a zip.
_COMPRESSED = (".3mf", ".zip")


class DeliveryRefused(RuntimeError):
    """A file cannot be delivered: unknown, changed since slicing, or wrong for the target printer."""


def current_model(config=None):
    """The configured PrinterModel, with its vendor profile."""
    config = config or settings.current()
    return printers.MODELS[config["printer"]["model"]]


def methods(config=None):
    """Delivery methods enabled for the configured printer, in vendor order ([delivery] methods)."""
    config = config or settings.current()
    model = current_model(config)
    enabled = config.get("delivery", {}).get("methods") or list(model.vendor.default_delivery)
    return tuple(m for m in model.vendor.delivery if m in enabled)


def only_printer(enabled):
    """True when the profile offers nothing but printing: the post-slice question stays the Start button."""
    return tuple(enabled) == (PRINTER,)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def bound_artifacts(job, gcodes, recorded):
    """The sliced files, refused unless each is in this JOB.json and unchanged since the slice returned.

    recorded maps each G-code path to the SHA256 taken when slicing finished.
    """
    listed = {Path(job.get("gcode") or "").resolve()} if job.get("gcode") else set()
    listed |= {Path(row["gcode"]).resolve() for row in job.get("derived_plates") or [] if row.get("gcode")}
    out = []
    for gcode in gcodes:
        path = Path(gcode)
        if path.resolve() not in listed:
            raise DeliveryRefused(f"{path.name} is not a G-code of this job; nothing was sent")
        if not path.is_file():
            raise DeliveryRefused(f"{path.name} is gone; reslice before delivery")
        if recorded.get(str(gcode)) != sha256(path):
            raise DeliveryRefused(f"{path.name} changed after slicing; reslice before delivery")
        out.append(path)
    return out


@dataclass
class Attachment:
    source: Path  # the sliced or packaged file
    file: Path | None  # what is attached: the source, a zip of it, or None when too big
    size: int  # bytes of the source
    sent_size: int | None = None  # bytes of `file`
    mode: str = "attach"  # attach | zip | too_big


def _fmt_mb(size):
    return f"{size / 1_000_000:.1f} MB"


def upload_limit(config=None):
    """Largest chat attachment in bytes for the configured platform (#164).

    Discord reads [discord] upload_max_bytes, exactly as before; Telegram
    reads [telegram] upload_max_bytes (the Bot API cloud limit). Platforms
    without a section of their own keep the Discord value.
    """
    config = config or settings.current()
    platform = str((config.get("bot") or {}).get("platform") or "discord")
    section = config.get(platform)
    if isinstance(section, dict) and section.get("upload_max_bytes"):
        return int(section["upload_max_bytes"])
    return int(config["discord"]["upload_max_bytes"])


def effective_upload_limit(adapter=None, config=None):
    """The limit attachment planning must respect for one send (P2-92).

    The configured platform limit, lowered by the sending adapter's own
    cap when it reports a smaller one. The Matrix adapter's cap is the
    homeserver's discovered media limit — it lives only on the adapter,
    so planning from settings alone sized files the server then rejected
    instead of zipping them or reporting them too big. An adapter
    reporting no cap (or a larger one) changes nothing.
    """
    limit = upload_limit(config)
    cap = getattr(adapter, "upload_max_bytes", None)
    if cap:
        limit = min(limit, int(cap))
    return limit


def discord_plan(files, limit, workdir):
    """How each file reaches Discord under the upload limit (bytes): as is, zipped, or not at all.

    G-code compresses well, so a file over the limit is deflated into workdir and sent when the zip
    fits. A file still too big is not sent; the message names its size and where it stays.
    """
    plan = []
    for source in map(Path, files):
        size = source.stat().st_size
        if size <= limit:
            plan.append(Attachment(source, source, size, size, "attach"))
            continue
        if source.suffix.lower() in _COMPRESSED:
            plan.append(Attachment(source, None, size, None, "too_big"))
            continue
        workdir = Path(workdir)
        workdir.mkdir(parents=True, exist_ok=True)
        packed = workdir / (source.name + ".zip")
        with zipfile.ZipFile(packed, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            archive.write(source, source.name)
        packed_size = packed.stat().st_size
        if packed_size <= limit:
            plan.append(Attachment(source, packed, size, packed_size, "zip"))
        else:
            packed.unlink()
            plan.append(Attachment(source, None, size, packed_size, "too_big"))
    return plan


def discord_message(item, limit, label=None):
    """One Discord message for one planned attachment. MEDIA: lines attach a file through the gateway."""
    name = label or item.source.name
    if item.mode == "attach":
        return f"{name} ({_fmt_mb(item.size)})\nMEDIA:{item.file}"
    if item.mode == "zip":
        return (f"{name} is {_fmt_mb(item.size)}, over the {_fmt_mb(limit)} Discord upload limit; "
                f"sent zipped ({_fmt_mb(item.sent_size)}).\nMEDIA:{item.file}")
    zipped = f" and {_fmt_mb(item.sent_size)} zipped" if item.sent_size else ""
    return (f"{name} is {_fmt_mb(item.size)}{zipped}, over the {_fmt_mb(limit)} Discord upload limit, so it "
            f"was not sent. It stays on the Print Handy host at {item.source}.")


# ----------------------------------------------------------------------------- early size check (#272)

@dataclass
class Candidate:
    """One file 'Send the files here' would attach, sized before the button is offered.

    source is what Discord would get: the sliced G-code, or the Bambu package built from it (which may not
    exist yet: built_from then names the G-code it will hold). size is the bytes of source on disk, None for
    a package not built yet. packed is the deflated size, what a zip of a G-code or the package's G-code
    entry weighs: measured only when the plain size is over the limit, else None.
    """
    source: Path
    size: int | None
    packed: int | None
    limit: int
    built_from: Path | None = None

    @property
    def fits(self):
        """True when some form of this file can reach Discord under the limit (as is, zipped, or packaged)."""
        known = [n for n in (self.size, self.packed) if n is not None]
        return not known or min(known) <= self.limit


def deflated_size(path, level=9):
    """Bytes of the raw deflate stream of a file: what a zip entry of it would weigh, measured without
    writing anything. Streams in 1 MB blocks, so an 80 MB G-code costs seconds and no disk."""
    packer = zlib.compressobj(level, zlib.DEFLATED, -zlib.MAX_WBITS)
    total = 0
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            total += len(packer.compress(block))
    return total + len(packer.flush())


def package_target(gcode):
    """Where bambu_handoff writes the package for one G-code: <its folder>/bambu/<name>.gcode.3mf."""
    gcode = Path(gcode)
    stem = gcode.name[:-len(".gcode")] if gcode.name.endswith(".gcode") else gcode.stem
    return gcode.parent / "bambu" / (stem + ".gcode.3mf")


def discord_candidates(files, limit, config=None, cache=None):
    """Size what 'Send the files here' would attach, before the button is offered (#272).

    For a Bambu Lab printer the attachment is the .gcode.3mf package: its exact size once built, else a
    lower bound from deflating the G-code it will hold (the package is that G-code deflated plus a few
    hundred bytes). For other printers it is the G-code, zipped when over the limit. A deflate pass runs
    only for a file whose plain size is over the limit; cache (a dict the caller keeps) stores it by path
    and size so a menu shown twice measures once.
    """
    config = config or settings.current()
    packaged = current_model(config).vendor.file_formats[0] == ".gcode.3mf"
    cache = cache if cache is not None else {}
    out = []
    for gcode in map(Path, files):
        source, built_from = (package_target(gcode), gcode) if packaged else (gcode, None)
        if not gcode.is_file():  # unknown size: the send itself reports a file that is gone
            out.append(Candidate(source, None, None, limit, built_from))
            continue
        if source.is_file():
            size = source.stat().st_size
            if size <= limit or source.suffix.lower() in _COMPRESSED:
                out.append(Candidate(source, size, None, limit, None))
                continue
            measured = source
        else:
            size = None
            measured = gcode
            if gcode.stat().st_size <= limit:  # deflating never grows a G-code past this limit in practice
                out.append(Candidate(source, None, None, limit, built_from))
                continue
        key = (str(measured), measured.stat().st_size)
        if key not in cache:
            cache[key] = deflated_size(measured)
        out.append(Candidate(source, size, cache[key], limit, built_from))
    return out


def oversize_message(candidates, limit, for_handoff=False):
    """Why each file that cannot reach Discord will not be offered: its size, the limit, where it lives.

    Empty when everything fits. The paths are on the Print Handy host, where the operator fetches a
    file Discord cannot carry. for_handoff phrases a package that was just built.
    """
    lines = []
    for item in candidates:
        if item.fits:
            continue
        over = f"over the {_fmt_mb(limit)} Discord upload limit, so it cannot be sent here"
        if item.size is None:  # a package not built yet, sized from its G-code
            gcode = f"its G-code {item.built_from.name} is {_fmt_mb(item.built_from.stat().st_size)}"
            lines.append(f"{item.source.name} would be about {_fmt_mb(item.packed)} ({gcode}), {over}. "
                         f"'{LABELS[BAMBU_HANDOFF]}' writes it to {item.source} on the Print Handy host.")
            continue
        zipped = f" and {_fmt_mb(item.packed)} zipped" if item.packed is not None else ""
        lines.append(f"{item.source.name} is {_fmt_mb(item.size)}{zipped}, {over}. "
                     f"It stays on the Print Handy host at {item.source}.")
    if lines and not for_handoff and not any(item.fits for item in candidates):
        lines.append(f"'{LABELS[DISCORD]}' is not offered for this slice; "
                     "[discord] upload_max_bytes is the limit (docs/SETTINGS.md).")
    return "\n".join(lines)


# ----------------------------------------------------------------------------- Bambu handoff

_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
 <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
 <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>
 <Default Extension="gcode" ContentType="text/x.gcode"/>
</Types>
"""
_RELS = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Target="/3D/3dmodel.model" Id="rel-1" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>
</Relationships>
"""
_MODEL = """<?xml version="1.0" encoding="UTF-8"?>
<model unit="millimeter" xml:lang="en-US" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
 <metadata name="Application">Print Handy</metadata>
 <resources/>
 <build/>
</model>
"""
# Fixed timestamp: the same G-code always packs to the same bytes.
_ZIP_TIME = (2020, 1, 1, 0, 0, 0)


def _slice_info(plate, nozzle_mm):
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<config>\n <header>\n'
            '  <header_item key="X-BBL-Client-Type" value="slicer"/>\n'
            '  <header_item key="X-Print-Handy" value="offline handoff"/>\n </header>\n <plate>\n'
            f'  <metadata key="index" value="{int(plate)}"/>\n'
            f'  <metadata key="nozzle_diameters" value="{float(nozzle_mm):g}"/>\n </plate>\n</config>\n')


# Preview roles -> Bambu Studio's file names for plate n. slice_info.config does not reference them:
# the printer, Bambu Handy and Studio read the plate previews by these names.
_THUMBNAIL_NAMES = {"plate": "Metadata/plate_{n}.png", "plate_small": "Metadata/plate_{n}_small.png"}


def bambu_package(gcode, target, plate=1, nozzle_mm=0.4, thumbnails=None):
    """Write a .gcode.3mf holding one plate's G-code, in the layout Bambu Studio exports.

    Metadata/plate_<n>.gcode with its MD5 in Metadata/plate_<n>.gcode.md5, a slice_info.config naming
    the plate, an empty 3D model and, when `thumbnails` (plate_thumbnail.Thumbnails or its images dict)
    holds them, the plate previews plate_<n>.png (512 px) and plate_<n>_small.png (128 px) (#275). Built
    offline from the validated G-code; not yet verified on Bambu hardware (see docs/BAMBU-LAB.md).
    """
    raw = Path(gcode).read_bytes()
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    images = getattr(thumbnails, "images", thumbnails) or {}
    entries = [
        ("[Content_Types].xml", _CONTENT_TYPES.encode()),
        ("_rels/.rels", _RELS.encode()),
        ("3D/3dmodel.model", _MODEL.encode()),
        (f"Metadata/plate_{int(plate)}.gcode", raw),
        (f"Metadata/plate_{int(plate)}.gcode.md5", hashlib.md5(raw).hexdigest().upper().encode()),
        ("Metadata/slice_info.config", _slice_info(plate, nozzle_mm).encode()),
    ] + [(name.format(n=int(plate)), images[role]) for role, name in _THUMBNAIL_NAMES.items() if images.get(role)]
    tmp = target.with_name(target.name + ".part")
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries:
            info = zipfile.ZipInfo(name, date_time=_ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            # Level 1 per entry, the same trade gcode_3mf.package made in
            # #929 (#952: this path was missed and kept the default level 6):
            # several times faster deflate of a 50+ MB plate for a modestly
            # larger package; ZIP_DEFLATED at any level is the same format.
            # With an explicit ZipInfo, zipfile uses the entry's own level.
            info._compresslevel = 1
            archive.writestr(info, data)
    tmp.replace(target)
    return target


def bambu_handoff(job, gcodes, config=None):
    """Package each validated plate as a .gcode.3mf for a Bambu Lab printer. Uploads and starts nothing.

    Refused unless the configured printer and the job's printer are Bambu Lab, unless every
    G-code line is a plain G/M/T code (Klipper or ZMod start code never reaches Bambu hardware), and
    unless the printer's own guard passes (adapter.gcode_problems: vendor blocks against the pinned
    templates, toolpath bounds, limits).
    """
    config = config or settings.current()
    model = current_model(config)
    if BAMBU_HANDOFF not in model.vendor.delivery:
        raise DeliveryRefused(f"{model.label} is not a Bambu Lab printer; no Bambu package was made")
    job_key = printers.job_model(job)
    if job_key != model.key:
        raise DeliveryRefused(f"this job was sliced for {job_key}, not {model.key}; no Bambu package was made")
    nozzle = config["printer"].get("nozzle_diameter_mm", 0.4)
    adapter = printers.get(model.key, config)
    packages = []
    for index, gcode in enumerate(map(Path, gcodes), 1):
        text = gcode.read_text(errors="replace")
        problems = start_code_problems(text, model.vendor) + adapter.gcode_problems(text)
        if problems:
            from .material_accept import TOKEN
            raise DeliveryRefused("; ".join(TOKEN.sub('', p) for p in problems) + "; no Bambu package was made")
        packages.append(bambu_package(gcode, package_target(gcode), index, nozzle,
                                      _thumbnails(gcode, job, config, text)))
    return packages


def _thumbnails(gcode, job, config, text):
    """Plate previews for one G-code, or none: a preview problem never blocks a validated delivery."""
    try:
        found = plate_thumbnail.plate_thumbnails(gcode, job, config["printer"].get("bed_mm"), text)
    except Exception as exc:  # noqa: BLE001  cosmetic: log, package without previews
        logger.warning("bambu package: no plate preview for %s: %s", gcode.name, exc)
        return None
    logger.info("bambu package: plate preview for %s: %s", gcode.name, found.source)
    return found


def handoff_note(model):
    """What a Bambu handoff does and does not do, for the Discord reply."""
    return (f"Nothing was uploaded to the {model.label} and no print was started: this handoff never "
            "uploads. (Printing over LAN is a separate, opt-in delivery — 'Slice and print now' — and "
            "only when you enable it.) The "
            ".gcode.3mf follows the Bambu Studio export layout, but opening it in Bambu Studio or Bambu Handy, or "
            "printing it from the SD card, has not been tested on a printer yet: check it before you start it.")


def files_for_discord(job, artifacts, config=None):
    """Files the Discord delivery sends: the vendor's primary format. Bambu Lab gets .gcode.3mf packages."""
    config = config or settings.current()
    model = current_model(config)
    if model.vendor.file_formats[0] == ".gcode.3mf":
        return bambu_handoff(job, artifacts, config)
    return list(artifacts)


def record(receipt, method, items, complete, detail=""):
    """Append one delivery attempt to the intake receipt.

    items: [{'source', 'sent', 'ok'}] per file. complete means nothing is left to retry: every file was
    delivered or reported as impossible (too big). A transient failure leaves the method on offer.
    """
    receipt.setdefault("deliveries", []).append({
        "method": method, "complete": bool(complete), "detail": detail,
        "files": [{**item, "source": str(item["source"]), "sent": str(item["sent"]) if item.get("sent") else None}
                  for item in items],
    })
    return receipt


def done(receipt, method):
    """True when this method already finished for this slice: it is not offered or delivered twice."""
    return any(d.get("method") == method and d.get("complete") for d in receipt.get("deliveries") or [])


def delivered(receipt, method):
    """Sources this method already delivered for this slice, so a retry sends only the rest."""
    return {f["source"] for d in receipt.get("deliveries") or [] if d.get("method") == method
            for f in d.get("files") or [] if f.get("ok")}
