"""Offline tests for the Moonraker update channel switch (issue #1634).

No printer or network: Moonraker responses are recorded shapes replayed
through a fake transport, and the config files live in a fake file-API
store that records every upload in order. The tests prove the switch
changes only the one `channel:` line of the named section, that the
backup is uploaded before the config file is touched, that a failed
write and a failed verification restore the backup and say so, and that
the switch refuses while printing, for a section in the read-only
moonraker.conf ZMod owns, and for a channel the component's type does
not accept.
"""
import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace as S
from unittest import mock

import pytest

from print_handy import firmware_update as fw
from print_handy import printer_control
from print_handy import printers
from print_handy.bot import llm
from print_handy.runtime import spool_sync
from print_handy.webui import channel_jobs, firmware_handlers, firmware_page

ROOT = Path(__file__).parents[1]
PLUGIN_PATH = ROOT / "plugins/print-feedback/__init__.py"
spec = importlib.util.spec_from_file_location("print_feedback_channel", PLUGIN_PATH)
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)


def entry(key):
    found = fw.printer_entry(key)
    assert found is not None, key
    return found


AD5X = entry("flashforge.ad5x")
GENERIC = entry("klipper.generic")
NEPTUNE4 = entry("elegoo.neptune4")

USER_FILE = "mod_data/user.moonraker.conf"
BACKUP_FILE = USER_FILE + fw.BACKUP_SUFFIX

USER_CONF = """# Sections the owner added.

[update_manager helixscreen]
type: web
channel: stable
repo: prestonbrown/helixscreen
path: /usr/data/helixscreen

[update_manager fluidd]
type: web
channel: stable
"""

USER_CONF_BETA = USER_CONF.replace(
    "[update_manager helixscreen]\ntype: web\nchannel: stable",
    "[update_manager helixscreen]\ntype: web\nchannel: beta")


def _status(channels=None, types=None):
    channels = channels or {}
    types = types or {}

    def info(name, version):
        row = {"version": version, "remote_version": version}
        if name in types:
            row["configured_type"] = types[name]
        if name in channels:
            row["channel"] = channels[name]
        return row

    return {"result": {"version_info": {
        "klipper": info("klipper", "v0.12.0-289"),
        "moonraker": info("moonraker", "v0.9.3-10"),
        "helixscreen": info("helixscreen", "1.0.0"),
        "fluidd": info("fluidd", "1.31.3"),
        "system": {"package_count": 0, "package_list": []},
    }, "busy": False}}


def status(channels=None):
    """The live status: helixscreen is a web component, the rest git repos."""
    merged = {"klipper": "stable", "moonraker": "stable", "helixscreen": "stable"}
    merged.update(channels or {})
    return _status(merged, {"klipper": "git_repo", "moonraker": "git_repo",
                            "helixscreen": "web"})


CONFIG = {"result": {"config": {}, "files": [
    {"filename": "moonraker.conf",
     "sections": ["server", "update_manager", "update_manager klipper",
                  "update_manager moonraker"]},
    {"filename": USER_FILE,
     "sections": ["update_manager helixscreen", "update_manager fluidd"]},
]}}


class ChannelTransport:
    """The spool_sync.moonraker call shape for the channel flow.

    Routes update status, the /server/config listing, the print-state
    query and the Moonraker restart. After a restart, the status reports
    `channel_after` channels (what a real Moonraker would report once it
    re-reads the edited config); any other path is a test failure.
    """

    def __init__(self, status_payload=None, config=None, states=("standby",),
                 channel_after=None, restart_error=None):
        self.status_payload = status() if status_payload is None else status_payload
        self.config = CONFIG if config is None else config
        self.states = list(states)
        self.channel_after = channel_after
        self.restart_error = restart_error
        self.restarted = False
        self.restarts = 0
        self.calls = []
        self.posts = []

    def restart(self):
        """The service-routed Moonraker restart the surfaces hand to the flow."""
        self.restarts += 1
        self.restarted = True
        if self.restart_error is not None:
            raise self.restart_error

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method == "POST":
            self.posts.append((path, body))
        if path == fw.UPDATE_STATUS_PATH:
            if self.restarted and self.channel_after is not None:
                return status(self.channel_after)
            return self.status_payload
        if path == fw.CONFIG_PATH:
            return self.config
        if path == fw.PRINT_STATE_PATH:
            state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
            return {"result": {"status": {"print_stats": {"state": state}}}}
        raise AssertionError(f"unexpected transport call: {method} {path}")


