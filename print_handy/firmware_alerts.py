"""Chat alerts when a ZMod update is available ([firmware] update_alerts).

The owner's ask: be told in chat when a printer has an update, and opt
into it with one press. This module is the read-and-decide half; the bot
delivers (print_handy/bot/firmware_alert.py). Nothing here writes to a
printer: the only printer requests are GETs, and the Update button runs
the existing confirmed apply (firmware_update.confirmed_update: prepare,
the same confirmation, both idle checks, the verified re-read).

Scope: the ZMod source below came first (2.3.0, ZMod printers only);
#1280 section I adds the HelixScreen source (#1572) and a generic
component source for every other Moonraker Update Manager component
with a real available version, on every apply-eligible printer (ZMod,
vanilla Klipper — generic Klipper and the Sovol Klipper family — and
Forge-X's ``forge-x`` component when ``[firmware] forgex_apply`` is
on). A source is one function that returns offers from an
:class:`Observation`; :func:`offers` is where a later source plugs in.

Where the versions come from, and what decides "can it be applied":

- **Moonraker first.** ZMod's Moonraker lists a ``Z-Mod`` git_repo
  component (origin ``ghzserg/z_ad5x``, branch pinned to the base's
  major.minor, for example ``1.7``) with its installed ``version`` and
  ``remote_version`` (for example ``1.7.5-2`` and ``1.7.6-17``). That is
  the update Fluidd's Software Update installs, and the one the Update
  button can apply through Moonraker.
- **ZMod's GitHub releases second** (``ghzserg/zmod``, latest final
  release; unauthenticated, short timeout, fail soft). Used when
  Moonraker cannot report the remote (``?``, typically the printer's own
  unauthenticated GitHub limit), and to spot a release on a new base:
  Moonraker's remote follows the pinned branch and never shows one.
- **USB-only.** ZMod's ``zversion.sh`` (z_ad5x ``.shell/zversion.sh``)
  prints "Update Z-Mod from USB" when the first two numbers of the
  network-updated version (``/usr/data/zmod/zmod/version.txt``) differ
  from the USB-installed one (``/root/printer_data/version.txt``). Print
  Handy reads the same two facts: the base from Moonraker's
  ``/machine/system_info`` distribution (ZMod's ``start.sh`` writes it as
  ``"zmod <usb version> -> <current version>"``), the target from the
  alert. A target on another major.minor than the base is USB-only.
- **Not valid.** Moonraker refuses to update a git_repo it marks
  ``is_valid: false`` (a dirty checkout, for example), so such an alert
  says so and carries no button.

One alert per ZMod release: builds inside a release (the ``-N`` suffix)
do not alert on their own, and the button applies whatever Moonraker
offers at press time, shown exactly in the confirmation. A release first
announced without a button (unreported, not valid) is announced once
more when it later becomes applicable; a skipped release never again.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path

from . import firmware_feed
from . import firmware_update as fw

STATE_NAME = "firmware_update_alerts.json"
ZMOD_RELEASES_URL = "https://api.github.com/repos/ghzserg/zmod/releases?per_page=10"
GITHUB_TIMEOUT_S = 10.0
# Releases are shared by every ZMod printer on the install: one GitHub
# request per hour at most, whatever the roster size.
RELEASE_CACHE_S = 3600.0
COMPONENT = "zmod"
FLUIDD_HOW = "Fluidd or Mainsail: Settings, Software Update"
USB_HOW = ("insert a USB drive and run `ZFLASH` at the printer "
           "(`/print-firmware guide zmod` has the steps)")

BLOCK_INVALID = "invalid"
BLOCK_UNREPORTED = "unreported"
BLOCK_USB = "usb"
BLOCK_PAIRING = "pairing"
BLOCK_HELIXSCREEN_INVALID = "helixscreen_invalid"
BLOCK_COMPONENT_INVALID = "component_invalid"

_BLOCK_TEXT = {
    BLOCK_INVALID: ("Moonraker marks this Z-Mod install as not valid (usually files "
                    "changed by another installer, such as HelixScreen's edit to ZMod's "
                    "`moonraker.conf`) and refuses to update it. `/print-firmware` "
                    "shows what changed and how to fix it."),
    BLOCK_UNREPORTED: ("the printer does not report this update yet (Moonraker shows "
                       "`?` as the latest version, often the printer's own GitHub "
                       f"limit). Update from {FLUIDD_HOW}."),
    BLOCK_USB: f"this release changes the ZMod base, so it installs from USB: {USB_HOW}.",
    BLOCK_PAIRING: ("the printer's ZMod base and component versions do not pair, so "
                    f"the base updates from USB first: {USB_HOW}."),
    BLOCK_HELIXSCREEN_INVALID: ("Moonraker marks this HelixScreen component as not valid "
                               "and refuses to update it. Fix it, then update from "
                               "Fluidd or Mainsail: Settings, Software Update."),
    BLOCK_COMPONENT_INVALID: ("Moonraker marks this component as not valid "
                              "and refuses to update it. `/print-firmware` shows what "
                              "Moonraker reports; fix the component, then update from "
                              "Fluidd or Mainsail: Settings, Software Update."),
}


# ------------------------------------------------------------------ versions

_STAGE_RANK = {"dev": 0, "alpha": 1, "beta": 2, "rc": 3}
_FINAL_RANK = 4

# ------------------------------------------------------- prerelease tracks

# The tracks an owner can subscribe to with [firmware] update_channel
# (#1627), least stable first. The setting names the least stable track
# the owner wants to hear about; each level includes every more stable
# one. The tuple mirrors settings.FIRMWARE_UPDATE_CHANNELS (settings is
# validated without this module; tests pin the two together).
TRACKS = ("alpha", "beta", "rc", "stable")
# Stability rank per track: a subscription accepts a release whose track
# is at least as stable as the subscribed track.
_TRACK_RANK = {"alpha": 0, "beta": 1, "rc": 2, "stable": 3}
# Stage words as they appear in versions and tags, mapped to their
# track. One-letter stages are Marlin's and the feed builder's shape
# ("2.1.3-b3" is a beta). Anything else lettered (dev, preview, nightly,
# a bare suffix) is a prerelease with no recognizable track and counts
# as alpha tier, so only alpha subscribers hear about it.
_STAGE_TRACK = {"alpha": "alpha", "a": "alpha", "beta": "beta", "b": "beta",
                "rc": "rc"}


def release_track(version) -> str:
    """The track of one version: "stable", "rc", "beta" or "alpha".

    A final release is "stable", including a numeric build suffix
    (``1.7.5-2`` is a later build of the 1.7.5 final, not a prerelease).
    A lettered suffix is a prerelease: its stage word names the track
    (``-rc1``, glued ``2.0.0rc7``, ``6.4.0-RC2``, ``-beta.2``), and a
    prerelease whose stage is not one of those words is "alpha" tier.
    The ordering is the one version_key already uses: alpha < beta <
    rc < final.
    """
    value = str(version or "").strip().lstrip("vV")
    main, _, suffix = value.partition("-")
    if not suffix:
        glued = re.match(r"^([\d.]*\d)([a-zA-Z].*)$", main)
        if glued:
            main, suffix = glued.group(1), glued.group(2)
    if not suffix or suffix.isdigit():
        return "stable"
    match = re.match(r"([a-zA-Z]+)", suffix)
    stage = match.group(1).lower() if match else ""
    return _STAGE_TRACK.get(stage, "alpha")


def is_prerelease(version) -> bool:
    """Whether a version is a prerelease on any track."""
    return release_track(version) != "stable"


def channel_accepts(channel, track) -> bool:
    """Whether an update_channel subscription includes one track.

    An unknown channel value reads as "stable" (the default), so a
    hand-edited configuration fails toward fewer notices, never more.
    """
    wanted = _TRACK_RANK.get(str(channel or "stable").strip().lower(), _TRACK_RANK["stable"])
    seen = _TRACK_RANK.get(str(track or "stable").strip().lower(), _TRACK_RANK["alpha"])
    return seen >= wanted


def version_key(text) -> tuple:
    """Ordering key for ZMod versions: ``1.7.5 < 1.7.5-2 < 1.7.6``.

    The same ordering as ``firmware_version_key`` in
    scripts/check_firmware_releases.py (the CI release watch; that script
    runs without the package installed, so the rule is mirrored here and
    tests/test_firmware_alerts.py pins the two together). A numeric
    ``-N`` suffix is a later build of the same release; a lettered one
    (``-rc1``) is a prerelease below it.
    """
    value = str(text or "").strip().lstrip("v")
    main, _, suffix = value.partition("-")
    numbers = []
    for part in main.split("."):
        match = re.match(r"\d+", part)
        numbers.append(int(match.group()) if match else 0)
    while len(numbers) < 4:
        numbers.append(0)
    if not suffix:
        return (*numbers, 1, _FINAL_RANK, 0)
    if suffix.isdigit():
        return (*numbers, 1, _FINAL_RANK, int(suffix))
    # "beta.2", "rc1" and Forge-X's "beta-4" all carry a stage serial:
    # 1.4.2-beta-4 < 1.4.2-beta-10 (#1506).
    match = re.match(r"([a-zA-Z]+)[.-]?(\d*)", suffix)
    stage = match.group(1).lower() if match else ""
    serial = int(match.group(2)) if match and match.group(2) else 0
    return (*numbers, 0, _STAGE_RANK.get(stage, 0), serial)


def release_of(version) -> str:
    """The release a version belongs to: ``1.7.6-17`` -> ``1.7.6``."""
    return str(version or "").strip().lstrip("v").partition("-")[0]


def _same_base(version, base) -> bool:
    """Whether two versions share major.minor (unknown counts as the same)."""
    a, b = fw.version_parts(version), fw.version_parts(base)
    return len(a) < 2 or len(b) < 2 or a[:2] == b[:2]


# ------------------------------------------------------------------ reading

@dataclass(frozen=True)
class Observation:
    """What one check read from a printer and from ZMod's releases."""

    current: str | None = None         # installed zmod version
    remote: str | None = None          # Moonraker's remote_version, None for "?"
    valid: bool = True                 # Moonraker's is_valid (and not corrupt)
    base: str | None = None            # the USB-installed ZMod base
    latest_release: str | None = None  # newest final GitHub release, None if unread
    helixscreen_current: str | None = None
    helixscreen_remote: str | None = None
    helixscreen_valid: bool = True
    # Every component the printer's Update Manager listed, as parsed by
    # firmware_update.parse_update_status (the generic source's input).
    components: tuple = ()
    # The registry vendor of the printer read ("" when the caller does
    # not say): the generic source's per-vendor exclusions read it.
    vendor: str = ""
    custom_stack: str | None = None


