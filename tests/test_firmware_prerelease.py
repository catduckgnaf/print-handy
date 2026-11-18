"""#1627: prerelease subscriptions for firmware updates, offline.

One setting, [firmware] update_channel, names the least stable track
an owner subscribes to (stable | rc | beta | alpha). These tests pin,
in order: the track classification of a version, the feed builder
publishing each line's newest prerelease with its track, the runtime
reader, the per-level filtering of feed notices and of Moonraker
component offers, the notice memory across restarts, the settings
surfaces (defaults, validation, environment, Web UI catalog), and the
Web UI Firmware page. No network, no printer.
"""
import importlib.util
from datetime import datetime, timezone
from pathlib import Path

import pytest

from print_handy import firmware_alerts as fa
from print_handy import firmware_feed, settings
from print_handy.bot import firmware_alert
from print_handy.webui import catalog, firmware_page

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "build_firmware_feed", ROOT / "scripts" / "build_firmware_feed.py")
build = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build)

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------- tracks

@pytest.mark.parametrize("version, track", [
    ("1.7.6", "stable"), ("2.0.0", "stable"), ("0.0.1.1", "stable"),
    ("1.7.5-2", "stable"),            # a numeric build suffix is a final build
    ("2.0.0rc7", "rc"), ("2.0.0rc2", "rc"), ("6.4.0-RC2", "rc"),
    ("v6.5.3-RC", "rc"), ("1.1.0-rc.2", "rc"), ("1.1.0-RC8", "rc"),
    ("8.0.3-beta", "beta"), ("v6.5.3-beta", "beta"), ("1.4.2-beta-4", "beta"),
    ("v1.1.0-beta.18", "beta"), ("2.1.3-b3", "beta"),
    ("1.0.0-alpha1", "alpha"), ("1.0.0-a2", "alpha"),
    ("1.0.0-dev.3", "alpha"), ("1.0.0-preview2", "alpha"),
    ("1.0.0-nightly", "alpha"), ("1.0.0-x1", "alpha"),
])
def test_release_track_classifies_versions(version, track):
    assert fa.release_track(version) == track
    assert fa.is_prerelease(version) is (track != "stable")


def test_prerelease_ordering_is_alpha_beta_rc_final():
    versions = ["1.4.2-alpha1", "1.4.2-beta.2", "1.4.2-rc1", "1.4.2"]
    keys = [fa.version_key(v) for v in versions]
    assert keys == sorted(keys) and len(set(keys)) == 4


@pytest.mark.parametrize("channel, accepted", [
    ("stable", {"stable"}),
    ("rc", {"stable", "rc"}),
    ("beta", {"stable", "rc", "beta"}),
    ("alpha", {"stable", "rc", "beta", "alpha"}),
])
def test_a_subscription_includes_every_more_stable_track(channel, accepted):
    for track in ("alpha", "beta", "rc", "stable"):
        assert fa.channel_accepts(channel, track) is (track in accepted)


def test_an_unknown_channel_reads_as_stable():
    assert fa.channel_accepts("nightly", "rc") is False
    assert fa.channel_accepts("nightly", "stable") is True
    assert fa.channel_accepts(None, "beta") is False


def test_the_track_words_match_the_settings_channels():
    assert set(fa.TRACKS) == set(settings.FIRMWARE_UPDATE_CHANNELS)


# ---------------------------------------------------------------- the builder

def _release(tag, *, pre=False, when="2026-11-01T00:00:00Z", title=""):
    return {"tag_name": tag, "prerelease": pre, "draft": False, "published_at": when,
            "html_url": f"https://github.com/o/r/releases/tag/{tag}", "name": title}


class FakeGitHub:
    def __init__(self, releases=None, broken=()):
        self.releases, self.broken = releases or {}, set(broken)

    def request(self, method, path, payload=None):
        repo = "/".join(path.split("/")[2:4])
        if repo in self.broken:
            raise build.watch.WatchError(f"GitHub API GET {path} failed: HTTP 500")
        return self.releases.get(repo)


