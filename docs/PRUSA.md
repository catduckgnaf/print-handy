# Prusa printers (PrusaLink LAN; MK4S, CORE One, XL, MINI+)

[Documentation home](README.md) · [Printer profiles](PRINTER-PROFILES.md) · Issue [#242](https://github.com/catduckgnaf/print-handy/issues/242)

Print Handy supports Prusa's current Buddy-firmware printers through **PrusaLink**, the HTTP API that runs on the printer itself — no PrusaConnect cloud account, no OctoPrint in between. Nothing here has been tested on a Prusa printer yet: slicing, profile generation and the LAN client are covered by offline tests with fake transports, and live support is explicitly unclaimed until a hardware test exists.

| Model | Key | Bed (mm) | Height | Notes |
|---|---|---|---|---|
| MK4S | `prusa.mk4s` | 250 × 210 | 220 | Single tool |
| CORE One | `prusa.coreone` | 250 × 220 | 270 | Enclosed CoreXY; chamber not driven |
| XL (5-tool) | `prusa.xl` | 360 × 360 | 360 | Five toolheads, driven as a multi-tool model (T0–T4; see below) |
| MINI+ | `prusa.miniplus` | 180 × 180 | 180 | Runs Orca's MINI profiles (`printer_model` is `Prusa MINI`) |

Select one with `printer.model: prusa.mk4s` (etc.). The MK3.9S/MK3.5S upgrade paths and the CORE One L are follow-up profiles on #242: same vendor and client, added once their Orca identities are pinned.

## Set up the profiles

Print Handy ships no Prusa slicer profiles (OrcaSlicer's Prusa profiles are AGPL-3.0). Generate them from your own OrcaSlicer v2.5.0-rc installation (the shipped slicer; the 2.5 line since #356; its Prusa profiles are unchanged from v2.5.0-beta, so the recorded beta identities cover it):

```bash
python3 scripts/prusa-templates.py --printer mk4s --resources <OrcaSlicer resources folder>
```

The resources folder holds `profiles/Prusa.json` and `profiles/Prusa/`, or — OrcaSlicer 2.5 and later — the packed preset cache `profiles/Prusa.opc` on its own; the command reads either. It resolves the profiles' `inherits`/`include` chains and refuses unless they hash to a checked identity recorded in `print_handy/printers/prusa/facts.py` (OrcaSlicer 2.4.2, Prusa vendor profiles `02.04.00.03`; OrcaSlicer v2.5.0-alpha, `02.04.00.17`, and OrcaSlicer v2.5.0-beta, `02.04.00.17`, each from its tree or its packed cache — each packaging has its own identity), then writes them into `<state>/orca-profiles/prusa_<model>/`, never into the checkout. One 2.5 change to know: upstream renamed the whole `Prusa Generic …` filament family to `Generic … @Prusa …`, and the checked default filaments follow the new names, so generate Prusa templates from a 2.5 install (2.5.0-alpha or 2.5.0-beta) — a folder generated from 2.4.2 keeps verifying, but its profile names no longer resolve in a 2.4.2 tree. Prusa's machine profiles store start/end G-code as a list of lines; the generated templates join them into the string form every other vendor uses (the stored upstream copies keep the vendor's form, so the identity check is unaffected). Missing, edited or unrecognised profiles fail closed, exactly as for Bambu Lab.

## PrusaLink (opt-in)

Off by default. To enable status — and, separately, printing — set the printer's address:

```yaml
prusa:
  link_host: 192.168.1.70        # the printer's LAN address (plain host, no scheme or port)
  api_key_env: PRUSA_LINK_API_KEY  # the default; names an environment variable
```

Put the PrusaLink API key (shown on the printer under Settings → Network → PrusaLink) in the environment variable that `api_key_env` names. The key is never a config value and never logged; the log redactor covers `*_KEY` names. The client sends it as the `X-Api-Key` header and answers a Digest challenge as user `maker` with the same key, the two forms PrusaLink accepts.

With the host set, the health check reads one status snapshot (state, job, progress, temperatures). Printing additionally requires opting in, like Bambu Lab:

```yaml
delivery:
  methods: [printer, discord]
```

The print path then runs the usual sequence: readiness from a live snapshot → offline G-code verification (right model, tools within the model's toolheads, temperatures within the model's published limits, no foreign start code) → launch lease → bed-clear approval inside the lease → a second snapshot → upload (`PUT /api/v1/files/<storage>/<file>`, USB storage preferred, with the size read back and required to match before anything starts) → start (`POST` on the uploaded file) → confirmation from a fresh snapshot. An unconfirmed start is recorded as uncertain and is never resubmitted. Pause/resume/stop map to `PUT /api/v1/job/<id>/pause`, `PUT .../resume` and `DELETE /api/v1/job/<id>`.

Both `.gcode` and the binary `.bgcode` form are accepted file types; what OrcaSlicer produces here is `.gcode`. `services.moonraker_url` and `services.octoprint_url` must stay empty for a Prusa model; settings refuse to load otherwise.

There is no PrusaLink listener, so a print Print Handy launches is followed by a launched-print monitor (the same pattern as the Bambu, stock Flashforge and OctoPrint families). When the print ends, the monitor makes the completion post. If `[timelapse] capture_enabled` is on and the printer registers a camera with PrusaLink (`GET /api/v1/cameras` — a camera attached to the printer, such as a USB camera or the CORE One's built-in camera), the monitor captures stills from that camera during the print (`GET /api/v1/cameras/<id>/snap`, PNG stills transcoded to JPEG frames) and attaches the assembled timelapse — encoded and sized to the chat platform's upload limit exactly like every other family's — to the completion post. A printer or firmware with no camera registered gets the completion post without a video, and the log says why; capture trouble never delays or loses the completion post. The monitor runs inside the process that launched the print and is not restarted if that process restarts mid-print; prints started outside Print Handy are not monitored. The camera endpoints are from Prusa's published PrusaLink specification and, like everything else in this document, are **not verified on a live Prusa printer**.

In the combined Docker container the supervisor honours that: for a Prusa model it does not start the Moonraker listener at all, and the healthcheck does not require one, so the container starts, stays up and reports healthy with just the bot (with a token) and the bundled Spoolman — see [Which processes run](DOCKER.md#which-processes-run).

## The XL's toolheads

The XL is a **multi-tool** model, like the Flashforge Creator pair: a job's colors are answered per toolhead, each toolhead binds its own Spoolman spool, and the file is sliced with one filament profile per toolhead through the XL's own 5-extruder profiles. Slicer tool *i* always prints with toolhead *i* (T0–T4) — the mapping is identity-only, and a remap or a sixth tool is refused with the reason named. PrusaLink starts a file and takes no tool parameter, so tool changes are the sliced file's own `T` lines, executed by the firmware; the offline guard accepts T0–T4 for the XL and still refuses a tool beyond its toolheads (and still refuses any `T1`+ for the other, single-tool Prusa models). Split (non-merged) plates stay refused, as on the Creator pair: they are IFS-shaped.

One limit is Prusa's, not Print Handy's: the published PrusaLink API reports a single nozzle temperature and no per-toolhead state (evidence on #670), so readiness and status for the XL are printer-level — there are no per-toolhead temperature or loaded-filament checks, and none are invented. The file itself is fully verified offline before upload, and the firmware cross-checks each used tool's nozzle at print start via the file's own `M862.1` lines.

## What is not supported

- PrusaConnect cloud (any model) — LAN only, the same decision as Bambu Lab in #187.
- MMU slicing, and multi-tool slicing on the single-tool models (MK4S, CORE One, MINI+): jobs there are single-tool; a `T1`+ tool change in a file is refused by the guard. The XL is the exception — see above.
- Input Shaper tuning, chamber control, live camera streaming, firmware updates. (Timelapse stills from a PrusaLink-registered camera are supported — see above.)
- Any claim of a successful print on real hardware.