def zmod_base_from_system_info(payload):
    """The USB-installed ZMod base (firmware_update.zmod_base_from_system_info)."""
    return fw.zmod_base_from_system_info(payload)


def _raw_component(payload, name):
    info = (payload.get("result") or {}).get("version_info") if isinstance(payload, dict) else None
    if not isinstance(info, dict):
        return {}
    wanted = re.sub(r"[^0-9a-z]", "", name.casefold())
    for key, raw in info.items():
        if re.sub(r"[^0-9a-z]", "", str(key).casefold()) == wanted and isinstance(raw, dict):
            return raw
    return {}


def observe(transport, latest_release=None, *, vendor="", entry=None) -> Observation:
    """Read one printer's update state through ``transport``: GETs only.

    The update status read is required (its failure raises
    FirmwareError); the system info read is best-effort. Moonraker is
    never asked to refresh here: a background check stays a plain read,
    and ZMod's Moonraker refreshes on its own schedule. ``vendor`` is
    the printer's registry vendor, carried for the generic source's
    per-vendor exclusions; the reads themselves are the same for every
    Moonraker printer.
    """
    try:
        payload = transport("GET", fw.UPDATE_STATUS_PATH)
    except Exception as exc:
        raise fw.FirmwareError(f"the update-status request failed: {exc}") from exc
    status = fw.parse_update_status(payload)
    component = status.component(COMPONENT)
    raw = _raw_component(payload, COMPONENT)
    helixscreen_component = status.component("helixscreen")
    helixscreen_raw = _raw_component(payload, "helixscreen")
    helixscreen_valid = (helixscreen_raw.get("is_valid", True) is not False
                         and not helixscreen_raw.get("corrupt"))
    try:
        system_info = transport("GET", fw.SYSTEM_INFO_PATH)
    except Exception:
        system_info = None
    current = component.current if component is not None else None
    if not current and system_info is not None:
        current = fw.zmod_version_from_system_info(system_info)
    valid = raw.get("is_valid", True) is not False and not raw.get("corrupt")
    return Observation(current=current,
                       remote=component.remote if component is not None else None,
                       valid=valid,
                       base=zmod_base_from_system_info(system_info) if system_info else None,
                       latest_release=latest_release,
                       helixscreen_current=(helixscreen_component.current
                                            if helixscreen_component is not None else None),
                       helixscreen_remote=(helixscreen_component.remote
                                           if helixscreen_component is not None else None),
                       helixscreen_valid=helixscreen_valid,
                       components=status.components,
                       vendor=vendor,
                       custom_stack=fw.flashforge_custom_firmware(entry, status)
                       or ("ZMod" if current else None))


