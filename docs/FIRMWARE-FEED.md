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
is followed on a prerelease channel, the printer models the firmware
runs on, the version Print Handy's code was last audited against, and
one sentence on how that firmware is updated.

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
  then the whole feed as a table. A Moonraker printer has a "Check
  installed versions" link that reads that one printer's update state
  and shows the `/print-firmware` report. The page is read-only.

If the feed cannot be read, nothing breaks: no notice is sent, the
Firmware page says so and still lists your printers, and the ZMod alert
falls back to GitHub as it did before the feed existed. Print Handy
reads the feed again within the hour.

The read sends nothing about you: it is a request for a public file,
with no printer, model, version or account in it.

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

You need a firmware whose releases are on GitHub, as GitHub Releases or
as version tags. A firmware published only on a vendor's download page
or through a cloud service cannot be followed this way (Bambu Lab, stock
Flashforge, Creality's stock firmware and Anycubic's are in that group);
do not add a scraper.

1. **Add one entry to `docs/firmware-feed.toml`.**

   ```toml
   [[firmware]]
   id = "forge-x"
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
   | `name` | The name people use. If the firmware also has an audited baseline, use exactly the same name as in `docs/firmware-baselines.toml` so the audited version is published with it. |
   | `repo` | `owner/name` on GitHub. |
   | `feed` | `releases`, or `tags` for a repository that publishes no usable releases (Klipper, Moonraker). |
   | `channel` | `stable` unless the line Print Handy supports is a prerelease. `prerelease` makes the newest release of any kind the latest. |
   | `models` | The printer model keys this firmware runs on, exactly as in the printer registry, or a whole vendor as `vendor.*`. Leave it empty for a component that is not tied to a model. |
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
