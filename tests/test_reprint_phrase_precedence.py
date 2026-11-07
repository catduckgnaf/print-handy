"""#979: natural-language reprint target selection.

The deep dive (issue #979) proved two wrong-target routes and one unclaimed
phrase, all reproduced here against the real ``model_link_dispatch``:

* "again" phrases carrying a pronoun ("print it again") were captured by the
  affirmative-followup link prepend, so the channel's remembered link was
  intaken instead of the last print. "Again" now wins over "it": reprint
  phrases resolve from launch records, never from the recent-link store.
  The deictic class ("print it" right after a link) is unchanged.
* The reuse branch resolved through JOB.json mtime (newest record rewrite,
  printed or not). It now uses launch-record selection — the same source
  ``/print-again`` uses — so both routes name the same target, with re-slice
  execution preserved ("print the previous print in black" still works).
* Bare "print again" matched no deterministic hook; it is a reprint phrase.
* A dispatcher-prepended link no longer refreshes the recent link's TTL,
  and a stored row records who posted the link and the channel's printer.

No Discord, no network, no printer: ``prepare_link`` is stubbed and records
what the dispatch asked it to intake.
"""
import asyncio
import copy
import json
import os
import time
from types import SimpleNamespace as S

import pytest

from print_handy import settings
from print_handy.bot import recent_links, runtime
from print_handy.bot.router import load_plugin
from print_handy.core import model_job, prepared_intake, printer_state
from print_handy.hermes import _config
from print_handy.hermes import print_control as pc

plugin = load_plugin()

SHIELD_URL = "https://www.printables.com/model/999-nvidia-shield-remote-holder"
PANEL = {"title": "Panel A Lower V21", "intake_id": "panel-intake"}
OPERATOR = "42"
GARAGE = "flashforge-ad5x"      # default printer (first roster entry)
WORKSHOP = "workshop-ad5x"       # second printer
HOME = "100000000000000001"      # the garage printer's own channel
OTHER = "100000000000000099"     # the workshop printer's own channel


@pytest.fixture()
def cfg(tmp_path, monkeypatch):
    data = copy.deepcopy(_config.cfg())
    data.setdefault("paths", {})["hermes_home"] = tmp_path
    monkeypatch.setattr(_config, "_override", data)
    return data


@pytest.fixture()
def two_printer_cfg(tmp_path, monkeypatch):
    data = copy.deepcopy(_config.cfg())
    data.setdefault("paths", {})["hermes_home"] = tmp_path
    base = copy.deepcopy(data["printers"][0])
    data["printers"] = [
        {**base, "id": GARAGE, "name": "Garage AD5X", "chat_id": HOME},
        {**base, "id": WORKSHOP, "name": "Workshop AD5X", "chat_id": OTHER},
    ]
    monkeypatch.setattr(_config, "_override", data)
    return data


class Adapter:
    def __init__(self):
        self.sent = []
        self.questions = []

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append(str(content))
        return S(success=True)


class Gateway:
    """One printer per channel when `routed` is given; otherwise the source's
    own stamp (None = the default printer), as the harness gateway does."""

    def __init__(self, adapter, routed=None):
        self._adapter = adapter
        self._routed = routed

    def _is_user_authorized_for_source(self, source):
        return True

    def _delivery_adapter_for(self, source):
        return self._adapter

    def _session_key_for_source(self, source):
        return f"{source.chat_id}:{source.user_id}"

    def printer_id_for(self, source):
        if self._routed is not None:
            return self._routed.get(str(source.chat_id))
        return getattr(source, "printer_id", None)

    def shared_printer_ids_for(self, source):
        ids = getattr(source, "shared_printer_ids", None)
        return list(ids) if ids else None


@pytest.fixture()
def intake(monkeypatch):
    calls = []

    async def fake_prepare(event, adapter, state, root, reuse=None, **kwargs):
        from print_handy.core.link_intake import model_link
        calls.append({"text": event.text, "linked": model_link(event.text),
                      "reuse": reuse, "kwargs": kwargs})
        return {"action": "skip", "reason": "recorded intake (stub)"}

    monkeypatch.setattr(prepared_intake, "prepare_link", fake_prepare)
    monkeypatch.setattr(printer_state, "load", lambda **kw: {})
    return calls


@pytest.fixture()
def stub_last_print(monkeypatch):
    box = {"job": dict(PANEL)}
    monkeypatch.setattr(prepared_intake, "last_printed_job",
                        lambda printer_id=None, archive=None: box["job"])
    return box


def make_event(text, user=OPERATOR, chat_id="chan"):
    source = runtime.make_source(user, chat_id, platform_name="discord")
    return runtime.make_event(source, text, "m1")


