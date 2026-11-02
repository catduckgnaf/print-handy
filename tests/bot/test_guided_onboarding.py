"""Guided printer onboarding (#537): add a printer to a configured install by
walkthrough — model, name, per-domain fields, a live probe step, review,
apply through the roster writer; secrets never collected or stored."""
import json

import pytest

from print_handy.bot import commands, guided

EXISTING = {"printers": [{"id": "ad5x", "model": "flashforge.ad5x",
                          "name": "Workshop AD5X",
                          "moonraker_url": "http://ad5x.local:7125"}]}


def store(tmp_path):
    return guided.SetupStore(tmp_path / "guided-setup.json")


_CLOCK = [10_000]


def say(store_obj, text, key="k", **kwargs):
    kwargs.setdefault("report", lambda: "REPORT")
    if "now" not in kwargs:  # one monotonic fake clock: sessions never expire mid-test
        _CLOCK[0] += 10
        kwargs["now"] = _CLOCK[0]
    return guided.handle_text(key, text, store=store_obj, **kwargs)


def loader():
    return {"printers": [dict(entry) for entry in EXISTING["printers"]]}


def ok_probe(answers):
    return guided.ProbeOutcome(True, "the printer answered")


def _review_session(probe):
    return {
        "status": "onboard_review",
        "ob_step": 0,
        "ob_keys": ["model", "name"],
        "ob_answers": {
            "name": "Bench",
            "printer_id": "bench",
            "model": "flashforge.ad5x",
            "moonraker_url": "http://printer.local:7125",
            "probe": probe,
        },
    }


def test_review_shows_moonraker_geometry_without_rewriting_or_writing():
    shown = _review_session({
        "ok": True, "summary": "Moonraker answered",
        "geometry": {"nozzle_mm": 0.4, "bed_mm": [220, 220], "max_z_mm": 220},
    })
    text = guided._onboard_review_text(shown)
    assert "nozzle 0.4 mm" in text and "bed 220 x 220 mm" in text and "max Z 220 mm" in text
    assert "not a tuned profile" in text
    assert "Geometry:" not in text
    assert "Nothing has been written yet" in text
    assert shown["ob_answers"]["model"] == "flashforge.ad5x"

    mismatch = _review_session({
        "ok": True, "summary": "Moonraker answered",
        "geometry": {"nozzle_mm": 1.2, "bed_mm": [100, 100], "max_z_mm": 80},
    })
    warned = guided._onboard_review_text(mismatch)
    assert "nozzle 1.2" in warned and "generic Klipper" in warned and "Apply still uses" in warned
    assert mismatch["ob_answers"]["model"] == "flashforge.ad5x"
    entry = guided._onboard_entry(mismatch["ob_answers"])
    assert entry["model"] == "flashforge.ad5x"
    assert "nozzle_mm" not in entry

    failed = _review_session({
        "ok": True, "summary": "Moonraker answered",
        "geometry_error": "the printer config did not include nozzle, bed, or Z",
    })
    named = guided._onboard_review_text(failed)
    assert "could not read nozzle, bed, or Z" in named
    assert "Nothing has been written yet" in named
    assert "Geometry:" not in named



def start(store_obj, **kwargs):
    kwargs.setdefault("config_loader", loader)
    return say(store_obj, "/print-handy add-printer", **kwargs)


def walk(store_obj, answers, **kwargs):
    reply = None
    for answer in answers:
        reply = say(store_obj, answer, **kwargs)
    return reply


# ---------------------------------------------------------------------- start and guards

def test_add_printer_is_registered_and_in_help(tmp_path):
    assert commands.as_text("print-handy", {"action": "add-printer"}) == "/print-handy add-printer"
    reply = say(store(tmp_path), "/print-handy help")
    assert "add-printer" in reply


def test_start_asks_for_the_model_first(tmp_path):
    st = store(tmp_path)
    reply = start(st, now=1000)
    assert "Add a printer (1 of 2): Printer model" in reply
    assert "nothing is written until you approve" in reply
    assert "Secrets are never collected" in reply