class FakeFiles:
    """The config file-API seam: an in-memory store recording uploads in order."""

    def __init__(self, files=None, fail_counts=None):
        self.files = dict(files if files is not None else {USER_FILE: USER_CONF})
        self.fail_counts = dict(fail_counts or {})
        self.uploads = []
        self.downloads = []

    def download(self, filename):
        self.downloads.append(filename)
        if filename not in self.files:
            raise OSError(f"no such file: {filename}")
        return self.files[filename]

    def upload(self, filename, text):
        self.uploads.append((filename, text))
        if self.fail_counts.get(filename, 0) > 0:
            self.fail_counts[filename] -= 1
            raise OSError("upload refused")
        self.files[filename] = text


def _no_sleep(_seconds):
    return None


def prepare(transport=None, files=None, component="helixscreen", channel="beta",
            for_entry=AD5X):
    return fw.prepare_channel_switch(transport or ChannelTransport(),
                                     files or FakeFiles(), for_entry,
                                     component, channel, None)


# --------------------------------------------------------------------- parsing

def test_parse_reads_channel_and_configured_type():
    parsed = fw.parse_update_status(status())
    helix = parsed.component("helixscreen")
    assert (helix.channel, helix.configured_type) == ("stable", "web")
    git = parsed.component("klipper")
    assert (git.channel, git.configured_type) == ("stable", "git_repo")
    system = parsed.component("system")
    assert system.channel is None and system.configured_type is None


def test_channels_for_type_follows_moonrakers_docs():
    assert fw.channels_for_type("git_repo") == ("stable", "beta", "dev")
    assert fw.channels_for_type("web") == ("stable", "beta")
    assert fw.channels_for_type("zip") == ("stable", "beta")
    assert fw.channels_for_type(None) == ("stable", "beta")
    assert fw.channels_for_type("something-else") == ("stable", "beta")


# ------------------------------------------------------------------ text edit

def test_switch_in_text_changes_only_the_named_sections_channel_line():
    text = ("[update_manager fluidd]\ntype: web\nchannel: stable\n\n"
            "[update_manager helixscreen]\ntype: web\nchannel: stable\n")
    new_text, old, new, current = fw.switch_channel_in_text(
        text, "update_manager helixscreen", "beta")
    assert (old, new, current) == ("channel: stable", "channel: beta", "stable")
    assert new_text == ("[update_manager fluidd]\ntype: web\nchannel: stable\n\n"
                        "[update_manager helixscreen]\ntype: web\nchannel: beta\n")


def test_switch_in_text_preserves_spacing_and_a_trailing_comment():
    text = "[update_manager helixscreen]\nchannel:   stable  # pinned for now\n"
    new_text, _old, new, _current = fw.switch_channel_in_text(
        text, "update_manager helixscreen", "beta")
    assert new == "channel:   beta  # pinned for now"
    assert new_text == "[update_manager helixscreen]\nchannel:   beta  # pinned for now\n"


def test_switch_in_text_refuses_missing_section_missing_line_and_same_channel():
    with pytest.raises(fw.FirmwareRefusal, match="does not contain"):
        fw.switch_channel_in_text("[server]\n", "update_manager helixscreen", "beta")
    with pytest.raises(fw.FirmwareRefusal, match="no `channel:` line"):
        fw.switch_channel_in_text("[update_manager helixscreen]\ntype: web\n",
                                  "update_manager helixscreen", "beta")
    with pytest.raises(fw.FirmwareRefusal, match="already"):
        fw.switch_channel_in_text("[update_manager helixscreen]\nchannel: beta\n",
                                  "update_manager helixscreen", "beta")


