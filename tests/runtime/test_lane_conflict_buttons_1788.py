"""The screen-conflict notice asks with buttons, not only a command (#1788).

2026-11-21: the lane watcher posted "slot 3 on the screen names spool #21 as
PETG, but Spoolman records that spool as PLA+ ... relink it with /spool
change". The question was right, but the only way to answer was typing a
command. The notice now carries the insertion prompt's answers.
"""

from unittest import mock

from print_handy.runtime import moonraker_event_listener as listener
from print_handy.runtime import print_alert_buttons as pab

CONFLICT = {"key": "3:identity_conflict:x", "slot": 3, "kind": "identity_conflict", "spool_id": 21,
            "text": "**HelixScreen lane needs attention:** slot 3 (IFS channel 3) on the screen names "
                    "spool #21 as PETG, but Spoolman records that spool as PLA+."}


def _post(event, *, buttons_ok=True, buttonless=None):
    with mock.patch.object(listener, "_buttonless_standalone", return_value=buttonless), \
            mock.patch.object(listener, "_bot_token", return_value="token"), \
            mock.patch.object(pab, "post_insertion_confirmation", return_value=buttons_ok) as buttons, \
            mock.patch.object(listener, "post", return_value=True) as text:
        assert listener.post_lane_event(event) is True
    return buttons, text


def test_the_conflict_notice_posts_the_insertion_answers():
    buttons, text = _post(CONFLICT)
    text.assert_not_called()
    _token, _channel, content, slot = buttons.call_args.args
    assert slot == 3
    assert content.startswith(CONFLICT["text"])
    assert "**Same spool** keeps spool #21" in content and "**Existing spool**" in content


def test_a_failed_button_post_falls_back_to_the_plain_notice():
    buttons, text = _post(CONFLICT, buttons_ok=False)
    buttons.assert_called_once()
    text.assert_called_once_with(CONFLICT["text"])


def test_a_platform_without_buttons_gets_the_plain_notice():
    buttons, text = _post(CONFLICT, buttonless="telegram")
    buttons.assert_not_called()
    text.assert_called_once_with(CONFLICT["text"])


def test_other_lane_notices_stay_plain_text():
    loaded = dict(CONFLICT, kind="loaded", text="**HelixScreen change:** slot 3 now holds spool #22.")
    buttons, text = _post(loaded)
    buttons.assert_not_called()
    text.assert_called_once_with(loaded["text"])


def test_the_buttons_are_the_insertion_prompts():
    labels = [b["label"] for b in pab.insertion_components("a1")[0]["components"]]
    assert labels == ["Same spool", "Existing spool", "New spool"]


def test_claimed_elsewhere_posts_distinct_repair_buttons_1886():
    event = dict(CONFLICT, kind="claimed_elsewhere", slot=1, other_slot=2,
                 recorded_spool_id=16, spool_id=19, text="Conflict. Run /spool sync.")
    with mock.patch.object(listener, "_buttonless_standalone", return_value=None), \
            mock.patch.object(listener, "_bot_token", return_value="token"), \
            mock.patch.object(pab, "post_claimed_lane", return_value=True) as buttons, \
            mock.patch.object(listener, "post") as text:
        assert listener.post_lane_event(event)
    text.assert_not_called()
    assert buttons.call_args.args[-1] == event
    assert "/spool sync" in buttons.call_args.args[-2]
    components = pab.claimed_lane_components("a1", 16, 19)[0]["components"]
    assert [b["label"] for b in components] == [
        "Keep #16", "Move #19 here (swap)", "Pick another spool"]
    assert [pab.parse_insertion_custom_id(b["custom_id"]) for b in components] == [
        ("keep", "a1"), ("move", "a1"), ("pick", "a1")]


def test_claimed_elsewhere_button_failure_preserves_text_repair_1886():
    event = dict(CONFLICT, kind="claimed_elsewhere", text="Conflict. Run /spool sync.")
    with mock.patch.object(listener, "_buttonless_standalone", return_value=None), \
            mock.patch.object(listener, "_bot_token", return_value="token"), \
            mock.patch.object(pab, "post_claimed_lane", return_value=False), \
            mock.patch.object(listener, "post", return_value=True) as text:
        assert listener.post_lane_event(event)
    text.assert_called_once_with(event["text"])