def test_start_refuses_when_the_config_does_not_load(tmp_path):
    st = store(tmp_path)
    def broken():
        raise ValueError("bad config")
    reply = say(st, "/print-handy add-printer", config_loader=broken)
    assert "cannot start" in reply and "bad config" in reply
    assert st.get("k") is None


def test_start_refuses_up_front_when_the_settings_file_is_not_writable(tmp_path, monkeypatch):
    # Docker: /app/config/docker.yaml is baked into the image and root-owned, so
    # the apply at the end of the walkthrough could never succeed.
    from print_handy import roster_write
    monkeypatch.setattr(roster_write, "write_refusal",
                        lambda environ=None: "REFUSED: set PRINT_HANDY_CONFIG to a writable file")
    st = store(tmp_path)
    reply = start(st)
    assert "cannot start" in reply and "PRINT_HANDY_CONFIG" in reply
    assert st.get("k") is None


def test_onboarding_and_setup_refuse_each_other(tmp_path):
    st = store(tmp_path)
    start(st, now=1000)
    reply = say(st, "/print-handy setup")
    assert "add-printer" in reply  # setup points at the running onboarding
    st2 = store(tmp_path / "b")
    say(st2, "/print-handy setup", now=1000)
    reply = say(st2, "/print-handy add-printer", config_loader=loader)
    assert "guided setup is in progress" in reply


# ---------------------------------------------------------------------- walkthroughs

def test_moonraker_walkthrough_probe_then_apply(tmp_path):
    st = store(tmp_path)
    applied = []
    start(st, now=1000)
    walk(st, ["creality.k1", "K1 Bench"], probe_runner=ok_probe)
    reply = say(st, "http://k1.local:7125", probe_runner=ok_probe)
    assert "Live connection test" in reply and "`test`" in reply
    reply = say(st, "test", probe_runner=ok_probe)
    assert "Ready to add this printer" in reply
    assert "Live test: **passed**" in reply and "the printer answered" in reply
    assert "K1 Bench" in reply and "`k1-bench`" in reply
    reply = say(st, "apply", onboard_apply=lambda answers: applied.append(answers) or "written.")
    assert "Printer onboarding complete" in reply and "written." in reply
    assert applied[0]["model"] == "creality.k1"
    assert applied[0]["moonraker_url"] == "http://k1.local:7125"
    assert applied[0]["probe"]["ok"] is True
    assert st.get("k")["status"] == "complete"


def test_name_must_differ_from_existing_printers(tmp_path):
    st = store(tmp_path)
    start(st, now=1000)
    say(st, "creality.k1")
    reply = say(st, "workshop ad5x")  # case-insensitive clash
    assert "already called" in reply
    reply = say(st, "default")  # the model's own label is fine
    assert "Printer URL" in reply
    session = st.get("k")
    assert session["ob_answers"]["name"] == "Creality K1"
    assert session["ob_answers"]["printer_id"] == "creality-k1"


def test_probe_failure_back_fix_and_retry(tmp_path):
    st = store(tmp_path)
    outcomes = [guided.ProbeOutcome(False, "nothing answered at that address",
                                    "moonraker_url"),
                guided.ProbeOutcome(True, "answered on the second address")]
    calls = []
    def flaky(answers):
        calls.append(dict(answers))
        return outcomes.pop(0)
    start(st, now=1000)
    walk(st, ["creality.k1", "K1", "http://wrong.local:7125"], probe_runner=flaky)
    reply = say(st, "test", probe_runner=flaky)
    assert "did not pass" in reply and "nothing answered" in reply
    reply = say(st, "back", probe_runner=flaky)
    assert "Printer URL" in reply  # returned to the failing field
    assert "moonraker_url" not in st.get("k")["ob_answers"]
    say(st, "http://right.local:7125", probe_runner=flaky)
    reply = say(st, "test", probe_runner=flaky)
    assert "Live test: **passed**" in reply
    assert calls[1]["moonraker_url"] == "http://right.local:7125"


