"""Offline tests for live camera source resolution and encoder planning (issue #311, stages 1-2).

No network, printer or ffmpeg execution: resolvers run against recorded
payload shapes, the encoder probe is injected, and ffmpeg construction is
asserted as a pure argv list. The redaction test proves a credential
carried in a camera URL appears in no error message and no repr.
"""
import pytest

from print_handy import live_stream as ls
from print_handy.runtime import timelapse_encode

SECRET = "s3cr3t-PASS-xyz789"
MOONRAKER_BASE = "http://printer.local:7125"
OCTOPRINT_BASE = "http://octopi.local"


# --------------------------------------------------------------------- Moonraker

MOONRAKER_PAYLOAD = {
    "webcams": [
        {
            "name": "bedcam",
            "location": "printer",
            "service": "mjpegstreamer",
            "stream_url": "/webcam/?action=stream",
            "snapshot_url": "/webcam/?action=snapshot",
            "enabled": True,
        },
        {
            "name": "nozzlecam",
            "service": "mjpegstreamer",
            "stream_url": "http://cam2.local/stream",
            "snapshot_url": "http://cam2.local/snapshot",
        },
    ]
}


def test_moonraker_relative_urls_resolved_and_first_picked():
    source = ls.camera_source_from_moonraker(MOONRAKER_PAYLOAD, base_url=MOONRAKER_BASE)
    assert source.name == "bedcam"
    assert source.stream_url == "http://printer.local:7125/webcam/?action=stream"
    assert source.snapshot_url == "http://printer.local:7125/webcam/?action=snapshot"
    assert source.kind == "mjpeg"
    assert source.service == "mjpegstreamer"


def test_moonraker_absolute_urls_pass_through():
    source = ls.camera_source_from_moonraker(
        MOONRAKER_PAYLOAD, base_url=MOONRAKER_BASE, preferred_name="nozzlecam")
    assert source.name == "nozzlecam"
    assert source.stream_url == "http://cam2.local/stream"
    assert source.snapshot_url == "http://cam2.local/snapshot"


def test_moonraker_preferred_name_is_case_insensitive_and_wins():
    source = ls.camera_source_from_moonraker(
        MOONRAKER_PAYLOAD, base_url=MOONRAKER_BASE, preferred_name="NozzleCam")
    assert source.name == "nozzlecam"


def test_moonraker_result_wrapper_shape():
    wrapped = {"result": {"webcams": MOONRAKER_PAYLOAD["webcams"]}}
    source = ls.camera_source_from_moonraker(wrapped, base_url=MOONRAKER_BASE)
    assert source.name == "bedcam"
    assert source.kind == "mjpeg"


def test_moonraker_disabled_webcam_is_skipped():
    payload = {"webcams": [
        {"name": "off", "stream_url": "http://off.local/stream",
         "snapshot_url": "http://off.local/snap", "enabled": False},
        {"name": "on", "stream_url": "http://on.local/stream",
         "snapshot_url": "http://on.local/snap"},
    ]}
    source = ls.camera_source_from_moonraker(payload, base_url=MOONRAKER_BASE)
    assert source.name == "on"


def test_moonraker_rtsp_and_webrtc_kinds():
    rtsp = ls.camera_source_from_moonraker(
        {"webcams": [{"name": "r", "stream_url": "rtsp://cam.local/feed",
                      "snapshot_url": "http://cam.local/snap", "service": "camera-streamer"}]},
        base_url=MOONRAKER_BASE)
    assert rtsp.kind == "rtsp"
    webrtc = ls.camera_source_from_moonraker(
        {"webcams": [{"name": "w", "stream_url": "http://cam.local/webrtc",
                      "snapshot_url": "http://cam.local/snap", "service": "webrtc-camerastreamer"}]},
        base_url=MOONRAKER_BASE)
    assert webrtc.kind == "webrtc"


@pytest.mark.parametrize("payload", [None, {}, {"webcams": []}, {"webcams": [{"name": "empty"}]}])
def test_moonraker_fallback_derives_stream_from_snapshot(payload):
    source = ls.camera_source_from_moonraker(
        payload, base_url=MOONRAKER_BASE,
        fallback_snapshot_url="http://printer.local:7125/webcam/?action=snapshot")
    assert source.stream_url == "http://printer.local:7125/webcam/?action=stream"
    assert source.snapshot_url == "http://printer.local:7125/webcam/?action=snapshot"
    assert source.kind == "mjpeg"


