"""#1280 section I: the firmware feed, end to end and offline.

The builder (scripts/build_firmware_feed.py) against a scripted GitHub,
the reader (print_handy/firmware_feed.py), the release notices
(firmware_alerts.feed_notices and the alerter's feed pass), the ZMod
alert's feed-first release read, and the Web UI Firmware page. No test
here touches the network or a printer.
"""
import asyncio
import importlib.util
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from print_handy import firmware_alerts as fa
from print_handy import firmware_feed, printers
from print_handy.bot import firmware_alert
from print_handy.webui import firmware_page, routes, shell

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "build_firmware_feed", ROOT / "scripts" / "build_firmware_feed.py")
build = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build)

NOW = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)


def _release(tag, *, pre=False, when="2026-11-01T00:00:00Z", title="", draft=False):
    return {"tag_name": tag, "prerelease": pre, "draft": draft, "published_at": when,
            "html_url": f"https://github.com/o/r/releases/tag/{tag}", "name": title}


class FakeGitHub:
    """Answers the two list endpoints the builder uses; unknown repos fail."""

    def __init__(self, releases=None, tags=None, broken=()):
        self.releases, self.tags, self.broken = releases or {}, tags or {}, set(broken)
        self.calls = []

    def request(self, method, path, payload=None):
        self.calls.append((method, path))
        assert method == "GET" and payload is None
        repo = "/".join(path.split("/")[2:4])
        if repo in self.broken:
            raise build.watch.WatchError(f"GitHub API GET {path} failed: HTTP 500")
        if "/releases" in path:
            return self.releases.get(repo)
        return self.tags.get(repo)


def _source(**over):
    base = {"id": "fw", "kind": "firmware", "name": "FW", "repo": "o/r", "feed": "releases",
            "channel": "stable", "models": ["v.*"], "stock_models": [], "line": [],
            "release": None, "asset": None, "tag_prefix": None, "update": "How."}
    return {**base, **over}


# ---------------------------------------------------------------- the builder

def test_shipped_sources_parse_and_name_only_registered_models():
    sources = build.parse_sources((ROOT / build.SOURCES_FILE).read_text(encoding="utf-8"))
    ids = [s["id"] for s in sources]
    assert {"zmod", "forge-x", "opencreator", "helixscreen", "klipper"} <= set(ids)
    vendors = {key.split(".")[0] for key in printers.MODELS}
    for source in sources:
        assert source["update"].endswith("."), source["id"]
        for pattern in source["models"]:
            vendor, _, model = pattern.partition(".")
            if model == "*":
                # flashforge_forgex lands with its own models (#1506);
                # every other glob must name a vendor that exists today.
                assert vendor in vendors or vendor == "flashforge_forgex", pattern
            else:
                assert pattern in printers.MODELS, pattern


def test_every_audited_baseline_reaches_the_feed():
    sources = build.parse_sources((ROOT / build.SOURCES_FILE).read_text(encoding="utf-8"))
    audited = build.audited_versions(ROOT)
    names = {s["name"] for s in sources}
    # Every firmware the weekly watch audits is in the feed under the
    # same name, so its audited version is published beside the newest.
    assert set(audited) <= names, sorted(set(audited) - names)


@pytest.mark.parametrize("text, problem", [
    ("", "no [[firmware]] entries"),
    ("[[firmware]]\nid='a'\n", "missing or empty 'name'"),
    ("[[firmware]]\nid='A b'\nname='n'\nrepo='o/r'\nfeed='releases'\nchannel='stable'\nupdate='u'\n",
     "lowercase letters"),
    ("[[firmware]]\nid='a'\nkind='firmware'\nname='n'\nrepo='o/r'\nfeed='rss'\nchannel='stable'\nupdate='u'\n",
     "feed must be one of"),
    ("[[firmware]]\nid='a'\nkind='firmware'\nname='n'\nrepo='o/r'\nfeed='releases'\nchannel='beta'\nupdate='u'\n",
     "channel must be one of"),
    ("[[firmware]]\nid='a'\nkind='firmware'\nname='n'\nrepo='o/r'\nfeed='releases'\nchannel='stable'\nupdate='u'\n"
     "models=['Not A Key']\n", "'models' must be registry keys"),
    ("[[firmware]]\nid='a'\nkind='firmware'\nname='n'\nrepo='Klipper3d/klipper'\nfeed='releases'\n"
     "channel='stable'\nupdate='u'\n", "publishes no usable releases"),
    ("[[firmware]]\nid='a'\nkind='firmware'\nname='n'\nrepo='o/r'\nfeed='releases'\nchannel='stable'\nupdate='u'\n"
     "extra=1\n", "unknown field"),
])
def test_malformed_sources_fail_closed(text, problem):
    with pytest.raises(build.FeedError, match=re.escape(problem)):
        build.parse_sources(text)


def test_duplicate_ids_are_refused():
    one = "[[firmware]]\nid='a'\nkind='firmware'\nname='{}'\nrepo='o/r'\nfeed='releases'\nchannel='stable'\nupdate='u'\n"
    with pytest.raises(build.FeedError, match="duplicate id or name"):
        build.parse_sources(one.format("n") + one.format("m"))


