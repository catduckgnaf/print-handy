# Elegoo printers (Neptune 4 family on Moonraker; Centauri Carbon on SDCP)

[Documentation home](README.md) · [Printer profiles](PRINTER-PROFILES.md)

Two Elegoo families, nothing in common. Nobody has put these through a real print yet: the slicing and LAN paths pass offline tests, but your Neptune is the first live run. Keep a hand on the stop button.

The Neptune 4 line runs Klipper and talks Moonraker; the Centauri Carbon speaks SDCP over the LAN. They share nothing at runtime, so they are two vendors here.

| Model | Key | Vendor | Bed (mm) | Height | Connection |
|---|---|---|---|---|---|
| Neptune 4 | `elegoo.neptune4` | `elegoo` | 230 × 230 | 265 | Klipper + Moonraker |
| Neptune 4 Pro | `elegoo.neptune4pro` | `elegoo` | 230 × 230 | 265 | Klipper + Moonraker |
| Neptune 4 Plus | `elegoo.neptune4plus` | `elegoo` | 325 × 325 | 385 | Klipper + Moonraker |
| Neptune 4 Max | `elegoo.neptune4max` | `elegoo` | 426 × 426 | 480 | Klipper + Moonraker |
| Centauri Carbon | `elegoo_sdcp.centauricarbon` | `elegoo_sdcp` | 256 × 256 | 256 | SDCP v3.0 LAN |

Bed sizes are the pinned Orca profiles' printable areas; the values slicing enforces. Elegoo's marketed beds differ (Neptune 4: 235 × 235, Plus 320 × 320, Max 420 × 420, Centauri Carbon 260 × 260); both figures are recorded in each model's facts, and the Orca value is the one used. Elegoo's resin printers (Mars/Saturn/Jupiter) are out of scope: they print `.goo`/Chitu files, not G-code.

## Set up the profiles