def latest_release(opener=None, timeout=GITHUB_TIMEOUT_S):
    """The newest final ZMod release tag from GitHub, or None on any failure.

    Unauthenticated (no token, no CI dependency), short timeout, fail
    soft: a network or parse failure returns None, never raises.
    Drafts and prereleases never count.
    """
    opener = opener or urllib.request.urlopen
    request = urllib.request.Request(ZMOD_RELEASES_URL, headers={
        "Accept": "application/vnd.github+json", "User-Agent": "print-handy"})
    try:
        with opener(request, timeout=timeout) as response:
            releases = json.loads(response.read() or b"[]")
    except Exception:
        return None
    if not isinstance(releases, list):
        return None
    finals = [str(r.get("tag_name") or "").strip().lstrip("v") for r in releases
              if isinstance(r, dict) and not r.get("draft") and not r.get("prerelease")]
    finals = [tag for tag in finals if fw.version_parts(tag)]
    return max(finals, key=version_key, default=None)


def newest_zmod_release(feed=None, fallback=latest_release):
    """The newest final ZMod release: the firmware feed first, GitHub second.

    The feed (firmware_feed) is one plain file read shared by every
    install, so it costs none of the install's 60 unauthenticated GitHub
    API requests an hour. When the feed is unreachable or carries no
    ZMod entry, the direct GitHub read is the fallback, as before the
    feed existed. Either way a failure is None, never an exception.
    """
    feed = firmware_feed.current() if feed is None else feed
    row = feed.get(COMPONENT) if feed is not None else None
    if row is not None and row.latest_stable is not None \
            and fw.version_parts(row.latest_stable.version):
        return row.latest_stable.version.lstrip("v")
    return fallback()