def test_moonraker_no_payload_and_no_fallback_raises():
    with pytest.raises(ls.LiveStreamError):
        ls.camera_source_from_moonraker(None, base_url=MOONRAKER_BASE)
    with pytest.raises(ls.LiveStreamError):
        ls.camera_source_from_moonraker({"webcams": []}, base_url=MOONRAKER_BASE)


# --------------------------------------------------------------------- derivation / snapshot-only

def test_derive_action_snapshot_becomes_stream():
    source = ls.derive_from_snapshot_url("http://cam.local/webcam/?action=snapshot")
    assert source.kind == "mjpeg"
    assert source.stream_url == "http://cam.local/webcam/?action=stream"
    assert source.snapshot_url == "http://cam.local/webcam/?action=snapshot"


def test_derive_preserves_other_query_params():
    source = ls.derive_from_snapshot_url("http://cam.local/webcam/?foo=1&action=snapshot")
    assert source.stream_url == "http://cam.local/webcam/?foo=1&action=stream"


def test_snapshot_without_action_is_snapshot_only():
    source = ls.derive_from_snapshot_url("http://cam.local/snapshot.jpg")
    assert source.kind == "snapshot"
    assert source.stream_url is None
    assert source.snapshot_url == "http://cam.local/snapshot.jpg"


def test_snapshot_only_from_moonraker_fallback_is_valid():
    source = ls.camera_source_from_moonraker(
        None, base_url=MOONRAKER_BASE, fallback_snapshot_url="http://cam.local/frame.jpg")
    assert source.kind == "snapshot"
    assert source.stream_url is None


def test_moonraker_entry_with_only_snapshot_without_action_is_snapshot_kind():
    payload = {"webcams": [{"name": "still", "snapshot_url": "http://cam.local/frame.jpg"}]}
    source = ls.camera_source_from_moonraker(payload, base_url=MOONRAKER_BASE)
    assert source.kind == "snapshot"
    assert source.stream_url is None


# --------------------------------------------------------------------- OctoPrint

def test_octoprint_classic_webcam_relative_urls():
    payload = {"webcam": {"streamUrl": "/?action=stream", "snapshotUrl": "/?action=snapshot",
                          "webcamEnabled": True}}
    source = ls.camera_source_from_octoprint(payload, base_url=OCTOPRINT_BASE)
    assert source.stream_url == "http://octopi.local/?action=stream"
    assert source.snapshot_url == "http://octopi.local/?action=snapshot"
    assert source.kind == "mjpeg"


def test_octoprint_absolute_and_top_level_shape():
    payload = {"streamUrl": "http://cam.local/stream", "snapshotUrl": "http://cam.local/snap"}
    source = ls.camera_source_from_octoprint(payload, base_url=OCTOPRINT_BASE)
    assert source.stream_url == "http://cam.local/stream"
    assert source.kind == "mjpeg"


def test_octoprint_multi_webcam_list_under_webcam():
    payload = {"webcam": {"webcams": [
        {"name": "first", "streamUrl": "http://a.local/stream", "snapshotUrl": "http://a.local/snap"},
        {"name": "second", "streamUrl": "http://b.local/stream", "snapshotUrl": "http://b.local/snap"},
    ]}}
    source = ls.camera_source_from_octoprint(payload, base_url=OCTOPRINT_BASE, preferred_name="second")
    assert source.name == "second"
    assert source.stream_url == "http://b.local/stream"


def test_octoprint_multi_webcam_top_level_list():
    payload = {"webcams": [
        {"name": "only", "streamUrl": "/stream", "snapshotUrl": "/snap"}]}
    source = ls.camera_source_from_octoprint(payload, base_url=OCTOPRINT_BASE)
    assert source.stream_url == "http://octopi.local/stream"
    assert source.snapshot_url == "http://octopi.local/snap"


def test_octoprint_snapshot_only_derives_stream_from_action():
    payload = {"webcam": {"snapshotUrl": "http://octopi.local/?action=snapshot", "streamUrl": ""}}
    source = ls.camera_source_from_octoprint(payload, base_url=OCTOPRINT_BASE)
    assert source.stream_url == "http://octopi.local/?action=stream"
    assert source.kind == "mjpeg"


