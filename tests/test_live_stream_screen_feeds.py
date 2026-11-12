"""Printer screen feeds are never selected as the bed camera (issue #1306).

Z-Mod redirects the printer's physical screen as a virtual Moonraker
webcam (the ``WEB_SCREEN`` global parameter, on by default): the screen
appears in the webcams list as an enabled entry named ``screen``, next
to the real camera. HelixScreen's screen sharing registers a webcam
named ``HelixScreen Screen``, and its built-in remote screen is the
``gui`` webcam. None of those feeds shows the build plate: they show
the printer's own display, which can carry prompts and on-screen
information an operator never asked to stream, snapshot or monitor.

The resolvers in :mod:`print_handy.live_stream` are the one selection
layer every registry-based camera consumer shares, so the rule is
pinned here: automatic selection never lands on a screen feed, a
screen-only registry is a clear unavailable-camera error (never a
silent screen stream), and names that are not screen evidence keep
working exactly as before.

Offline only: recorded payload shapes, no network or printer.
"""
import pytest

from print_handy import live_stream as ls

BASE = "http://printer.local:7125"

BEDCAM = {
    "name": "webcam",
    "location": "printer",
    "service": "mjpegstreamer",
    "stream_url": "/webcam/?action=stream",
    "snapshot_url": "/webcam/?action=snapshot",
    "enabled": True,
}

# Z-Mod's WEB_SCREEN redirect: the printer screen as a virtual camera.
ZMOD_SCREEN = {
    "name": "screen",
    "location": "printer",
    "service": "mjpegstreamer",
    "stream_url": "/webcam2/?action=stream",
    "snapshot_url": "/webcam2/?action=snapshot",
    "enabled": True,
}

HELIX_SCREEN = {
    "name": "HelixScreen Screen",
    "location": "printer",
    "service": "mjpegstreamer",
    "stream_url": "http://printer.local:8081/?action=stream",
    "snapshot_url": "http://printer.local:8081/?action=snapshot",
    "enabled": True,
}


# --------------------------------------------------------------------- the risk

def test_screen_listed_first_is_not_the_automatic_pick():
    """The regression: a Z-Mod list whose first usable entry is the screen
    used to resolve to the screen. It must resolve to the bed camera."""
    source = ls.camera_source_from_moonraker(
        {"webcams": [ZMOD_SCREEN, BEDCAM]}, base_url=BASE)
    assert source.name == "webcam"
    assert source.snapshot_url == "http://printer.local:7125/webcam/?action=snapshot"


def test_screen_listed_after_the_camera_is_not_picked_either():
    source = ls.camera_source_from_moonraker(
        {"webcams": [BEDCAM, ZMOD_SCREEN]}, base_url=BASE)
    assert source.name == "webcam"


def test_helixscreen_screen_feed_is_skipped():
    source = ls.camera_source_from_moonraker(
        {"webcams": [HELIX_SCREEN, BEDCAM]}, base_url=BASE)
    assert source.name == "webcam"


def test_screen_feed_is_skipped_for_octoprint_multi_webcam_lists():
    """The picker is shared: an OctoPrint-style webcams list gets the
    same protection, not just the Moonraker resolver."""
    payload = {"webcams": [dict(ZMOD_SCREEN), dict(BEDCAM)]}
    source = ls.camera_source_from_octoprint(payload, base_url=BASE)
    assert source.name == "webcam"


# --------------------------------------------------------------------- screen-only

def test_screen_only_registry_is_a_clear_unavailable_error():
    """A registry whose only usable entry is the screen must not stream
    the screen: it raises, and the message names the reason."""
    with pytest.raises(ls.LiveStreamError) as excinfo:
        ls.camera_source_from_moonraker({"webcams": [ZMOD_SCREEN]}, base_url=BASE)
    message = str(excinfo.value).lower()
    assert "screen" in message
    assert "camera" in message


def test_screen_only_registry_still_honors_the_fallback_snapshot():
    """No usable camera in the registry is the fallback case as before:
    the configured snapshot convention applies, not the screen."""
    source = ls.camera_source_from_moonraker(
        {"webcams": [ZMOD_SCREEN]}, base_url=BASE,
        fallback_snapshot_url="http://printer.local:7125/webcam/?action=snapshot")
    assert source.snapshot_url == "http://printer.local:7125/webcam/?action=snapshot"
    assert source.name != "screen"