class ReleaseCache:
    """One release read per RELEASE_CACHE_S for every printer; a failure is cached too."""

    def __init__(self, fetch=None, clock=time.monotonic):
        self._fetch, self._clock = fetch or newest_zmod_release, clock
        self._at, self._value = None, None

    def get(self):
        now = self._clock()
        if self._at is None or now - self._at >= RELEASE_CACHE_S:
            self._value, self._at = self._fetch(), now
        return self._value


# ------------------------------------------------------------------ deciding

@dataclass(frozen=True)
class Offer:
    """One update to announce: what, from which version to which, and how."""

    component: str           # the token the confirmed apply resolves ("zmod")
    current: str
    available: str
    key: str                 # one alert per key: "zmod:<release>"
    block: str | None = None  # None: the Update button applies it from chat
    display_name: str = "ZMod"
    track: str = ""          # prerelease track ("beta"), "" for a final release

    @property
    def applicable(self):
        return self.block is None

    def text(self, label, *, buttons=True):
        shown = (f"{self.available} ({self.track} prerelease)" if self.track
                 else self.available)
        head = f"**{self.display_name} update available for {label}:** {self.current} -> {shown}."
        if self.block is not None:
            return f"{head}\nNot from chat: {_BLOCK_TEXT[self.block]}"
        if buttons:
            return (f"{head}\nPress **Update** to install it. Print Handy asks once more, "
                    "checks the printer is idle and verifies the result.")
        return (f"{head}\nTo install it, send `/print-firmware update {self.component}`. "
                "It asks first and checks the printer is idle.")


