"""Helpers every printer listener shares: the chat post path, bot tokens, pause classes, the vision route (#1320).

These used to live in ``moonraker_event_listener``, and the Bambu Lab and
family listeners imported the whole Moonraker listener (its websocket
settings, its camera-watch thread pool, its announcer) only to reach
them. They live here now, with no listener state at all; the Moonraker
listener re-exports every name it always had.

Each function resolves its collaborators through ``ns``: this module by
default, or the Moonraker listener module when it calls in. That keeps
one implementation while every long-standing ``moonraker_event_listener``
seam (``_post_rest``, ``_telegram_standalone``, ``_profile_env_value``,
``log`` ...) still steers the Moonraker listener's own posts, exactly as
before the move.
"""
from __future__ import annotations

import contextvars
import functools
import logging
import re
import sys

from .. import logs
from . import _config
from . import moonraker_discord_announcements as announcer
from . import starter_pings
from . import visual_watch as _visual_watch

logger = logging.getLogger(__name__)

DISCORD_TARGET = f"discord:{announcer.DISCORD_CHAT_ID}"


def log(line):
    """Record one routine event at INFO, redacted (#1413: the level is explicit)."""
    logger.info("%s", logs.redact(str(line)))


def warn(line):
    """Record one failure at WARNING, redacted, with the in-flight traceback if any (#1413)."""
    logger.warning("%s", logs.redact(str(line)), exc_info=sys.exc_info() if sys.exc_info()[0] else None)


def _ns(ns):
    return sys.modules[__name__] if ns is None else ns


# -- chat platform -----------------------------------------------------------

def _platform_name():
    """The standalone bot's chat platform ([bot] platform, #164); 'discord' unless configured."""
    return str(_config.cfg()["bot"].get("platform") or "discord")


def _telegram_standalone(ns=None):
    return _ns(ns)._platform_name() == "telegram"


def _telegram_token(ns=None):
    """The Telegram bot token, from the variable [telegram] token_env names."""
    return _ns(ns)._profile_env_value(
        str(_config.cfg()["telegram"].get("token_env") or "TELEGRAM_BOT_TOKEN"))


def _telegram_chat_id():
    return str(_config.cfg()["telegram"].get("chat_id") or "")


def _matrix_standalone(ns=None):
    return _ns(ns)._platform_name() == "matrix"


def _buttonless_standalone(ns=None):
    """The platform name when the bot's posts have no Discord buttons, else None.

    Telegram (#164 stage 2) and Matrix (stage 3) have no Discord-shaped
    button posts: the interactive offers go out as plain text and the
    operator answers with the typed commands the text names. The
    exceptions are ``post_alert`` itself: on Telegram it carries the
    alert's own buttons as an inline keyboard, and on Matrix as
    reactions (#1291).
    """
    ns = _ns(ns)
    if ns._telegram_standalone():
        return "telegram"
    if ns._matrix_standalone():
        return "matrix"
    return None


def _matrix_token(ns=None):
    """The Matrix access token, from the variable [matrix] access_token_env names."""
    return _ns(ns)._profile_env_value(
        str(_config.cfg()["matrix"].get("access_token_env") or "MATRIX_ACCESS_TOKEN"))


def _matrix_homeserver():
    return str(_config.cfg()["matrix"].get("homeserver_url") or "")


def _matrix_room_id():
    return str(_config.cfg()["matrix"].get("room_id") or "")


def _profile_env_value(name):
    """Read one profile env value. The listener units load no EnvironmentFile."""
    return _config.profile_env_value(name)


def _bot_token(ns=None):
    """The Discord bot token, from the variable [discord] token_env names (DISCORD_BOT_TOKEN by default)."""
    return _ns(ns)._profile_env_value(
        str(_config.cfg()["discord"].get("token_env") or "DISCORD_BOT_TOKEN"))


# -- the post path -----------------------------------------------------------

def post(message, *, mention_user_id=None, ping_kind=None, ping_job_key=None, ping_episode=None,
         ns=None):
    """Deliver physical print lifecycle text to the print chat only, with no agent.

    A ping for the print's starter is a send-path decision (#1095): pass
    ``ping_kind`` (and ``ping_job_key`` naming the print) and the starter
    pings classifier decides whether this post may ping, or pass a
    ``mention_user_id`` a caller already decided. With neither, the post
    pings nobody — the rendered text itself never carries a mention.
    """
    if mention_user_id is None and ping_kind:
        mention_user_id = starter_pings.ping_for(
            ping_kind, job_key=ping_job_key, episode=ping_episode) or None
    delivered = _ns(ns)._post_rest(message, mention_user_id=mention_user_id)
    if delivered and not _WEB_ALERT_POSTED.get():
        # Web chat parity: the post the chat got goes to web chat too
        # (bot/web_feed.py). After the delivery, never instead of it, and
        # only once it landed, so a retried completion is not repeated.
        _web_publish(message, ns=ns)
    return delivered


# Set while an alert that already went to web chat (with its buttons,
# _web_alert) is being delivered to the chat, so its plain-text post or
# fallback is not sent to web chat a second time. Each alert function
# runs in its own context (_web_scoped), so the flag never outlives it.
_WEB_ALERT_POSTED = contextvars.ContextVar("web_alert_posted", default=False)


def _web_publish(message, alert=None, ns=None):
    """Hand one post to web chat's feed (bot/web_feed.py). Never raises."""
    try:
        from print_handy.bot import web_feed
        cfg = _config.cfg()
        if not web_feed.enabled(cfg):
            return False
        return web_feed.publish(message, state_dir=_config.state_dir(),
                                platform=_ns(ns)._platform_name(), alert=alert)
    except Exception as exc:
        _ns(ns).warn(f"web chat feed publish failed: {exc!r}")
        return False