def dispatch(text, gateway=None, adapter=None, **event_kw):
    adapter = adapter or Adapter()
    gateway = gateway or Gateway(adapter)
    return asyncio.run(plugin.model_link_dispatch(make_event(text, **event_kw), gateway)), adapter


def seed_link(age_s, url=SHIELD_URL, chat_id="chan"):
    recent_links.remember(chat_id, url, now=time.time() - age_s)


AGAIN_PHRASES = ["print it again", "print that again", "do it again", "print it again in white"]
DEICTIC_PHRASES = ["print it", "print this", "let's print it", "go ahead"]


# --------------------------------------------------------------- Step 1: "again" wins over "it"

@pytest.mark.parametrize("phrase", AGAIN_PHRASES)
@pytest.mark.parametrize("age", [3600, 6 * 86400, None], ids=["fresh-link", "6-day-link", "no-link"])
def test_again_phrases_never_intake_the_remembered_link(cfg, intake, stub_last_print, phrase, age):
    if age is not None:
        seed_link(age)
    out, _adapter = dispatch(phrase)
    assert out == {"action": "skip", "reason": "recorded intake (stub)"}
    assert len(intake) == 1
    assert intake[0]["linked"] is None, intake  # the stored link URL must not reach intake
    assert intake[0]["reuse"] and intake[0]["reuse"]["title"] == "Panel A Lower V21"


@pytest.mark.parametrize("phrase", DEICTIC_PHRASES)
def test_deictic_followups_still_intake_the_remembered_link(cfg, intake, stub_last_print, phrase):
    seed_link(3600)
    out, _adapter = dispatch(phrase)
    assert out == {"action": "skip", "reason": "recorded intake (stub)"}
    assert len(intake) == 1
    assert intake[0]["linked"] == SHIELD_URL and intake[0]["reuse"] is None


# --------------------------------------------------------------- Step 3: "print again" is claimed

@pytest.mark.parametrize("age", [3600, None], ids=["fresh-link", "no-link"])
def test_print_again_takes_the_reuse_branch(cfg, intake, stub_last_print, age):
    if age is not None:
        seed_link(age)
    out, _adapter = dispatch("print again")
    assert out == {"action": "skip", "reason": "recorded intake (stub)"}
    assert len(intake) == 1 and intake[0]["reuse"]["title"] == "Panel A Lower V21"
    assert intake[0]["linked"] is None


@pytest.mark.parametrize("phrase", ["print again", "print the last one again", "print it again"])
def test_reprint_with_empty_history_refuses_instead_of_falling_through(
        cfg, intake, stub_last_print, phrase):
    stub_last_print["job"] = None
    out, adapter = dispatch(phrase)
    assert intake == []  # no intake was started for any target
    assert out is not None and out["action"] == "skip"  # the hook claimed the phrase
    assert any("Nothing to print again" in text for text in adapter.sent)


# --------------------------------------------------------------- Step 4: recent-link hygiene

def test_followup_use_does_not_refresh_the_link_ttl(cfg, intake, stub_last_print):
    posted_at = time.time() - 6 * 86400
    seed_link(6 * 86400)
    out, _adapter = dispatch("print it")  # the deictic follow-up consumes the link
    assert out["reason"] == "recorded intake (stub)" and intake[0]["linked"] == SHIELD_URL
    row = json.loads(recent_links._path().read_text())["chan:"]
    assert abs(row["at"] - posted_at) < 120, row  # still ~6 days old, not reset to now


def test_stored_row_records_the_poster_and_the_channel_printer(cfg, intake, stub_last_print):
    out, _adapter = dispatch(SHIELD_URL)  # the user posts the link in their own message
    assert out["reason"] == "recorded intake (stub)"
    row = json.loads(recent_links._path().read_text())["chan:"]
    assert row["url"] == SHIELD_URL
    assert row["user_id"] == OPERATOR
    assert row["printer_id"] == settings.default_printer_id(_config.cfg())


def test_remember_without_provenance_keeps_the_legacy_row_shape(cfg):
    recent_links.remember("chan", SHIELD_URL)
    row = json.loads(recent_links._path().read_text())["chan:"]
    assert row["url"] == SHIELD_URL and isinstance(row["at"], (int, float))
    assert recent_links.recall("chan") == SHIELD_URL


def test_legacy_rows_still_load(cfg):
    path = recent_links._path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"chan:": {"url": SHIELD_URL, "at": time.time()}}))
    assert recent_links.recall("chan") == SHIELD_URL


# --------------------------------------------------------------- Step 2: launch-record selection