def zmod_offers(obs: Observation) -> list:
    """The ZMod offers one observation supports (pure: no I/O)."""
    if not obs.current:
        return []
    have = version_key(release_of(obs.current))
    base = obs.base or obs.current
    out = []
    if obs.remote and version_key(release_of(obs.remote)) > have:
        block = None
        if not obs.valid:
            block = BLOCK_INVALID
        elif not _same_base(obs.remote, base):
            block = BLOCK_USB
        elif obs.base and fw.pairing_state(obs.base, obs.current) == "mismatch":
            block = BLOCK_PAIRING
        out.append(Offer(COMPONENT, obs.current, obs.remote,
                         f"{COMPONENT}:{release_of(obs.remote)}", block))
    release = obs.latest_release
    if release and version_key(release) > have \
            and all(o.key != f"{COMPONENT}:{release_of(release)}" for o in out):
        if not _same_base(release, base):
            out.append(Offer(COMPONENT, obs.current, release,
                             f"{COMPONENT}:{release_of(release)}", BLOCK_USB))
        elif not obs.remote:
            out.append(Offer(COMPONENT, obs.current, release,
                             f"{COMPONENT}:{release_of(release)}", BLOCK_UNREPORTED))
        # Otherwise Moonraker reports a remote on this base that has not
        # reached the release yet: its own refresh catches up, and the
        # alert then comes from Moonraker with the button.
    return out


def helixscreen_offers(obs: Observation) -> list:
    """HelixScreen offers reported by the same Moonraker status read."""
    if fw.custom_update_block("helixscreen", obs.custom_stack or
                              ("Forge-X" if obs.vendor == fw.FORGEX_VENDOR else None)):
        return []
    current, remote = obs.helixscreen_current, obs.helixscreen_remote
    if not current or not remote:
        return []
    if version_key(remote) <= version_key(current):
        return []
    block = None if obs.helixscreen_valid else BLOCK_HELIXSCREEN_INVALID
    available = remote.lstrip("v")
    return [Offer("helixscreen", current, remote,
                  f"helixscreen:{available}", block, "HelixScreen")]


_DISPLAY_NAMES = {"klipper": "Klipper", "moonraker": "Moonraker",
                  "fluidd": "Fluidd", "mainsail": "Mainsail",
                  "guppyscreen": "GuppyScreen", "forgex": "Forge-X"}


def component_offers(obs: Observation) -> list:
    """Offers for the Moonraker components the named sources do not own.

    #1280 section I: every other Update Manager component with a real
    available version — Klipper, Moonraker, the web clients (Fluidd,
    Mainsail), GuppyScreen where a printer lists one, any other
    component the printer's own Update Manager configuration names —
    on an apply-eligible printer (the alerter only checks those). The
    button runs the same confirmed apply as `/print-firmware update
    <component>`; the offer's component is the canonical token that
    path resolves against the live status.

    Never offered:

    - ``zmod`` and ``helixscreen`` (their own sources above), and the
      OpenCreator components (report-only by decision, #1490 — and an
      OpenCreator printer is never apply-eligible anyway);
    - the ``system`` component: it reports a package count, not a
      version, so there is no version to alert once per version (the
      `/print-firmware` report still shows its packages);
    - a component whose remote is unknown (``?``), is not a real
      version, or is not newer than the installed one — a prerelease
      of the installed release included;
    - on Forge-X, anything but ``forge-x`` itself, and ``forge-x``
      only within the installed version line (the apply gate's own
      rule: a new line is a USB re-flash, never an alert button).

    A component Moonraker marks invalid, and any component on a ZMod
    printer whose base and component versions do not pair, is
    announced without a button, with the reason — the same blocks the
    confirmed apply would refuse with.
    """
    if obs.vendor == fw.OPENCREATOR_VENDOR:
        return []
    pairing = (fw.pairing_state(obs.base, obs.current)
               if obs.current else "unknown")
    out, seen = [], set()
    stack = obs.custom_stack or ("ZMod" if obs.current else None)
    for comp in obs.components:
        canonical = fw.canonical_component(comp.name)
        if fw.custom_update_block(canonical, stack):
            continue
        if canonical in (COMPONENT, "helixscreen", "system") or canonical in seen:
            continue
        if canonical in fw.OPENCREATOR_COMPONENTS:
            continue
        if obs.vendor == fw.FORGEX_VENDOR or stack == "Forge-X":
            if canonical != fw.FORGEX_COMPONENT:
                continue
            have = fw.version_parts(comp.current)
            offered = fw.version_parts(comp.remote)
            if len(have) < 2 or len(offered) < 2 or have[:2] != offered[:2]:
                continue
        if not comp.current or not comp.remote \
                or not fw.version_parts(comp.remote):
            continue
        if version_key(comp.remote) <= version_key(comp.current):
            continue
        seen.add(canonical)
        block = None
        if comp.invalid:
            block = BLOCK_COMPONENT_INVALID
        elif pairing == "mismatch":
            block = BLOCK_PAIRING
        out.append(Offer(canonical, comp.current, comp.remote,
                         f"{canonical}:{release_of(comp.remote)}", block,
                         _DISPLAY_NAMES.get(canonical, comp.name)))
    return out