def test_octoprint_snapshot_without_action_is_snapshot_kind():
    payload = {"webcam": {"snapshotUrl": "http://cam.local/frame.jpg", "streamUrl": ""}}
    source = ls.camera_source_from_octoprint(payload, base_url=OCTOPRINT_BASE)
    assert source.kind == "snapshot"
    assert source.stream_url is None


def test_octoprint_empty_payload_raises_without_fallback():
    with pytest.raises(ls.LiveStreamError):
        ls.camera_source_from_octoprint({}, base_url=OCTOPRINT_BASE)
    with pytest.raises(ls.LiveStreamError):
        ls.camera_source_from_octoprint(None, base_url=OCTOPRINT_BASE)


# --------------------------------------------------------------------- redaction

def test_redaction_secret_appears_nowhere_in_repr_or_errors():
    credential_url = f"http://user:{SECRET}@cam.local/webcam/?action=stream&token={SECRET}"
    source = ls.camera_source_from_moonraker(
        {"webcams": [{"name": "bed", "stream_url": credential_url,
                      "snapshot_url": f"http://user:{SECRET}@cam.local/snap?api_key={SECRET}"}]},
        base_url=MOONRAKER_BASE)
    # The fields keep the real URL (the encoder needs it); repr must not.
    assert source.stream_url == credential_url
    assert SECRET not in repr(source)
    assert SECRET not in str(source)
    assert SECRET not in ls.redact_url(credential_url)
    assert "user" not in ls.redact_url(credential_url).split("@")[0]

    errors = []
    # Unsupported scheme error path includes a (redacted) URL.
    try:
        ls.camera_source_from_moonraker(
            {"webcams": [{"name": "x", "stream_url": f"ftp://user:{SECRET}@cam.local/feed"}]},
            base_url=MOONRAKER_BASE)
    except ls.LiveStreamError as exc:
        errors.append(str(exc))
    # Relative URL with no base: the message includes a (redacted) URL.
    try:
        ls.camera_source_from_moonraker(
            {"webcams": [{"name": "x", "stream_url": f"/feed?token={SECRET}"}]},
            base_url=None)
    except ls.LiveStreamError as exc:
        errors.append(str(exc))
    # Bad snapshot scheme on the derive path.
    try:
        ls.derive_from_snapshot_url(f"ftp://user:{SECRET}@cam.local/snap")
    except ls.LiveStreamError as exc:
        errors.append(str(exc))

    assert len(errors) == 3
    for message in errors:
        assert SECRET not in message


def test_redact_url_redacts_nested_ffmpeg_camera_source():
    # P1-71: an ffmpeg: source wraps a nested URL; its credentials must not
    # survive redaction just because the outer scheme has no authority.
    redacted = ls.redact_url("ffmpeg:rtsp://user:hunter2@cam.example/live#video=h264")
    assert "hunter2" not in redacted
    assert "user" not in redacted.split("@")[0]
    assert redacted.startswith("ffmpeg:rtsp://")
    assert "cam.example" in redacted and "/live" in redacted
    # A credential-free ffmpeg source keeps its shape.
    assert ls.redact_url("ffmpeg:rtsp://cam.example/live") == \
        "ffmpeg:rtsp://cam.example/live"


# A fullwidth solidus in the authority makes urlsplit raise a ValueError
# whose message embeds the whole authority, password included (P2-103).
BAD_AUTHORITY_URL = "http://user:secretpw@cam\uff0f.local/stream"


def test_urlsplit_failure_is_sanitized_at_every_entry_point():
    attempts = [
        lambda: ls.derive_from_snapshot_url(BAD_AUTHORITY_URL),
        lambda: ls.camera_source_from_moonraker(
            {"webcams": [{"name": "c", "stream_url": BAD_AUTHORITY_URL}]}),
        lambda: ls.camera_source_from_octoprint(
            {"webcam": {"streamUrl": BAD_AUTHORITY_URL}}),
        lambda: ls.camera_source_from_moonraker(
            {"webcams": [{"name": "c", "stream_url": "/feed"}]},
            base_url=BAD_AUTHORITY_URL),
    ]
    for attempt in attempts:
        with pytest.raises(ls.LiveStreamError) as caught:
            attempt()
        assert "secretpw" not in str(caught.value)
        # The original ValueError must not ride along as the chain: traceback
        # logging of the chain would print the credential-bearing message.
        assert caught.value.__context__ is None
        assert caught.value.__cause__ is None


