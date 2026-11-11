"""Offline tests for firmware update reporting, guarded apply, guides and
per-family guidance (issue #561).

No printer or network: Moonraker responses are recorded shapes replayed
through a fake transport that records every call, and the dispatch tests
use a fake adapter that records sends, in the style of tests/test_macros.py.
Stage 1 tests prove the report's reads are read-only and that a refresh
happens only on the user-invoked check path; stage 2 tests prove an apply
makes exactly one upgrade POST, only for an eligible profile, only while
idle at both checks, and that success is claimed only from the post-update
re-read.
"""
import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace as S
from unittest import mock

import pytest

from print_handy import firmware_update as fw
from print_handy import printers
from print_handy.bot import commands as bot_commands
from print_handy.bot import llm
from print_handy.hermes import spool_sync

ROOT = Path(__file__).parents[1]
PLUGIN_PATH = ROOT / "plugins/print-feedback/__init__.py"
spec = importlib.util.spec_from_file_location("print_feedback_firmware", PLUGIN_PATH)
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)


def entry(key):
    found = fw.printer_entry(key)
    assert found is not None, key
    return found


AD5X = entry("flashforge.ad5x")
GENERIC = entry("klipper.generic")
NEPTUNE4 = entry("elegoo.neptune4")
BAMBU_A1 = entry("bambulab.a1")

# A recorded-shape Moonraker update-status answer (ZMod-flavoured): one
# entry per Update Manager component, versions as Moonraker reports them.
STATUS = {
    "result": {
        "version_info": {
            "klipper": {"version": "v0.12.0-289", "remote_version": "v0.12.0-310"},
            "moonraker": {"version": "v0.9.3-10", "remote_version": "v0.9.3-10"},
            "zmod": {"version": "1.9.2", "remote_version": "1.9.2"},
            "fluidd": {"version": "1.31.3", "remote_version": "1.31.3"},
            "system": {"package_count": 0, "package_list": []},
        },
        "busy": False,
    }
}

STATUS_UP_TO_DATE = {
    "result": {
        "version_info": {
            "klipper": {"version": "v0.12.0-289", "remote_version": "v0.12.0-289"},
            "moonraker": {"version": "v0.9.3-10", "remote_version": "v0.9.3-10"},
        }
    }
}

SYSTEM_INFO_194 = {"result": {"system_info": {"distribution": {"name": "ZMod OS", "version": "1.9.4"}}}}
SYSTEM_INFO_180 = {"result": {"system_info": {"distribution": {"name": "ZMod OS", "version": "1.8.0"}}}}

STATE_STANDBY = {"result": {"status": {"print_stats": {"state": "standby"}}}}


class FakeTransport:
    """The spool_sync.moonraker call shape, recording every call.

    Routes by path: update status / refresh, system info, the MCU objects
    query and the print-state query each get their recorded shape; any
    other path is a test failure, so an unexpected request is loud.
    """

    def __init__(self, status=None, system_info=None, mcu=None, states=("standby",),
                 error=None, refresh_error=None):
        self.status = STATUS if status is None else status
        self.system_info = system_info
        self.mcu = mcu
        self.states = list(states)
        self.error = error
        self.refresh_error = refresh_error
        self.calls = []
        self.posts = []

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method == "POST":
            self.posts.append((path, body))
        if self.error is not None:
            raise self.error
        if path == fw.UPDATE_REFRESH_PATH:
            if self.refresh_error is not None:
                raise self.refresh_error
            return self.status
        if path == fw.UPDATE_STATUS_PATH:
            return self.status
        if path == fw.SYSTEM_INFO_PATH:
            if self.system_info is None:
                raise OSError("no system info")
            return self.system_info
        if path == fw.MCU_STATE_PATH:
            if self.mcu is None:
                raise OSError("no mcu object")
            return self.mcu
        if path == fw.PRINT_STATE_PATH:
            state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
            return {"result": {"status": {"print_stats": {"state": state}}}}
        raise AssertionError(f"unexpected transport call: {method} {path}")


# --------------------------------------------------------------------- parsing

def test_parse_reads_the_documented_version_info_shape():
    status = fw.parse_update_status(STATUS)
    klipper = status.component("klipper")
    assert (klipper.current, klipper.remote) == ("v0.12.0-289", "v0.12.0-310")
    assert klipper.update_available
    assert not status.component("moonraker").update_available
    assert [c.name for c in status.updates_available] == ["klipper"]
    assert status.component("KLIPPER") is klipper  # case-insensitive, like Klipper names


def test_parse_tolerates_field_variants_and_system_package_counts():
    payload = {"result": {"version_info": {
        "klipper": {"current_version": "1.0", "available_version": "1.1"},
        "mainsail": {"version": "2.0", "latest_version": "2.0"},
        "system": {"package_count": 3, "package_list": ["a", "b", "c"]}}}}
    status = fw.parse_update_status(payload)
    assert status.component("klipper").update_available
    assert not status.component("mainsail").update_available
    system = status.component("system")
    assert system.package_count == 3 and system.update_available


@pytest.mark.parametrize("payload", [
    None, "nope", 42, {}, {"result": None}, {"result": {}},
    {"result": {"version_info": None}}, {"result": {"version_info": {}}},
    {"result": {"version_info": {"klipper": "v0.12"}}},
])
def test_parse_rejects_malformed_responses(payload):
    with pytest.raises(fw.FirmwareError):
        fw.parse_update_status(payload)


# ------------------------------------------------------------------- reading

def test_plain_read_makes_exactly_one_get_and_never_refreshes():
    transport = FakeTransport()
    status = fw.fetch_status(transport)
    assert transport.calls == [("GET", fw.UPDATE_STATUS_PATH, None)]
    assert status.component("zmod").current == "1.9.2"


def test_check_path_refreshes_first_and_parses_the_refresh_response():
    transport = FakeTransport()
    status = fw.fetch_status(transport, refresh=True)
    assert transport.calls == [("POST", fw.UPDATE_REFRESH_PATH, None)]
    assert status.component("klipper").remote == "v0.12.0-310"


def test_check_path_falls_back_to_a_plain_read_when_refresh_fails():
    transport = FakeTransport(refresh_error=OSError("503 while printing"))
    status = fw.fetch_status(transport, refresh=True)
    assert transport.calls == [("POST", fw.UPDATE_REFRESH_PATH, None),
                               ("GET", fw.UPDATE_STATUS_PATH, None)]
    assert status.component("klipper").current == "v0.12.0-289"


def test_read_wraps_transport_failures_and_malformed_payloads():
    with pytest.raises(fw.FirmwareError):
        fw.fetch_status(FakeTransport(error=OSError("unreachable")))
    with pytest.raises(fw.FirmwareError):
        fw.fetch_status(FakeTransport(status={"result": {"status": {}}}))