# Sources: each turns an observation into offers.
SOURCES = (zmod_offers, helixscreen_offers, component_offers)


def offers(obs: Observation, channel: str = "stable") -> list:
    """Every source's offers, with prerelease remotes gated by subscription.

    A source compares versions without judging prereleases: a remote on
    a newer release counts even when it is that release's beta. The
    gate lives here, in the one path every source flows through, so a
    later source inherits it: a prerelease offer is dropped unless the
    owner's [firmware] update_channel subscribes to its track (#1627),
    and a kept prerelease is labeled with its track and keyed by its
    full version, so it neither silences nor is silenced by the final
    release of the same numbers (the sources key finals by release).
    """
    out = []
    for source in SOURCES:
        for offer in source(obs):
            track = release_track(offer.available)
            if track == "stable":
                out.append(offer)
            elif channel_accepts(channel, track):
                out.append(replace(offer, key=f"{offer.component}:{offer.available}",
                                   track=track))
    return out


# ------------------------------------------------------------------ feed notices

FEED_SEEN = "feed_seen"


@dataclass(frozen=True)
class ReleaseNotice:
    """A new upstream release of the firmware a printer runs, from the feed.

    Information only: it names the release and how that firmware is
    updated. It carries no button and never claims the printer is out
    of date, because for these firmware lines Print Handy has no
    trustworthy installed version to compare (the feed says what was
    published, not what a printer runs).
    """

    firmware_id: str
    name: str
    version: str
    url: str = ""
    update: str = ""
    track: str = ""          # prerelease track ("beta"), "" for a final release

    @property
    def key(self):
        return f"feed:{self.firmware_id}:{self.version}"

    @property
    def seen_key(self):
        """The feed_seen record this notice is remembered under."""
        return f"{self.firmware_id}:prerelease" if self.track else self.firmware_id

    def text(self, label):
        shown = f"{self.version} ({self.track})" if self.track else self.version
        lines = [f"**New {self.name} release for {label}:** {shown}."]
        if self.update:
            lines.append(self.update)
        if self.url:
            lines.append(f"Release notes: <{self.url}>")
        return "\n".join(lines)


def _is_newer(release, seen) -> bool:
    """Whether a feed release is newer than the one last recorded for a printer."""
    if not isinstance(seen, dict) or release.version == seen.get("version"):
        return False
    before = seen.get("published_at")
    if release.published_at and isinstance(before, str) and before:
        # Same ISO shape on both sides: an older date is a withdrawn
        # release falling back to its predecessor, never news.
        return release.published_at > before
    return True