def test_section_file_reads_the_server_config_listing():
    assert fw.section_file(CONFIG, ("update_manager helixscreen",)) == USER_FILE
    assert fw.section_file(CONFIG, ("update_manager klipper",)) == "moonraker.conf"
    assert fw.section_file(CONFIG, ("update_manager nosuch",)) is None
    assert fw.section_file({"result": {}}, ("update_manager helixscreen",)) is None
    # The legacy client form names the same section.
    legacy = {"result": {"files": [{"filename": "extra.conf",
                                    "sections": ["update_manager client helixscreen"]}]}}
    assert fw.section_file(legacy, ("update_manager helixscreen",
                                    "update_manager client helixscreen")) == "extra.conf"


# --------------------------------------------------------------------- prepare

def test_prepare_reads_the_exact_change_and_writes_nothing():
    transport, files = ChannelTransport(), FakeFiles()
    prepared = prepare(transport, files)
    assert prepared.component.name == "helixscreen"
    assert prepared.section == "update_manager helixscreen"
    assert prepared.filename == USER_FILE
    assert (prepared.current_channel, prepared.target_channel) == ("stable", "beta")
    assert (prepared.old_line, prepared.new_line) == ("channel: stable", "channel: beta")
    assert prepared.new_text == USER_CONF_BETA
    assert prepared.backup_filename == BACKUP_FILE
    assert files.downloads == [USER_FILE]
    assert files.uploads == []  # a prepared switch that is never confirmed writes nothing
    assert transport.posts == []


def test_prepare_refuses_an_ineligible_printer_before_any_read():
    transport = ChannelTransport()
    with pytest.raises(fw.FirmwareRefusal, match="vendor fork"):
        prepare(transport, FakeFiles(), for_entry=NEPTUNE4)
    assert transport.calls == []


def test_prepare_refuses_a_channel_word_moonraker_does_not_accept():
    transport = ChannelTransport()
    with pytest.raises(fw.FirmwareRefusal, match="not an update channel"):
        prepare(transport, FakeFiles(), channel="nightly")
    assert transport.calls == []


def test_prepare_refuses_dev_for_a_web_component_and_allows_it_for_git():
    with pytest.raises(fw.FirmwareRefusal, match="stable or beta"):
        prepare(channel="dev")
    config = {"result": {"files": [{"filename": "user.moonraker.conf",
                                     "sections": ["update_manager moonraker"]}]}}
    files = FakeFiles({"user.moonraker.conf":
                       "[update_manager moonraker]\ntype: git_repo\nchannel: stable\n"})
    prepared = prepare(ChannelTransport(config=config), files,
                       component="moonraker", channel="dev", for_entry=GENERIC)
    assert prepared.target_channel == "dev"
    assert prepared.new_line == "channel: dev"


def test_prepare_refuses_the_channel_the_component_is_already_on():
    with pytest.raises(fw.FirmwareRefusal, match="already on the stable channel"):
        prepare(channel="stable")


def test_prepare_refuses_a_section_in_the_read_only_moonraker_conf():
    with pytest.raises(fw.FirmwareRefusal) as caught:
        prepare(component="klipper")
    message = str(caught.value)
    assert "moonraker.conf" in message and USER_FILE in message
    assert "Nothing was sent" in message


def test_prepare_refuses_while_a_print_is_running():
    transport = ChannelTransport(states=("printing",))
    with pytest.raises(fw.FirmwareRefusal, match="printing"):
        prepare(transport, FakeFiles())
    assert transport.posts == []


def test_prepare_refuses_an_unlisted_component_and_a_missing_section():
    with pytest.raises(fw.FirmwareRefusal, match="does not list a component"):
        prepare(component="nosuch")
    config = {"result": {"files": [{"filename": "moonraker.conf",
                                     "sections": ["server"]}]}}
    with pytest.raises(fw.FirmwareRefusal, match="does not place"):
        prepare(ChannelTransport(config=config), FakeFiles())


