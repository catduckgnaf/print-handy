# Stock Flashforge printers

The stock (non-ZMod) Flashforge path: an Adventurer 5X on its factory
firmware, driven over the printer's own LAN HTTP API. For the same printer
running ZMod (Klipper + Moonraker), see [ZMod printers](ZMOD.md) instead, 
the two are different printer profiles and never mix.

| Model key | Printer | Status |
|---|---|---|
| `flashforge_stock.ad5x` | Adventurer 5X (stock firmware) | LAN upload/start implemented; see verification status below |

## Every Flashforge model, and which route applies

So the stock page never reads as if the AD5X were the only Flashforge printer, here is the whole Flashforge line-up with the route that applies to each model today:

| Printer | Stock firmware (this page) | ZMod (Moonraker) |
|---|---|---|
| Adventurer 5X (AD5X) | Built-in profile: `flashforge_stock.ad5x` | Built-in profile: `flashforge.ad5x` |
| Adventurer 5M (AD5M) | No stock profile yet | Built-in profile: `flashforge.ad5m` |
| Adventurer 5M Pro (AD5M Pro) | No stock profile yet | Built-in profile: `flashforge.ad5m_pro` |
| Creator 5 | No stock profile yet | Built-in profile: `flashforge.creator5` |
| Creator 5 Pro | No stock profile yet | Built-in profile: `flashforge.creator5_pro` |

The ZMod profiles are covered in [ZMod printers](ZMOD.md). Nothing on this page claims stock-firmware support for a model without a stock profile.

## How Print Handy talks to it

- **Transport:** HTTP POSTs to port **8898** on `[flashforge_stock] host`
  (`/detail`, `/uploadGcode`, `/printGcode`, `/control`). Every request
  carries the printer serial number and check code in the body; both come
  from the environment (`AD5X_SERIAL`, `AD5X_CHECK_CODE`), never from a
  settings file, a URL or a log line.
- **Prints:** one tool (T0) fed from one of the four IFS bays. Print Handy
  uploads the G-code with printing disabled, reads slot occupancy back
  from `/detail`, and starts only the file it uploaded, only after the
  bed-clear approval is consumed. Stock firmware does not speak
  Moonraker, and Print Handy sends it no Klipper macros, temperatures or
  fan commands; the slicer's G-code owns the print.
- **No listener:** stock firmware has no event stream Print Handy can
  subscribe to, so a monitor started by the print path itself polls
  `/detail` for the prints Print Handy launched (below). Prints started
  from the printer screen or another app are not watched.

## Timelapse

With `[timelapse] capture_enabled` on, the monitor that watches a
Print Handy-launched print also captures a timelapse and attaches it to
the completion post:

1. On the first active poll, the monitor reads the camera source **the
   printer itself advertises** in `/detail` (`cameraStreamUrl`), opens
   the stream (`streamCtrl_cmd`), and starts one bounded capture session
   through the shared #246 capture pipeline (same settings, caps and
   retention as the Moonraker path; frames are stored per job and pruned
   the same way).
2. While the print runs, frames are pulled from the stream at
   `capture_interval_s` (a pause holds the session but takes no frames).
3. At the terminal state the session stops, the stream is closed, the
   frames are encoded to an H.264 MP4 with ffmpeg (the `[timelapse]
   encoder` choice, CPU by default, hardware falling back to CPU) and
   sized to the configured platform's upload limit. The completion post
   carries it through the usual `MEDIA:` attachment.
4. Any failure, no camera, a refused stream, a failed encode, leaves
   the ordinary completion post intact and logs the reason. A video that
   was recorded but cannot be sized under the platform limit is reported
   in the post instead of vanishing silently.

The monitor runs inside the process that launched the print; if that
process restarts mid-print, monitoring (and the timelapse) stops with
it; the completion post then does not happen either. This is the same
stated limitation as the Bambu launched-print monitor.

### The AD5X camera: what is actually known

Read this before expecting a video:

- **The AD5X has no built-in camera.** Per the public Flashforge API
  documentation the LAN client is built on (Parallel-7/flashforge-api-docs:
  capability matrix and the AD5X endpoint schemas for firmware 1.1.7 and
  1.2.1; 3.1.0 documents the same endpoint set), the AD5X ships without a
  camera and without the camera port enabled. A camera is an **accessory**:
  with one installed and enabled, the firmware serves an MJPEG stream and
  reports it in `/detail`.
- **Protocol:** MJPEG over HTTP on port **8080**, `http://<printer-ip>:8080/?action=stream`.
  The stream URL is whatever `/detail` returns in `cameraStreamUrl`, 
  empty means no camera is installed or the camera is disabled, and
  Print Handy then captures nothing and says so in the log. The stream
  itself carries no credentials in any documented example; the serial +
  check code authentication belongs to the 8898 API above.
- **Not live-verified.** No AD5X running Print Handy has yet confirmed a
  `cameraStreamUrl`, a frame, or an attached timelapse on real hardware.
  Everything above is implemented and proven offline against recorded
  fixtures; the endpoint's behaviour on a real AD5X (with a camera
  accessory fitted) is the remaining verification step. Until then, treat
  AD5X timelapse as **protocol-documented, not verified**.
- **Other Flashforge models** (5M, 5M Pro, Creator 5 series) have no
  stock-firmware Print Handy profile on this path today, so nothing here
  claims timelapse, or any other support, for them. The 5M Pro's
  built-in camera is documented by the same source, but there is no
  stock 5M Pro print path for it to attach to yet.
