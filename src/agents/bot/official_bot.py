"""Official Telegram Bot - re-exports from the canonical implementation.

The full OfficialBot implementation lives in ``src.tg_clients.bot_client``.
This module re-exports it so that ``src.agents.bot.official_bot.OfficialBot``
remains a valid import path.
"""

from src.tg_clients.bot_client import OfficialBot

__all__ = ["OfficialBot"]