def test_spelled_stages_order_by_their_serial():
    key = lambda tag: build.parse_version(tag)[1]  # noqa: E731
    assert key("1.4.2-beta-4") < key("1.4.2-beta-10") < key("1.4.2-rc.1") < key("1.4.2")
    assert key("1.4.1") < key("1.4.2-beta-2")
    assert key("1.7.5") < key("1.7.5-2") < key("1.7.6")
    assert build.parse_version("Plus4_v1.7.1")[0] == "1.7.1"
    assert build.parse_version("v1.1.0-rc.1")[0] == "1.1.0-rc.1"
    assert build.parse_version("python-hardfloat") is None


def test_stable_channel_never_offers_a_prerelease():
    github = FakeGitHub(releases={"o/r": [
        _release("1.4.2-beta-4", pre=True, when="2026-10-08T00:00:00Z"),
        _release("1.4.1", when="2026-02-18T00:00:00Z"),
        _release("9.9.9", draft=True)]})
    feed = build.build_feed([_source()], {}, github, now=NOW, log=lambda _m: None)
    [entry] = feed["firmware"]
    assert entry["latest"]["version"] == "1.4.1" and entry["latest_stable"]["version"] == "1.4.1"
    assert entry["stale"] is False and feed["schema"] == 1


def test_prerelease_channel_offers_the_newest_of_any_kind():
    github = FakeGitHub(releases={"o/r": [
        _release("1.4.2-beta-4", pre=True), _release("1.4.2-beta-10", pre=True),
        _release("1.4.1")]})
    feed = build.build_feed([_source(channel="prerelease")], {}, github, now=NOW,
                            log=lambda _m: None)
    [entry] = feed["firmware"]
    assert entry["latest"]["version"] == "1.4.2-beta-10"
    assert entry["latest_stable"]["version"] == "1.4.1"


def test_a_beta_published_as_a_full_release_is_still_a_prerelease():
    github = FakeGitHub(releases={"o/r": [_release("2.1.3-b3"), _release("2.1.2.5")]})
    [entry] = build.build_feed([_source()], {}, github, now=NOW,
                               log=lambda _m: None)["firmware"]
    assert entry["latest"]["version"] == "2.1.2.5"


def test_tags_feed_and_printer_line_selection():
    github = FakeGitHub(
        tags={"o/t": [{"name": "v0.13.0"}, {"name": "v0.12.0"}, {"name": "nightly"}]},
        releases={"o/r": [
            _release("v8.0.1", title="8.0.1 Firmware for Prusa XL+, XL, MINI+, MINI"),
            _release("v8.0.0", title="8.0.0 Firmware for Prusa CORE One, MK4S")]})
    feed = build.build_feed(
        [_source(id="t", name="T", repo="o/t", feed="tags"),
         _source(id="mk4s", name="M", line=["MK4S"]), _source(id="xl", name="X", line=["XL"])],
        {"T": "0.13.0"}, github, now=NOW, log=lambda _m: None)
    tags, mk4s, xl = feed["firmware"]
    assert tags["latest"]["version"] == "0.13.0" and tags["latest"]["published_at"] is None
    assert tags["audited"] == "0.13.0" and mk4s["audited"] is None
    assert mk4s["latest"]["version"] == "8.0.0" and xl["latest"]["version"] == "8.0.1"


def test_an_unreadable_source_keeps_its_previous_entry_marked_stale():
    good = FakeGitHub(releases={"o/r": [_release("1.0.0")], "o/b": [_release("2.0.0")]})
    sources = [_source(), _source(id="b", name="B", repo="o/b")]
    first = build.build_feed(sources, {}, good, now=NOW, log=lambda _m: None)
    bad = FakeGitHub(releases={"o/r": [_release("1.1.0")]}, broken={"o/b"})
    logged = []
    second = build.build_feed(sources, {}, bad, previous=first, now=NOW, log=logged.append)
    fresh, kept = second["firmware"]
    assert fresh["latest"]["version"] == "1.1.0" and fresh["stale"] is False
    assert kept["latest"]["version"] == "2.0.0" and kept["stale"] is True
    assert any("b:" in line for line in logged)


def test_nothing_readable_and_nothing_to_keep_is_an_error():
    with pytest.raises(build.FeedError, match="no firmware source could be read"):
        build.build_feed([_source()], {}, FakeGitHub(broken={"o/r"}), now=NOW,
                         log=lambda _m: None)


def test_an_unchanged_feed_is_byte_identical():
    github = FakeGitHub(releases={"o/r": [_release("1.0.0")]})
    first = build.build_feed([_source()], {}, github, now=NOW, log=lambda _m: None)
    later = datetime(2026, 11, 16, tzinfo=timezone.utc)
    again = build.build_feed([_source()], {}, github, previous=first, now=later,
                             log=lambda _m: None)
    assert build.render(again) == build.render(first)
    changed = build.build_feed(
        [_source()], {}, FakeGitHub(releases={"o/r": [_release("1.0.1")]}),
        previous=first, now=later, log=lambda _m: None)
    assert changed["generated_at"] == "2026-11-16T00:00:00Z"