def test_redact_url_keeps_host_path_and_non_secret_query():
    redacted = ls.redact_url(f"http://user:{SECRET}@cam.local:8080/webcam/?action=stream&token={SECRET}")
    assert redacted == "http://***@cam.local:8080/webcam/?action=stream&token=%2A%2A%2A" or \
        "token=" in redacted and SECRET not in redacted and "cam.local:8080" in redacted


# --------------------------------------------------------------------- encoder plan

ALL_H264 = {"h264_vaapi", "h264_nvenc", "h264_qsv", "libx264"}
ALL_VP8 = {"vp8_vaapi", "libvpx"}


def _probe(encoders, dri=True, nvidia=True):
    return ls.EncoderProbe(encoders=set(encoders), dri_present=dri, nvidia_present=nvidia)


def _encoders(candidates):
    return [c.encoder for c in candidates]


def test_h264_default_order_vaapi_nvenc_qsv_cpu():
    candidates = ls.encoder_candidates(_probe(ALL_H264), target_codec="h264")
    assert _encoders(candidates) == ["h264_vaapi", "h264_nvenc", "h264_qsv", "libx264"]
    assert [c.vendor for c in candidates] == ["vaapi", "nvenc", "qsv", "cpu"]
    assert all(c.codec == "h264" for c in candidates)
    assert candidates[-1].hardware is False


def test_h264_vaapi_and_qsv_require_dri_nvenc_requires_nvidia():
    no_dri = ls.encoder_candidates(_probe(ALL_H264, dri=False, nvidia=True), target_codec="h264")
    assert _encoders(no_dri) == ["h264_nvenc", "libx264"]
    no_nvidia = ls.encoder_candidates(_probe(ALL_H264, dri=True, nvidia=False), target_codec="h264")
    assert _encoders(no_nvidia) == ["h264_vaapi", "h264_qsv", "libx264"]
    neither = ls.encoder_candidates(_probe(ALL_H264, dri=False, nvidia=False), target_codec="h264")
    assert _encoders(neither) == ["libx264"]


def test_h264_encoder_must_be_in_probe_set():
    candidates = ls.encoder_candidates(_probe({"libx264"}), target_codec="h264")
    assert _encoders(candidates) == ["libx264"]
    assert ls.encoder_candidates(_probe(set()), target_codec="h264") == ()


def test_vp8_order_is_vaapi_then_cpu_and_never_nvenc_or_qsv():
    candidates = ls.encoder_candidates(
        _probe(ALL_VP8 | ALL_H264), target_codec="vp8")
    assert _encoders(candidates) == ["vp8_vaapi", "libvpx"]
    assert "h264_nvenc" not in _encoders(candidates)
    assert all("nvenc" != c.vendor and "qsv" != c.vendor for c in candidates)


def test_vp8_vaapi_requires_dri_and_encoder_listed():
    no_dri = ls.encoder_candidates(_probe(ALL_VP8, dri=False), target_codec="vp8")
    assert _encoders(no_dri) == ["libvpx"]
    not_listed = ls.encoder_candidates(_probe({"libvpx"}, dri=True), target_codec="vp8")
    assert _encoders(not_listed) == ["libvpx"]


@pytest.mark.parametrize("preference,expected_first", [
    ("vaapi", "h264_vaapi"), ("nvenc", "h264_nvenc"), ("qsv", "h264_qsv"), ("cpu", "libx264"),
])
def test_h264_preference_moves_vendor_first(preference, expected_first):
    candidates = ls.encoder_candidates(_probe(ALL_H264), preference=preference, target_codec="h264")
    assert candidates[0].encoder == expected_first
    assert set(_encoders(candidates)) == ALL_H264


def test_vp8_preferences():
    vaapi_first = ls.encoder_candidates(_probe(ALL_VP8), preference="vaapi", target_codec="vp8")
    assert _encoders(vaapi_first) == ["vp8_vaapi", "libvpx"]
    cpu_first = ls.encoder_candidates(_probe(ALL_VP8), preference="cpu", target_codec="vp8")
    assert _encoders(cpu_first) == ["libvpx", "vp8_vaapi"]
    # NVENC preference is inapplicable to VP8: default order, no NVENC candidate.
    nvenc_pref = ls.encoder_candidates(
        _probe(ALL_VP8 | ALL_H264), preference="nvenc", target_codec="vp8")
    assert _encoders(nvenc_pref) == ["vp8_vaapi", "libvpx"]