def test_prepare_refuses_system_and_a_component_with_no_channel():
    with pytest.raises(fw.FirmwareRefusal, match="no update channel"):
        prepare(component="system")
    with pytest.raises(fw.FirmwareRefusal, match="does not report an update channel"):
        prepare(component="fluidd")


def test_prepare_refuses_when_the_section_has_no_channel_line():
    files = FakeFiles({USER_FILE: "[update_manager helixscreen]\ntype: web\n"})
    with pytest.raises(fw.FirmwareRefusal, match="no `channel:` line"):
        prepare(files=files)


def test_prompt_shows_the_exact_line_the_backup_and_the_no_downgrade_note():
    prompt = fw.channel_switch_prompt_text(prepare())
    assert f"`{USER_FILE}`" in prompt
    assert "`channel: stable` becomes `channel: beta`" in prompt
    assert BACKUP_FILE in prompt
    back = fw.channel_switch_prompt_text(prepare(
        ChannelTransport(status_payload=status({"helixscreen": "beta"})),
        FakeFiles({USER_FILE: USER_CONF_BETA}), channel="stable"))
    assert "does not downgrade" in back


# ------------------------------------------------------------------------ send

def _prepared(transport, files, **kwargs):
    return fw.prepare_channel_switch(transport, files, AD5X,
                                     kwargs.get("component", "helixscreen"),
                                     kwargs.get("channel", "beta"), None)


def test_send_backs_up_first_writes_restarts_and_verifies():
    transport = ChannelTransport(channel_after={"helixscreen": "beta"})
    files = FakeFiles()
    prepared = _prepared(transport, files)
    message = fw.send_channel_switch(transport, files, prepared, restart=transport.restart,
                                     verify_delay_s=0, sleep=_no_sleep)
    assert message.startswith("Switched **helixscreen** to the beta channel")
    assert "verified" in message
    assert [name for name, _text in files.uploads] == [BACKUP_FILE, USER_FILE]
    assert files.uploads[0][1] == USER_CONF          # the backup is the original
    assert files.files[USER_FILE] == USER_CONF_BETA  # only the one line changed
    assert transport.restarts == 1


def test_send_restores_backup_after_unexpected_verification_exception():
    transport = ChannelTransport()
    original_fetch = fw.fetch_status
    failed = False

    def unexpected_once(client):
        nonlocal failed
        if client is transport and transport.restarted and not failed:
            failed = True
            raise RuntimeError("unexpected transport failure")
        return original_fetch(client)

    with mock.patch.object(fw, "fetch_status", side_effect=unexpected_once):
        files = FakeFiles()
        prepared = _prepared(transport, files)
        message = fw.send_channel_switch(transport, files, prepared, restart=transport.restart,
                                         verify_attempts=1, verify_delay_s=0, sleep=_no_sleep)
    assert "backup was restored" in message and "restarted again" in message
    assert files.files[USER_FILE] == USER_CONF
    assert [text for name, text in files.uploads if name == USER_FILE] == [USER_CONF_BETA, USER_CONF]


def test_send_rechecks_idle_and_writes_nothing_when_the_printer_started():
    transport = ChannelTransport(states=("standby", "printing"))
    files = FakeFiles()
    prepared = _prepared(transport, files)
    with pytest.raises(fw.FirmwareRefusal, match="printing"):
        fw.send_channel_switch(transport, files, prepared, restart=transport.restart,
                               verify_delay_s=0, sleep=_no_sleep)
    assert files.uploads == []
    assert transport.posts == []


def test_send_writes_nothing_when_the_backup_cannot_be_saved():
    transport = ChannelTransport(channel_after={"helixscreen": "beta"})
    files = FakeFiles(fail_counts={BACKUP_FILE: 1})
    prepared = _prepared(transport, files)
    with pytest.raises(fw.FirmwareError, match="backup"):
        fw.send_channel_switch(transport, files, prepared, restart=transport.restart,
                               verify_delay_s=0, sleep=_no_sleep)
    assert [name for name, _text in files.uploads] == [BACKUP_FILE]
    assert files.files[USER_FILE] == USER_CONF
    assert transport.posts == []


