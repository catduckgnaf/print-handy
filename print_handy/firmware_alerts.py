"""Chat alerts when a ZMod update is available ([firmware] update_alerts).

The owner's ask: be told in chat when a printer has an update, and opt
into it with one press. This module is the read-and-decide half; the bot
delivers (print_handy/bot/firmware_alert.py). Nothing here writes to a
printer: the only printer requests are GETs, and the Update button runs
the existing confirmed apply (firmware_update.confirmed_update: prepare,
the same confirmation, both idle checks, the verified re-read).

Scope (2.3.0): ZMod printers only (AD5X, AD5M, AD5M Pro, Creator 5,
Creator 5 Pro), the ``zmod`` component only. Other components and other
families are #1294. A source is one function that returns offers from an
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
from dataclasses import dataclass
from pathlib import Path

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

_BLOCK_TEXT = {
    BLOCK_INVALID: ("Moonraker marks this Z-Mod install as not valid (usually files "
                    "changed by another installer) and refuses to update it. Check "
                    f"{FLUIDD_HOW}."),
    BLOCK_UNREPORTED: ("the printer does not report this update yet (Moonraker shows "
                       "`?` as the latest version, often the printer's own GitHub "
                       f"limit). Update from {FLUIDD_HOW}."),
    BLOCK_USB: f"this release changes the ZMod base, so it installs from USB: {USB_HOW}.",
    BLOCK_PAIRING: ("the printer's ZMod base and component versions do not pair, so "
                    f"the base updates from USB first: {USB_HOW}."),
}


# ------------------------------------------------------------------ versions

_STAGE_RANK = {"dev": 0, "alpha": 1, "beta": 2, "rc": 3}
_FINAL_RANK = 4


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
    match = re.match(r"([a-zA-Z]+)\.?(\d*)", suffix)
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
    """What one check read from a ZMod printer and from ZMod's releases."""

    current: str | None = None         # installed zmod version
    remote: str | None = None          # Moonraker's remote_version, None for "?"
    valid: bool = True                 # Moonraker's is_valid (and not corrupt)
    base: str | None = None            # the USB-installed ZMod base
    latest_release: str | None = None  # newest final GitHub release, None if unread


def zmod_base_from_system_info(payload):
    """The USB-installed ZMod base from /machine/system_info, or None.

    ZMod's start.sh writes the distribution name as ``"zmod <usb> ->
    <current>"``; the first version is the base. A plain ``"zmod 1.7.5"``
    is its own base; otherwise the distribution version is used.
    """
    try:
        distribution = payload["result"]["system_info"]["distribution"]
    except (TypeError, KeyError):
        return None
    if not isinstance(distribution, dict):
        return None
    name = str(distribution.get("name") or "")
    if name.casefold().startswith("zmod"):
        found = re.findall(r"v?\d+(?:\.\d+)+(?:-[0-9A-Za-z.]+)?", name)
        if found:
            return found[0].lstrip("v")
    return fw.os_version_from_system_info(payload)


def _raw_component(payload, name):
    info = (payload.get("result") or {}).get("version_info") if isinstance(payload, dict) else None
    if not isinstance(info, dict):
        return {}
    wanted = re.sub(r"[^0-9a-z]", "", name.casefold())
    for key, raw in info.items():
        if re.sub(r"[^0-9a-z]", "", str(key).casefold()) == wanted and isinstance(raw, dict):
            return raw
    return {}


def observe(transport, latest_release=None) -> Observation:
    """Read one ZMod printer's zmod state through ``transport``: GETs only.

    The update status read is required (its failure raises
    FirmwareError); the system info read is best-effort. Moonraker is
    never asked to refresh here: a background check stays a plain read,
    and ZMod's Moonraker refreshes on its own schedule.
    """
    try:
        payload = transport("GET", fw.UPDATE_STATUS_PATH)
    except Exception as exc:
        raise fw.FirmwareError(f"the update-status request failed: {exc}") from exc
    component = fw.parse_update_status(payload).component(COMPONENT)
    raw = _raw_component(payload, COMPONENT)
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
                       latest_release=latest_release)


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


class ReleaseCache:
    """One GitHub read per RELEASE_CACHE_S for every printer; a failure is cached too."""

    def __init__(self, fetch=latest_release, clock=time.monotonic):
        self._fetch, self._clock = fetch, clock
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

    @property
    def applicable(self):
        return self.block is None

    def text(self, label, *, buttons=True):
        head = f"**ZMod update available for {label}:** {self.current} -> {self.available}."
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


# Sources: each turns an observation into offers. #1294 adds the others
# (klipper, moonraker, web clients, plugins, other families) here.
SOURCES = (zmod_offers,)


def offers(obs: Observation) -> list:
    return [offer for source in SOURCES for offer in source(obs)]


# ------------------------------------------------------------------ state

def state_path(state_dir) -> Path:
    return Path(state_dir) / STATE_NAME


def load(state_dir) -> dict:
    try:
        data = json.loads(state_path(state_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save(state_dir, data) -> None:
    path = state_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def to_announce(offer_list, state) -> list:
    """The offers not yet announced in this state (restart-safe dedupe).

    An offer is new when its release was never announced, or was
    announced without a button and can now be applied. A skipped
    release is never announced again.
    """
    announced = state.get("announced") if isinstance(state.get("announced"), dict) else {}
    skipped = state.get("skipped") if isinstance(state.get("skipped"), dict) else {}
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