# ------------------------------------------------------------------- report

def test_report_shows_current_to_available_and_the_update_command():
    text = fw.firmware_report(FakeTransport(), AD5X)
    assert "**klipper**: v0.12.0-289 -> v0.12.0-310 available" in text
    assert "`/print-firmware update klipper`" in text
    assert "moonraker: v0.9.3-10 (up to date)" in text
    assert "Not covered by this check" in text  # the ZMod base + stock layers are named


def test_report_says_up_to_date_when_nothing_is_available():
    text = fw.firmware_report(FakeTransport(status=STATUS_UP_TO_DATE), GENERIC)
    assert "Everything Print Handy can check here is up to date." in text
    assert "available" not in text.split("up to date.")[0].split("**Firmware")[-1]


def test_report_for_a_fork_is_information_only_with_no_update_command():
    text = fw.firmware_report(FakeTransport(), NEPTUNE4)
    assert "-> v0.12.0-310 available" in text
    assert "`/print-firmware update" not in text
    assert "does not apply updates on vendor forks" in text


def test_report_fails_soft_when_the_status_cannot_be_read():
    with pytest.raises(fw.FirmwareError):
        fw.firmware_report(FakeTransport(error=OSError("unreachable")), AD5X)


# ------------------------------------------------- ZMod version line (#562)

STATUS_NO_ZMOD = {"result": {"version_info": {
    "klipper": {"version": "v0.12.0-289", "remote_version": "v0.12.0-289"},
    "moonraker": {"version": "v0.9.3-10", "remote_version": "v0.9.3-10"}}}}

STATUS_ZMOD_NO_VERSION = {"result": {"version_info": {
    "klipper": {"version": "v0.12.0-289", "remote_version": "v0.12.0-289"},
    "zmod": {"remote_version": "1.9.2"}}}}


def test_report_names_the_zmod_version_when_moonraker_reports_it():
    text = fw.firmware_report(FakeTransport(), AD5X)
    assert "ZMod version: 1.9.2" in text


def test_report_says_zmod_version_unknown_when_the_component_is_absent():
    text = fw.firmware_report(FakeTransport(status=STATUS_NO_ZMOD), AD5X)
    assert "ZMod version: unknown (not reported by Moonraker)" in text


def test_report_says_zmod_version_unknown_when_no_current_version_is_reported():
    text = fw.firmware_report(FakeTransport(status=STATUS_ZMOD_NO_VERSION), AD5X)
    assert "ZMod version: unknown (not reported by Moonraker)" in text


def test_report_has_no_zmod_version_line_for_other_families():
    text = fw.firmware_report(FakeTransport(), GENERIC)
    assert "ZMod version:" not in text


# ------------------------------------------------------------- ZMod pairing

@pytest.mark.parametrize("os_version,zmod_version,expected", [
    ("1.9.4", "1.9.2", "match"),
    ("1.9.4", "1.9.0", "match"),
    ("1.8.0", "1.9.2", "mismatch"),
    ("2.0.1", "1.9.9", "mismatch"),
    (None, "1.9.2", "unknown"),
    ("1.9.4", None, "unknown"),
    ("1.9.4", "dev-build", "unknown"),
    ("", "", "unknown"),
])
def test_pairing_state(os_version, zmod_version, expected):
    assert fw.pairing_state(os_version, zmod_version) == expected


def test_report_names_a_pairing_mismatch_and_points_at_the_base_guide():
    transport = FakeTransport(system_info=SYSTEM_INFO_180)
    text = fw.firmware_report(transport, AD5X)
    assert "ZMod version mismatch" in text
    assert "`/print-firmware guide zmod`" in text


def test_report_does_not_claim_a_mismatch_when_the_os_version_is_unreadable():
    text = fw.firmware_report(FakeTransport(system_info=None), AD5X)
    assert "mismatch" not in text.casefold()


def test_report_notes_a_correct_pairing():
    text = fw.firmware_report(FakeTransport(system_info=SYSTEM_INFO_194), AD5X)
    assert "pair correctly" in text


# ------------------------------------------------------------------ MCU (Klipper 13)

STATUS_K13 = {"result": {"version_info": {
    "klipper": {"version": "v0.13.0-100", "remote_version": "v0.13.0-100"},
    "zmod": {"version": "1.9.2", "remote_version": "1.9.2"}}}}


def _mcu_payload(version):
    return {"result": {"status": {"mcu": {"mcu_version": version}}}}


@pytest.mark.parametrize("host,mcu,expected", [
    ("v0.13.0-100", "v0.13.0-90", "match"),       # same release triple: in step
    ("v0.13.0-100", "v0.12.0-289", "mismatch"),
    ("v0.12.0-289", "v0.11.0-1", "unknown"),      # only Klipper 13 is judged
    ("v0.13.0-100", None, "unknown"),
    ("v0.13.0-100", "garbage", "unknown"),
    (None, "v0.13.0-1", "unknown"),
])
def test_mcu_currency(host, mcu, expected):
    assert fw.mcu_currency(host, mcu) == expected


def test_report_names_an_mcu_mismatch_when_the_data_exposes_one():
    transport = FakeTransport(status=STATUS_K13, mcu=_mcu_payload("v0.12.0-289"))
    text = fw.firmware_report(transport, AD5X)
    assert "MCU out of step" in text and "UPDATE_MCU" in text
    # ZMod's bare UPDATE_MCU toggles the MCU firmware: on a Klipper 13
    # install it flashes the native build, the opposite of in step.
    # The report must name the explicit Klipper 13 form.
    assert "UPDATE_MCU FORCE=13" in text
    assert "run ZMod's `UPDATE_MCU` macro" not in text


def test_report_is_silent_about_the_mcu_when_it_cannot_be_read():
    text = fw.firmware_report(FakeTransport(status=STATUS_K13, mcu=None), AD5X)
    assert "MCU out of step" not in text


# ------------------------------------------------------------------ bot wiring

def test_bot_command_registered_with_help_and_llm_parity():
    assert bot_commands.by_name("print-firmware") is not None
    assert bot_commands.as_text("print-firmware", {}) == "/print-firmware"
    assert "/print-firmware" in bot_commands.help_text()
    # Reporting and the guides are read-only, so the chat model may name them;
    # applying an update may never come from the model.
    assert llm.allowed_command("/print-firmware") == "/print-firmware"
    assert llm.allowed_command("/print-firmware guide zmod") == "/print-firmware guide zmod"
    assert llm.allowed_command("/print-firmware update klipper") is None