def test_unavailable_preference_leaves_default_order():
    candidates = ls.encoder_candidates(
        _probe({"h264_vaapi", "libx264"}), preference="nvenc", target_codec="h264")
    assert _encoders(candidates) == ["h264_vaapi", "libx264"]


def test_encoder_candidates_bare_set_and_kwargs_forms():
    by_set = ls.encoder_candidates({"h264_vaapi", "libx264"}, target_codec="h264")
    assert _encoders(by_set) == ["h264_vaapi", "libx264"]
    by_set_no_dri = ls.encoder_candidates(
        {"h264_vaapi", "libx264"}, dri_present=False, target_codec="h264")
    assert _encoders(by_set_no_dri) == ["libx264"]
    by_dict = ls.encoder_candidates(
        {"encoders": {"libx264"}, "dri_present": False, "nvidia_present": False}, target_codec="h264")
    assert _encoders(by_dict) == ["libx264"]


def test_encoder_candidates_reject_bad_preference_and_codec():
    with pytest.raises(ls.LiveStreamError):
        ls.encoder_candidates(_probe(ALL_H264), preference="amf", target_codec="h264")
    with pytest.raises(ls.LiveStreamError):
        ls.encoder_candidates(_probe(ALL_H264), target_codec="av1")


# --------------------------------------------------------------------- ffmpeg argv pins

INPUT = "http://printer.local:7125/webcam/?action=stream"
OUT = "pipe:1"


def _candidate(encoder, vendor, codec):
    return ls.EncoderCandidate(encoder=encoder, vendor=vendor, codec=codec, hardware=(vendor != "cpu"))


def test_ffmpeg_vaapi_h264_pinned():
    argv = ls.build_ffmpeg_args(_candidate("h264_vaapi", "vaapi", "h264"),
                                input_url=INPUT, target_codec="h264", output=OUT)
    assert argv == ["ffmpeg", "-hide_banner", "-nostdin",
                    "-vaapi_device", "/dev/dri/renderD128",
                    "-i", INPUT, "-an",
                    "-vf", "scale=-2:720,format=nv12,hwupload",
                    "-r", "15", "-b:v", "2500k", "-c:v", "h264_vaapi", OUT]


def test_ffmpeg_vaapi_vp8_pinned():
    argv = ls.build_ffmpeg_args(_candidate("vp8_vaapi", "vaapi", "vp8"),
                                input_url=INPUT, target_codec="vp8", output=OUT)
    assert argv == ["ffmpeg", "-hide_banner", "-nostdin",
                    "-vaapi_device", "/dev/dri/renderD128",
                    "-i", INPUT, "-an",
                    "-vf", "scale=-2:720,format=nv12,hwupload",
                    "-r", "15", "-b:v", "2500k", "-c:v", "vp8_vaapi", OUT]


def test_ffmpeg_nvenc_h264_pinned():
    argv = ls.build_ffmpeg_args(_candidate("h264_nvenc", "nvenc", "h264"),
                                input_url=INPUT, target_codec="h264", output=OUT)
    assert argv == ["ffmpeg", "-hide_banner", "-nostdin",
                    "-i", INPUT, "-an",
                    "-vf", "scale=-2:720",
                    "-r", "15", "-b:v", "2500k", "-c:v", "h264_nvenc", OUT]
    assert "-vaapi_device" not in argv


def test_ffmpeg_qsv_h264_pinned():
    argv = ls.build_ffmpeg_args(_candidate("h264_qsv", "qsv", "h264"),
                                input_url=INPUT, target_codec="h264", output=OUT)
    assert argv == ["ffmpeg", "-hide_banner", "-nostdin",
                    "-i", INPUT, "-an",
                    "-vf", "scale=-2:720",
                    "-r", "15", "-b:v", "2500k", "-c:v", "h264_qsv", OUT]