Print Handy ships no Elegoo slicer profiles (OrcaSlicer's Elegoo profiles are AGPL-3.0). Generate them from your own OrcaSlicer v2.5.0 installation (the shipped slicer; the 2.5 line; its Elegoo profiles are unchanged from v2.5.0-beta, so the recorded beta identities cover it):

```bash
python3 scripts/elegoo-templates.py --printer neptune4 --resources <OrcaSlicer resources folder>
python3 scripts/elegoo-templates.py --vendor elegoo_sdcp --printer centauricarbon --resources <OrcaSlicer resources folder>
```

The resources folder holds `profiles/Elegoo.json` and `profiles/Elegoo/`, or, OrcaSlicer 2.5 and later, the packed preset cache `profiles/Elegoo.opc` on its own; the command reads either. It resolves the profiles' `inherits`/`include` chains and refuses unless they hash to a checked identity recorded in the model's facts (OrcaSlicer 2.4.2, Elegoo vendor profiles `02.04.00.06`; OrcaSlicer v2.5.0-alpha, `02.04.00.11`, and OrcaSlicer v2.5.0-beta, `02.04.00.13`, each from its tree or its packed cache, each packaging has its own identity), then writes them into `<state>/orca-profiles/<model folder>`, never into the checkout. No Elegoo profile was renamed or removed in 2.5.0-alpha or 2.5.0-beta. Missing, edited or unrecognised profiles fail closed, exactly as for Bambu Lab and Prusa.

## Neptune 4 family (Moonraker)

The Neptunes run stock Klipper, so everything Moonraker-shaped works the way it does for any Klipper printer: set `services.moonraker_url` to the printer on the standard Moonraker port 7125 (for example `http://192.168.1.60:7125`) and Print Handy uploads, starts and watches prints through the usual path, with `printer` delivery on by default. That `:7125` endpoint is the verified stock-firmware endpoint; it supersedes the port-80 guidance briefly documented by #322, which rested on documentary sources only; the printer's port-80 web interface is not the endpoint to configure here. Self-installed Mainsail, Fluidd or OpenNept4une setups use the same `:7125` address. What the dedicated `elegoo` vendor adds over `klipper.generic` is the correct bed size per model and the pinned templates: the generic profile's 220 × 220 × 250 mm defaults match no Neptune and would silently mis-slice.

Elegoo's Neptune start G-code is a self-contained sequence in the file (their profiles do not call a `PRINT_START` macro), so the guard checks the file carries that template start, names this model, and stays within the model's published limits (300 °C nozzle, 110 °C bed), and refuses other ecosystems' start code, including a bare `PRINT_START` macro call.

In the combined Docker container the Neptune models behave like any other Moonraker printer: the supervisor starts the Moonraker listener and the healthcheck requires its heartbeat, see [Which processes run](DOCKER.md#which-processes-run).

## Centauri Carbon (SDCP, opt-in)

The Centauri line runs closed firmware and speaks **SDCP v3.0** on the LAN: a WebSocket on port 3030 for status and control, a UDP broadcast (`M99999`, port 3000) for discovery, and chunked HTTP for file uploads. **SDCP has no authentication at all** and is plaintext: anyone on the network can control the printer. Print Handy cannot change that; run the printer on a trusted network only.

Off by default. To enable status, and, separately, printing, set the printer's address:

```yaml
elegoo_sdcp:
  host: 192.168.1.80        # the printer's LAN address (plain host, no scheme or port)
```

With the host set, the health check reads one SDCP status answer (state, current/total layer, temperatures). Printing additionally requires opting in, like Bambu Lab and Prusa:

```yaml
delivery:
  methods: [printer, discord]
```

The print path then runs the usual sequence: readiness from a live status answer → offline G-code verification (right model, single tool, temperatures within limits, 320 °C nozzle, 110 °C bed, no foreign start code) → launch lease → bed-clear approval inside the lease → a second status read → upload (1 MiB chunks with the file's MD5, the size read back and required to match before anything starts) → start command for that exact file → confirmation from a fresh status answer. An unconfirmed start is recorded as uncertain and is never resubmitted. Pause/resume/stop map to SDCP commands 129/131/130. Start requests carry `Calibration_switch: 0`: Print Handy never starts a calibration the operator did not ask for.

There is no SDCP listener, so a print Print Handy launches is followed by a launched-print monitor (the same pattern as the Bambu, stock Flashforge, OctoPrint and Prusa families). When the print ends, the monitor makes the completion post. Timelapse capture is wired into that monitor through the shared #246 machinery, but **no SDCP camera source is verified**: the SDCP client speaks status, upload, start/control and file commands only, no camera still or stream endpoint is recorded in the vendor facts or Elegoo's published SDCP documentation as this codebase uses it, and Print Handy does not guess endpoints, so a launched Centauri print currently gets the completion post without a video, and the log says why (see `printers/elegoo_sdcp/camera.py` for the audit). If a camera source is verified later, capture slots into the same monitor unchanged. The monitor runs inside the process that launched the print and is not restarted if that process restarts mid-print; prints started outside Print Handy are not monitored.

`services.moonraker_url` and `services.octoprint_url` must stay empty for a Centauri model; settings refuse to load otherwise.

In the combined Docker container the supervisor honors that: for a Centauri Carbon model it does not start the Moonraker listener at all, and the healthcheck does not require one, so the container starts, stays up and reports healthy with just the bot (with a token) and the bundled Spoolman, see [Which processes run](DOCKER.md#which-processes-run).

## What is not supported

- The open-frame Centauri and the Centauri Carbon 2 / Centauri 2 (profiles exist in Orca; follow-ups on #243 once their SDCP behavior is confirmed to match).
- Elegoo resin printers (Mars/Saturn/Jupiter) and their `.goo` files.
- Fan control over SDCP (not wired up). The chamber light **is** wired, as the auto light-off's target: Cmd 403 (`LightStatus.SecondLight`) turns it on at print start. (Cmd 386 is the video-stream enable in published SDCP command tables, not a light command.) Auto-off still cannot run on SDCP, see the timelapse note below.
- Timelapse video for launched Centauri prints: the monitor and the shared capture/encode pipeline are in place, but no SDCP camera endpoint is verified, so completion posts carry no video until one is (see above).
- Any claim of a successful print on real hardware.
