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


def test_a_marker_inside_a_title_never_attaches(tmp_path):
    """A model title or printer filename carrying MEDIA: is never a marker:
    only a marker alone on its own line is read."""
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"video")
    body, files = rest.split_media(f"Print complete: Widget MEDIA:{clip}")
    assert files == [] and "MEDIA:" in body


def test_secrets_and_state_files_never_attach(tmp_path, monkeypatch):
    """Even on its own line, a marker attaches only a Print Handy output file
    inside the bot's own folders: an .env, a state .json or a file outside
    those folders is dropped and never uploaded."""
    home = tmp_path / "state"
    (home / "state").mkdir(parents=True)
    env = home / ".env"
    env.write_text("DISCORD_BOT_TOKEN=secret")
    state = home / "state" / "webui_sessions.json"
    state.write_text("{}")
    outside = tmp_path / "elsewhere" / "clip.mp4"
    outside.parent.mkdir()
    outside.write_bytes(b"video")
    monkeypatch.setattr(rest, "_output_roots", lambda: [home.resolve()])
    for path in (env, state, outside):
        body, files = rest.split_media(f"Done\nMEDIA:{path}")
        assert files == [] and body == "Done"
    # A link inside the output folder that points outside it is refused too.
    link = home / "state" / "clip.mp4"
    link.symlink_to(outside)
    assert rest.split_media(f"Done\nMEDIA:{link}")[1] == []
    assert rest.split_media(f"Done\nMEDIA:{home}/state/../../elsewhere/clip.mp4")[1] == []
    inside = home / "state" / "frame.jpg"
    inside.write_bytes(b"jpeg")
    assert rest.split_media(f"Done\nMEDIA:{inside}")[1] == [inside.resolve()]
    bundle = home / "state" / "diagnostics-bundle.md"
    bundle.write_text("# bundle")
    assert rest.split_media(f"Done\nMEDIA:{bundle}")[1] == [bundle.resolve()]


def test_an_injected_title_cannot_upload_the_env_file(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("SECRET=1")
    monkeypatch.setattr(rest, "_output_roots", lambda: [tmp_path.resolve()])
    opener = Opener()
    ok, _ = rest.send("tok", "123", f"Print complete: x\nMEDIA:{env}", opener=opener)
    assert ok and b"SECRET" not in opener.requests[0].data


def test_explicit_files_attach_without_a_marker(tmp_path):
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"VIDEOBYTES")
    opener = Opener()
    ok, _ = rest.send("tok", "123", "Done", opener=opener, files=[clip])
    assert ok and b"VIDEOBYTES" in opener.requests[0].data
    assert b'"content": "Done"' in opener.requests[0].data


def test_every_platform_attaches_real_output_and_refuses_injected_markers(tmp_path, monkeypatch):
    """Telegram, Matrix and web chat share the same restricted path: a real
    output file on its own line (or passed explicitly) attaches, a marker
    in a title or one naming a secret does not."""
    import asyncio
    from print_handy.bot import matrix_rest, telegram_rest, web
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"VIDEOBYTES")
    env = tmp_path / ".env"
    env.write_text("SECRET=1")
    monkeypatch.setattr(rest, "_output_roots", lambda: [tmp_path.resolve()])
    text = f"Print complete: t MEDIA:{clip}\nMEDIA:{env}\nMEDIA:{clip}"

    seen = []

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def opener(req, timeout=None):
        seen.append(req.data or b"")
        if "/media/" in req.full_url:
            return Response(json.dumps({"content_uri": "mxc://x/y"}).encode())
        return Response(json.dumps({"ok": True, "result": {"message_id": 1},
                                    "event_id": "$e"}).encode())

    assert telegram_rest.send("tok", "-1", text, opener=opener)[0]
    assert sum(b"VIDEOBYTES" in data for data in seen) == 1
    assert not any(b"SECRET" in data for data in seen)
    seen.clear()
    assert matrix_rest.send("https://hs.example", "tok", "!r:hs.example", text, opener=opener)[0]
    assert sum(b"VIDEOBYTES" in data for data in seen) == 1
    assert not any(b"SECRET" in data for data in seen)

    class RecordingOutbox:
        # The web adapter's outbox takes the browser session scope (#1145).
        def __init__(self):
            self.entries = []

        def append(self, entry, scope=""):
            self.entries.append((entry, scope))

    outbox = RecordingOutbox()
    adapter = web.WebAdapter(outbox)
    asyncio.run(adapter.send("webui", text))
    asyncio.run(adapter.send("webui", "Here", files=[clip]))
    notes = [entry["content"] for entry, _scope in outbox.entries]
    assert notes.count("📎 clip.mp4") == 2 and not any(".env" in n for n in notes)
    assert {scope for _entry, scope in outbox.entries} == {web.WebAdapter._scope(None)}


def test_user_text_is_inert_in_events():
    from print_handy.bot import runtime
    source = runtime.make_source("1", "2")
    event = runtime.make_event(source, "MEDIA:/data/state/.env", "m1")
    assert rest.split_media(event.text) == (event.text, [])


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