def _source(**over):
    base = {"id": "fw", "kind": "firmware", "name": "FW", "repo": "o/r",
            "feed": "releases", "channel": "stable", "models": ["v.*"],
            "stock_models": [], "line": [], "release": None, "asset": None,
            "tag_prefix": None, "update": "How."}
    return {**base, **over}


def _entry(releases, **source_over):
    github = FakeGitHub(releases={"o/r": releases})
    feed = build.build_feed([_source(**source_over)], {}, github, now=NOW,
                            log=lambda _m: None)
    return feed["firmware"][0]


def test_the_builder_publishes_a_newer_prerelease_with_its_track():
    entry = _entry([_release("1.4.2-beta-4", pre=True), _release("1.4.1")])
    assert entry["latest"]["version"] == "1.4.1"
    assert entry["latest_stable"]["version"] == "1.4.1"
    pre = entry["latest_prerelease"]
    assert pre["version"] == "1.4.2-beta-4" and pre["track"] == "beta"
    assert pre["tag"] == "1.4.2-beta-4" and pre["url"].endswith("1.4.2-beta-4")


def test_the_builder_publishes_no_prerelease_at_or_below_the_final():
    entry = _entry([_release("1.4.1"), _release("1.4.0-rc.1", pre=True)])
    assert entry["latest_prerelease"] is None
    entry = _entry([_release("1.4.1")])
    assert entry["latest_prerelease"] is None


def test_a_flagged_prerelease_without_a_track_token_is_alpha_tier():
    entry = _entry([_release("0.0.2", pre=True), _release("0.0.1")])
    assert entry["latest_prerelease"]["version"] == "0.0.2"
    assert entry["latest_prerelease"]["track"] == "alpha"


def test_a_beta_spelled_in_a_full_release_publishes_as_beta():
    entry = _entry([_release("2.1.3-b3"), _release("2.1.2.10")])
    assert entry["latest"]["version"] == "2.1.2.10"
    assert entry["latest_prerelease"]["version"] == "2.1.3-b3"
    assert entry["latest_prerelease"]["track"] == "beta"


def test_a_glued_rc_publishes_as_rc():
    entry = _entry([_release("2.0.0rc7", pre=True), _release("1.11.8")])
    assert entry["latest_prerelease"]["version"] == "2.0.0rc7"
    assert entry["latest_prerelease"]["track"] == "rc"


def test_the_prerelease_channel_latest_is_unchanged_and_still_publishes():
    entry = _entry([_release("1.4.2-beta-10", pre=True),
                    _release("1.4.2-beta-4", pre=True), _release("1.4.1")],
                   channel="prerelease")
    assert entry["latest"]["version"] == "1.4.2-beta-10"
    assert entry["latest_stable"]["version"] == "1.4.1"
    assert entry["latest_prerelease"]["version"] == "1.4.2-beta-10"


def test_a_stale_entry_keeps_its_published_prerelease():
    sources = [_source(), _source(id="b", name="B", repo="o/b")]
    good = FakeGitHub(releases={"o/r": [_release("1.0.0")],
                                "o/b": [_release("2.1.0-rc.1", pre=True),
                                        _release("2.0.0")]})
    first = build.build_feed(sources, {}, good, now=NOW, log=lambda _m: None)
    bad = FakeGitHub(releases={"o/r": [_release("1.0.0")]}, broken={"o/b"})
    second = build.build_feed(sources, {}, bad, previous=first, now=NOW,
                              log=lambda _m: None)
    kept = second["firmware"][1]
    assert kept["stale"] is True
    assert kept["latest_prerelease"]["version"] == "2.1.0-rc.1"
    assert kept["latest_prerelease"]["track"] == "rc"


# ---------------------------------------------------------------- the reader

