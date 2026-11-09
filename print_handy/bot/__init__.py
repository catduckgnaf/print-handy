"""Standalone Discord bot: Print Handy without Hermes and without an LLM (#73).

    python3 -m print_handy.bot            run the bot (bot.mode must be standalone)
    python3 -m print_handy.bot check      validate the setup offline, exit 1 on a problem
    python3 -m print_handy.bot health     exit 0 only while a running bot reports a fresh heartbeat

The bot runs the print-feedback handlers in their plugin order, with
Print Handy's own Discord adapter and prompt registry. Importing this package does not import
discord.py; only the runtime does.
"""