def test_the_feed_carries_nothing_but_public_release_facts():
    github = FakeGitHub(releases={"o/r": [_release("1.0.0")]})
    feed = build.build_feed([_source()], {}, github, now=NOW, log=lambda _m: None)
    assert set(feed) == {"schema", "generated_at", "source", "firmware"}
    assert set(feed["firmware"][0]) == {
        "id", "name", "repo", "feed", "channel", "models", "update",
        "latest", "latest_stable", "audited", "stale", "kind", "stock_models"}
    assert all(method == "GET" for method, _ in github.calls)


def test_workflow_publishes_to_its_own_branch_with_least_privilege():
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "firmware-feed.yml").read_text(encoding="utf-8"))
    assert workflow["permissions"] == {"contents": "read"}
    triggers = workflow[True]  # YAML reads the bare key `on` as True
    assert {"schedule", "workflow_dispatch", "push"} <= set(triggers)
    assert "docs/firmware-feed.toml" in triggers["push"]["paths"]
    job = workflow["jobs"]["publish-firmware-feed"]
    assert job["permissions"] == {"contents": "write"}
    assert job["env"]["FEED_BRANCH"] == "firmware-feed"
    script = "\n".join(step.get("run", "") for step in job["steps"])
    assert "scripts/build_firmware_feed.py" in script
    assert 'git push --quiet origin "HEAD:$FEED_BRANCH"' in script
    # It never pushes to main and never force-pushes.
    assert "HEAD:main" not in script and "--force" not in script
    assert firmware_feed.FEED_URL.endswith("/firmware-feed/firmware-feed.json")


# ---------------------------------------------------------------- the reader

FEED = {
    "schema": 1, "generated_at": "2026-11-15T12:00:00Z",
    "firmware": [
        {"id": "zmod", "kind": "firmware", "name": "ZMod", "repo": "ghzserg/zmod", "channel": "stable",
         "models": ["flashforge.*"], "update": "Use chat.",
         "latest": {"version": "1.7.6", "tag": "1.7.6", "published_at": "2026-11-11T11:08:42Z",
                    "url": "https://github.com/ghzserg/zmod/releases/tag/1.7.6"},
         "latest_stable": {"version": "1.7.6", "tag": "1.7.6"}, "audited": "1.7.6"},
        {"id": "opencreator", "kind": "firmware", "name": "OpenCreator", "channel": "stable",
         "models": ["flashforge_opencreator.*"], "update": "Its own updater.",
         "latest": {"version": "0.0.7.4", "published_at": "2026-11-14T05:51:55Z",
                    "url": "https://example.invalid/not-github"},
         "latest_stable": {"version": "0.0.7.4"}, "audited": None},
        {"id": "helixscreen", "kind": "plugin", "name": "HelixScreen", "models": ["flashforge.*"],
         "update": "",
         "latest": {"version": "1.0.9"}},
        "not an entry", {"id": "", "name": "blank"}, {"id": "zmod", "name": "duplicate"},
    ],
}


class _Resp:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, size=-1):
        return self.body if size < 0 else self.body[:size]


def test_parse_keeps_good_entries_and_drops_bad_ones():
    feed = firmware_feed.parse(FEED)
    assert [row.id for row in feed.firmware] == ["zmod", "opencreator", "helixscreen"]
    assert feed.get("zmod").latest.version == "1.7.6" and feed.get("nope") is None
    assert [r.id for r in feed.for_model("flashforge.ad5x")] == ["zmod", "helixscreen"]
    assert [r.id for r in feed.for_model("flashforge_opencreator.creator5_pro")] == ["opencreator"]
    assert feed.for_model("bambulab.x1c") == [] and feed.for_model(None) == []
    # A link that is not GitHub's is dropped, never rendered.
    assert feed.get("opencreator").latest.url == ""


@pytest.mark.parametrize("payload", [None, [], {}, {"schema": 2, "firmware": []},
                                     {"schema": 1, "firmware": "x"}])
def test_parse_refuses_what_it_does_not_understand(payload):
    assert firmware_feed.parse(payload) is None


def test_fetch_reads_the_published_file_and_sends_nothing_about_the_install():
    seen = []

    def opener(request, timeout=None):
        seen.append(request)
        return _Resp(json.dumps(FEED).encode())

    feed = firmware_feed.fetch(opener)
    assert feed.get("zmod").audited == "1.7.6"
    [request] = seen
    assert request.full_url == firmware_feed.FEED_URL and request.get_method() == "GET"
    assert request.data is None
    assert {k.lower() for k in request.headers} == {"accept", "user-agent"}


@pytest.mark.parametrize("failure", [OSError("down"), TimeoutError("slow"), ValueError("bad")])
def test_fetch_fails_soft(failure):
    def opener(request, timeout=None):
        raise failure

    assert firmware_feed.fetch(opener) is None
    assert firmware_feed.fetch(lambda r, timeout=None: _Resp(b"{not json")) is None
    huge = b" " * (firmware_feed.MAX_BYTES + 5)
    assert firmware_feed.fetch(lambda r, timeout=None: _Resp(huge)) is None