def test_probe_failure_can_be_saved_offline(tmp_path):
    st = store(tmp_path)
    fail = lambda answers: guided.ProbeOutcome(False, "printer off", "moonraker_url")
    start(st, now=1000)
    walk(st, ["creality.k1", "K1", "http://k1.local:7125"], probe_runner=fail)
    say(st, "test", probe_runner=fail)
    reply = say(st, "offline", probe_runner=fail)
    assert "not passed" in reply and "printer off" in reply
    assert st.get("k")["ob_answers"]["probe"]["offline"] is True


def test_bambu_walkthrough_uses_a_per_printer_secret_name(tmp_path):
    st = store(tmp_path)
    start(st, now=1000)
    walk(st, ["bambulab.a1", "A1 Shelf"])
    reply = say(st, "yes")
    assert "Printer URL" in reply
    walk(st, ["192.0.2.44", "01P00A000000001"])
    reply = say(st, "stored")
    assert "$BAMBU_LAN_PASSWORD_A1_SHELF" in reply or "Live connection test" in reply
    session_text = (tmp_path / "guided-setup.json").read_text()
    assert "a1-shelf" in session_text  # the derived id; the env NAME is derived, never stored
    reply = say(st, "test", probe_runner=ok_probe)
    assert "Live test: **passed**" in reply
    entry = guided._onboard_entry(st.get("k")["ob_answers"])
    assert entry == {"id": "a1-shelf", "model": "bambulab.a1", "name": "A1 Shelf",
                     "lan_host": "192.0.2.44", "lan_serial": "01P00A000000001",
                     "lan_access_code_env": "BAMBU_LAN_PASSWORD_A1_SHELF"}
    assert "chat_id" not in entry  # shared channel under #536


def test_bambu_secret_skip_goes_straight_to_unverified_review(tmp_path):
    st = store(tmp_path)
    start(st, now=1000)
    walk(st, ["bambulab.a1", "A1", "yes", "192.0.2.44", "01P00A000000001"])
    reply = say(st, "skip")  # secret not stored: no probe step at all
    assert "Ready to add this printer" in reply
    assert "not run" in reply and "unverified" in reply


def test_bambu_lan_gate_no_is_an_offline_entry(tmp_path):
    st = store(tmp_path)
    start(st, now=1000)
    walk(st, ["bambulab.a1", "A1"])
    reply = say(st, "no")
    assert "Ready to add this printer" in reply
    assert "offline entry" in reply
    entry = guided._onboard_entry(st.get("k")["ob_answers"])
    assert entry == {"id": "a1", "model": "bambulab.a1", "name": "A1"}


def test_sdcp_blank_host_is_offline_and_host_runs_trust_then_probe(tmp_path):
    st = store(tmp_path)
    start(st, now=1000)
    walk(st, ["elegoo_sdcp.centauricarbon", "Centauri"])
    reply = say(st, "skip")
    assert "offline entry" in reply

    st2 = store(tmp_path / "c")
    start(st2, now=1000)
    walk(st2, ["elegoo_sdcp.centauricarbon", "Centauri", "192.0.2.60"])
    reply = say(st2, "ok")
    assert "Live connection test" in reply
    reply = say(st2, "test", probe_runner=ok_probe)
    assert "Live test: **passed**" in reply


