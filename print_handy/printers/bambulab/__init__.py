"""Bambu Lab vendor profile: slicing and packaging, plus opt-in LAN telemetry, upload and gated print start (#187, #127).

Models: a1, a1mini, p1p, p1s, p2s, x1, x1c, x1e, h2s and a2l — every current single-nozzle Bambu Lab
printer — plus the dual-nozzle h2d, h2dpro, x2d and h2c (#127 Stage 2, #308): their profiles carry two
nozzles, handled by the extruder-aware checks, and their print starts resolve AMS trays per filament
slot (ams_mapping.py). The h2c and a2l exist only in the OrcaSlicer 2.5 line's BBL profiles and were
registered when that line became the shipped slicer (#356, on v2.5.0-alpha).

The H3C (Bambu's nine-tool Swaptek changer, launched with Bambu Studio 02.09.01.70) has initial
support only: its sourced facts and its HMS serial prefix (32X) are recorded in facts.py, but it is
NOT a registered model — the pinned OrcaSlicer v2.5.0-rc carries no H3C profiles, so no checked
identity can be computed for it. See facts.py (the H3C block) and docs/BAMBU-LAB.md.

Print Handy slices, validates and packages; a person moves the file. When the operator configures
[bambu] lan_host/lan_serial and supplies the LAN access code as a secret, the adapter can also read
live status over LAN MQTT (client.py), upload the package over FTPS, and — only when the operator
also adds `printer` to [delivery] methods and the printer is in Developer Mode — start it, through
the gated print path in core/model_job. Without that configuration the vendor behaves exactly as
the original offline vendor did. The handoff format is a .gcode.3mf (Metadata/plate_N.gcode plus its
MD5) in Bambu Studio's export layout; no printer, Bambu Studio, Bambu Handy or SD-card run of it has
been tested yet, and neither has the LAN telemetry path. Bambu Studio is the default slicer for every
Bambu Lab printer; [printer] slicer: orca opts back into Orca.
Every G-code line must be a plain G/M/T code: Klipper or ZMod start code is refused (base.start_code_problems).

Printer modules (bambulab/a1.py, later p1s.py) define MODEL = PrinterModel(..., vendor=VENDOR) with the
printer's own bed, height, nozzles and adapter; their sourced facts and profile identity are in facts.py,
their G-code checks in guard.py. Their Orca templates are generated from the operator's own OrcaSlicer
(profiles.py); none are shipped. See docs/PRINTER-PROFILES.md and docs/BAMBU-LAB.md.
"""
from ..base import SHARED_SETTINGS, VendorProfile, merge_settings

VENDOR = VendorProfile(
    key="bambulab",
    label="Bambu Lab",
    connection="bambulab_lan",
    start_code="bambu",
    slicers=("bambu_studio", "orca"),  # Bambu Studio first: the default for every Bambu Lab printer
    file_formats=(".gcode.3mf", ".gcode"),
    # `printer` is offered but NOT a default: the print button appears only when the operator
    # adds "printer" to [delivery] methods, on top of configuring LAN + Developer Mode.
    delivery=("printer", "discord", "bambu_handoff"),
    default_delivery=("discord", "bambu_handoff"),
    settings=merge_settings(SHARED_SETTINGS, {
        "printer": {
            "firmware": "Bambu Lab (offline handoff only)",
            # Unused: the vendor start/end G-code comes from the Orca template. Kept valid for settings checks.
            "start_macro": "PRINT_START",
            "end_macro": "PRINT_END",
        },
    }),
)