def test_cache_reads_once_an_hour_and_keeps_the_last_good_feed():
    now, answers, calls = [0.0], [firmware_feed.parse(FEED), None], []

    def fetcher():
        calls.append(1)
        return answers[min(len(calls) - 1, 1)]

    cache = firmware_feed.FeedCache(fetcher=fetcher, clock=lambda: now[0])
    assert cache.get().get("zmod") is not None and cache.get() is not None
    assert len(calls) == 1
    now[0] = firmware_feed.CACHE_S + 1
    assert cache.get().get("zmod") is not None  # the failed read keeps the last good feed
    assert len(calls) == 2
    assert cache.get() is not None and len(calls) == 2  # and the failure is cached too


# ---------------------------------------------------------------- ZMod release read

def test_zmod_release_comes_from_the_feed_before_github():
    def fallback():
        raise AssertionError("GitHub must not be asked when the feed answers")

    assert fa.newest_zmod_release(firmware_feed.parse(FEED), fallback) == "1.7.6"


def test_zmod_release_falls_back_to_github_without_a_usable_feed():
    empty = firmware_feed.parse({"schema": 1, "firmware": []})
    assert fa.newest_zmod_release(empty, lambda: "1.7.5") == "1.7.5"
    no_stable = firmware_feed.parse({"schema": 1, "firmware": [
        {"id": "zmod", "name": "ZMod", "latest": {"version": "1.8.0-rc1"}}]})
    assert fa.newest_zmod_release(no_stable, lambda: None) is None


# ---------------------------------------------------------------- notices

def _rows(version="0.0.7.4", when="2026-11-14T05:51:55Z"):
    payload = {"schema": 1, "firmware": [
        {"id": "opencreator", "name": "OpenCreator", "models": ["flashforge_opencreator.*"],
         "update": "Its own updater.",
         "latest": {"version": version, "published_at": when,
                    "url": f"https://github.com/o/r/releases/tag/{version}"}}]}
    return firmware_feed.parse(payload).firmware


def test_first_sight_records_the_release_without_a_notice():
    notices, seen = fa.feed_notices(_rows(), {})
    assert notices == []
    assert seen == {"opencreator": {"version": "0.0.7.4",
                                    "published_at": "2026-11-14T05:51:55Z"}}


def test_a_newer_release_is_one_notice_and_the_same_release_is_none():
    _, seen = fa.feed_notices(_rows(), {})
    state = {fa.FEED_SEEN: seen}
    assert fa.feed_notices(_rows(), state)[0] == []
    [notice], after = fa.feed_notices(_rows("0.0.8", "2026-11-20T00:00:00Z"), state)
    assert notice.key == "feed:opencreator:0.0.8"
    text = notice.text("Shop Creator")
    assert text.startswith("**New OpenCreator release for Shop Creator:** 0.0.8.")
    assert "Its own updater." in text and "<https://github.com/o/r/releases/tag/0.0.8>" in text
    assert "Update" not in text.replace("updater", "")  # no button wording, no apply offer
    assert after["opencreator"]["version"] == "0.0.8"


def test_a_withdrawn_release_falling_back_is_not_news():
    state = {fa.FEED_SEEN: {"opencreator": {"version": "0.0.8",
                                            "published_at": "2026-11-20T00:00:00Z"}}}
    notices, seen = fa.feed_notices(_rows(), state)
    assert notices == [] and seen["opencreator"]["version"] == "0.0.8"


def test_a_line_another_alert_owns_is_skipped_and_an_unread_line_is_ignored():
    rows = firmware_feed.parse(FEED).firmware
    notices, seen = fa.feed_notices(rows, {fa.FEED_SEEN: {"zmod": {"version": "1.7.5"}}},
                                    skip_ids=("zmod",))
    assert notices == [] and seen["zmod"] == {"version": "1.7.5"}
    unread = firmware_feed.parse({"schema": 1, "firmware": [
        {"id": "x", "name": "X", "models": ["v.*"], "latest": None}]}).firmware
    assert fa.feed_notices(unread, {}) == ([], {})


class _Adapter:
    supports_buttons = True

    def __init__(self, ok=True):
        self.sent, self.ok = [], ok

    async def send(self, chat_id, content):
        self.sent.append((chat_id, content))
        return SimpleNamespace(success=self.ok)


def _alerter(tmp_path, monkeypatch, feed, *, ok=True, alerts=True, notice=None):
    cfg = {"firmware": {"update_alerts": alerts,
                        **({} if notice is None else {"notice_firmwares": notice})},
           "printers": [{"id": "creator", "model": "flashforge_opencreator.creator5_pro",
                         "name": "Shop Creator"}]}
    monkeypatch.setattr(firmware_alert.settings, "printers", lambda c: c["printers"])
    monkeypatch.setattr(firmware_alert.settings, "printer_state_dir",
                        lambda pid, c: tmp_path / pid)
    monkeypatch.setattr(firmware_alert.routing, "chat_for_printer", lambda *a: "chat-1")
    monkeypatch.setattr(firmware_alert.routing, "printer_name", lambda c, pid: "Shop Creator")
    monkeypatch.setattr(firmware_alert, "_delivered", lambda result: bool(result.success))
    core = SimpleNamespace(cfg=cfg, adapter=_Adapter(ok), platform_name="discord",
                           chat_id="chat-1", gateway=None)
    clock = [1_800_000_000.0]
    alerter = firmware_alert.FirmwareAlerter(
        core, feed=lambda: feed[0], clock=lambda: clock[0],
        transport_for=lambda pid: pytest.fail("a feed notice must not contact a printer"))
    return alerter, core, clock