def _payload(**row_over):
    row = {"id": "octoprint", "name": "OctoPrint", "repo": "OctoPrint/OctoPrint",
           "kind": "plugin", "channel": "stable", "models": ["octoprint.*"],
           "stock_models": [], "update": "How.",
           "latest": {"version": "2.0.0", "tag": "2.0.0",
                      "published_at": "2026-11-16T00:00:00Z",
                      "url": "https://github.com/OctoPrint/OctoPrint/releases/tag/2.0.0"},
           "latest_stable": {"version": "2.0.0", "tag": "2.0.0",
                             "published_at": "2026-11-16T00:00:00Z",
                             "url": "https://github.com/OctoPrint/OctoPrint/releases/tag/2.0.0"},
           "audited": "1.11.8", "stale": False}
    row.update(row_over)
    return {"schema": 1, "generated_at": "2026-11-17T00:00:00Z",
            "source": "x", "firmware": [row]}


def test_the_reader_reads_the_prerelease_and_its_track():
    feed = firmware_feed.parse(_payload(latest_prerelease={
        "version": "2.1.0-rc.1", "tag": "2.1.0rc1", "track": "rc",
        "published_at": "2026-11-17T00:00:00Z",
        "url": "https://github.com/OctoPrint/OctoPrint/releases/tag/2.1.0rc1"}))
    [row] = feed.firmware
    assert row.latest_prerelease.version == "2.1.0-rc.1"
    assert row.latest_prerelease.track == "rc"


def test_a_feed_without_the_prerelease_field_still_parses():
    feed = firmware_feed.parse(_payload())
    [row] = feed.firmware
    assert row.latest_prerelease is None
    feed = firmware_feed.parse(_payload(latest_prerelease={
        "version": "2.1.0-x", "tag": "t", "track": "nightly",
        "published_at": None, "url": ""}))
    assert feed.firmware[0].latest_prerelease.track == ""


# ---------------------------------------------------------------- feed notices

def _row(pre_version=None, *, track="", channel="stable", latest="2.0.0",
         firmware_id="octoprint", name="OctoPrint",
         latest_when="2026-11-16T00:00:00Z", pre_when="2026-11-17T00:00:00Z"):
    return firmware_feed.Firmware(
        id=firmware_id, name=name, repo="o/r", kind="plugin", channel=channel,
        models=("octoprint.*",), update="Update it from its own page.",
        latest=firmware_feed.Release(latest, published_at=latest_when),
        latest_stable=firmware_feed.Release(latest, published_at=latest_when),
        latest_prerelease=(firmware_feed.Release(
            pre_version, published_at=pre_when, track=track)
            if pre_version else None))


def _notices(rows, state, **kw):
    notices, seen = fa.feed_notices(rows, state, **kw)
    return notices, {fa.FEED_SEEN: seen}


def test_a_prerelease_notice_is_recorded_silently_at_first_sight():
    notices, state = _notices([_row("2.1.0-rc.1", track="rc")], {}, channel="rc")
    assert notices == []
    assert state[fa.FEED_SEEN]["octoprint:prerelease"]["version"] == "2.1.0-rc.1"
    # The same version again (a restart re-reading the state): still quiet.
    notices, _ = _notices([_row("2.1.0-rc.1", track="rc")], state, channel="rc")
    assert notices == []


def test_a_newer_prerelease_notices_once_with_its_track_named():
    _, state = _notices([_row("2.1.0-rc.1", track="rc")], {}, channel="beta")
    notices, state = _notices([_row("2.1.0-rc.2", track="rc",
                                   pre_when="2026-11-18T00:00:00Z")],
                              state, channel="beta")
    [notice] = notices
    assert notice.version == "2.1.0-rc.2" and notice.track == "rc"
    assert notice.seen_key == "octoprint:prerelease"
    assert "2.1.0-rc.2 (rc)" in notice.text("Shop printer")
    # Guidance only: a feed notice is the button-free notice type.
    assert not hasattr(notice, "applicable")
    notices, _ = _notices([_row("2.1.0-rc.2", track="rc")], state, channel="beta")
    assert notices == []