def test_screen_only_octoprint_list_is_a_clear_unavailable_error():
    with pytest.raises(ls.LiveStreamError) as excinfo:
        ls.camera_source_from_octoprint({"webcams": [dict(ZMOD_SCREEN)]}, base_url=BASE)
    assert "screen" in str(excinfo.value).lower()


# --------------------------------------------------------------------- entry shapes

def test_disabled_screen_and_enabled_camera_picks_the_camera():
    payload = {"webcams": [dict(ZMOD_SCREEN, enabled=False), BEDCAM]}
    source = ls.camera_source_from_moonraker(payload, base_url=BASE)
    assert source.name == "webcam"


def test_disabled_camera_and_enabled_screen_is_unavailable_not_the_screen():
    payload = {"webcams": [dict(BEDCAM, enabled=False), ZMOD_SCREEN]}
    with pytest.raises(ls.LiveStreamError):
        ls.camera_source_from_moonraker(payload, base_url=BASE)


def test_unnamed_entry_is_still_a_camera():
    """An entry with no name carries no screen evidence: it stays
    selectable, so unnamed legitimate cameras keep working."""
    unnamed = {key: value for key, value in BEDCAM.items() if key != "name"}
    source = ls.camera_source_from_moonraker(
        {"webcams": [ZMOD_SCREEN, unnamed]}, base_url=BASE)
    assert source.name == ""
    assert source.snapshot_url == "http://printer.local:7125/webcam/?action=snapshot"


def test_empty_named_entry_is_still_a_camera():
    source = ls.camera_source_from_moonraker(
        {"webcams": [dict(BEDCAM, name="")]}, base_url=BASE)
    assert source.name == ""


# --------------------------------------------------------------------- explicit choice

def test_explicitly_preferred_screen_is_the_operators_choice():
    """A preferred name is an explicit operator configuration, not
    automatic selection: naming the screen feed gets the screen feed,
    under its real name, so it is never mislabeled as a bed view. The
    separated, labeled screen view itself is issue #1303's feature."""
    source = ls.camera_source_from_moonraker(
        {"webcams": [BEDCAM, ZMOD_SCREEN]}, base_url=BASE, preferred_name="screen")
    assert source.name == "screen"


def test_preferred_real_camera_wins_over_the_screen():
    source = ls.camera_source_from_moonraker(
        {"webcams": [ZMOD_SCREEN, BEDCAM]}, base_url=BASE, preferred_name="webcam")
    assert source.name == "webcam"


def test_unmatched_preference_falls_back_without_landing_on_the_screen():
    """A configured name that matches nothing makes selection automatic
    again, and automatic selection still skips the screen."""
    source = ls.camera_source_from_moonraker(
        {"webcams": [ZMOD_SCREEN, BEDCAM]}, base_url=BASE, preferred_name="nosuchcam")
    assert source.name == "webcam"


def test_unmatched_preference_with_only_a_screen_is_unavailable():
    with pytest.raises(ls.LiveStreamError):
        ls.camera_source_from_moonraker(
            {"webcams": [ZMOD_SCREEN]}, base_url=BASE, preferred_name="nosuchcam")


# --------------------------------------------------------------------- classification

@pytest.mark.parametrize("name", [
    "screen", "Screen", "SCREEN", " screen ",
    "HelixScreen Screen", "helixscreen screen",
    "printer screen", "Printer Screen", "touchscreen", "screen 2",
])
def test_screen_feed_names_are_classified(name):
    assert ls.is_screen_feed_name(name) is True


@pytest.mark.parametrize("name", [
    "webcam", "bedcam", "nozzlecam", "chamber", "camera", "cam1",
    "Pi Cam", "c920", "top view", "enclosure cam",
    "", "   ", None, 42,
])
def test_camera_names_are_not_classified_as_screens(name):
    """Legitimate cameras, including ambiguous and missing names, are
    never classified as screen feeds."""
    assert ls.is_screen_feed_name(name) is False


def test_gui_webcam_is_a_screen_feed():
    """HelixScreen's built-in remote screen is exposed as the ``gui``
    webcam: a feed of the screen UI, not of the build plate."""
    assert ls.is_screen_feed_name("gui") is True
    payload = {"webcams": [dict(ZMOD_SCREEN, name="gui"), BEDCAM]}
    source = ls.camera_source_from_moonraker(payload, base_url=BASE)
    assert source.name == "webcam"