def _run_feed(alerter, clock):
    async def flow():
        # The first tick only schedules (a jittered first check); nothing
        # is due until the clock passes it, or the stored next check.
        assert alerter.tick_feed() == []
        clock[0] += 3 * 86400
        tasks = alerter.tick_feed()
        return [await task for task in tasks]

    return asyncio.run(flow())


def test_alerter_posts_one_notice_per_new_release_and_survives_restart(tmp_path, monkeypatch):
    feed = [firmware_feed.Feed(None, _rows())]
    alerter, core, clock = _alerter(tmp_path, monkeypatch, feed)
    assert _run_feed(alerter, clock) == [[]]          # first sight: recorded, silent
    assert core.adapter.sent == []
    feed[0] = firmware_feed.Feed(None, _rows("0.0.8", "2026-11-20T00:00:00Z"))
    [[notice]] = _run_feed(alerter, clock)
    assert notice.version == "0.0.8"
    [(chat, text)] = core.adapter.sent
    assert chat == "chat-1" and "New OpenCreator release for Shop Creator" in text
    # A fresh alerter (a restart) reads the same state and stays quiet.
    again, core2, clock2 = _alerter(tmp_path, monkeypatch, feed)
    clock2[0] = clock[0]  # the restart happens later, not back in time
    assert _run_feed(again, clock2) == [[]] and core2.adapter.sent == []


def test_an_undelivered_notice_is_announced_again_next_time(tmp_path, monkeypatch):
    feed = [firmware_feed.Feed(None, _rows())]
    alerter, core, clock = _alerter(tmp_path, monkeypatch, feed, ok=False)
    _run_feed(alerter, clock)
    feed[0] = firmware_feed.Feed(None, _rows("0.0.8", "2026-11-20T00:00:00Z"))
    assert _run_feed(alerter, clock) == [[]] and len(core.adapter.sent) == 1
    core.adapter.ok = True
    [[notice]] = _run_feed(alerter, clock)
    assert notice.version == "0.0.8" and len(core.adapter.sent) == 2


def test_no_feed_and_opt_out_send_nothing(tmp_path, monkeypatch):
    alerter, core, clock = _alerter(tmp_path, monkeypatch, [None])
    assert _run_feed(alerter, clock) == [[]] and core.adapter.sent == []
    off, core_off, clock_off = _alerter(tmp_path, monkeypatch, [None], alerts=False)
    assert _run_feed(off, clock_off) == [] and core_off.adapter.sent == []


def test_only_listed_firmware_lines_get_a_chat_notice(tmp_path, monkeypatch):
    # [firmware] notice_firmwares (catduck, 2026-11-15): a line not listed
    # posts nothing, even with a new release; the default lists OpenCreator.
    feed = [firmware_feed.Feed(None, _rows())]
    off, core_off, clock_off = _alerter(tmp_path / "off", monkeypatch, feed, notice=["zmod", "forge-x"])
    _run_feed(off, clock_off)
    feed[0] = firmware_feed.Feed(None, _rows("0.0.8", "2026-11-20T00:00:00Z"))
    assert _run_feed(off, clock_off) == [[]] and core_off.adapter.sent == []
    assert firmware_alert.notice_firmwares({}) == frozenset({"zmod", "opencreator", "forge-x"})


def test_notice_firmwares_setting_defaults_and_validates():
    from print_handy import settings as st
    data = {"firmware": {}}
    st._validate_firmware(data)
    assert data["firmware"]["notice_firmwares"] == ["zmod", "opencreator", "forge-x"]
    data = {"firmware": {"notice_firmwares": [" ZMod ", ""]}}
    st._validate_firmware(data)
    assert data["firmware"]["notice_firmwares"] == ["zmod"]
    with pytest.raises(st.SettingsError):
        st._validate_firmware({"firmware": {"notice_firmwares": "zmod"}})


# ---------------------------------------------------------------- the Web UI page

DATA = {"printers": [
    {"id": "ad5x", "model": "flashforge.ad5x", "name": "Garage AD5X"},
    {"id": "creator", "model": "flashforge_opencreator.creator5_pro", "name": "Shop <Creator>"},
    {"id": "x1c", "model": "bambulab.x1c", "name": "Bambu"}]}


def _page(monkeypatch, **kw):
    monkeypatch.setattr(firmware_page.settings, "printers", lambda data: data["printers"])
    kw.setdefault("feed", firmware_feed.parse(FEED))
    kw.setdefault("transport_for", lambda pid: pytest.fail("no printer read was asked for"))
    return firmware_page.render_firmware_page(DATA, **kw)


def test_firmware_route_and_nav_entry_exist():
    route, _capture = routes.resolve("GET", "/firmware")
    assert route is not None and route.policy == routes.SESSION
    assert route.handler == "_firmware_route"
    assert ("/firmware", "Firmware") in shell.NAV