def test_ffmpeg_cpu_h264_pinned_ultrafast():
    argv = ls.build_ffmpeg_args(_candidate("libx264", "cpu", "h264"),
                                input_url=INPUT, target_codec="h264", output=OUT)
    assert argv == ["ffmpeg", "-hide_banner", "-nostdin",
                    "-i", INPUT, "-an",
                    "-vf", "scale=-2:720",
                    "-r", "15", "-b:v", "2500k", "-c:v", "libx264",
                    "-preset", "ultrafast", OUT]


def test_ffmpeg_cpu_vp8_pinned_realtime():
    argv = ls.build_ffmpeg_args(_candidate("libvpx", "cpu", "vp8"),
                                input_url=INPUT, target_codec="vp8", output=OUT)
    assert argv == ["ffmpeg", "-hide_banner", "-nostdin",
                    "-i", INPUT, "-an",
                    "-vf", "scale=-2:720",
                    "-r", "15", "-b:v", "2500k", "-c:v", "libvpx",
                    "-deadline", "realtime", OUT]


def test_ffmpeg_custom_limits_and_output_is_final_arg():
    argv = ls.build_ffmpeg_args("libx264", input_url=INPUT, max_height=480, fps=10,
                                bitrate_kbps=1200, output="/tmp/feed.out")
    assert argv[-1] == "/tmp/feed.out"
    assert "scale=-2:480" in argv
    assert argv[argv.index("-r") + 1] == "10"
    assert argv[argv.index("-b:v") + 1] == "1200k"


def test_ffmpeg_accepts_string_candidate_and_infers_codec():
    argv = ls.build_ffmpeg_args("h264_nvenc", input_url=INPUT, output=OUT)
    assert argv[argv.index("-c:v") + 1] == "h264_nvenc"


def test_ffmpeg_rejects_codec_mismatch_and_bad_limits():
    with pytest.raises(ls.LiveStreamError):
        ls.build_ffmpeg_args(_candidate("libvpx", "cpu", "vp8"),
                             input_url=INPUT, target_codec="h264", output=OUT)
    with pytest.raises(ls.LiveStreamError):
        ls.build_ffmpeg_args("libx264", input_url=INPUT, max_height=0, output=OUT)
    with pytest.raises(ls.LiveStreamError):
        ls.build_ffmpeg_args("libx264", input_url="", output=OUT)
    with pytest.raises(ls.LiveStreamError):
        ls.build_ffmpeg_args("libx264", input_url=INPUT, output="")
    with pytest.raises(ls.LiveStreamError):
        ls.build_ffmpeg_args("not_an_encoder", input_url=INPUT, output=OUT)


def test_plan_then_build_round_trip_for_each_codec():
    for codec, probe in (("h264", _probe(ALL_H264)), ("vp8", _probe(ALL_VP8 | ALL_H264))):
        for candidate in ls.encoder_candidates(probe, target_codec=codec):
            argv = ls.build_ffmpeg_args(candidate, input_url=INPUT, output=OUT)
            assert argv[0] == "ffmpeg"
            assert argv[-1] == OUT
            assert argv[argv.index("-c:v") + 1] == candidate.encoder


def test_ffmpeg_args_start_with_the_given_executable():
    # P2-262: the binary that gets launched must be the one that was
    # configured in paths.ffmpeg and probed — not a bare "ffmpeg" that
    # may resolve to a different binary, or to none at all.
    argv = ls.build_ffmpeg_args("libx264", input_url=INPUT, output=OUT,
                                executable="/opt/custom/ffmpeg")
    assert argv[0] == "/opt/custom/ffmpeg"
    assert argv[1:] == ls.build_ffmpeg_args("libx264", input_url=INPUT, output=OUT)[1:]
    with pytest.raises(ls.LiveStreamError):
        ls.build_ffmpeg_args("libx264", input_url=INPUT, output=OUT, executable="")


# --------------------------------------------------------------------- stage 3: the viewing session

ENABLED_SECTION = {"enabled": True, "voice_channel_id": "123456789012345678",
                   "auto_start_on_print": False, "max_height": 720, "fps": 15,
                   "max_bitrate_kbps": 2500, "encoder": "auto", "empty_timeout_s": 300,
                   "activity_origin": ""}


def test_session_starts_idle_and_start_records_who_and_when():
    session = ls.LiveStreamSession()
    assert session.state == "idle" and not session.is_live
    assert ls.SESSION_STATES == ("idle", "live")
    session.start("42", now=1000.0)
    assert session.is_live and session.started_by == "42" and session.started_at == 1000.0
    assert session.viewers == 0 and session.empty_since == 1000.0


