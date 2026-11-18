"""Starter attribution capture tests (#1647).

The print record's ``started_by`` gains the starter's display name at
start time: the intake stamp records it when the platform supplied
one, the launch context carries it into the record, and a start path
with no known starter records nothing — the record is unattributed,
never guessed. Recording only: nothing here gates a print.
"""
from types import SimpleNamespace

from print_handy.bot import runtime, telegram
from print_handy.core import prepared_intake
from print_handy.runtime import print_records


def _source(user_id="111", platform="discord", display_name=None):
    return SimpleNamespace(user_id=user_id,
                           platform=SimpleNamespace(value=platform),
                           display_name=display_name)


def test_stamp_records_the_display_name_when_the_platform_supplies_one():
    job = {}
    prepared_intake.stamp_started_by(job, _source(display_name="Alice"))
    assert job["started_by"] == {"platform": "discord", "user_id": "111",
                                  "display_name": "Alice"}


def test_stamp_without_a_name_records_the_id_only():
    job = {}
    prepared_intake.stamp_started_by(job, _source())
    assert job["started_by"] == {"platform": "discord", "user_id": "111"}


def test_stamp_without_a_user_id_records_nothing():
    job = {}
    prepared_intake.stamp_started_by(job, _source(user_id=""))
    assert "started_by" not in job


def test_make_source_carries_the_display_name():
    source = runtime.make_source("111", "chan", display_name="Alice")
    assert source.display_name == "Alice"
    assert runtime.make_source("111", "chan").display_name is None


def test_author_display_name_prefers_display_then_global_then_name():
    author = SimpleNamespace(display_name="Ally", global_name="Alice G",
                             name="alice")
    assert runtime.author_display_name(author) == "Ally"
    assert runtime.author_display_name(
        SimpleNamespace(global_name="Alice G", name="alice")) == "Alice G"
    assert runtime.author_display_name(SimpleNamespace(name="alice")) == "alice"
    assert runtime.author_display_name(SimpleNamespace(id=1)) is None


def test_telegram_sender_name_rides_the_parsed_message():
    message = {"from": {"id": 42, "is_bot": False, "first_name": "Alice"},
               "chat": {"id": 7}, "text": "hi"}
    parsed = telegram.parse_message(message)
    assert runtime.author_display_name(parsed.author) == "Alice"
    message["from"] = {"id": 42, "is_bot": False, "username": "alice_t"}
    parsed = telegram.parse_message(message)
    assert runtime.author_display_name(parsed.author) == "alice_t"


def test_record_carries_the_stamp_into_the_print_record():
    data = print_records.build_record_data(
        "plate_1.gcode", {"state": "completed"}, {},
        context={"started_by": {"platform": "discord", "user_id": "111",
                                "display_name": "Alice"},
                 "printer": "AD5X"})
    assert data["started_by"]["display_name"] == "Alice"
    assert data["printer"] == "AD5X"
    unnamed = print_records.build_record_data("plate_1.gcode",
                                              {"state": "completed"}, {})
    assert not unnamed.get("started_by")