def feed_notices(rows, state, *, skip_ids=(), channel="stable"):
    """``(notices, feed_seen)`` for one printer's firmware lines.

    ``rows`` are the feed entries covering the printer's model. The
    first time a firmware line is seen its newest release is recorded
    silently: an install that has just been set up, or just gained the
    feed, is not told about a release that may be months old. After
    that, each newer release is one notice. ``skip_ids`` are lines
    another alert already owns (ZMod's own alert on a ZMod printer).

    A line's newest prerelease (the feed's ``latest_prerelease``) is a
    second, separate notice on the same terms (#1627): recorded
    silently at first sight under its own ``<id>:prerelease`` record,
    noticed once per version after that, and only when the owner's
    [firmware] update_channel subscribes to its track. Lines the feed
    itself follows on a prerelease channel (Forge-X) are not part of
    this: their ``latest`` already is the prerelease, and they keep
    notifying exactly as before at every subscription.
    """
    seen = dict(state.get(FEED_SEEN) or {}) if isinstance(state.get(FEED_SEEN), dict) else {}
    notices = []
    for row in rows:
        if row.id in skip_ids:
            continue
        release = row.latest
        if release is not None:
            before = seen.get(row.id)
            if before is None or _is_newer(release, before):
                if before is not None:
                    notices.append(ReleaseNotice(row.id, row.name, release.version,
                                                 release.url, row.update))
                seen[row.id] = {"version": release.version,
                                "published_at": release.published_at}
        pre = getattr(row, "latest_prerelease", None)
        if pre is None or row.channel == "prerelease":
            continue
        track = getattr(pre, "track", "") or release_track(pre.version)
        if not channel_accepts(channel, track):
            continue
        stable = row.latest_stable
        if stable is not None and version_key(pre.version) <= version_key(stable.version):
            continue
        key = f"{row.id}:prerelease"
        before = seen.get(key)
        if before is None or _is_newer(pre, before):
            if before is not None:
                notices.append(ReleaseNotice(row.id, row.name, pre.version,
                                             pre.url, row.update, track))
            seen[key] = {"version": pre.version, "published_at": pre.published_at}
    return notices, seen


# ------------------------------------------------------------------ state

# Each surface that delivers alerts keeps its own state file: the chat
# bot (Discord, Telegram or Matrix: one per install) the plain name, the
# Web UI (its own process, its own web chat) a suffixed one. Each process
# then writes only its own file, so the two never lose each other's
# writes, and each announces a release once on its own surface. A skip
# is the person's answer about the release, so every surface honors a
# skip pressed on any of them (skipped_everywhere).
WEBUI_SURFACE = "webui"


def surface_for(platform_name) -> str:
    """The state surface for a delivering platform ("" for the chat bot)."""
    return WEBUI_SURFACE if str(platform_name or "") == WEBUI_SURFACE else ""


def state_path(state_dir, surface="") -> Path:
    if surface:
        return Path(state_dir) / f"{Path(STATE_NAME).stem}.{surface}.json"
    return Path(state_dir) / STATE_NAME


def load(state_dir, surface="") -> dict:
    try:
        data = json.loads(state_path(state_dir, surface).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def skipped_everywhere(state_dir) -> dict:
    """Every release skipped on any surface: {key: when}."""
    out = {}
    for surface in ("", WEBUI_SURFACE):
        skipped = load(state_dir, surface).get("skipped")
        if isinstance(skipped, dict):
            out.update(skipped)
    return out


def save(state_dir, data, surface="") -> None:
    path = state_path(state_dir, surface)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def to_announce(offer_list, state, skipped=None) -> list:
    """The offers not yet announced in this state (restart-safe dedupe).

    An offer is new when its release was never announced, or was
    announced without a button and can now be applied. A skipped
    release is never announced again: skipped in this state, or in
    ``skipped`` (skipped_everywhere: a skip pressed on another surface).
    """
    announced = state.get("announced") if isinstance(state.get("announced"), dict) else {}
    skipped = {**(state.get("skipped") if isinstance(state.get("skipped"), dict) else {}),
               **(skipped or {})}
    fresh = []
    for offer in offer_list:
        if offer.key in skipped:
            continue
        row = announced.get(offer.key)
        if isinstance(row, dict) and (row.get("applicable") or not offer.applicable):
            continue
        fresh.append(offer)
    return fresh


def record_announced(state, offer, now) -> dict:
    announced = dict(state.get("announced") or {})
    announced[offer.key] = {"at": now, "available": offer.available,
                            "applicable": offer.applicable}
    # Keep the newest few: a release older than these never re-qualifies.
    newest = sorted(announced.items(), key=lambda kv: -float(kv[1].get("at") or 0))[:20]
    return {**state, "announced": dict(newest)}


def record_skipped(state, key, now) -> dict:
    return {**state, "skipped": {**(state.get("skipped") or {}), key: now}}
