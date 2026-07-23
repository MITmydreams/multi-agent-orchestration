"""Check a Telegram account's health status via Telethon.

Connects through the assigned proxy, verifies the account is not restricted,
and lists joined groups.

Usage:
    cd /Users/hermit/Desktop/RWANS/Rwans_op/RWANS_TG_OP/promo-bot
    source .venv/bin/activate
    PYTHONPATH=. python scripts/check_account.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

load_dotenv()


async def check_account() -> None:
    """Load Telethon session and check account health."""

    # --- Configuration ---
    session_file = "tdlib_sessions/account_1/10000000001"  # Telethon adds .session
    api_id = int(os.getenv("TG_API_ID", "2040"))
    api_hash = os.getenv("TG_API_HASH", "b18441a1ff607e10a989891a5462e627")

    # Proxy config (from config/proxies.json, proxy_id=1)
    proxy_config = {
        "host": "127.0.0.1",
        "port": 443,
        "username": "novLpURukVhWz",
        "password": "9D5aEctSyc",
    }

    # --- Import Telethon & socks ---
    try:
        from telethon import TelegramClient
        from telethon.tl.functions.channels import GetFullChannelRequest
        from telethon.tl.types import (
            Channel,
            Chat,
            User,
            UserStatusOffline,
            UserStatusOnline,
            UserStatusRecently,
        )
    except ImportError:
        print("ERROR: telethon not installed. Run: pip install telethon")
        sys.exit(1)

    try:
        import socks
    except ImportError:
        print("ERROR: PySocks not installed. Run: pip install PySocks")
        sys.exit(1)

    # --- Setup proxy ---
    proxy = (
        socks.SOCKS5,
        proxy_config["host"],
        proxy_config["port"],
        True,
        proxy_config["username"],
        proxy_config["password"],
    )

    # --- Create Telethon client ---
    client = TelegramClient(
        session_file,
        api_id,
        api_hash,
        proxy=proxy,
    )

    print(f"Connecting to Telegram via proxy {proxy_config['host']}:{proxy_config['port']}...")

    try:
        await client.connect()

        if not await client.is_user_authorized():
            print("ERROR: Account is not authorized. Session may be expired.")
            await client.disconnect()
            return

        # --- Get account info ---
        me = await client.get_me()

        print(f"\n{'='*60}")
        print(f"  ACCOUNT HEALTH REPORT")
        print(f"{'='*60}")
        print(f"  User ID:       {me.id}")
        print(f"  Phone:         +{me.phone}")
        print(f"  Name:          {me.first_name or ''} {me.last_name or ''}")
        print(f"  Username:      @{me.username or 'N/A'}")
        print(f"  Verified:      {me.verified}")
        print(f"  Restricted:    {me.restricted}")
        print(f"  Scam:          {me.scam}")
        print(f"  Fake:          {me.fake}")
        print(f"  Premium:       {me.premium}")

        if me.restriction_reason:
            print(f"\n  RESTRICTION REASONS:")
            for reason in me.restriction_reason:
                print(f"    - Platform: {reason.platform}, Reason: {reason.reason}, Text: {reason.text}")

        # --- Health Assessment ---
        health_ok = True
        issues = []

        if me.restricted:
            health_ok = False
            issues.append("Account is RESTRICTED")

        if me.scam:
            health_ok = False
            issues.append("Account is flagged as SCAM")

        if me.fake:
            health_ok = False
            issues.append("Account is flagged as FAKE")

        # --- List joined groups/channels ---
        print(f"\n  JOINED GROUPS & CHANNELS:")
        print(f"  {'-'*54}")

        groups = []
        channels = []
        dialogs = await client.get_dialogs()

        for dialog in dialogs:
            entity = dialog.entity
            if isinstance(entity, Channel):
                if entity.megagroup:
                    groups.append({
                        "id": entity.id,
                        "title": entity.title,
                        "username": entity.username or "",
                        "participants": entity.participants_count or 0,
                    })
                else:
                    channels.append({
                        "id": entity.id,
                        "title": entity.title,
                        "username": entity.username or "",
                    })
            elif isinstance(entity, Chat):
                groups.append({
                    "id": entity.id,
                    "title": entity.title,
                    "username": "",
                    "participants": entity.participants_count or 0,
                })

        print(f"  Groups ({len(groups)}):")
        if groups:
            for g in groups:
                username_str = f" (@{g['username']})" if g['username'] else ""
                print(f"    - {g['title']}{username_str}  [{g['participants']} members]")
        else:
            print(f"    (none)")

        print(f"\n  Channels ({len(channels)}):")
        if channels:
            for c in channels:
                username_str = f" (@{c['username']})" if c['username'] else ""
                print(f"    - {c['title']}{username_str}")
        else:
            print(f"    (none)")

        # --- Summary ---
        print(f"\n  {'-'*54}")
        if health_ok:
            print(f"  STATUS: HEALTHY")
            print(f"  The account is in good standing, no restrictions detected.")
        else:
            print(f"  STATUS: UNHEALTHY")
            for issue in issues:
                print(f"    - {issue}")

        print(f"  Total groups:   {len(groups)}")
        print(f"  Total channels: {len(channels)}")
        print(f"  Total dialogs:  {len(dialogs)}")
        print(f"{'='*60}")

    except Exception as e:
        print(f"\nERROR: {e}")
        import traceback
        traceback.print_exc()
    finally:
        await client.disconnect()
        print("\nDisconnected.")


if __name__ == "__main__":
    asyncio.run(check_account())