class FakeAdapter:
    """Records everything the dispatch tries to show the user; has no printer API at all."""

    def __init__(self):
        self.sent = []
        self.clarified = []

    async def send(self, chat_id=None, content=None, metadata=None):
        self.sent.append(content)

    async def send_clarify(self, **kwargs):
        self.clarified.append(kwargs["question"])
        return S(success=True)


def _event(text):
    source = S(platform=S(value="discord"), profile="3dprinting", is_bot=False,
               profile_route_rejected=False, user_id="u", chat_id="c", thread_id=None)
    return S(source=source, text=text, internal=False)


def _gateway(adapter):
    return S(_is_user_authorized_for_source=lambda s: True, _delivery_adapter_for=lambda s: adapter,
             _session_key_for_source=lambda s: "session-key")


def _configured(key, connection):
    return mock.patch.object(printers, "current",
                             return_value=S(connection=connection, model=S(key=key)))


def test_dispatch_reports_the_firmware_state_with_a_check_refresh():
    adapter = FakeAdapter()
    transport = FakeTransport(system_info=SYSTEM_INFO_194)
    with _configured("flashforge.ad5x", "moonraker"), \
            mock.patch.object(spool_sync, "moonraker", transport):
        out = asyncio.run(plugin.firmware_dispatch(_event("/print-firmware"), _gateway(adapter)))
    assert out == {"action": "skip", "reason": "print firmware handled"}
    assert transport.calls[0] == ("POST", fw.UPDATE_REFRESH_PATH, None)
    assert "**klipper**: v0.12.0-289 -> v0.12.0-310 available" in adapter.sent[-1]


def test_dispatch_explains_non_moonraker_printers_without_calling_out():
    adapter = FakeAdapter()
    transport = FakeTransport()
    with _configured("bambulab.a1", "bambulab_lan"), \
            mock.patch.object(spool_sync, "moonraker", transport):
        out = asyncio.run(plugin.firmware_dispatch(_event("/print-firmware"), _gateway(adapter)))
    assert out["reason"] == "print firmware not applicable to this printer"
    assert transport.calls == []
    # Stage 4: a non-Moonraker printer gets its family's guidance, not an error.
    assert "Firmware updates for Bambu Lab A1" in adapter.sent[-1]
    assert "LAN-only mode" in adapter.sent[-1]


def test_dispatch_reports_an_unreachable_moonraker_and_sends_nothing():
    adapter = FakeAdapter()
    transport = FakeTransport(error=OSError("unreachable"))
    with _configured("flashforge.ad5x", "moonraker"), \
            mock.patch.object(spool_sync, "moonraker", transport):
        out = asyncio.run(plugin.firmware_dispatch(_event("/print-firmware"), _gateway(adapter)))
    assert out == {"action": "skip", "reason": "print firmware handled"}
    assert adapter.sent[-1] == fw.COULD_NOT_READ_TEXT
    assert all(method == "GET" or path == fw.UPDATE_REFRESH_PATH
               for method, path, _ in transport.calls)
    assert not any(path == fw.UPDATE_UPGRADE_PATH for _, path, _ in transport.calls)


def test_dispatch_ignores_other_text():
    assert asyncio.run(plugin.firmware_dispatch(_event("/print-firmwarex"),
                                                _gateway(FakeAdapter()))) is None
    assert asyncio.run(plugin.firmware_dispatch(_event("print-firmware"),
                                                _gateway(FakeAdapter()))) is None


# ------------------------------------------------------------------ stage 2: apply

STATUS_AFTER = {
    "result": {
        "version_info": {
            "klipper": {"version": "v0.12.0-310", "remote_version": "v0.12.0-310"},
            "moonraker": {"version": "v0.9.3-10", "remote_version": "v0.9.3-10"},
            "zmod": {"version": "1.9.2", "remote_version": "1.9.2"},
            "fluidd": {"version": "1.31.3", "remote_version": "1.31.3"},
            "system": {"package_count": 0, "package_list": []},
        }
    }
}


