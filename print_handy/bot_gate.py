"""Shell-facing probe: should the container start the standalone chat bot?

docker/supervise.sh runs ``python3 -m print_handy.bot_gate`` before
starting the bot, and docker/healthcheck.py uses the same decision for
its bot row, mirroring print_handy.listener_gate and webui_gate. The
decision comes from the resolved configuration: the ``[bot] platform``
(discord, telegram or matrix) and that platform's own token variable
(``discord.token_env``, ``telegram.token_env`` or
``matrix.access_token_env``). Before this gate the supervisor checked
``DISCORD_BOT_TOKEN`` alone, so a Telegram- or Matrix-only container
never started its bot and the healthcheck still reported it fine.

Exit codes: 0 = the configured platform has its token, start the bot;
3 = its token is empty, start nothing (the reason is printed); 2 = the
configuration could not be resolved (the supervisor stops rather than
guessing).
"""
from __future__ import annotations

import sys


def decide():
    """(start, platform, detail). Raises on an unresolved configuration."""
    from .runtime import _config
    cfg = _config.cfg()
    name = str((cfg.get("bot") or {}).get("platform") or "discord").strip().lower()
    if name == "telegram":
        from .bot.telegram import telegram_token
        env = str((cfg.get("telegram") or {}).get("token_env") or "TELEGRAM_BOT_TOKEN")
        token = telegram_token(cfg)
    elif name == "matrix":
        from .bot.matrix import matrix_token
        env = str((cfg.get("matrix") or {}).get("access_token_env") or "MATRIX_ACCESS_TOKEN")
        token = matrix_token(cfg)
    else:
        env = str((cfg.get("discord") or {}).get("token_env") or "DISCORD_BOT_TOKEN")
        token = _config.discord_token()
    if token:
        return True, name, f"{name} bot: ${env} is set"
    return False, name, f"{env} is empty; {name} bot disabled."


def main():
    try:
        start, _name, detail = decide()
    except Exception as exc:  # configuration unresolved: stop, never guess
        print(f"bot gate: configuration unresolved: {exc} [PH_GATE_CONFIGURATION]", file=sys.stderr)
        return 2
    from . import settings
    warning = settings.unknown_settings_warning()
    if warning:
        print(f"bot gate: WARNING {warning}", file=sys.stderr)
    print(detail)
    return 0 if start else 3


if __name__ == "__main__":
    sys.exit(main())