def _web_alert(message, kind, filename, *, connection=None, printer_id=None, identity=None,
               ns=None):
    """Send one alert to web chat with its buttons, before its chat delivery.

    The line names the alert (print_alert_buttons.alert_fields, the same
    routing the chat button records), so web chat offers the same actions
    and claims them through the alert store (bot/web.py). An alert is one
    post per episode with no retry, so it goes to web chat whether or not
    the chat post lands. Call it inside a ``_web_scoped`` function: the
    chat post that follows is then not mirrored again. Never raises.
    """
    try:
        from print_handy.bot import web_feed
        from . import print_alert_buttons as pab
        spec = web_feed.alert_spec(kind, filename, pab.alert_fields(
            connection=connection, printer_id=printer_id, identity=identity))
        _web_publish(message, alert=spec, ns=ns)
    except Exception as exc:
        _ns(ns).warn(f"web chat alert publish failed: {exc!r}")
    _WEB_ALERT_POSTED.set(True)


def _web_scoped(fn):
    """Run an alert function in its own context, so _web_alert's flag stays inside it."""
    @functools.wraps(fn)
    def run(*args, **kwargs):
        return contextvars.copy_context().run(fn, *args, **kwargs)
    return run


def _post_rest(message, mention_user_id=None, ns=None):
    """The configured platform's REST sender with its bot token.

    Works with no gateway connected. Discord is the original path; Telegram
    (#164 stage 2) posts text and MEDIA files through telegram_rest, Matrix
    (stage 3) through matrix_rest.
    """
    ns = _ns(ns)
    if ns._platform_name() == "telegram":
        from print_handy.bot import telegram_rest
        chat = ns._telegram_chat_id()
        if not chat:
            ns.log("post skipped: telegram.chat_id is not configured")
            return False
        ok, detail = telegram_rest.send(ns._telegram_token(), chat, message,
                                        **({"mention_user_id": mention_user_id} if mention_user_id else {}))
        ns.log(f"post rest target=telegram:{chat} ok={ok} {message.splitlines()[0][:120] if message else ''!r}"
               f"{'' if ok else ' ' + str(detail)[:200]}")
        return ok
    if ns._platform_name() == "matrix":
        from print_handy.bot import matrix_rest
        room = ns._matrix_room_id()
        if not room:
            ns.log("post skipped: matrix.room_id is not configured")
            return False
        ok, detail = matrix_rest.send(ns._matrix_homeserver(), ns._matrix_token(), room, message,
                                      **({"mention_user_id": mention_user_id} if mention_user_id else {}))
        ns.log(f"post rest target=matrix:{room} ok={ok} {message.splitlines()[0][:120] if message else ''!r}"
               f"{'' if ok else ' ' + str(detail)[:200]}")
        return ok
    return ns._post_rest_discord(message, mention_user_id=mention_user_id)


def _post_rest_discord(message, mention_user_id=None, ns=None):
    """Discord delivery through the bot's own REST sender with its bot token (#524).

    The one Discord post path: delivery through the bot's own REST sender,
    never a host CLI (#524). The token and chat id resolve from
    ([discord] token_env / chat_id, via
    the profile env). rest.send refuses honestly when either is missing —
    the failure is logged with its detail and False is returned; there is
    no silent fallback to another transport.
    """
    from print_handy.bot import rest
    ns = _ns(ns)
    # The one ping Discord is allowed to deliver is the starter ping the
    # caller decided through starter_pings (#1095); every other post
    # mentions nobody. The kwarg is passed only when a mention exists, so
    # the plain send call keeps its long-standing three-argument shape.
    kwargs = {"mention_user_id": mention_user_id} if mention_user_id else {}
    ok, detail = rest.send(ns._bot_token(), ns.announcer.DISCORD_CHAT_ID, message, **kwargs)
    ns.log(f"post rest target={ns.DISCORD_TARGET} ok={ok} {message.splitlines()[0][:120] if message else ''!r}"
           f"{'' if ok else ' ' + str(detail)[:200]}")
    return ok


# -- pause classes -----------------------------------------------------------

def classify_pause(message, where=""):
    """A pause is a filament runout only when the alert text says so.

    Moonraker's print_stats payload has no pause cause. Ordinary pauses stay ordinary.
    A jam (#1020 E1) is its own class: the printer's own pause text
    ("Filament jam detected (IFS)") names it, and it is checked first so
    a jam is never folded into the runout flow.
    """
    text = f"{message or ''} {where or ''}".casefold()
    # A whole word ("jam", "jammed", "jams"), never part of another word
    # or of a file name in the text ("pajama", "jam_jar.gcode").
    if re.search(r"(?<![a-z0-9_])jam(?:med|s)?(?![a-z0-9_])", text):
        return "jam"
    return "runout" if "runout" in text or "filament sensor" in text else "other"


def pause_is_jam(message, where=""):
    """True when the printer's own pause text reports an IFS jam (#1020 E1)."""
    return classify_pause(message, where) == "jam"


# -- the vision route --------------------------------------------------------

def _vision_chat_url():
    return _config.require(_config.service_url("vision_chat_url"), "services.vision_chat_url")


_VISION_MODEL = str(_config.cfg()["vision"]["model"])


def _vision_ready(ns=None):
    """Whether the camera watch has a vision endpoint to judge frames with (visual_watch.vision_ready)."""
    ns = _ns(ns)
    return _visual_watch.vision_ready(lambda: ns._profile_env_value("CLIPROXY_API_KEY"), ns.log)
