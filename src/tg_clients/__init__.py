"""Telegram client layer -- proxy pool, user clients, and official bot.

Uses Telethon for user accounts (reusing the existing .session files
created by scripts/smart_onboard.py) and python-telegram-bot for the
official bot. The previous TDLib path was removed in favour of Telethon
to match the onboarding tooling.
"""

from src.tg_clients.bot_client import OfficialBot
from src.tg_clients.proxy_pool import ProxyConfig, ProxyPool
from src.tg_clients.user_client import TDLibClient, UserClientManager

__all__ = [
    "OfficialBot",
    "ProxyConfig",
    "ProxyPool",
    "TDLibClient",
    "UserClientManager",
]
