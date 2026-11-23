"""The job card is published under the print_handy Moonraker namespace (#1817 W2)."""
from unittest import mock

from print_handy.launch import print_now


def test_the_job_card_is_written_to_print_handy_current_job():
    with mock.patch.object(print_now, "job_card", return_value={"title": "Benchy"}), \
         mock.patch.object(print_now, "request_json") as post:
        assert print_now._publish_job_card({"moonraker_url": "http://printer.test:7125"}, {}, "a.gcode") == \
            {"title": "Benchy"}
    url = post.call_args.args[0]
    data = post.call_args.kwargs["data"]
    assert url == "http://printer.test:7125/server/database/item"
    assert data == {"namespace": "print_handy", "key": "current_job", "value": {"title": "Benchy"}}


def test_a_failed_publish_never_blocks_the_print():
    with mock.patch.object(print_now, "job_card", return_value={}), \
         mock.patch.object(print_now, "request_json", side_effect=OSError("down")):
        assert print_now._publish_job_card({"moonraker_url": "http://printer.test:7125"}, {}, "a.gcode") is None
