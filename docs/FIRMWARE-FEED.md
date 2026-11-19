# The firmware feed

Print Handy follows the releases of the firmware its printers run and
tells you when a new one appears. It does that from one small public
file, the firmware feed, that GitHub Actions rebuilds every six hours.
This page says what the feed is, what Print Handy does with it, and how
to add a firmware to it.

For what `/print-firmware` reports and applies per printer family, see
[Firmware updates](FIRMWARE-UPDATE.md). The feed adds awareness; it does
not change which firmware Print Handy can apply an update to.

## What it is

`firmware-feed.json` lives on this repository's `firmware-feed` branch:

`https://raw.githubusercontent.com/catduckgnaf/print-handy/firmware-feed/firmware-feed.json`

For each firmware it carries the newest release (version, date and a
link to the release notes), the newest final release when the firmware
is followed on a prerelease channel, the newest prerelease with its
track when one is newer than the newest final (see
[Prerelease subscriptions](#prerelease-subscriptions)), the printer
models the firmware runs on, the version Print Handy's code was last
audited against, and one sentence on how that firmware is updated.

Three pieces make it:

| Piece | Job |
|---|---|
| `docs/firmware-feed.toml` | The list of firmware: where its releases are and which models it belongs to. The only file you edit. |
| `scripts/build_firmware_feed.py` | Asks GitHub for each firmware's releases or tags and writes the feed. Standard library only. |
| `.github/workflows/firmware-feed.yml` | Runs the script every six hours, on demand, and whenever the list changes on main, then commits the feed to the `firmware-feed` branch when its content changed. |

The feed is public and the same for everyone. It holds nothing about any
printer or install.

## Why a feed and not a GitHub request per firmware

An install has no GitHub token, and GitHub allows an address without one
60 API requests an hour. ZMod's own Moonraker spends from that same
allowance, which is why a printer sometimes shows `?` as its latest
version. A roster with several firmware families would spend it faster.
The feed is one plain file read an hour for the whole install, served
from GitHub's file host, which that allowance does not cover. The
workflow does the API reading once, with the repository's own token, for
every install.

## What Print Handy does with it

- **Release notices in chat.** For every printer on your roster whose
  model the feed covers, Print Handy posts one notice in that printer's
  chat when a newer release appears: the firmware, the version, how it
  is updated and a link to the release notes. Same switch and same
  interval as the ZMod alerts (`[firmware] update_alerts`,
  `[firmware] update_check_hours`, see [Settings](SETTINGS.md)).
  A notice has no button. It says a release was published; it does not
  say your printer is out of date, because for most firmware Print Handy
  cannot read a trustworthy installed version. The first time Print
  Handy sees a firmware line it records the current release without a
  notice, so a new install is not told about a release that is months
  old. The notice reads nothing from the printer.
- **ZMod alerts.** A ZMod printer keeps its fuller alert, with the
  Update button, described in [Firmware updates](FIRMWARE-UPDATE.md).
  That alert now learns the newest ZMod release from the feed first and
  asks GitHub directly only when the feed cannot be read. A ZMod printer
  gets no second, plainer notice for ZMod.
- **The Web UI Firmware page** (`/firmware`). Each printer with its
  firmware, the newest release, the audited version and how it updates,
  then the whole feed. Both are split into three tables: **Firmware**
  (ZMod, Forge-X, OpenCreator, Klipper, Marlin), **Stock firmware** (the
  maker's own, such as Prusa Buddy, QIDI and stock Flashforge) and
  **Plugins** (HelixScreen, Moonraker, OctoPrint). Klipper and Marlin can
  be stock or modified: on a printer whose maker ships one of them as its
  own firmware (Marlin on a Sovol), that printer's section lists it under
  Stock firmware. A Moonraker printer has a "Check
  installed versions" link that reads that one printer's update state
  and shows the `/print-firmware` report. The page is read-only.

If the feed cannot be read, nothing breaks: no notice is sent, the
Firmware page says so and still lists your printers, and the ZMod alert
falls back to GitHub as it did before the feed existed. Print Handy
reads the feed again within the hour.

The read sends nothing about you: it is a request for a public file,
with no printer, model, version or account in it.

## Prerelease subscriptions

Most firmware is followed on its final releases. If you want to hear
about prerelease builds too, subscribe with
`[firmware] update_channel` (see [Settings](SETTINGS.md)). The value
names the least stable track you want: `stable` (the default, finals
only), `rc` (finals and release candidates), `beta` (adds betas) or
`alpha` (adds every other prerelease a firmware publishes). Each level
includes every more stable one.

A prerelease's track is read from its version: `2.0.0rc7` is an RC,
`8.0.3-beta` is a beta, `6.4.0-RC2` is an RC, and Marlin's `2.1.3-b3`
shape is a beta. A prerelease with no recognizable track word (a dev or
preview build, or one upstream flags as a prerelease with no track in
its name) counts as alpha tier, so only `alpha` subscribers hear about
it. A final release is always the stable track, including a ZMod build
suffix like `1.7.5-2`, which is a later build of a final, not a
prerelease.

The feed publishes each line's newest prerelease, with its track, when
it is newer than the line's newest final. With a subscription that
covers its track, Print Handy surfaces it in the same places as a
release: a chat notice for the line (labeled with the track, for
example `8.1.0-beta1 (beta)`), the Web UI Firmware page under the
newest release, and, for a Moonraker component whose remote version is
a prerelease on a subscribed track, an alert whose Update button runs
the same confirmed `/print-firmware update` path as a final release.
A stock firmware prerelease is guidance only, with the maker's update
instructions and no button, like every stock firmware release. One
notice or alert per prerelease version, remembered across restarts,
and the first sight of a line's prerelease is recorded silently, both
exactly as for final releases. Nothing is ever applied on its own.

Forge-X is the exception, and it is not a subscription case: Print
Handy supports Forge-X on its 1.4.2 beta line, so its feed channel is
`prerelease` and its notices already follow the beta, whatever
`update_channel` says. The setting neither adds nor removes Forge-X
notices.

Whether a subscription can ever surface anything depends on the
firmware: some projects publish prereleases and some never do. This
table was checked against each line's upstream releases and tags on
2026-11-17; "publishes" means at least one prerelease was observed
there, with an example.

| Feed line | Upstream | Publishes prereleases? |
|---|---|---|
| ZMod | ghzserg/zmod | Never observed: 45 releases, the only flagged one is tag `R` ("Native firmware", 2025-04-03), which carries no version |
| Forge-X | DrA1ex/ff5m | Yes, beta: `1.4.2-beta-2`, `1.4.2-beta-3`, `1.4.2-beta-4` (already followed on the prerelease channel) |
| OpenCreator | FlashForge-C5-Modding-Group/OpenCreator | Yes, flagged with no track token: `0.0.1`, `0.0.1.1` (alpha tier under this classification) |
| HelixScreen | prestonbrown/helixscreen | Yes, beta and RC: `v1.1.0-beta.18`, `v1.1.0-rc.2` |
| Klipper | Klipper3d/klipper (tags) | Never observed: 14 tags, none carries a stage token |
| Moonraker | Arksine/moonraker (tags) | Never observed: 25 tags, none carries a stage token |
| OctoPrint | OctoPrint/OctoPrint | Yes, RC: `2.0.0rc2` through `2.0.0rc7` (57 of the last 100 releases flagged) |
| Marlin | MarlinFirmware/Marlin | Yes, beta published as full releases: `2.1.3-b1`, `2.1.3-b2`, `2.1.3-b3`; the flagged prereleases are the old `1.1.0-RC` series |
| Prusa Buddy (MK4S, CORE One, XL, MINI+) | prusa3d/Prusa-Firmware-Buddy | Yes, beta and RC, selected per line by release title: `v8.0.3-beta` (CORE One), `v6.5.3-RC` (incl. MK4S), `v6.4.1-RC` (XL), `v6.4.0-RC2` (incl. MINI) |
| QIDI (Q1 Pro, X-Plus 3, X-Plus 4, X-Max 3, X-Smart 3) | QIDITECH model repositories | Never observed: no flagged prerelease and no stage-token tag in any of the five repositories |
| Stock Flashforge (AD5X, AD5M, AD5M Pro, Creator 5, Creator 5 Pro) | Parallel-7/FlashForge-Firmware-Updates (mirror of Flashforge's update server) | Never observed: 32 releases, none flagged, tags are model/version finals |

The weekly audit watch is not part of this: it stays stable-only, and
a prerelease never moves an audited baseline.

## Feed and audit are different things

- **The feed** says what upstream has published. It is for owners.
- **The audited version** (`docs/firmware-baselines.toml`, and
  `docs/ZMOD.md` for ZMod) says which upstream version Print Handy's
  code was last checked against. The weekly firmware watch compares
  upstream with it and opens a maintainer issue for each newer release.

The separate weekly upstream issue watch checks Forge-X and OpenCreator
for recent issues matching the editable keywords in
`docs/upstream-issue-watch.toml`. It opens one labeled, deduplicated
Print Handy issue per match, including issues a maintainer previously
closed. It is notify-only and never writes to the upstream trackers.

The feed shows both side by side. A release newer than the audited
version is one Print Handy has not been checked with yet, not one it is
known to fail on. A firmware can be in the feed without an audited
version (Forge-X, OpenCreator and HelixScreen are today).

## Adding a firmware

You need a firmware whose releases are on GitHub: as GitHub Releases, as
version tags, or as versioned files attached to one release. A firmware
published only on a vendor's download page or through a cloud service
cannot be followed this way (Bambu Lab, Creality's stock firmware and
Anycubic's are in that group); do not add a scraper. Stock Flashforge
firmware is the exception that proves the rule: Flashforge publishes it
on its own update server and pages, and
`Parallel-7/FlashForge-Firmware-Updates` mirrors each package from that
server as a GitHub release tagged per model (`adventurer-5x/3.1.8-1.1.1`),
which the feed reads with `tag_prefix`.

1. **Add one entry to `docs/firmware-feed.toml`.**

   ```toml
   [[firmware]]
   id = "forge-x"
   kind = "firmware"
   name = "Forge-X"
   repo = "DrA1ex/ff5m"
   feed = "releases"
   channel = "prerelease"
   models = ["flashforge_forgex.*"]
   update = "Update from Fluidd or Mainsail within your Forge-X line; a new line installs from a USB image. Print Handy reports Forge-X updates and does not apply them."
   ```

   | Field | What to write |
   |---|---|
   | `id` | A short key that never changes: lowercase letters, digits and hyphens. Notices are remembered by it. |
   | `kind` | Which group the Web UI Firmware page shows it in: `firmware` for community or open firmware (ZMod, Forge-X, OpenCreator, Klipper, Marlin), `stock` for the printer maker's own firmware (Prusa Buddy, QIDI), or `plugin` for an add-on that runs beside the firmware (HelixScreen, Moonraker, OctoPrint). Required; anything else fails the build. |
   | `name` | The name people use. If the firmware also has an audited baseline, use exactly the same name as in `docs/firmware-baselines.toml` so the audited version is published with it. |
   | `repo` | `owner/name` on GitHub. |
   | `feed` | `releases`, or `tags` for a repository that publishes no usable releases (Klipper, Moonraker), or `assets` for versioned files attached to one release (stock Flashforge). |
   | `release`, `asset` | Only with `feed = "assets"`: the release tag whose files are read, and the file-name prefix before the version. `AD5X-3.1.0-1.1.1-3.0.7-20260515.tgz` is asset `AD5X`, version 3.1.0; a file whose version does not follow the prefix directly (a recovery image such as `AD5X-factory-1.0.9.tgz`) is skipped. |
   | `stock_models` | Optional, `kind = "firmware"` only: the models on which this firmware is the maker's own (Marlin on `sovol_marlin.*`). Those printers list it under Stock firmware. |
   | `channel` | `stable` unless the line Print Handy supports is a prerelease. `prerelease` makes the newest release of any kind the latest. |
   | `models` | The printer model keys this firmware runs on, exactly as in the printer registry, or a whole vendor as `vendor.*`. Leave it empty for a component that is not tied to a model. |
   | `tag_prefix` | Only with `feed = "releases"`, for one repository that publishes every model: keep only tags starting with this (`adventurer-5x/`), and read the version right after it up to the first `-` (`adventurer-5x/3.1.8-1.1.1` is 3.1.8). |
   | `line` | Only for a repository that publishes several printer lines together: the printer names that pick this line's releases out by their title. |
   | `update` | One sentence on how an owner installs the update, including what Print Handy does not do. It is shown with every notice. |

2. **Check it locally.** This reads GitHub and writes the feed to a file
   you can look at; it changes nothing else.

   ```bash
   python3 scripts/build_firmware_feed.py --output /tmp/firmware-feed.json
   ```

   Look at the new entry's `latest`. If it is `null` and `stale` is
   true, the script could not read a version from the repository's tags:
   the version has to be at the end of the tag (`v1.2.3`,
   `Plus4_v1.7.1`, `1.4.2-beta-4`).

3. **Run the tests.** `tests/test_firmware_feed.py` checks that the list
   parses and that every model key in it is a real one.

4. **Open the pull request.** When it merges, the workflow rebuilds the
   feed, and installs pick the firmware up within the hour. No release
   of Print Handy is needed for a firmware that only needs notices and
   the Firmware page.

A firmware that also needs Print Handy to print to it (models, an
adapter, slicer templates) is a larger change. [Forge-X](FORGEX.md) and
[OpenCreator](OPENCREATOR.md) are the worked examples; the feed entry is
the last and smallest part of that work.

## If a source breaks

When one repository cannot be read, the script keeps that firmware's
last known release, marks it `stale`, and the run still succeeds, so one
unreachable repository never empties the feed. The Firmware page shows a
stale entry with a note. The run fails only when the list itself is
malformed, or when nothing could be read and there is no earlier feed to
keep.

The `firmware-feed` branch holds only the feed and a short README. Do
not edit it by hand: the next run overwrites it.