def test_page_lists_each_printer_with_its_firmware_and_contacts_no_printer(monkeypatch):
    page = _page(monkeypatch)
    assert "Garage AD5X" in page and "Shop &lt;Creator&gt;" in page and "<Creator>" not in page
    assert 'href="https://github.com/ghzserg/zmod/releases/tag/1.7.6"' in page
    assert "0.0.7.4" in page and "2026-11-14" in page and "Its own updater." in page
    assert 'href="/firmware?printer=ad5x"' in page and 'href="/firmware?printer=creator"' in page
    # A model the feed does not cover says so, and a non-Moonraker printer
    # gets its family's guidance, not a check link.
    assert "lists no firmware for this model" in page
    assert 'href="/firmware?printer=x1c"' not in page
    assert "Every firmware Print Handy follows" in page and "HelixScreen" in page
    assert "<form" not in page and "<button" not in page  # read-only: nothing to press


def test_checking_one_printer_reads_only_that_printer(monkeypatch):
    asked = []
    monkeypatch.setattr(firmware_page.fw, "firmware_report",
                        lambda transport, entry, refresh: f"REPORT for {transport} refresh={refresh}")
    page = _page(monkeypatch, checked="creator",
                 transport_for=lambda pid: asked.append(pid) or pid)
    assert asked == ["creator"]
    assert "REPORT for creator refresh=False" in page
    assert 'href="/firmware?printer=creator"' not in page
    assert 'href="/firmware?printer=ad5x"' in page


def test_an_unknown_printer_id_and_an_unreadable_printer_are_harmless(monkeypatch):
    page = _page(monkeypatch, checked="../../etc")
    assert "Installed versions" not in page

    def broken(transport, entry, refresh):
        raise firmware_page.fw.FirmwareError("connection refused")

    monkeypatch.setattr(firmware_page.fw, "firmware_report", broken)
    page = _page(monkeypatch, checked="ad5x", transport_for=lambda pid: pid)
    assert "Could not read the printer" in page and "connection refused" in page


def test_page_without_a_feed_still_renders_the_roster(monkeypatch):
    page = _page(monkeypatch, feed=None)
    assert firmware_page.FEED_UNAVAILABLE[:40] in page
    assert "Garage AD5X" in page and "Every firmware Print Handy follows" not in page


# ------------------------------------------------- firmware, stock and plugins
# catduck (2026-11-16): plugins, firmware and the maker's stock firmware are
# three different things, shown separately on the Web UI.

def test_every_shipped_source_names_its_kind():
    sources = build.parse_sources((ROOT / build.SOURCES_FILE).read_text(encoding="utf-8"))
    kinds = {s["id"]: s["kind"] for s in sources}
    assert set(kinds.values()) <= set(build.KINDS) == set(firmware_feed.KINDS)
    assert kinds["zmod"] == kinds["forge-x"] == kinds["opencreator"] == "firmware"
    assert kinds["helixscreen"] == kinds["moonraker"] == kinds["octoprint"] == "plugin"
    assert kinds["prusa-buddy-mk4s"] == kinds["qidi-q1pro"] == "stock"
    # HelixScreen is listed under the ZMod printers it runs on, so it shows
    # in the printer's own Plugins table.
    helix = next(s for s in sources if s["id"] == "helixscreen")
    assert helix["models"] == ["flashforge.*"]


def test_a_source_without_a_known_kind_fails_closed():
    base = ("[[firmware]]\nid='a'\nname='n'\nrepo='o/r'\nfeed='releases'\n"
            "channel='stable'\nupdate='u'\n")
    for text in (base, base + "kind='addon'\n"):
        with pytest.raises(build.FeedError, match="kind must be one of"):
            build.parse_sources(text)


def test_built_entry_carries_its_kind_and_an_old_feed_reads_as_firmware():
    github = FakeGitHub(releases={"o/r": [_release("1.0.0")]})
    feed = build.build_feed([_source(kind="plugin")], {}, github, now=NOW, log=lambda _m: None)
    assert feed["firmware"][0]["kind"] == "plugin"
    old = {**FEED, "firmware": [{k: v for k, v in row.items() if k != "kind"}
                                for row in FEED["firmware"] if isinstance(row, dict)]}
    assert {row.kind for row in firmware_feed.parse(old).firmware} == {"firmware"}
    odd = {**FEED, "firmware": [{**FEED["firmware"][0], "kind": "addon"}]}
    assert firmware_feed.parse(odd).firmware[0].kind == "firmware"