def test_send_restores_the_backup_when_the_write_fails():
    transport = ChannelTransport(channel_after={"helixscreen": "beta"})
    files = FakeFiles(fail_counts={USER_FILE: 1})
    prepared = _prepared(transport, files)
    message = fw.send_channel_switch(transport, files, prepared, restart=transport.restart,
                                     verify_delay_s=0, sleep=_no_sleep)
    assert "failed while writing" in message and "backup was restored" in message
    assert [name for name, _text in files.uploads] == [BACKUP_FILE, USER_FILE, USER_FILE]
    assert files.files[USER_FILE] == USER_CONF
    assert transport.posts == []  # no restart: Moonraker never saw a new file


def test_send_names_a_failed_restore_honestly():
    transport = ChannelTransport()
    files = FakeFiles(fail_counts={USER_FILE: 99})
    prepared = _prepared(transport, files)
    message = fw.send_channel_switch(transport, files, prepared, restart=transport.restart,
                                     verify_delay_s=0, sleep=_no_sleep)
    assert "restoring the backup failed too" in message
    assert BACKUP_FILE in message


def test_send_restores_the_backup_when_the_new_channel_never_shows():
    transport = ChannelTransport()  # the status keeps reporting stable
    files = FakeFiles()
    prepared = _prepared(transport, files)
    message = fw.send_channel_switch(transport, files, prepared, restart=transport.restart, verify_attempts=2,
                                     verify_delay_s=0, sleep=_no_sleep)
    assert "did not take effect" in message and "backup was restored" in message
    assert "still on the stable channel" in message
    assert files.files[USER_FILE] == USER_CONF
    # One restart for the switch, one more after the restore.
    assert transport.restarts == 2


def test_send_verifies_even_when_the_restart_drops_the_connection():
    transport = ChannelTransport(channel_after={"helixscreen": "beta"},
                                 restart_error=OSError("connection reset"))
    files = FakeFiles()
    prepared = _prepared(transport, files)
    message = fw.send_channel_switch(transport, files, prepared, restart=transport.restart,
                                     verify_delay_s=0, sleep=_no_sleep)
    assert message.startswith("Switched **helixscreen**")


def test_send_switching_back_to_stable_says_it_does_not_downgrade():
    transport = ChannelTransport(
        status_payload=status({"helixscreen": "beta"}),
        channel_after={"helixscreen": "stable"})
    files = FakeFiles({USER_FILE: USER_CONF_BETA})
    prepared = _prepared(transport, files, channel="stable")
    message = fw.send_channel_switch(transport, files, prepared, restart=transport.restart,
                                     verify_delay_s=0, sleep=_no_sleep)
    assert message.startswith("Switched **helixscreen** to the stable channel")
    assert "does not downgrade" in message


# ------------------------------------------------------------------ confirmed

def test_confirmed_switch_asks_first_and_sends_only_on_the_switch_label():
    async def ask_yes(question, choices):
        assert choices == [fw.SWITCH_LABEL, fw.CANCEL_LABEL]
        assert "`channel: stable` becomes `channel: beta`" in question
        return fw.SWITCH_LABEL

    async def ask_no(question, choices):
        return fw.CANCEL_LABEL

    transport = ChannelTransport(channel_after={"helixscreen": "beta"})
    files = FakeFiles()
    message = asyncio.run(fw.confirmed_channel_switch(
        transport, files, AD5X, "helixscreen", "beta", ask_yes, None,
        restart=transport.restart))
    assert message.startswith("Switched **helixscreen**")

    transport = ChannelTransport(channel_after={"helixscreen": "beta"})
    files = FakeFiles()
    message = asyncio.run(fw.confirmed_channel_switch(
        transport, files, AD5X, "helixscreen", "beta", ask_no, None,
        restart=transport.restart))
    assert message == fw.NOT_SWITCHED_TEXT
    assert files.uploads == [] and transport.posts == []