def test_session_double_start_and_idle_stop_are_refused():
    session = ls.LiveStreamSession()
    with pytest.raises(ls.LiveStreamError):
        session.stop()
    session.start("42", now=0.0)
    with pytest.raises(ls.LiveStreamError):
        session.start("43", now=1.0)
    session.stop(now=2.0)
    assert session.state == "idle" and session.started_by == "" and session.started_at is None


def test_session_viewer_hook_drives_empty_expiry():
    session = ls.LiveStreamSession().start("42", now=0.0)
    assert session.empty_expired(300, now=299.0) is False
    assert session.empty_expired(300, now=300.0) is True
    session.note_viewers(2, now=400.0)
    assert session.viewers == 2 and session.empty_since is None
    assert session.empty_expired(300, now=10_000.0) is False
    session.note_viewers(0, now=500.0)
    assert session.empty_expired(300, now=799.0) is False
    assert session.empty_expired(300, now=800.0) is True
    with pytest.raises(ls.LiveStreamError):
        session.note_viewers(-1, now=0.0)


def test_transcode_engages_only_away_from_the_default_feed_values():
    # The four feed values act as one switch (2.2.2 wiring): at their
    # defaults the feed is the raw relay under the raw stream name;
    # changing any one engages the transcoded stream the Activity plays.
    assert ls.transcode_engaged({}) is False
    assert ls.transcode_engaged({"max_height": 720, "fps": 15,
                                 "max_bitrate_kbps": 2500, "encoder": "auto"}) is False
    for changed in ({"max_height": 1080}, {"fps": 30},
                    {"max_bitrate_kbps": 6000}, {"encoder": "cpu"}):
        assert ls.transcode_engaged(changed) is True
        assert ls.feed_stream_name(changed) == ls.GO2RTC_LIVE_STREAM_NAME
    assert ls.feed_stream_name({}) == ls.GO2RTC_STREAM_NAME == "camera"
    assert ls.GO2RTC_LIVE_STREAM_NAME == "camera-live"


def test_start_problem_gates_on_enabled_then_channel():
    assert "off" in ls.start_problem({})
    assert "off" in ls.start_problem({"enabled": False, "voice_channel_id": "1"})
    assert "no voice channel" in ls.start_problem({"enabled": True})
    assert ls.start_problem(ENABLED_SECTION) is None


def test_pointer_text_names_the_channel_the_activity_and_the_plan():
    session = ls.LiveStreamSession().start("42", now=0.0)
    text = ls.pointer_text(session, ENABLED_SECTION)
    assert "<#123456789012345678>" in text and "activity" in text
    # At the default feed values the pointer says the feed is relayed
    # as-is — the 2.2.1 text printed plan numbers the relay ignored
    # (the audit's dead-option finding); the numbers are named only
    # when a transcode value is changed and they are actually applied.
    assert "relayed to the live view as it arrives" in text
    section = dict(ENABLED_SECTION, activity_origin="https://pb.example")
    assert "https://pb.example" in ls.pointer_text(session, section)
    tuned = dict(ENABLED_SECTION, max_height=1080, fps=30, max_bitrate_kbps=6000)
    tuned_text = ls.pointer_text(session, tuned)
    assert "1080p at 30 fps" in tuned_text and "6000 kbit/s" in tuned_text
    assert "transcoded to this plan" in tuned_text


def test_session_text_reports_off_ready_and_live_states():
    assert "off" in ls.session_text(ls.LiveStreamSession(), {}, now=0.0)
    assert "no voice channel" in ls.session_text(
        ls.LiveStreamSession(), {"enabled": True}, now=0.0)
    ready = ls.session_text(ls.LiveStreamSession(), ENABLED_SECTION, now=0.0)
    assert "ready but not running" in ready and "<#123456789012345678>" in ready
    session = ls.LiveStreamSession().start("42", now=1000.0)
    live = ls.session_text(session, ENABLED_SECTION, now=1125.0)
    assert "is on" in live and "<@42>" in live and "2 min 5 s" in live
    assert "nobody in the channel yet" in live
    session.note_viewers(3, now=1130.0)
    assert "3 watching" in ls.session_text(session, ENABLED_SECTION, now=1130.0)