def test_page_shows_firmware_stock_and_plugins_as_separate_tables(monkeypatch):
    stock = {"id": "prusa-buddy-mk4s", "kind": "stock", "name": "Prusa Buddy (MK4S)",
             "models": ["prusa.mk4s"], "update": "USB.", "latest": {"version": "6.4.0"}}
    page = _page(monkeypatch, feed=firmware_feed.parse({**FEED, "firmware": FEED["firmware"] + [stock]}))
    every = page[page.index("Every firmware Print Handy follows"):]
    fw_at, stock_at, plugin_at = (every.index(f"<h3>{t}</h3>")
                                  for t in ("Firmware", "Stock firmware", "Plugins"))
    assert fw_at < stock_at < plugin_at
    assert "ZMod" in every[fw_at:stock_at] and "HelixScreen" not in every[fw_at:stock_at]
    assert "Prusa Buddy (MK4S)" in every[stock_at:plugin_at]
    assert "HelixScreen" in every[plugin_at:] and "ZMod" not in every[plugin_at:]
    assert "<th>Plugin</th>" in every[plugin_at:]
    # The AD5X's own section splits the same way: ZMod under Firmware,
    # HelixScreen under Plugins, and no empty Stock firmware heading.
    ad5x = page[page.index("Garage AD5X"):page.index("Shop &lt;Creator&gt;")]
    assert ad5x.index("<h3>Firmware</h3>") < ad5x.index("<td>ZMod</td>") < ad5x.index("<h3>Plugins</h3>")
    assert ad5x.index("<h3>Plugins</h3>") < ad5x.index("<td>HelixScreen</td>")
    assert "<h3>Stock firmware</h3>" not in ad5x


# ------------------------------------------- stock Flashforge, stock or modified
# catduck (2026-11-16): add stock Flashforge firmware, and Marlin and Klipper
# can be stock or modified.

def _asset(name, when="2026-06-03T00:00:00Z"):
    return {"name": name, "updated_at": when}


class AssetGitHub(FakeGitHub):
    def __init__(self, release):
        super().__init__()
        self.release = release

    def request(self, method, path, payload=None):
        self.calls.append((method, path))
        assert method == "GET" and path == "/repos/ghzserg/FF/releases/tags/R"
        return self.release


FF_RELEASE = {"html_url": "https://github.com/ghzserg/FF/releases/tag/R", "assets": [
    _asset("AD5X-1.2.3-1.1.1-3.0.7-20260205-Factory.tgz", "2026-02-13T00:00:00Z"),
    _asset("AD5X-3.1.0-1.1.1-3.0.7-20260515.tgz", "2026-06-03T00:00:00Z"),
    _asset("AD5X-3.0.9-1.1.1-3.0.7-20260417.tgz", "2026-05-05T00:00:00Z"),
    _asset("AD5X-factory-1.0.9.tgz", "2026-06-10T00:00:00Z"),      # recovery image
    _asset("AD5X-fix_klipper.tgz"), _asset("AD5X-enable-zmod.tgz"),
    _asset("Adventurer5MPro-5.1.2-2.2.3-20260418.tgz"),
    _asset("Adventurer5M-5.1.1-2.2.3-20260417.tgz"),
]}


def test_assets_feed_reads_the_newest_versioned_file():
    """The assets feed type: versioned files attached to one release."""
    source = _source(id="mirror", kind="stock", repo="ghzserg/FF", feed="assets",
                     release="R", asset="AD5X")
    feed = build.build_feed([source], {}, AssetGitHub(FF_RELEASE), now=NOW, log=lambda _m: None)
    assert feed["firmware"][0]["latest"]["version"] == "3.1.0"
    pro = _source(id="pro", kind="stock", repo="ghzserg/FF", feed="assets",
                  release="R", asset="Adventurer5M")
    feed = build.build_feed([pro], {}, AssetGitHub(FF_RELEASE), now=NOW, log=lambda _m: None)
    assert feed["firmware"][0]["latest"]["version"] == "5.1.1"


def _tagged(tag, when):
    return {"tag_name": tag, "prerelease": False, "draft": False, "published_at": when,
            "html_url": f"https://github.com/Parallel-7/FlashForge-Firmware-Updates/releases/tag/{tag}",
            "name": tag}


FF_RELEASES = [
    _tagged("adventurer-5x/3.1.8-1.1.1", "2026-11-06T02:51:00Z"),
    _tagged("creator-5/2.0.0-1.3.0", "2026-10-22T00:00:00Z"),
    _tagged("adventurer-5x/3.1.7-1.1.1", "2026-10-16T00:00:00Z"),
    _tagged("adventurer-5x/3.1.0-1.1.1", "2026-08-10T00:00:00Z"),
    _tagged("adventurer-5m-pro/5.1.9-2.2.3", "2026-09-15T00:00:00Z"),
    _tagged("adventurer-5m/5.1.9-2.2.3", "2026-09-04T00:00:00Z"),
    _tagged("adventurer-5m/5.1.8-2.2.3", "2026-07-09T00:00:00Z"),
]