def test_driver_server_restart_posts_the_restart():
    from print_handy.runtime import print_control
    sent = []
    with mock.patch.object(print_control, "_send",
                           lambda path, **kw: sent.append((path, kw))):
        print_control.server_restart(printer_id="kl1")
    assert sent == [("/server/restart",
                     {"method": "POST", "timeout": 15, "printer_id": "kl1"})]


def test_the_chat_model_may_never_name_the_channel_form():
    from print_handy.bot import commands as bot_commands
    assert llm.allowed_command("/print-firmware channel helixscreen beta") is None
    assert bot_commands.as_text("print-firmware", {"subcommand": "channel",
                                                   "component": "helixscreen",
                                                   "channel": "beta"}) == \
        "/print-firmware channel helixscreen beta"


# ----------------------------------------------------------------- file access

def test_config_files_download_and_upload_through_the_file_api(monkeypatch):
    from print_handy import moonraker
    seen = {}

    def fake_text(url, **kwargs):
        seen["download"] = (url, kwargs)
        return "file text"

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_open(url, **kwargs):
        seen["upload"] = (url, kwargs)
        return _Response()

    monkeypatch.setattr(moonraker, "url_text", fake_text)
    monkeypatch.setattr(moonraker, "open_url", fake_open)
    files = fw.MoonrakerConfigFiles("http://printer.local:7125/")
    assert files.download(USER_FILE) == "file text"
    assert seen["download"][0] == \
        f"http://printer.local:7125/server/files/config/{USER_FILE}"
    files.upload(USER_FILE, USER_CONF)
    url, kwargs = seen["upload"]
    assert url == "http://printer.local:7125/server/files/upload"
    body = kwargs["data"].decode("utf-8")
    assert 'name="root"\r\n\r\nconfig' in body
    assert f'filename="{USER_FILE}"' in body
    assert "channel: stable" in body
    with pytest.raises(fw.FirmwareRefusal):
        files.download("../outside.conf")
    with pytest.raises(fw.FirmwareRefusal):
        files.upload("/absolute.conf", "x")


def test_config_files_for_reads_the_printers_moonraker_url():
    files = fw.config_files_for(None, {"services": {"moonraker_url": "http://x:7125/"}})
    assert files.base == "http://x:7125"


# ------------------------------------------------------------- chat dispatch

class FakeAdapter:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id=None, content=None, metadata=None):
        self.sent.append(content)

    async def send_clarify(self, **kwargs):
        return S(success=True)


def _event(text):
    source = S(platform=S(value="discord"), profile="3dprinting", is_bot=False,
               profile_route_rejected=False, user_id="u", chat_id="c", thread_id=None)
    return S(source=source, text=text, internal=False)


def _gateway(adapter):
    return S(_is_user_authorized_for_source=lambda s: True,
             _delivery_adapter_for=lambda s: adapter,
             _session_key_for_source=lambda s: "session-key")


def _configured(key, connection):
    return mock.patch.object(printers, "current",
                             return_value=S(connection=connection, model=S(key=key)))


async def _confirm_switch(*args, **kwargs):
    return fw.SWITCH_LABEL


async def _confirm_cancel(*args, **kwargs):
    return fw.CANCEL_LABEL


def _fake_service(transport):
    """The printer-control service with this transport's restart."""
    return mock.patch.object(printer_control, "for_printer",
                             lambda pid=None: S(server_restart=transport.restart))


def test_dispatch_channel_switches_after_confirmation():
    adapter = FakeAdapter()
    transport = ChannelTransport(channel_after={"helixscreen": "beta"})
    files = FakeFiles()
    with _configured("flashforge.ad5x", "moonraker"), \
            mock.patch.object(spool_sync, "moonraker", transport), \
            mock.patch.object(fw, "config_files_for", lambda pid, cfg=None: files), \
            _fake_service(transport), \
            mock.patch.object(plugin, "_confirm", _confirm_switch):
        out = asyncio.run(plugin.firmware_dispatch(
            _event("/print-firmware channel helixscreen beta"), _gateway(adapter)))
    assert out == {"action": "skip", "reason": "print firmware channel handled"}
    assert adapter.sent[-1].startswith("Switched **helixscreen** to the beta channel")
    assert [name for name, _text in files.uploads] == [BACKUP_FILE, USER_FILE]


