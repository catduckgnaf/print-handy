"""Elegoo SDCP camera source: none verified — the camera-less seam (#295).

The Centauri Carbon has an onboard camera, and #295's Stage 1 audit
marked its source "to verify: endpoint and auth". Verification against
everything this codebase actually speaks comes back empty:

- The SDCP client (`client.py`) — written against Elegoo's published
  SDCP documentation, the same source the vendor's other protocol
  facts come from — implements discovery (UDP `M99999`), status
  (Cmd 0), start/pause/stop/resume (Cmds 128–131) and file list/info
  (Cmds 258/260). No command in it reads a camera still or opens a
  stream, and no snapshot or stream URL for the printer is recorded
  anywhere in the vendor facts (`facts.py`), the vendor doc
  (`docs/ELEGOO.md`), or the client.
- The only other SDCP commands the repo records at all are 386/403 in
  `docs/ELEGOO.md`'s "not supported" list. Cmd 403 is device control
  (chamber light/fan) — wired for the light by the #516 follow-up,
  not a camera command. Cmd 386 is named the video-stream enable in
  the published SDCP command tables (an MJPEG URL on :3031 in the
  OpenCentauri protocol notes): a lead worth one hardware check, but
  unverified against any firmware this codebase can cite, so it does
  not change this audit's answer — no endpoint is guessed from it.

So there is no camera source to wire: this module — the vendor's
camera seam, the counterpart of `printers.prusa.camera` and
`printers.flashforge_stock.camera` — resolves to "no source", and the
launched-print monitor (`print_handy.runtime.elegoo_watch`) runs its
status-driven completion flow with that answer, logging the reason
once per print. Inventing an endpoint here would be worse than the
honest gap: a guessed URL that 404s (or answers something else) on
real firmware would masquerade as a broken camera instead of an
unverified one.

When a source IS verified — on hardware, or in a published SDCP
revision this client can cite the way it cites its other facts —
this is the single place that changes: `camera_source` returns the
`(url, fetch)` pair the #246 capture session consumes (a still URL
for the manifest plus a fetch returning one frame as JPEG bytes),
and the monitor picks it up unchanged.

Nothing here is live-hardware verified: the whole Elegoo SDCP vendor
is offline-verified only (docs/ELEGOO.md), and this module does not
change that status.
"""
from __future__ import annotations


def camera_source(client):
    """The printer's camera source as a (url, fetch) pair, or ("", None).

    Today this always returns ("", None): no SDCP camera endpoint is
    verified in this codebase (see the module docstring), so a
    launched Centauri print gets its completion post with no capture —
    never a guessed endpoint. The client argument is accepted (and
    unused) so the seam keeps the shape the other vendors' camera
    modules present to their monitors, and so a future verified
    source can be resolved from the printer itself.
    """
    return "", None