def test_stock_flashforge_reads_each_models_newest_tagged_release():
    """catduck's AD5X runs stock 3.1.7; the feed must know 3.1.8 is out."""
    sources = {s["id"]: s for s in build.parse_sources(
        (ROOT / build.SOURCES_FILE).read_text(encoding="utf-8"))}
    picked = [sources[i] for i in ("flashforge-stock-ad5x", "flashforge-stock-ad5m",
                                   "flashforge-stock-ad5m-pro", "flashforge-stock-creator5")]
    github = FakeGitHub(releases={"Parallel-7/FlashForge-Firmware-Updates": FF_RELEASES})
    feed = build.build_feed(picked, {}, github, now=NOW, log=lambda _m: None)
    by_id = {row["id"]: row["latest"] for row in feed["firmware"]}
    assert by_id["flashforge-stock-ad5x"]["version"] == "3.1.8"
    assert by_id["flashforge-stock-ad5x"]["tag"] == "adventurer-5x/3.1.8-1.1.1"
    assert by_id["flashforge-stock-ad5x"]["published_at"] == "2026-11-06T02:51:00Z"
    # "adventurer-5m/" never picks up an "adventurer-5m-pro/" release, and
    # the control-board suffix (-2.2.3) is not part of the version.
    assert by_id["flashforge-stock-ad5m"]["version"] == "5.1.9"
    assert by_id["flashforge-stock-ad5m-pro"]["version"] == "5.1.9"
    assert by_id["flashforge-stock-creator5"]["version"] == "2.0.0"
    assert all(row["kind"] == "stock" for row in feed["firmware"])


def test_shipped_stock_flashforge_covers_every_flashforge_model():
    sources = build.parse_sources((ROOT / build.SOURCES_FILE).read_text(encoding="utf-8"))
    stock = [s for s in sources if s["id"].startswith("flashforge-stock-")]
    assert {s["kind"] for s in stock} == {"stock"}
    assert {s["repo"] for s in stock} == {"Parallel-7/FlashForge-Firmware-Updates"}
    assert len({s["tag_prefix"] for s in stock}) == len(stock) == 5
    covered = {m for s in stock for m in s["models"]}
    flashforge = {key for key in printers.MODELS
                  if key.split(".")[0].startswith("flashforge")}
    assert flashforge - covered <= {"flashforge_opencreator.creator5",
                                    "flashforge_opencreator.creator5_pro"}
    assert covered <= set(printers.MODELS)
    # The update line never claims Print Handy installs it.
    assert all("never installs" in s["update"] for s in stock)


@pytest.mark.parametrize("extra, problem", [
    ("feed='tags'\ntag_prefix='adventurer-5x/'\n", "needs feed = \"releases\""),
    ("feed='releases'\ntag_prefix='Adventurer 5X'\n", "must be lowercase and end in '/'"),
])
def test_tag_prefix_sources_fail_closed(extra, problem):
    text = ("[[firmware]]\nid='a'\nkind='stock'\nname='n'\nrepo='o/r'\nchannel='stable'\n"
            "update='u'\n" + extra)
    with pytest.raises(build.FeedError, match=re.escape(problem)):
        build.parse_sources(text)


@pytest.mark.parametrize("extra, problem", [
    ("feed='assets'\nrelease='R'\n", "'asset' goes with feed"),
    ("feed='releases'\nrelease='R'\n", "'release' goes with feed"),
    ("feed='assets'\nrelease='R'\nasset='A B'\n", "plain tag or file-name prefix"),
])
def test_asset_sources_fail_closed(extra, problem):
    text = ("[[firmware]]\nid='a'\nkind='stock'\nname='n'\nrepo='o/r'\nchannel='stable'\n"
            "update='u'\n" + extra)
    with pytest.raises(build.FeedError, match=re.escape(problem)):
        build.parse_sources(text)


def test_marlin_is_stock_on_a_sovol_and_firmware_elsewhere(monkeypatch):
    sources = {s["id"]: s for s in build.parse_sources(
        (ROOT / build.SOURCES_FILE).read_text(encoding="utf-8"))}
    assert sources["marlin"]["kind"] == "firmware"
    assert sources["marlin"]["stock_models"] == ["sovol_marlin.*"]
    with pytest.raises(build.FeedError, match="stock_models"):
        build.parse_sources("[[firmware]]\nid='a'\nkind='plugin'\nname='n'\nrepo='o/r'\n"
                            "feed='releases'\nchannel='stable'\nupdate='u'\n"
                            "stock_models=['sovol_marlin.*']\n")
    marlin = {"id": "marlin", "kind": "firmware", "name": "Marlin",
              "models": ["octoprint.*", "sovol_marlin.*"], "stock_models": ["sovol_marlin.*"],
              "update": "Board tooling.", "latest": {"version": "2.1.2.5"}}
    feed = firmware_feed.parse({**FEED, "firmware": [marlin]})
    row = feed.firmware[0]
    assert row.kind_for("sovol_marlin.sv06") == "stock"
    assert row.kind_for("octoprint.marlin") == "firmware"
    data = {"printers": [{"id": "sv06", "model": "sovol_marlin.sv06", "name": "Sovol"},
                         {"id": "op", "model": "octoprint.marlin", "name": "Op"}]}
    monkeypatch.setattr(firmware_page.settings, "printers", lambda d: d["printers"])
    page = firmware_page.render_firmware_page(data, feed=feed,
                                              transport_for=lambda pid: pytest.fail("read"))
    sovol = page[page.index("<h2>Sovol</h2>"):page.index("<h2>Op</h2>")]
    other = page[page.index("<h2>Op</h2>"):page.index("Every firmware Print Handy follows")]
    assert "<h3>Stock firmware</h3>" in sovol and "<h3>Firmware</h3>" not in sovol
    assert "<h3>Firmware</h3>" in other and "<h3>Stock firmware</h3>" not in other
