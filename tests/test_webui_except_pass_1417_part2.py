"""Recovery behavior for Web UI silent exception handlers (#1417 part 2)."""
from unittest import mock

from print_handy.webui import data, instance, server, walkthrough, wrapped_video


def test_unreadable_records_directory_does_not_reuse_a_fingerprint():
    with mock.patch.object(data.os, "scandir", side_effect=PermissionError("denied")), \
            mock.patch.object(data.logger, "debug") as debug:
        first = data._records_fingerprint("/records")
        second = data._records_fingerprint("/records")
    assert first != second
    debug.assert_called()


def test_invalid_timezone_falls_back_to_configured_timezone():
    configured = object()
    with mock.patch("print_handy.webui.data.ZoneInfo", side_effect=ValueError("bad zone"),
                    create=True), mock.patch("print_handy.runtime._config.tz",
                                            return_value=configured):
        assert data._display_tz({"discord": {"timezone": "not/a-zone"}}) is configured


def test_clip_roots_stay_available_when_state_folder_resolution_fails():
    with mock.patch("print_handy.settings.state_dir", side_effect=RuntimeError("bad state")):
        roots = wrapped_video._clip_roots({"paths": {"cache": "/cache"}})
    assert roots == [wrapped_video.Path("/cache/moonraker-announcements")]


def test_walkthrough_completion_record_failure_is_logged_and_soft():
    with mock.patch.object(walkthrough.setup_state, "record_completion",
                           side_effect=OSError("read only")), \
            mock.patch.object(walkthrough.logger, "warning") as warning:
        walkthrough._mark_done({}, {"step": 5})
    warning.assert_called_once()
    assert "PH_SETUP_COMPLETION" in warning.call_args.args[0]


def test_walkthrough_reopen_write_failure_is_logged_and_soft():
    with mock.patch.object(walkthrough, "load_state", return_value={"done": True}), \
            mock.patch.object(walkthrough, "save_state", side_effect=OSError("read only")), \
            mock.patch.object(walkthrough.logger, "warning") as warning:
        walkthrough.reopen_at_login({})
    warning.assert_called_once()
    assert "PH_WALKTHROUGH_STATE" in warning.call_args.args[0]


def test_instance_id_temp_cleanup_failure_is_logged_without_failing_mint(tmp_path):
    path = tmp_path / "instance_id"
    with mock.patch.object(instance.os, "unlink", side_effect=PermissionError("denied")), \
            mock.patch.object(instance.uuid, "uuid4") as uuid4, \
            mock.patch.object(instance.logger, "debug") as debug:
        uuid4.return_value.hex = "a" * 32
        assert instance._mint(path) == "a" * 32
    debug.assert_called_once()
    assert "PH_INSTANCE_ID" in debug.call_args.args[0]


def test_webui_access_log_override_never_prints_request_details():
    handler = server._Handler.__new__(server._Handler)
    with mock.patch.object(server.log, "debug") as debug:
        handler.log_message("sensitive request path")
    debug.assert_called_once()
    assert "sensitive request path" not in str(debug.call_args)


def test_video_progress_callback_failure_is_soft_and_logged():
    callback = mock.Mock(side_effect=RuntimeError("observer failed"))
    with mock.patch.object(wrapped_video.logger, "debug") as debug:
        wrapped_video._notify_progress(callback, 1, 2)
    callback.assert_called_once_with(1, 2)
    debug.assert_called_once()
    assert "PH_WRAPPED_PROGRESS" in debug.call_args.args[0]