def test_dispatch_channel_sends_nothing_when_the_operator_cancels():
    adapter = FakeAdapter()
    transport = ChannelTransport(channel_after={"helixscreen": "beta"})
    files = FakeFiles()
    with _configured("flashforge.ad5x", "moonraker"), \
            mock.patch.object(spool_sync, "moonraker", transport), \
            mock.patch.object(fw, "config_files_for", lambda pid, cfg=None: files), \
            _fake_service(transport), \
            mock.patch.object(plugin, "_confirm", _confirm_cancel):
        asyncio.run(plugin.firmware_dispatch(
            _event("/print-firmware channel helixscreen beta"), _gateway(adapter)))
    assert adapter.sent[-1] == fw.NOT_SWITCHED_TEXT
    assert files.uploads == [] and transport.posts == []


def test_dispatch_channel_without_a_channel_word_shows_usage():
    adapter = FakeAdapter()
    transport = ChannelTransport()
    with _configured("flashforge.ad5x", "moonraker"), \
            mock.patch.object(spool_sync, "moonraker", transport):
        out = asyncio.run(plugin.firmware_dispatch(
            _event("/print-firmware channel helixscreen"), _gateway(adapter)))
    assert out["reason"] == "print firmware usage shown"
    assert "Use `/print-firmware channel <component> <channel>`" in adapter.sent[-1]
    assert transport.calls == []


# --------------------------------------------------------------------- Web UI

class FakeHandler:
    """The slice of the request handler the firmware routes use."""

    def __init__(self, data, path="/firmware/channel"):
        self.server = S(settings_data=data)
        self.path = path
        self.sent = []
        self.redirects = []
        self.refusals = []

    def _theme(self):
        return "dark"

    def _form_token(self):
        return "csrf-value"

    def _send(self, code, body, content_type, headers=()):
        self.sent.append((code, body))

    def _redirect(self, location, headers=()):
        self.redirects.append(location)

    def _html_refusal(self, code, message, back="/"):
        self.refusals.append((code, message, back))


def _data(tmp_path, *, controls=True):
    return {"paths": {"state_home": tmp_path},
            "webui": {"controls_enabled": controls},
            "printers": [{"id": "kl1", "model": "klipper.generic",
                          "name": "Garage Klipper"}]}


@pytest.fixture(autouse=True)
def _clean_jobs():
    channel_jobs._reset_for_tests()
    yield
    channel_jobs._reset_for_tests()


def _wire(monkeypatch, transport, files):
    from print_handy import settings
    monkeypatch.setattr(settings, "printers", lambda d: d["printers"])
    monkeypatch.setattr(firmware_page, "printer_transport",
                        lambda data, pid: transport)
    monkeypatch.setattr(firmware_handlers, "_channel_files",
                        lambda data, pid: files)
    monkeypatch.setattr(printer_control, "for_printer",
                        lambda pid=None: S(server_restart=transport.restart))


def test_firmware_page_lists_channels_and_the_switch_link(monkeypatch, tmp_path):
    transport = ChannelTransport()
    _wire(monkeypatch, transport, FakeFiles())
    page = firmware_page.render_firmware_page(
        _data(tmp_path), checked="kl1", feed=lambda: None,
        transport_for=lambda pid: transport, controls=True)
    assert "Update channels" in page
    assert "helixscreen</strong> is on the stable channel" in page
    assert 'href="/firmware/channel?printer=kl1&amp;component=helixscreen"' in page


def test_firmware_page_names_channels_without_a_link_when_controls_off(
        monkeypatch, tmp_path):
    transport = ChannelTransport()
    _wire(monkeypatch, transport, FakeFiles())
    page = firmware_page.render_firmware_page(
        _data(tmp_path), checked="kl1", feed=lambda: None,
        transport_for=lambda pid: transport, controls=False)
    assert "is on the stable channel" in page
    assert "/firmware/channel?printer=" not in page
    assert "/print-firmware channel" in page


