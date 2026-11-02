"""Listener posts over Discord REST: text, MEDIA attachments, the 2000 character limit."""
import io
import json

from print_handy.bot import rest


class Opener:
    def __init__(self):
        self.requests = []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        return io.BytesIO(json.dumps({"id": str(len(self.requests))}).encode())


def test_media_markers_become_files_and_leave_the_text(tmp_path):
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"video")
    body, files = rest.split_media(f"Print complete\nMEDIA:{clip}\nMEDIA:{tmp_path / 'missing.mp4'}")
    assert body == "Print complete" and files == [clip]
    body, files = rest.split_media(f"Preview.\\nMEDIA:{clip}")  # the literal backslash-n some callers use
    assert body == "Preview." and files == [clip]


def test_media_paths_with_spaces_run_to_the_end_of_the_line(tmp_path):
    """A MEDIA: path containing spaces is captured whole (P2-40): the marker
    runs to the end of its line, so a preview stored under a spaced path is
    attached instead of leaving the tail of the filename in the message."""
    clip = tmp_path / "my clip.mp4"
    clip.write_bytes(b"video")
    body, files = rest.split_media(f"Print complete\nMEDIA:{clip}")
    assert body == "Print complete" and files == [clip]
    body, files = rest.split_media(f"Preview.\\nMEDIA:{clip}")  # literal backslash-n form
    assert body == "Preview." and files == [clip]


def test_long_text_splits_on_lines():
    text = "\n".join("x" * 90 for _ in range(60))
    pieces = rest.chunks(text)
    assert len(pieces) == 3 and all(len(p) <= rest.LIMIT for p in pieces)
    assert "\n".join(pieces).replace("\n", "") == text.replace("\n", "")


def test_send_posts_with_the_bot_token_and_attaches_files(tmp_path):
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"VIDEOBYTES")
    opener = Opener()
    ok, message_id = rest.send("tok", "123", f"Done\nMEDIA:{clip}", opener=opener)
    assert ok and message_id == "1"
    req = opener.requests[0]
    assert req.full_url == "https://discord.com/api/v10/channels/123/messages"
    assert req.get_header("Authorization") == "Bot tok"
    assert b"VIDEOBYTES" in req.data and b'"content": "Done"' in req.data
    assert b'"allowed_mentions": {"parse": []}' in req.data


def test_send_refuses_without_token_or_channel():
    assert rest.send("", "123", "x") == (False, "no Discord bot token")
    assert rest.send("tok", "", "x")[0] is False


def test_send_reports_transport_errors():
    def broken(req, timeout=None):
        raise OSError("network down")
    ok, detail = rest.send("tok", "123", "x", opener=broken)
    assert not ok and "network down" in detail