def test_stock_walkthrough_names_the_shared_secret_limitation(tmp_path):
    st = store(tmp_path)
    def two_stock():
        data = loader()
        data["printers"].append({"id": "stock1", "model": "flashforge_stock.ad5x",
                                  "name": "Stock One", "host": "192.0.2.9"})
        return data
    say(st, "/print-handy add-printer", config_loader=two_stock)
    walk(st, ["flashforge_stock.ad5x", "Stock Two", "192.0.2.10"])
    reply = say(st, "stored")
    assert "shared" in reply or "Live connection test" in reply
    # the prompt itself carried the limitation before answering:
    st2 = store(tmp_path / "d")
    say(st2, "/print-handy add-printer", config_loader=two_stock)
    walk(st2, ["flashforge_stock.ad5x", "Stock Two"])
    prompt = say(st2, "192.0.2.10")
    assert "$AD5X_SERIAL" in prompt and "shared" in prompt


def test_zmod_secrets_step_is_ad5x_only():
    # The stock side channel the secrets serve is AD5X-only in the adapters:
    # ZmodSingleExtruderAdapter (AD5M) and ZmodMultiToolAdapter (Creator)
    # expose no native module and no stock /detail API (#648).
    def keys(model):
        return guided._onboard_keys({"model": model,
                                     "moonraker_url": "http://printer.local:7125"})
    ad5x = keys("flashforge.ad5x")
    assert "zmod_secrets" in ad5x and ad5x.index("zmod_secrets") < ad5x.index("probe")
    for model in ("flashforge.ad5m", "flashforge.ad5m_pro", "flashforge.creator5",
                  "flashforge.creator5_pro", "creality.k1", "klipper.generic"):
        other = keys(model)
        assert "zmod_secrets" not in other and "probe" in other, model


def test_ad5x_walkthrough_zmod_secrets_prompt_scopes_the_pair_to_the_ad5x(tmp_path):
    st = store(tmp_path)
    start(st, now=1000)
    reply = walk(st, ["flashforge.ad5x", "Second AD5X", "http://ad5x2.local:7125"])
    session = st.get("k")
    current = session["ob_keys"][session["ob_step"]]
    assert current == "zmod_secrets"
    prompt = guided._onboard_step_defs(session)["zmod_secrets"].prompt
    assert "Stock side-channel secrets" in reply
    assert "AD5X's stock side channel" in prompt
    assert "same pair this AD5X uses on the stock Flashforge LAN connection" in prompt
    assert "other ZMod printers (AD5M, Creator) have no stock side channel" in prompt
    assert "shared by every stock/ZMod printer" not in prompt


# ---------------------------------------------------------------------- review, apply, lifecycle

def test_apply_failure_keeps_the_session_at_review(tmp_path):
    st = store(tmp_path)
    start(st, now=1000)
    walk(st, ["creality.k1", "K1", "skip"], probe_runner=ok_probe)
    def refuse(answers):
        raise RuntimeError("the settings file could not be rewritten")
    reply = say(st, "apply", onboard_apply=refuse)
    assert "was not added" in reply
    assert st.get("k")["status"] == "onboard_review"
    reply = say(st, "apply", onboard_apply=lambda answers: "written.")
    assert "Printer onboarding complete" in reply


def test_apply_rolls_back_the_roster_write_when_a_later_step_fails(tmp_path, monkeypatch):
    """P2-218: add_printer commits the roster entry before the follow-up
    steps run. When one of them fails (here the hardware-record seed, e.g.
    an unwritable state directory), the append is rolled back from the
    backup the writer kept — otherwise the review's 'nothing was written'
    is false and the session's retry dies on the duplicate id its own
    first attempt left behind."""
    from print_handy import roster_write
    from print_handy.core import printer_state

    target = tmp_path / "local.yaml"
    target.write_text("printers: []\n")
    backup = tmp_path / "local.yaml.bak-test"
    backup.write_text("printers: []\n")

    def fake_add(entry):
        target.write_text("printers:\n  - id: bench\n")  # the committed append
        return {"path": target, "backup": backup, "converted": False,
                "printer_ids": ["bench"]}

    def fail_seed(pid, view):
        raise RuntimeError("state dir unwritable")

    monkeypatch.setattr(roster_write, "add_printer", fake_add)
    monkeypatch.setattr(guided.settings, "load", lambda *a, **k: {})
    monkeypatch.setattr(guided.settings, "view_for", lambda pid, cfg: object())
    monkeypatch.setattr(printer_state, "seed_hardware_record", fail_seed)
    answers = {"printer_id": "bench", "model": "flashforge.ad5x", "name": "Bench"}
    with pytest.raises(RuntimeError, match="state dir unwritable"):
        guided._apply_onboarding(answers)
    assert target.read_text() == "printers: []\n"  # the append was rolled back