@pytest.mark.parametrize("channel, beta_seen, alpha_seen", [
    ("stable", False, False), ("rc", False, False),
    ("beta", True, False), ("alpha", True, True),
])
def test_notice_filtering_per_subscription_level(channel, beta_seen, alpha_seen):
    for version, track, wanted in (("2.1.0-beta.1", "beta", beta_seen),
                                   ("2.1.0-dev.1", "alpha", alpha_seen)):
        _, state = _notices([_row(version, track=track)], {}, channel=channel)
        notices, _ = _notices([_row(version + ".1", track=track,
                                    pre_when="2026-11-18T00:00:00Z")], state,
                              channel=channel)
        assert bool(notices) is wanted, (channel, track)


def test_an_rc_subscriber_sees_an_rc_but_not_a_beta():
    _, state = _notices([_row("2.1.0-beta.1", track="beta")], {}, channel="rc")
    notices, _ = _notices([_row("2.1.0-beta.2", track="beta",
                                pre_when="2026-11-18T00:00:00Z")], state,
                          channel="rc")
    assert notices == []
    _, state = _notices([_row("2.1.0-rc.1", track="rc")], {}, channel="rc")
    notices, _ = _notices([_row("2.1.0-rc.2", track="rc",
                                pre_when="2026-11-18T00:00:00Z")], state,
                          channel="rc")
    assert len(notices) == 1


def test_a_prerelease_at_or_below_the_stable_latest_never_notices():
    _, state = _notices([_row("2.0.0-rc.9", track="rc")], {}, channel="alpha")
    notices, _ = _notices([_row("2.0.0-rc.10", track="rc")], state, channel="alpha")
    assert notices == []


def test_forgex_prerelease_channel_notices_are_unchanged_by_the_setting():
    row = _row("1.4.2-beta-11", track="beta", channel="prerelease",
               latest="1.4.2-beta-10", firmware_id="forge-x", name="Forge-X")
    for channel in ("stable", "alpha"):
        notices, state = _notices([row], {}, channel=channel)
        assert notices == []  # first sight of the beta line: silent, as today
        newer = _row("1.4.2-beta-11", track="beta", channel="prerelease",
                     latest="1.4.2-beta-11", firmware_id="forge-x", name="Forge-X",
                     latest_when="2026-11-20T00:00:00Z")
        notices, _ = _notices([newer], state, channel=channel)
        [notice] = notices
        assert notice.version == "1.4.2-beta-11" and notice.track == ""


# ---------------------------------------------------------------- component offers

def test_a_helixscreen_prerelease_remote_is_gated_by_subscription():
    obs = fa.Observation(helixscreen_current="1.0.0",
                         helixscreen_remote="1.1.0-rc.2", helixscreen_valid=True)
    assert fa.offers(obs) == []
    assert fa.offers(obs, channel="stable") == []
    [offer] = fa.offers(obs, channel="rc")
    assert offer.track == "rc" and offer.applicable
    assert offer.key == "helixscreen:1.1.0-rc.2"
    assert "1.1.0-rc.2 (rc prerelease)" in offer.text("Shop printer")
    # Beta includes rc; a beta remote needs beta or alpha.
    assert fa.offers(obs, channel="beta") != []
    obs_beta = fa.Observation(helixscreen_current="1.0.0",
                              helixscreen_remote="1.1.0-beta.3",
                              helixscreen_valid=True)
    assert fa.offers(obs_beta, channel="rc") == []
    assert fa.offers(obs_beta, channel="beta") != []
    assert fa.offers(obs_beta, channel="alpha") != []
    obs_dev = fa.Observation(helixscreen_current="1.0.0",
                             helixscreen_remote="1.1.0-dev.1",
                             helixscreen_valid=True)
    assert fa.offers(obs_dev, channel="beta") == []
    assert fa.offers(obs_dev, channel="alpha") != []


def test_a_zmod_prerelease_remote_is_offered_only_when_subscribed():
    obs = fa.Observation(current="1.7.5-2", remote="1.7.6-rc1", valid=True,
                         base="1.7.5")
    assert fa.offers(obs) == []
    [offer] = fa.offers(obs, channel="rc")
    assert offer.component == "zmod" and offer.track == "rc"
    # Keyed by the full prerelease version, not by the release it
    # precedes: the final 1.7.6 keeps its own alert afterwards.
    assert offer.key == "zmod:1.7.6-rc1"
    final = fa.Observation(current="1.7.5-2", remote="1.7.6", valid=True,
                           base="1.7.5")
    [final_offer] = fa.offers(final)
    assert final_offer.key == "zmod:1.7.6" and final_offer.track == ""
    state = fa.record_announced({}, offer, 100.0)
    assert fa.to_announce([final_offer], state) == [final_offer]