class ApplyTransport(FakeTransport):
    """FakeTransport that accepts the one upgrade POST and swaps the status."""

    def __init__(self, *args, post_status=None, upgrade_error=None,
                 drop_after_upgrade=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.post_status = post_status
        self.upgrade_error = upgrade_error
        self.drop_after_upgrade = drop_after_upgrade

    def __call__(self, method, path, body=None):
        if path == fw.UPDATE_UPGRADE_PATH:
            self.calls.append((method, path, body))
            self.posts.append((path, body))
            if self.upgrade_error is not None:
                raise self.upgrade_error
            if self.post_status is not None:
                self.status = self.post_status
            if self.drop_after_upgrade:
                self.error = OSError("printer went away")
            return {"result": "ok"}
        return super().__call__(method, path, body)


def _no_sleep(_seconds):
    return None


def test_eligibility_comes_from_the_registry_profile():
    assert fw.is_apply_eligible(AD5X)
    assert fw.is_apply_eligible(GENERIC)
    assert fw.is_apply_eligible(entry("sovol.sv06"))
    # Vendor Klipper forks: Moonraker printers, refused by vendor/family.
    for key in ("elegoo.neptune4", "elegoo.neptune4pro", "creality.k1", "creality.ender3v3",
                "qidi.q1pro", "anycubic.kobra2", "snapmaker.u1"):
        assert not fw.is_apply_eligible(entry(key)), key
        assert "vendor" in fw.apply_block_reason(entry(key)).casefold(), key
    # Non-Moonraker families: refused as non-Moonraker.
    for key in ("bambulab.a1", "prusa.mk4s", "elegoo_sdcp.centauricarbon",
                "octoprint.marlin", "flashforge_stock.ad5x"):
        assert not fw.is_apply_eligible(entry(key)), key
        assert "Moonraker" in fw.apply_block_reason(entry(key)), key
    assert fw.apply_block_reason(None) is not None


def test_prepare_resolves_the_component_against_the_live_status():
    transport = ApplyTransport(system_info=SYSTEM_INFO_194)
    prepared = fw.prepare_update(transport, AD5X, "KLIPPER")
    assert prepared.name == "klipper"
    assert prepared.component.current == "v0.12.0-289"
    assert prepared.component.remote == "v0.12.0-310"
    assert transport.posts == []  # prepare sends nothing


def test_prepare_refuses_a_component_the_printer_does_not_list():
    with pytest.raises(fw.FirmwareRefusal) as excinfo:
        fw.prepare_update(ApplyTransport(), AD5X, "nozzle")
    assert "does not list a component named" in str(excinfo.value)
    assert "Nothing was sent" in str(excinfo.value)


def test_prepare_refuses_a_component_with_no_update_available():
    with pytest.raises(fw.FirmwareRefusal) as excinfo:
        fw.prepare_update(ApplyTransport(), AD5X, "moonraker")
    assert "already up to date" in str(excinfo.value)


@pytest.mark.parametrize("state", ["printing", "paused"])
def test_prepare_refuses_while_printing_or_paused(state):
    transport = ApplyTransport(states=(state,))
    with pytest.raises(fw.FirmwareRefusal) as excinfo:
        fw.prepare_update(transport, AD5X, "klipper")
    assert state in str(excinfo.value) and "Nothing was sent" in str(excinfo.value)
    assert transport.posts == []


def test_prepare_refuses_an_unrecognised_state_fail_closed():
    with pytest.raises(fw.FirmwareRefusal):
        fw.prepare_update(ApplyTransport(states=("warming_up",)), AD5X, "klipper")


def test_prepare_refuses_the_neptune_4_before_reading_anything():
    transport = ApplyTransport()
    with pytest.raises(fw.FirmwareRefusal) as excinfo:
        fw.prepare_update(transport, NEPTUNE4, "klipper")
    assert "vendor fork" in str(excinfo.value)
    assert transport.calls == []  # eligibility is the profile's, checked first


def test_prepare_refuses_a_bambu_before_reading_anything():
    transport = ApplyTransport()
    with pytest.raises(fw.FirmwareRefusal) as excinfo:
        fw.prepare_update(transport, BAMBU_A1, "klipper")
    assert "does not connect over Moonraker" in str(excinfo.value)
    assert transport.calls == []


def test_prepare_is_blocked_by_a_zmod_pairing_mismatch():
    transport = ApplyTransport(system_info=SYSTEM_INFO_180)
    with pytest.raises(fw.FirmwareRefusal) as excinfo:
        fw.prepare_update(transport, AD5X, "klipper")
    assert "guide zmod" in str(excinfo.value)
    assert transport.posts == []


def test_update_prompt_shows_the_exact_version_change():
    prepared = fw.prepare_update(ApplyTransport(system_info=SYSTEM_INFO_194), AD5X, "klipper")
    text = fw.update_prompt_text(prepared)
    assert "**klipper**: v0.12.0-289 -> v0.12.0-310" in text
    assert "Only this one component updates" in text


def test_send_posts_exactly_one_upgrade_and_verifies_by_rereading():
    transport = ApplyTransport(system_info=SYSTEM_INFO_194, post_status=STATUS_AFTER)
    prepared = fw.prepare_update(transport, AD5X, "klipper")
    calls_before = len(transport.calls)
    message = fw.send_update(transport, prepared, verify_delay_s=0, sleep=_no_sleep)
    assert transport.posts == [(fw.UPDATE_UPGRADE_PATH, {"name": "klipper"})]
    verify_calls = transport.calls[calls_before:]
    assert ("GET", fw.UPDATE_STATUS_PATH, None) in verify_calls  # the post-update re-read
    assert message.startswith("Updated **klipper** to v0.12.0-310")
    assert "verified by re-reading" in message


def test_send_rechecks_idle_and_sends_nothing_when_a_print_started():
    # prepare sees standby; by send time the printer is printing.
    transport = ApplyTransport(states=("standby", "printing"), post_status=STATUS_AFTER)
    prepared = fw.prepare_update(transport, AD5X, "klipper")
    with pytest.raises(fw.FirmwareRefusal) as excinfo:
        fw.send_update(transport, prepared, verify_delay_s=0, sleep=_no_sleep)
    assert "re-check" in str(excinfo.value)
    assert transport.posts == []


@pytest.mark.parametrize("state", ["printing", "paused"])
def test_send_refuses_while_printing_or_paused(state):
    transport = ApplyTransport(states=(state,), post_status=STATUS_AFTER)
    prepared = fw.PreparedUpdate(component=fw.Component("klipper", "v0.12.0-289", "v0.12.0-310"))
    with pytest.raises(fw.FirmwareRefusal):
        fw.send_update(transport, prepared, verify_delay_s=0, sleep=_no_sleep)
    assert transport.posts == []


def test_send_reports_a_still_available_update_as_failed_not_success():
    transport = ApplyTransport(post_status=None)  # status never changes
    prepared = fw.PreparedUpdate(component=fw.Component("klipper", "v0.12.0-289", "v0.12.0-310"))
    message = fw.send_update(transport, prepared, verify_attempts=2,
                             verify_delay_s=0, sleep=_no_sleep)
    assert "still shows v0.12.0-310 as available" in message
    assert "failed" in message and not message.startswith("Updated")


def _status_with(component, entry):
    return {"result": {"version_info": {component: entry}}}


def test_send_does_not_verify_when_the_remote_version_turns_unknown():
    # Prepared fluidd 1.0 -> 2.0; after the upgrade the printer still runs
    # 1.0 and reports "?" for the latest version. "?" normalizes to no
    # remote, which reads as "no update available" — that alone must not
    # verify an update whose confirmed target was never installed (P2-229).
    transport = ApplyTransport(
        post_status=_status_with("fluidd", {"version": "1.0", "remote_version": "?"}))
    prepared = fw.PreparedUpdate(component=fw.Component("fluidd", "1.0", "2.0"))
    message = fw.send_update(transport, prepared, verify_attempts=2,
                             verify_delay_s=0, sleep=_no_sleep)
    assert not message.startswith("Updated")
    assert "verified by re-reading" not in message
    assert "cannot verify" in message
    assert "1.0" in message


def test_send_verifies_when_the_installed_version_reaches_the_target():
    transport = ApplyTransport(
        post_status=_status_with("fluidd", {"version": "2.0", "remote_version": "2.0"}))
    prepared = fw.PreparedUpdate(component=fw.Component("fluidd", "1.0", "2.0"))
    message = fw.send_update(transport, prepared, verify_attempts=2,
                             verify_delay_s=0, sleep=_no_sleep)
    assert message.startswith("Updated **fluidd** to 2.0")
    assert "verified by re-reading" in message


def test_send_verifies_on_the_target_even_when_the_remote_turns_unknown():
    # The target is installed; that the printer can no longer name a latest
    # version does not undo the proof.
    transport = ApplyTransport(
        post_status=_status_with("fluidd", {"version": "2.0", "remote_version": "?"}))
    prepared = fw.PreparedUpdate(component=fw.Component("fluidd", "1.0", "2.0"))
    message = fw.send_update(transport, prepared, verify_attempts=2,
                             verify_delay_s=0, sleep=_no_sleep)
    assert message.startswith("Updated **fluidd** to 2.0")
    assert "verified by re-reading" in message


def test_send_does_not_verify_a_version_that_is_not_the_confirmed_target():
    # No update shows as available, but the installed version is neither
    # the old one nor the confirmed target — not a verified success.
    transport = ApplyTransport(
        post_status=_status_with("fluidd", {"version": "1.5", "remote_version": "1.5"}))
    prepared = fw.PreparedUpdate(component=fw.Component("fluidd", "1.0", "2.0"))
    message = fw.send_update(transport, prepared, verify_attempts=2,
                             verify_delay_s=0, sleep=_no_sleep)
    assert not message.startswith("Updated")
    assert "verified by re-reading" not in message
    assert "2.0" in message and "1.5" in message


def test_send_still_verifies_a_system_package_update_by_its_count():
    # The system component reports packages, not versions: its proof stays
    # the package count clearing.
    transport = ApplyTransport(
        post_status=_status_with("system", {"package_count": 0, "package_list": []}))
    prepared = fw.PreparedUpdate(
        component=fw.Component("system", None, None, package_count=3))
    message = fw.send_update(transport, prepared, verify_attempts=2,
                             verify_delay_s=0, sleep=_no_sleep)
    assert message.startswith("Updated **system**")
    assert "verified by re-reading" in message


def test_send_reports_a_printer_that_never_comes_back_as_unverified():
    transport = ApplyTransport(drop_after_upgrade=True)
    prepared = fw.PreparedUpdate(component=fw.Component("klipper", "v0.12.0-289", "v0.12.0-310"))
    message = fw.send_update(transport, prepared, verify_attempts=2,
                             verify_delay_s=0, sleep=_no_sleep)
    assert "did not answer" in message and "cannot verify" in message
    assert not message.startswith("Updated")


def test_send_reports_a_failed_post_as_uncertain():
    # P2-224: a transport failure on the upgrade POST is uncertain — the
    # request may have landed — so send_update verifies by re-reading
    # instead of raising at once, and never resends the POST. Here the
    # re-read shows the update was not applied.
    transport = ApplyTransport(upgrade_error=OSError("connection reset"))
    prepared = fw.PreparedUpdate(component=fw.Component("klipper", "v0.12.0-289", "v0.12.0-310"))
    message = fw.send_update(transport, prepared, verify_attempts=2,
                             verify_delay_s=0, sleep=_no_sleep)
    assert len(transport.posts) == 1
    assert not message.startswith("Updated")
    assert "still shows v0.12.0-310 as available" in message
    assert "not known whether the printer started updating" in message


class _TimeoutAfterLandingTransport(FakeTransport):
    """The upgrade POST times out client-side after Moonraker applied it
    (Moonraker awaits the update before answering, P2-224)."""

    def __init__(self, post_status):
        super().__init__()
        self._post_status = post_status

    def __call__(self, method, path, body=None):
        if path == fw.UPDATE_UPGRADE_PATH:
            self.calls.append((method, path, body))
            self.posts.append((path, body))
            self.status = self._post_status
            raise TimeoutError("timed out")
        return super().__call__(method, path, body)


def test_send_verifies_an_update_whose_post_timed_out():
    transport = _TimeoutAfterLandingTransport(STATUS_AFTER)
    prepared = fw.PreparedUpdate(component=fw.Component("klipper", "v0.12.0-289", "v0.12.0-310"))
    message = fw.send_update(transport, prepared, verify_attempts=2,
                             verify_delay_s=0, sleep=_no_sleep)
    assert len(transport.posts) == 1  # verified by re-reading, never resent
    assert message.startswith("Updated **klipper** to v0.12.0-310")
    assert "verified by re-reading" in message


def test_send_still_raises_for_a_post_the_printer_refused():
    # An HTTP error means the printer answered and rejected the request:
    # a definite failure, not an uncertain outcome — no verification reads.
    import urllib.error
    transport = ApplyTransport(upgrade_error=urllib.error.HTTPError(
        "http://printer.test", 400, "Bad Request", {}, None))
    prepared = fw.PreparedUpdate(component=fw.Component("klipper", "v0.12.0-289", "v0.12.0-310"))
    with pytest.raises(fw.FirmwareError):
        fw.send_update(transport, prepared, verify_delay_s=0, sleep=_no_sleep)
    assert len(transport.posts) == 1
    assert ("GET", fw.UPDATE_STATUS_PATH, None) not in transport.calls


class _TimeoutRecordingTransport(ApplyTransport):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.timeouts = []

    def __call__(self, method, path, body=None, timeout=None):
        self.timeouts.append((path, timeout))
        return super().__call__(method, path, body)


def test_send_gives_the_upgrade_post_an_upgrade_sized_timeout():
    # P2-224: Moonraker awaits the whole update before answering the POST,
    # so the read-sized 15s client timeout must not bound it.
    transport = _TimeoutRecordingTransport(post_status=STATUS_AFTER)
    prepared = fw.PreparedUpdate(component=fw.Component("klipper", "v0.12.0-289", "v0.12.0-310"))
    message = fw.send_update(transport, prepared, verify_delay_s=0, sleep=_no_sleep)
    assert message.startswith("Updated **klipper**")
    post_timeouts = [t for path, t in transport.timeouts if path == fw.UPDATE_UPGRADE_PATH]
    assert post_timeouts == [fw.UPGRADE_TIMEOUT_S]
    assert fw.UPGRADE_TIMEOUT_S > 15


def test_send_does_not_verify_an_entry_with_no_version_evidence():
    # P2-225: an incomplete post-update entry (no versions at all) is not
    # positive evidence the confirmed target was reached.
    transport = ApplyTransport(post_status=_status_with("klipper", {}))
    prepared = fw.PreparedUpdate(component=fw.Component("klipper", "v0.12.0-289", "v0.12.0-310"))
    message = fw.send_update(transport, prepared, verify_attempts=2,
                             verify_delay_s=0, sleep=_no_sleep)
    assert not message.startswith("Updated")
    assert "verified by re-reading" not in message
    assert "cannot verify" in message


# ------------------------------------------------------------- dispatch: update

async def _confirm_yes(*args, **kwargs):
    return fw.UPDATE_LABEL


async def _confirm_no(*args, **kwargs):
    return fw.CANCEL_LABEL


def test_dispatch_update_applies_one_component_after_confirmation():
    adapter = FakeAdapter()
    transport = ApplyTransport(system_info=SYSTEM_INFO_194, post_status=STATUS_AFTER)
    with _configured("flashforge.ad5x", "moonraker"), \
            mock.patch.object(spool_sync, "moonraker", transport), \
            mock.patch.object(plugin, "_confirm", _confirm_yes):
        out = asyncio.run(plugin.firmware_dispatch(
            _event("/print-firmware update klipper"), _gateway(adapter)))
    assert out == {"action": "skip", "reason": "print firmware update handled"}
    assert transport.posts == [(fw.UPDATE_UPGRADE_PATH, {"name": "klipper"})]
    assert adapter.sent[-1].startswith("Updated **klipper** to v0.12.0-310")


def test_dispatch_update_sends_nothing_when_the_operator_cancels():
    adapter = FakeAdapter()
    transport = ApplyTransport(system_info=SYSTEM_INFO_194, post_status=STATUS_AFTER)
    with _configured("flashforge.ad5x", "moonraker"), \
            mock.patch.object(spool_sync, "moonraker", transport), \
            mock.patch.object(plugin, "_confirm", _confirm_no):
        asyncio.run(plugin.firmware_dispatch(
            _event("/print-firmware update klipper"), _gateway(adapter)))
    assert transport.posts == []
    assert adapter.sent[-1] == "Update not applied. Nothing sent."


def test_dispatch_update_refuses_the_neptune_4_without_any_call():
    adapter = FakeAdapter()
    transport = ApplyTransport()
    with _configured("elegoo.neptune4", "moonraker"), \
            mock.patch.object(spool_sync, "moonraker", transport), \
            mock.patch.object(plugin, "_confirm", _confirm_yes):
        out = asyncio.run(plugin.firmware_dispatch(
            _event("/print-firmware update klipper"), _gateway(adapter)))
    assert out["reason"] == "print firmware update refused for this printer"
    assert transport.calls == []
    assert "vendor fork" in adapter.sent[-1]


# ------------------------------------------------------------------ stage 3: guides

def test_zmod_guide_covers_zflash_md5_pairing_and_verification():
    text = fw.guide_text("zmod", AD5X)
    assert "ZFLASH" in text and "MD5" in text and "USB" in text
    assert "first two numbers" in text
    assert "mod_data" in text
    assert text.rstrip().endswith(fw.GUIDE_VERIFY_LINE)
    assert "`/print-firmware`" in fw.GUIDE_VERIFY_LINE


def test_zmod_guide_routes_unappliable_components_to_software_update():
    # ZMod's "Updating the Mod" (wiki Setup.md, tag 1.7.6) finishes in
    # Fluidd/Mainsail, Settings, Software Update, one module at a time. The
    # components Moonraker reports `?` for are refused by `/print-firmware
    # update`, so the guide must not send every component there (#1274).
    text = fw.guide_text("zmod", AD5X)
    assert "`/print-firmware update <component>`" in text
    assert "Software Update" in text
    assert "Fluidd or Mainsail" in text
    assert "`?`" in text
    assert "one by one" in text
    step2 = text.split("\n2. ", 1)[1].split("\n3. ", 1)[0]
    assert step2.index("/print-firmware update") < step2.index("only be updated from the Software Update")


def test_zmod_guide_puts_the_routine_no_usb_update_first():
    # ZMod's "Updating the Mod" (wiki Setup): the normal path is Software
    # Update in Fluidd/Mainsail with no USB drive; USB is required only when
    # ZMod shows "Update Z-Mod via USB". First-check errors are expected.
    text = fw.guide_text("zmod", AD5X)
    step1 = text.split("\n1. ", 1)[1].split("\n2. ", 1)[0]
    assert "Check for updates" in step1
    assert '"Update Z-Mod via USB"' in step1 and "even if you updated recently" in step1
    step2 = text.split("\n2. ", 1)[1].split("\n3. ", 1)[0]
    assert "no USB drive" in step2
    assert "errors on the first check are normal" in step2
    assert "restore and update each module one by one" in step2
    assert "reboots" in step2
    assert text.index("Routine update") < text.index("`ZFLASH`")


def test_zmod_guide_usb_steps_follow_zmods_drive_rules():
    # ZFLASH downloads to the USB drive and installs on reboot, so the drive
    # must be in; without the native screen it goes in before power on.
    # A manual install copies the file, not unpacked, to the root, power off.
    text = fw.guide_text("zmod", AD5X)
    step3 = text.split("\n3. ", 1)[1].split("\n4. ", 1)[0]
    assert "FAT32 (MBR)" in step3
    assert "**before** you power on" in step3 and "native screen" in step3
    assert "downloads it to the USB drive" in step3
    step4 = text.split("\n4. ", 1)[1].split("\n5. ", 1)[0]
    assert "without unpacking" in step4 and "power the printer off" in step4
    assert "without removing the drive" in step4


def test_zmod_guide_and_mismatch_report_say_where_the_os_version_is():
    text = fw.guide_text("zmod", AD5X)
    step5 = text.split("\n5. ", 1)[1].split("\n6. ", 1)[0]
    assert "System" in step5 and "Distribution" in step5
    assert "first two numbers" in step5 and "instability" in step5
    report = fw.firmware_report(FakeTransport(system_info=SYSTEM_INFO_180), AD5X)
    assert "System, Distribution" in report and "instability" in report


def test_zmod_guide_closing_mcu_step_names_force_13_never_bare():
    # A bare UPDATE_MCU toggles the MCU firmware (back to native on a
    # Klipper 13 install); the closing step must name FORCE=13 and be
    # conditional on the report, matching the MCU-mismatch report line.
    text = fw.guide_text("zmod", AD5X)
    step6 = text.split("\n6. ", 1)[1].split(fw.GUIDE_VERIFY_LINE, 1)[0]
    assert "`UPDATE_MCU FORCE=13`" in step6
    assert "Klipper 13" in step6 and "out of step" in step6
    assert "`/print-firmware` reports" in step6
    assert step6.index("FORCE=13") < step6.index("bare `UPDATE_MCU`")
    for line in text.splitlines():
        if "`UPDATE_MCU`" in line:
            assert "bare" in line or "without" in line, line
    # No ZFLASH2 / ZFLASH3: ZMod's sources at 1.7.6 (docs in every
    # language, the macro configs and their history) name only ZFLASH.
    assert "ZFLASH2" not in text and "ZFLASH3" not in text


def test_stock_guide_covers_every_documented_pre_and_post_step():
    text = fw.guide_text("stock", AD5X)
    for needle in ("DISABLE_PLUGIN", "recommend", "timelapse", "notify",
                   "UPDATE_MCU", "RESET_PASSWD", "AD5X-enable-zmod.tgz",
                   "IFS Update", "Firmware Update"):
        assert needle in text, needle
    assert text.rstrip().endswith(fw.GUIDE_VERIFY_LINE)


def test_guides_never_claim_print_handy_performed_the_manual_steps():
    for which, who in (("zmod", AD5X), ("stock", AD5X),
                       ("stock", entry("flashforge_stock.ad5x"))):
        text = fw.guide_text(which, who)
        assert "Print Handy cannot do these steps for you" in text
        for claim in ("Print Handy has updated", "Print Handy updated",
                      "we have updated", "I updated your"):
            assert claim not in text, (which, claim)


def test_guides_say_so_plainly_for_printers_they_do_not_cover():
    text = fw.guide_text("zmod", BAMBU_A1)
    assert "not one of those" in text and "Nothing was sent" in text
    assert "stock" in fw.guide_text("stock", entry("flashforge_stock.ad5x"))
    assert "not one of those" in fw.guide_text("stock", GENERIC)
    assert "Guides exist" in fw.guide_text("everything", AD5X)


def test_dispatch_guide_stock_works_on_a_stock_printer_without_any_call():
    adapter = FakeAdapter()
    transport = FakeTransport()
    with _configured("flashforge_stock.ad5x", "flashforge_stock"), \
            mock.patch.object(spool_sync, "moonraker", transport):
        out = asyncio.run(plugin.firmware_dispatch(
            _event("/print-firmware guide stock"), _gateway(adapter)))
    assert out == {"action": "skip", "reason": "print firmware guide shown"}
    assert transport.calls == []
    assert "RESET_PASSWD" in adapter.sent[-1]


def test_dispatch_guide_zmod_on_the_ad5x():
    adapter = FakeAdapter()
    transport = FakeTransport()
    with _configured("flashforge.ad5x", "moonraker"), \
            mock.patch.object(spool_sync, "moonraker", transport):
        out = asyncio.run(plugin.firmware_dispatch(
            _event("/print-firmware guide zmod"), _gateway(adapter)))
    assert out["reason"] == "print firmware guide shown"
    assert "ZFLASH" in adapter.sent[-1]
    assert transport.calls == []


# ------------------------------------------------------------------ stage 4: guidance

def test_every_registry_family_is_eligible_or_has_guidance():
    # Iterates the real registry: a future family that is neither eligible
    # for apply nor covered by stage 4 guidance fails here loudly instead
    # of being silently dropped.
    from print_handy import knowledge
    registry = knowledge.default_registry()
    assert len(registry.printers) >= 50
    for printer in registry.printers:
        assert fw.is_apply_eligible(printer) or fw.guidance_for(printer) is not None, \
            f"{printer.key} has neither apply eligibility nor stage 4 guidance"


def test_guidance_covers_each_stage_4_family_with_steps_and_a_reason():
    cases = {
        "elegoo.neptune4": ("ELEGOO_UPDATE_DIR", "will make the machine not work properly"),
        "creality.k1": ("USB", "vendor"),
        "qidi.q1pro": ("USB", "eMMC"),
        "anycubic.kobra2": ("update.swu", "responsible apply path"),
        "snapmaker.u1": ("printer's screen", "vendor fork"),
        "flashforge_stock.ad5x": ("Firmware Update", "stock LAN API"),
        "bambulab.a1": ("SD card", "LAN protocol"),
        "prusa.mk4s": (".bbf", "PrusaLink API"),
        "elegoo_sdcp.centauricarbon": ("update.bin", "SDCP"),
        "octoprint.marlin": ("Firmware Updater", "serial"),
    }
    for key, (step_needle, why_needle) in cases.items():
        guidance = fw.guidance_for(entry(key))
        assert guidance is not None, key
        assert step_needle in guidance.manual_steps, key
        assert why_needle in guidance.why_no_apply, key
        text = fw.guidance_text(entry(key))
        assert guidance.title in text and "Nothing was sent to the printer." in text


def test_guidance_is_none_for_eligible_printers():
    assert fw.guidance_for(AD5X) is None
    assert fw.guidance_for(GENERIC) is None
    assert fw.guidance_for(None) is None


def test_dispatch_bare_on_a_prusa_returns_its_guidance_without_any_call():
    adapter = FakeAdapter()
    transport = FakeTransport()
    with _configured("prusa.mk4s", "prusalink"), \
            mock.patch.object(spool_sync, "moonraker", transport):
        out = asyncio.run(plugin.firmware_dispatch(_event("/print-firmware"), _gateway(adapter)))
    assert out["reason"] == "print firmware not applicable to this printer"
    assert transport.calls == []
    assert ".bbf" in adapter.sent[-1]


# ----------------------------------------------- ZMod version from the OS info

# Recorded from a live AD5X on ZMod 1.7.5-2 (2026-10-29): the Update Manager's
# git_repo components have no usable checkout on the printer and report "?".
STATUS_LIVE_ZMOD_UNREADABLE = {"result": {"version_info": {
    "Z-Mod": {"configured_type": "git_repo", "version": "?", "remote_version": "?",
              "full_version_string": "?", "is_valid": False},
    "klippy": {"configured_type": "git_repo", "version": "?", "remote_version": "?"},
    "fluidd": {"configured_type": "web", "version": "v1.37.17", "remote_version": "?"}}}}
SYSTEM_INFO_LIVE_ZMOD = {"result": {"system_info": {"distribution": {
    "name": "zmod 1.7.5 -> 1.7.5-2", "id": "buildroot", "version": "1.7.5",
    "codename": "AD5X 3.1.7"}}}}


def test_question_mark_versions_are_unknown_not_versions_or_updates():
    status = fw.parse_update_status(STATUS_LIVE_ZMOD_UNREADABLE)
    assert status.component("klippy").current is None
    assert status.component("fluidd").remote is None
    assert status.updates_available == ()


def test_component_lookup_matches_zmods_own_component_name():
    status = fw.parse_update_status(STATUS_LIVE_ZMOD_UNREADABLE)
    assert status.component("zmod").name == "Z-Mod"


@pytest.mark.parametrize("name,expected", [
    ("zmod 1.7.5 -> 1.7.5-2", "1.7.5-2"),
    ("zmod 1.7.5", "1.7.5"),
    ("ZMod OS", "1.9.4"),
    ("Debian GNU/Linux", None),
])
def test_zmod_version_from_system_info(name, expected):
    payload = {"result": {"system_info": {"distribution": {"name": name, "version": "1.9.4"}}}}
    assert fw.zmod_version_from_system_info(payload) == expected


def test_unreadable_zmod_component_is_filled_from_the_os_info():
    transport = FakeTransport(status=STATUS_LIVE_ZMOD_UNREADABLE, system_info=SYSTEM_INFO_LIVE_ZMOD)
    zmod = fw.fetch_status(transport).component("zmod")
    assert (zmod.current, zmod.source) == ("1.7.5-2", "system_info")


def test_refresh_response_also_gets_the_zmod_os_info_fallback():
    # P1-113: the /print-firmware handler always reads with refresh=True;
    # a successful refresh must get the same OS-info enrichment as the
    # plain read, or the ZMod version stays "unknown" on the live path.
    transport = FakeTransport(status=STATUS_LIVE_ZMOD_UNREADABLE, system_info=SYSTEM_INFO_LIVE_ZMOD)
    zmod = fw.fetch_status(transport, refresh=True).component("zmod")
    assert (zmod.current, zmod.source) == ("1.7.5-2", "system_info")
    assert transport.calls[0] == ("POST", fw.UPDATE_REFRESH_PATH, None)


def test_os_info_is_only_read_when_the_zmod_version_is_unreadable():
    transport = FakeTransport(system_info=SYSTEM_INFO_LIVE_ZMOD)
    assert fw.fetch_status(transport).component("zmod").current == "1.9.2"
    assert [path for _m, path, _b in transport.calls] == [fw.UPDATE_STATUS_PATH]


def test_unreadable_os_info_keeps_the_version_unknown():
    transport = FakeTransport(status=STATUS_LIVE_ZMOD_UNREADABLE, system_info=None)
    assert fw.fetch_status(transport).component("zmod").current is None


def test_report_names_the_os_info_version_and_claims_no_pairing():
    transport = FakeTransport(status=STATUS_LIVE_ZMOD_UNREADABLE, system_info=SYSTEM_INFO_LIVE_ZMOD)
    text = fw.firmware_report(transport, AD5X)
    assert "ZMod version: 1.7.5-2 (from the printer's OS info" in text
    assert "pair correctly" not in text and "mismatch" not in text
    # Every remote is "?": the report must not claim anything is up to date.
    assert "up to date" not in text
    assert "- Z-Mod: 1.7.5-2 (latest version not reported)" in text
    assert "did not report the latest version for Z-Mod, klippy, fluidd" in text


def test_update_refuses_a_component_whose_latest_version_is_unknown():
    transport = FakeTransport(status=STATUS_LIVE_ZMOD_UNREADABLE, system_info=SYSTEM_INFO_LIVE_ZMOD)
    with pytest.raises(fw.FirmwareRefusal, match="does not report the latest version of .*Z-Mod.*Nothing was sent"):
        fw.prepare_update(transport, AD5X, "zmod")


# ------------------------------------------- live ZMod read, 2026-11-11 (#1295)

# Recorded from the owner's AD5X on ZMod 1.7.5-2 (2026-11-11): Moonraker now
# reads every checkout, lists `moonraker` and `klipper` as empty placeholders
# beside ZMod's real `moon` and `klippy`, and marks Z-Mod invalid because
# HelixScreen's installer appended a section to ZMod's own moonraker.conf.
DIRTY_WARNING = "Repo is dirty.  Detected the following modified files: ['moonraker.conf']"
STATUS_LIVE_ZMOD_DIRTY = {"result": {
    "busy": False, "github_rate_limit": 60, "github_requests_remaining": 57,
    "github_limit_reset_time": 1794435078.0,
    "version_info": {
        "moonraker": {}, "klipper": {},
        "Z-Mod": {"configured_type": "git_repo", "channel": "dev", "version": "1.7.5-2",
                  "remote_version": "1.7.6-17", "is_valid": False, "is_dirty": True,
                  "warnings": [DIRTY_WARNING], "anomalies": []},
        "klippy": {"configured_type": "git_repo", "version": "13.761-5",
                   "remote_version": "13.761-7", "is_valid": True, "warnings": [],
                   "anomalies": ["Repo has untracked source files: ['klippy/extras/quickswap.py']"]},
        "fluidd": {"configured_type": "web", "version": "v1.37.17",
                   "remote_version": "v1.37.22", "is_valid": True, "warnings": []},
        "recommend": {"configured_type": "git_repo", "version": "1.1.7-0",
                      "remote_version": "1.1.7-0", "is_valid": True, "warnings": []}}}}
SYSTEM_INFO_175 = {"result": {"system_info": {"distribution": {
    "name": "zmod 1.7.5 -> 1.7.5-2", "version": "1.7.5"}}}}


def test_issue_1295_empty_placeholder_entries_are_not_components():
    status = fw.parse_update_status(STATUS_LIVE_ZMOD_DIRTY)
    assert [c.name for c in status.components] == ["Z-Mod", "klippy", "fluidd", "recommend"]
    text = fw.firmware_report(FakeTransport(STATUS_LIVE_ZMOD_DIRTY, SYSTEM_INFO_175), AD5X)
    assert "version not reported" not in text
    assert "did not report the latest version" not in text


def test_issue_1295_an_invalid_zmod_repo_is_reported_with_the_fix_not_offered():
    text = fw.firmware_report(FakeTransport(STATUS_LIVE_ZMOD_DIRTY, SYSTEM_INFO_175), AD5X)
    zline = next(line for line in text.splitlines() if line.startswith("- **Z-Mod**"))
    assert "1.7.5-2 -> 1.7.6-17 available" in zline
    assert "Moonraker will not apply it" in zline and "moonraker.conf" in zline
    assert "/print-firmware update Z-Mod" not in text
    assert "`mod_data/user.moonraker.conf`" in text and "HelixScreen" in text
    # A valid component on the same printer is still offered.
    assert "`/print-firmware update klippy`" in text


def test_issue_1295_an_invalid_component_is_refused_before_anything_is_sent():
    transport = ApplyTransport(STATUS_LIVE_ZMOD_DIRTY, SYSTEM_INFO_175)
    with pytest.raises(fw.FirmwareRefusal) as refused:
        fw.prepare_update(transport, AD5X, "zmod")
    assert "invalid" in str(refused.value) and "user.moonraker.conf" in str(refused.value)
    assert "Nothing was sent" in str(refused.value)
    assert transport.posts == []
    assert fw.prepare_update(transport, AD5X, "klippy").name == "klippy"
    assert transport.posts == []


def test_issue_1295_a_spent_github_budget_is_named_with_its_reset_time():
    payload = {"result": {"github_requests_remaining": 0,
                          "github_limit_reset_time": 1794435078.0,
                          "version_info": {"klippy": {"version": "13.761-5",
                                                      "remote_version": "?"}}}}
    text = fw.firmware_report(FakeTransport(payload, SYSTEM_INFO_175), GENERIC)
    assert "GitHub's hourly limit" in text and "UTC" in text
    # With budget left, "?" is not blamed on the limit.
    payload["result"]["github_requests_remaining"] = 12
    assert "hourly limit" not in fw.firmware_report(FakeTransport(payload), GENERIC)