# --------------------------------------------------------------------- stage 4: lifecycle helpers

AUTO_SECTION = dict(ENABLED_SECTION, auto_start_on_print=True,
                    activity_origin="https://pb.example")


def test_auto_start_fires_only_when_the_switch_and_config_allow_it():
    session = ls.LiveStreamSession()
    text = ls.auto_start(session, AUTO_SECTION, now=1000.0)
    assert text is not None and "activity" in text and session.is_live
    assert session.started_by == ls.AUTO_STARTED_BY
    # Already live: no second start, no second pointer.
    assert ls.auto_start(session, AUTO_SECTION, now=1001.0) is None
    # Switch off: nothing happens.
    idle = ls.LiveStreamSession()
    assert ls.auto_start(idle, ENABLED_SECTION, now=1000.0) is None
    assert not idle.is_live
    # Enabled but unconfigured (no channel): nothing happens, no error.
    assert ls.auto_start(idle, dict(AUTO_SECTION, voice_channel_id=""), now=1000.0) is None
    assert not idle.is_live


def test_expire_empty_ends_only_a_timed_out_empty_session():
    session = ls.LiveStreamSession().start("42", now=1000.0)
    assert ls.expire_empty(session, AUTO_SECTION, now=1000.0 + 299) is False
    assert session.is_live
    assert ls.expire_empty(session, AUTO_SECTION, now=1000.0 + 300) is True
    assert not session.is_live
    # An idle session is untouched, and a watched one survives.
    assert ls.expire_empty(session, AUTO_SECTION, now=9999.0) is False
    watched = ls.LiveStreamSession().start("42", now=1000.0)
    watched.note_viewers(1, now=1100.0)
    assert ls.expire_empty(watched, AUTO_SECTION, now=1000.0 + 10000) is False
    assert watched.is_live


# ------------------------------------------------- stage 4: shared-record locking

def _live_record(tmp_path):
    path = tmp_path / "live_session.json"
    ls.write_session_file(ls.LiveStreamSession().start("42", now=1000.0), path)
    return path


def test_transact_session_file_serializes_concurrent_writers(tmp_path):
    # P2-143: two writers' read-modify-write transactions must never
    # overlap — each one reads the record the previous one wrote.
    import threading
    import time as time_module

    path = _live_record(tmp_path)
    intervals = []

    def make_update(tag):
        def update(session):
            began = time_module.monotonic()
            time_module.sleep(0.25)
            intervals.append((tag, began, time_module.monotonic()))
            session.note_viewers(1, now=1001.0)
            return True
        return update

    threads = [threading.Thread(target=ls.transact_session_file,
                                 args=(path, make_update(tag)), daemon=True)
               for tag in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert len(intervals) == 2
    (first, second) = sorted(intervals, key=lambda row: row[1])
    assert first[2] <= second[1], "session transactions overlapped"


def test_transact_session_file_a_stop_cannot_be_overwritten_by_a_stale_update(tmp_path):
    # P2-143's resurrection interleaving: a slow presence-style update
    # holds its transaction while a stop arrives. The stop waits for
    # the lock, then lands last — the record ends idle, not with the
    # stale live snapshot written over the stop.
    import threading
    import time as time_module

    path = _live_record(tmp_path)
    entered = threading.Event()
    release = threading.Event()

    def slow_presence(session):
        entered.set()
        release.wait(timeout=10)
        session.note_viewers(3, now=1001.0)
        return True

    def stop(session):
        session.stop(now=1002.0)
        return True

    first = threading.Thread(target=ls.transact_session_file,
                             args=(path, slow_presence), daemon=True)
    first.start()
    assert entered.wait(timeout=5)
    second = threading.Thread(target=ls.transact_session_file,
                              args=(path, stop), daemon=True)
    second.start()
    time_module.sleep(0.3)
    assert second.is_alive(), "the stop did not wait for the in-flight transaction"
    release.set()
    first.join(timeout=10)
    second.join(timeout=10)
    assert not ls.read_session_file(path).is_live


def test_transact_session_file_persists_only_when_the_update_asks(tmp_path):
    path = _live_record(tmp_path)
    before = path.read_text()
    session = ls.transact_session_file(path, lambda seen: False)
    assert session.is_live
    assert path.read_text() == before