def test_a_prerelease_of_the_installed_release_is_never_offered():
    obs = fa.Observation(current="1.7.6", remote="1.7.6-rc1", valid=True,
                         base="1.7.6")
    assert fa.offers(obs, channel="alpha") == []
    obs = fa.Observation(helixscreen_current="1.1.0",
                         helixscreen_remote="1.1.0-rc.2", helixscreen_valid=True)
    assert fa.offers(obs, channel="alpha") == []


def test_a_final_remote_is_offered_at_every_subscription():
    obs = fa.Observation(current="1.7.5-2", remote="1.7.6-17", valid=True,
                         base="1.7.5")
    for channel in ("stable", "rc", "beta", "alpha"):
        [offer] = fa.offers(obs, channel=channel)
        assert offer.track == "" and offer.key == "zmod:1.7.6"


# ---------------------------------------------------------------- the setting

def _load(environ=None, local=None):
    return settings.load(local=local if local is not None else False,
                         environ=environ or {})


def test_update_channel_defaults_to_stable_and_validates():
    assert _load()["firmware"]["update_channel"] == "stable"
    data = {"firmware": {"update_channel": " Beta "}}
    settings._validate_firmware(data)
    assert data["firmware"]["update_channel"] == "beta"
    for bad in ("nightly", "", 3, True, ["rc"]):
        with pytest.raises(settings.SettingsError, match="update_channel"):
            settings._validate_firmware({"firmware": {"update_channel": bad}})


def test_update_channel_comes_from_the_environment():
    data = _load(environ={"PRINT_HANDY__FIRMWARE__UPDATE_CHANNEL": "rc"})
    assert data["firmware"]["update_channel"] == "rc"
    with pytest.raises(settings.SettingsError, match="update_channel"):
        _load(environ={"PRINT_HANDY__FIRMWARE__UPDATE_CHANNEL": "nightly"})


def test_the_bot_reads_the_channel_with_a_stable_fallback():
    assert firmware_alert.update_channel({}) == "stable"
    assert firmware_alert.update_channel(
        {"firmware": {"update_channel": "beta"}}) == "beta"
    assert firmware_alert.update_channel(
        {"firmware": {"update_channel": "bogus"}}) == "stable"


def test_the_catalog_carries_the_channel_as_a_picker():
    [field] = [f for f in catalog.CATALOG
               if f.section == "firmware" and f.key == "update_channel"]
    assert field.kind == "picker"
    assert tuple(field.choices) == settings.FIRMWARE_UPDATE_CHANNELS
    assert catalog.GROUP_OF["firmware.update_channel"] == "notifications"


# ---------------------------------------------------------------- the Web UI page

def test_the_firmware_page_shows_a_subscribed_prerelease(monkeypatch):
    row = _row("8.0.3-beta", track="beta", latest="8.0.2",
               firmware_id="prusa-buddy-coreone", name="Prusa Buddy (CORE One)")
    feed = firmware_feed.Feed("2026-11-17T00:00:00Z", (row,))
    data = {"printers": [{"id": "coreone", "model": "prusa.coreone",
                          "name": "CORE One"}],
            "firmware": {"update_channel": "beta"}}
    monkeypatch.setattr(firmware_page.settings, "printers",
                        lambda d: d["printers"])
    page = firmware_page.render_firmware_page(
        data, feed=feed, transport_for=lambda pid: None)
    assert "Prerelease:" in page and "8.0.3-beta" in page and "(beta)" in page
    data["firmware"]["update_channel"] = "stable"
    page = firmware_page.render_firmware_page(
        data, feed=feed, transport_for=lambda pid: None)
    assert "8.0.3-beta" not in page