def _arch_job(arch, folder, title, *, launch_age_s=None, job_age_s=None, printer_id=None):
    d = arch / folder
    d.mkdir(parents=True)
    src = d / "source.stl"
    src.write_bytes(b"solid x\nendsolid x\n")
    job = {"title": title, "intake_id": folder,
           "selected_source": {"name": "source.stl"}, "downloaded_file": str(src)}
    if printer_id:
        job["printer_id"] = printer_id
    if launch_age_s is not None:
        sd = d / "sliced" / "orca" / "run1"
        sd.mkdir(parents=True)
        gcode = sd / "plate_1.gcode"
        gcode.write_text("; gcode\nG1 X1\n")
        job["gcode"] = str(gcode)
        rec = sd / "plate_1.launch.json"
        rec.write_text(json.dumps({"file": "plate_1.gcode"}))
        at = time.time() - launch_age_s
        os.utime(rec, (at, at))
    (d / "JOB.json").write_text(json.dumps(job))
    if job_age_s is not None:
        at = time.time() - job_age_s
        os.utime(d / "JOB.json", (at, at))
    return job


@pytest.fixture()
def inverted_archive(tmp_path, monkeypatch):
    """Panel A printed last (launch 1h ago); the Shield job's JOB.json was
    rewritten a minute ago and it was never printed. JOB.json mtime order
    and launch order deliberately disagree (deep-dive harness Part C)."""
    arch = tmp_path / "archive"
    arch.mkdir()
    _arch_job(arch, "panel-a--aaa", "Panel A Lower V21", launch_age_s=3600, job_age_s=7200)
    _arch_job(arch, "shield--bbb", "NVIDIA Shield Remote Holder", job_age_s=60)
    _arch_job(arch, "benchy--ccc", "Benchy", launch_age_s=3 * 86400, job_age_s=3 * 86400)
    monkeypatch.setattr(model_job, "ARCHIVE", arch)
    return arch


def test_last_printed_job_follows_launch_records_not_job_mtime(inverted_archive):
    job = prepared_intake.last_printed_job(archive=inverted_archive)
    assert job and job["title"] == "Panel A Lower V21"
    # The mtime helper still exists, honestly named, and still answers its own question.
    assert prepared_intake.newest_archived_job(archive=inverted_archive)["title"] \
        == "NVIDIA Shield Remote Holder"


def test_nl_reprint_and_print_again_name_the_same_target(cfg, intake, inverted_archive):
    out, _adapter = dispatch("print the last one again")
    assert out["reason"] == "recorded intake (stub)"
    assert intake[0]["reuse"]["title"] == "Panel A Lower V21"
    assert pc.resolve_last_print(inverted_archive)["title"] == "Panel A Lower V21"
    assert pc.resolve_print("", inverted_archive)["title"] == "Panel A Lower V21"


def test_last_printed_job_empty_history_is_none(tmp_path):
    arch = tmp_path / "archive"
    arch.mkdir()
    assert prepared_intake.last_printed_job(archive=arch) is None


# --------------------------------------------------------------- Step 6: printer-scoped resolvers

@pytest.fixture()
def two_printer_archive(tmp_path, monkeypatch):
    """Interleaved launches: the garage printed most recently overall, the
    workshop printed before that, and the newest JOB.json of all belongs to
    a never-printed workshop job."""
    arch = tmp_path / "archive"
    arch.mkdir()
    _arch_job(arch, "garage--a", "Garage Bracket", launch_age_s=3600, job_age_s=3600,
              printer_id=GARAGE)
    _arch_job(arch, "workshop--b", "Workshop Jig", launch_age_s=7200, job_age_s=7200,
              printer_id=WORKSHOP)
    _arch_job(arch, "workshop--c", "Workshop Unprinted", job_age_s=60, printer_id=WORKSHOP)
    monkeypatch.setattr(model_job, "ARCHIVE", arch)
    monkeypatch.setattr(pc, "ARCHIVE", arch)
    return arch


def test_last_printed_job_filters_by_printer(two_printer_archive):
    assert prepared_intake.last_printed_job(
        archive=two_printer_archive, printer_id=WORKSHOP)["title"] == "Workshop Jig"
    assert prepared_intake.last_printed_job(
        archive=two_printer_archive, printer_id=GARAGE)["title"] == "Garage Bracket"
    assert pc.resolve_last_print(two_printer_archive, printer_id=WORKSHOP)["title"] \
        == "Workshop Jig"


@pytest.mark.parametrize("chat_id,expected", [(HOME, "Garage Bracket"), (OTHER, "Workshop Jig")])
def test_nl_reprint_resolves_the_channel_printers_own_last_print(
        two_printer_cfg, intake, two_printer_archive, chat_id, expected):
    gateway = Gateway(Adapter(), routed={HOME: GARAGE, OTHER: WORKSHOP})
    out, _adapter = dispatch("print the last one again", gateway=gateway, chat_id=chat_id)
    assert out["reason"] == "recorded intake (stub)"
    assert intake[0]["reuse"]["title"] == expected


