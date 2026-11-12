"""Stock screen errors (e.g. E0102 feed timeout) halt prints while Klipper still says printing."""
import sys
import unittest
from pathlib import Path
from unittest import mock
from print_handy.runtime import moonraker_event_listener as mel

E0102 = {"status": "error", "errorCode": "E0102", "matlStationInfo": {"currentLoadSlot": 3}}
OK = {"status": "printing", "errorCode": "", "matlStationInfo": {"currentLoadSlot": 1}}


class NativeErrorKeyTests(unittest.TestCase):
    def test_keys(self):
        self.assertEqual(mel.native_error_key(E0102), ("E0102", 3))
        self.assertIsNone(mel.native_error_key(OK))
        self.assertEqual(mel.native_error_key({"status": "error"}), ("unknown", None))
        self.assertIsNone(mel.native_error_key({}))

    def test_text_names_code_slot_and_retry(self):
        text = mel.native_error_text("Ghost.gcode", ("E0102", 3))
        self.assertIn("E0102 (slot 3)", text)
        self.assertIn("could not feed", text)
        self.assertIn("retry", text)


class PollAlertTests(unittest.IsolatedAsyncioTestCase):
    async def poll(self, listener, detail):
        async def read():
            return detail, {}
        listener.read_slots = read
        await listener.poll_native_presence()

    async def test_alerts_once_per_error_and_rearms_after_clear(self):
        lis = mel.Listener()
        lis.state, lis.filename = "printing", "Ghost.gcode"
        with mock.patch.object(mel, "post") as post, mock.patch.object(mel, "log"):
            await self.poll(lis, E0102)
            await self.poll(lis, E0102)
            self.assertEqual(post.call_count, 1)
            self.assertIn("E0102", post.call_args[0][0])
            await self.poll(lis, OK)
            await self.poll(lis, E0102)
            self.assertEqual(post.call_count, 2)

    async def test_a_failed_post_releases_its_starter_ping(self):
        # Regression (review of #1098): the ping was recorded before the
        # post and never released when the post failed, so the next post
        # of the same error episode pinged nobody.
        lis = mel.Listener()
        lis.state, lis.filename = "printing", "Ghost.gcode"
        lis.native_error_alerted = None
        with mock.patch.object(mel, "post", return_value=False) as post, \
                mock.patch.object(mel, "log"), \
                mock.patch.object(mel, "save_native_error_alerted"), \
                mock.patch.object(mel.starter_pings, "ping_for", return_value="42"), \
                mock.patch.object(mel.starter_pings, "release_ping") as release:
            await self.poll(lis, E0102)
        self.assertEqual(post.call_args.kwargs, {"mention_user_id": "42"})
        release.assert_called_once_with("alert", job_key="Ghost.gcode", episode="native-error:E0102:3")

    async def test_a_delivered_post_keeps_its_starter_ping(self):
        lis = mel.Listener()
        lis.state, lis.filename = "printing", "Ghost.gcode"
        lis.native_error_alerted = None
        with mock.patch.object(mel, "post", return_value=True) as post, \
                mock.patch.object(mel, "log"), \
                mock.patch.object(mel, "save_native_error_alerted"), \
                mock.patch.object(mel.starter_pings, "ping_for", return_value="42"), \
                mock.patch.object(mel.starter_pings, "release_ping") as release:
            await self.poll(lis, E0102)
        self.assertEqual(post.call_count, 1)
        release.assert_not_called()

    async def test_idle_errors_do_not_alert(self):
        lis = mel.Listener()
        lis.state = "standby"
        with mock.patch.object(mel, "post") as post, mock.patch.object(mel, "log"):
            await self.poll(lis, E0102)
        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