def test_channel_picker_offers_only_the_other_channels(monkeypatch, tmp_path):
    transport = ChannelTransport()
    _wire(monkeypatch, transport, FakeFiles())
    handler = FakeHandler(_data(tmp_path),
                          "/firmware/channel?printer=kl1&component=helixscreen")
    firmware_handlers.channel_route(handler)
    _code, body = handler.sent[-1]
    assert "channel=beta" in body and "Switch helixscreen to beta" in body
    assert "channel=dev" not in body  # a web component takes stable or beta only
    git_handler = FakeHandler(_data(tmp_path),
                              "/firmware/channel?printer=kl1&component=moonraker")
    firmware_handlers.channel_route(git_handler)
    _code, git_body = git_handler.sent[-1]
    assert "channel=dev" in git_body  # a git_repo component may take dev


def test_channel_confirm_page_shows_the_exact_change_and_form(monkeypatch, tmp_path):
    transport = ChannelTransport()
    _wire(monkeypatch, transport, FakeFiles())
    handler = FakeHandler(
        _data(tmp_path),
        "/firmware/channel?printer=kl1&component=helixscreen&channel=beta")
    firmware_handlers.channel_route(handler)
    _code, body = handler.sent[-1]
    assert "`channel: stable` becomes `channel: beta`" in body
    assert BACKUP_FILE in body
    assert '<form method="post" action="/firmware/channel">' in body
    assert 'name="channel" value="beta"' in body
    assert 'name="_csrf" value="csrf-value"' in body


def test_channel_confirm_page_with_controls_off_offers_no_form(monkeypatch, tmp_path):
    transport = ChannelTransport()
    _wire(monkeypatch, transport, FakeFiles())
    handler = FakeHandler(
        _data(tmp_path, controls=False),
        "/firmware/channel?printer=kl1&component=helixscreen&channel=beta")
    firmware_handlers.channel_route(handler)
    _code, body = handler.sent[-1]
    assert "controls_enabled" in body and "<form" not in body


def test_channel_apply_with_controls_off_refuses_and_writes_nothing(
        monkeypatch, tmp_path):
    transport = ChannelTransport()
    files = FakeFiles()
    _wire(monkeypatch, transport, files)
    handler = FakeHandler(_data(tmp_path, controls=False))
    firmware_handlers.channel_apply_route(
        handler, {"printer": ["kl1"], "component": ["helixscreen"],
                  "channel": ["beta"]})
    assert handler.refusals and handler.refusals[0][0] == 403
    assert files.uploads == [] and transport.posts == []


def test_channel_apply_starts_a_job_and_the_status_page_follows_it(
        monkeypatch, tmp_path):
    import time
    transport = ChannelTransport(channel_after={"helixscreen": "beta"})
    files = FakeFiles()
    _wire(monkeypatch, transport, files)
    handler = FakeHandler(_data(tmp_path))
    firmware_handlers.channel_apply_route(
        handler, {"printer": ["kl1"], "component": ["helixscreen"],
                  "channel": ["beta"]})
    assert len(handler.redirects) == 1
    job_id = handler.redirects[0].split("job=")[1]
    job = channel_jobs.get_job(job_id)
    assert job is not None
    deadline = time.time() + 10
    while job.running and time.time() < deadline:
        time.sleep(0.05)
    assert not job.running
    assert "Switched **helixscreen**" in (job.outcome or "")
    assert files.files[USER_FILE] == USER_CONF_BETA
    status_handler = FakeHandler(_data(tmp_path),
                                 f"/firmware/channel/status?job={job_id}")
    firmware_handlers.channel_status_route(status_handler)
    _code, body = status_handler.sent[-1]
    assert "Switched" in body and "refresh" not in body
    missing = FakeHandler(_data(tmp_path), "/firmware/channel/status?job=nope")
    firmware_handlers.channel_status_route(missing)
    assert missing.refusals and missing.refusals[0][0] == 404