@pytest.mark.parametrize("chat_id,expected", [(HOME, "Garage Bracket"), (OTHER, "Workshop Jig")])
def test_bare_print_again_command_resolves_the_channel_printers_own_last_print(
        two_printer_cfg, two_printer_archive, monkeypatch, chat_id, expected):
    adapter = Adapter()
    gateway = Gateway(adapter, routed={HOME: GARAGE, OTHER: WORKSHOP})
    questions = []

    async def fake_confirm(adapter_, source, metadata, kind, question, labels, timeout, **kw):
        questions.append(question)
        return pc.AGAIN_CANCEL_LABEL

    monkeypatch.setattr(plugin, "_confirm", fake_confirm)
    source = runtime.make_source(OPERATOR, chat_id, platform_name="discord")
    event = runtime.make_event(source, "/print-again", "m1")
    out = asyncio.run(plugin.print_again_dispatch(event, gateway))
    assert out == {"action": "skip", "reason": "deterministic print again handled"}
    assert questions and expected in questions[0]
    other = "Workshop Jig" if expected == "Garage Bracket" else "Garage Bracket"
    assert other not in questions[0]


# --------------------------------------------------------------- #979 review follow-ups

@pytest.mark.parametrize("phrase,number", [("reprint 57", 57), ("reprint #57", 57),
                                           ("re-print 57", 57), ("print 57 again", 57),
                                           ("Print #57 again.", 57)])
def test_numbered_reprints_are_the_numbered_print_not_the_last_one(
        cfg, intake, stub_last_print, phrase, number):
    """A reprint phrase naming a print number is that exact print (#436),
    so the last-print branch steps aside for print_again_dispatch."""
    assert plugin._plain_print_number(phrase) == number
    out, _adapter = dispatch(phrase)
    assert out is None and intake == []


def test_unreusable_last_print_is_named_never_replaced_by_an_older_one(cfg, intake, tmp_path, monkeypatch):
    """The newest print's source is gone: refuse, naming it and /print-again,
    instead of silently re-slicing an older, different model (#979)."""
    arch = tmp_path / "archive"
    arch.mkdir()
    _arch_job(arch, "older--a", "Older Bracket", launch_age_s=7200, job_age_s=7200)
    newest = _arch_job(arch, "panel-a--b", "Panel A Lower V21", launch_age_s=600, job_age_s=600)
    os.remove(newest["downloaded_file"])
    monkeypatch.setattr(model_job, "ARCHIVE", arch)
    with pytest.raises(prepared_intake.LastPrintNotReusable) as info:
        prepared_intake.last_printed_job(archive=arch)
    assert info.value.title == "Panel A Lower V21"
    out, adapter = dispatch("print it again")
    assert out["reason"] == "last print not re-sliceable"
    assert intake == []
    assert any("Panel A Lower V21" in m and "/print-again" in m for m in adapter.sent)
    assert not any("Older Bracket" in m for m in adapter.sent)


@pytest.mark.parametrize("phrase", ["don't print it again", "never print that again",
                                    "do not reprint", "why did it print again",
                                    "did it print again?"])
def test_negated_or_questioning_reprint_phrases_are_not_requests(phrase):
    assert prepared_intake.reprint_request(phrase) is False


@pytest.mark.parametrize("phrase", ["can you print it again?", "could you reprint that",
                                    "print it again", "print again"])
def test_ordinary_reprint_requests_still_count(phrase):
    assert prepared_intake.reprint_request(phrase) is True


@pytest.mark.parametrize("phrase", ["don't print it", "do not do it", "did you print it?",
                                    "why did it print it", "don't print it again"])
def test_negations_and_questions_never_pull_in_the_remembered_link(cfg, intake, stub_last_print, phrase):
    seed_link(60)
    out, _adapter = dispatch(phrase)
    assert out is None and intake == []
    assert recent_links.recall("chan") == SHIELD_URL  # untouched, not consumed


def test_a_declined_followup_is_not_the_channels_current_job_for_edits(tmp_path, monkeypatch):
    """After "No, cancel" at the naming gate, "make it bigger" / "fix this"
    must not act on the declined model (#979 review)."""
    from print_handy.bot import edit_chat
    root = tmp_path / "model-link-intakes"
    root.mkdir()
    job = tmp_path / "shield" / "JOB.json"
    job.parent.mkdir()
    job.write_text(json.dumps({"title": "NVIDIA Shield Remote Holder"}))
    (root / "r.json").write_text(json.dumps({"chat_id": "chan", "thread_id": None,
                                             "job_json": str(job), "status": "DECLINED_FOLLOWUP"}))
    source = runtime.make_source(OPERATOR, "chan", platform_name="discord")
    assert edit_chat._edit_current_job(root, source) is None