def test_add_printer_restart_discards_an_unfinished_checklist(tmp_path):
    """P2-219: a saved onboarding checklist can be discarded and started
    over. add-printer alone always resumes it, cancel only pauses it, and
    setup restart is refused while one exists — so a wrong model or name,
    once accepted, could never be corrected short of applying it."""
    st = store(tmp_path)
    start(st, now=1000)
    say(st, "creality.k1")
    say(st, "Wrong Name")
    assert st.get("k")["ob_answers"].get("name") == "Wrong Name"
    reply = say(st, "/print-handy add-printer restart", config_loader=loader)
    assert "restarted" in reply and "discarded" in reply and "Printer model" in reply
    session = st.get("k")
    assert session["status"] == "onboarding" and session["ob_answers"] == {}


def test_add_printer_restart_discards_a_cancelled_checklist(tmp_path):
    """The manual trap (P2-219): a cancelled checklist — e.g. the person
    added the printer by hand after choosing manual — still resumes on
    add-printer and its review apply then fails on the existing id.
    Restart discards it instead."""
    st = store(tmp_path)
    start(st, now=1000)
    say(st, "creality.k1")
    say(st, "/print-handy cancel")
    assert st.get("k")["status"] == "cancelled"
    reply = say(st, "/print-handy add-printer restart", config_loader=loader)
    assert "discarded" in reply
    assert st.get("k")["status"] == "onboarding" and st.get("k")["ob_answers"] == {}


def test_manual_at_review_prints_the_roster_yaml(tmp_path):
    st = store(tmp_path)
    start(st, now=1000)
    walk(st, ["creality.k1", "K1", "http://k1.local:7125"], probe_runner=ok_probe)
    say(st, "test", probe_runner=ok_probe)
    reply = say(st, "manual")
    assert "printers:" in reply and "moonraker_url: http://k1.local:7125" in reply
    assert st.get("k")["status"] == "cancelled"


def test_cancel_and_resume_mid_onboarding(tmp_path):
    st = store(tmp_path)
    start(st, now=1000)
    say(st, "creality.k1")
    reply = say(st, "/print-handy cancel")
    assert "add-printer" in reply  # resume hint names the right command
    reply = start(st)
    assert "Welcome back" in reply and "Display name" in reply
    reply = say(st, "/print-handy status")
    assert "adding a printer" in reply


def test_plain_replies_reach_the_flow_and_slash_text_does_not(tmp_path):
    st = store(tmp_path)
    start(st, now=1000)
    assert say(st, "/print-stop") is None  # another command: not an answer
    assert st.get("k")["ob_answers"] == {}
    reply = say(st, "creality.k1")
    assert reply is not None and "Display name" in reply


def test_session_file_never_contains_a_secret_value(tmp_path):
    st = store(tmp_path)
    start(st, now=1000)
    walk(st, ["prusa.mk4s", "Prusa Bench", "192.0.2.77", "stored"],
         probe_runner=ok_probe)
    raw = (tmp_path / "guided-setup.json").read_text()
    data = json.loads(raw)
    answers = data["k"]["ob_answers"]
    assert answers["prusa_secret"] is True  # posture only, never a value
    assert answers["printer_id"] == "prusa-bench"
    assert "api_key_env" not in answers  # env names are derived at apply, not stored
    entry = guided._onboard_entry(answers)
    assert entry["api_key_env"] == "PRUSA_LINK_API_KEY_PRUSA_BENCH"
