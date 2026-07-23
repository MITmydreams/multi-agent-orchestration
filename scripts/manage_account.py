"""Single account management tool.

Perform various management operations on a single Telegram account.

Usage:
    python scripts/manage_account.py --phone 10000000001 --action check      # Check status
    python scripts/manage_account.py --phone 10000000001 --action groups     # List joined groups
    python scripts/manage_account.py --phone 10000000001 --action hibernate  # Set to hibernating
    python scripts/manage_account.py --phone 10000000001 --action activate   # Set to active
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")

import socks
from telethon import TelegramClient
from telethon.tl.functions.account import GetAuthorizationsRequest
from sqlalchemy import select

from src.models.base import get_session
from src.models.account import Account
from src.models.group import GroupAccount


PROXIES_FILE = PROJECT_ROOT / "config" / "proxies.json"

VALID_ACTIONS = ["check", "groups", "hibernate", "activate"]


def load_proxies() -> dict[int, dict]:
    if PROXIES_FILE.exists():
        proxies = json.loads(PROXIES_FILE.read_text())
        return {p["id"]: p for p in proxies}
    return {}


def make_proxy_tuple(proxy: dict) -> tuple:
    proto = proxy.get("protocol", "socks5").lower()
    proxy_type = socks.SOCKS5 if proto.startswith("socks") else socks.HTTP
    if proxy.get("username") and proxy.get("password"):
        return (proxy_type, proxy["host"], proxy["port"], True, proxy["username"], proxy["password"])
    return (proxy_type, proxy["host"], proxy["port"])


async def get_client(account: Account, proxies_map: dict) -> TelegramClient | None:
    """Create and connect a Telethon client for the given account."""
    api_id = int(os.getenv("TG_API_ID", "0"))
    api_hash = os.getenv("TG_API_HASH", "")

    if not api_id or not api_hash:
        print("[ERROR] TG_API_ID/TG_API_HASH not set in .env")
        return None

    session_file = account.session_string
    if not session_file or not Path(session_file).exists():
        print(f"[ERROR] Session file not found: {session_file}")
        return None

    session_name = session_file
    if session_name.endswith(".session"):
        session_name = session_name[:-8]

    proxy = proxies_map.get(account.proxy_id)
    proxy_tuple = make_proxy_tuple(proxy) if proxy else None

    client = TelegramClient(session_name, api_id, api_hash, proxy=proxy_tuple)
    await client.connect()

    if not await client.is_user_authorized():
        print(f"[ERROR] Account not authorized (session expired)")
        await client.disconnect()
        return None

    return client


async def action_check(account: Account, proxies_map: dict):
    """Check account status live on Telegram."""
    print(f"\n  Checking +{account.phone}...")

    client = await get_client(account, proxies_map)
    if not client:
        return

    try:
        me = await client.get_me()

        print(f"\n  {'='*50}")
        print(f"  ACCOUNT STATUS: +{account.phone}")
        print(f"  {'='*50}")
        print(f"  Telegram ID:   {me.id}")
        print(f"  Username:      @{me.username or 'N/A'}")
        print(f"  Name:          {me.first_name or ''} {me.last_name or ''}")
        print(f"  Phone:         +{me.phone or account.phone}")
        print(f"  Bot:           {me.bot}")
        print(f"  Restricted:    {getattr(me, 'restricted', False)}")

        if getattr(me, "restricted", False) and hasattr(me, "restriction_reason"):
            print(f"  Reason:        {me.restriction_reason}")

        print(f"  Verified:      {getattr(me, 'verified', False)}")
        print(f"  Premium:       {getattr(me, 'premium', False)}")

        # DB info
        print(f"\n  --- Database Info ---")
        print(f"  DB ID:         {account.id}")
        print(f"  Role:          {account.role}")
        print(f"  Status:        {account.status}")
        print(f"  Risk Score:    {account.risk_score}")
        print(f"  Trust Score:   {account.trust_score}")
        print(f"  Proxy:         #{account.proxy_id}")
        print(f"  Msgs Today:    {account.messages_sent_today}")
        print(f"  Promo Today:   {account.outreach_messages_today}")
        print(f"  Total Msgs:    {account.total_messages}")
        print(f"  Kicked:        {account.kicked_count}")
        print(f"  Last Active:   {account.last_active or 'never'}")
        print(f"  Created:       {account.created_at}")

        # Get active sessions count
        try:
            auths = await client(GetAuthorizationsRequest())
            print(f"\n  --- Active Sessions ---")
            print(f"  Total sessions: {len(auths.authorizations)}")
            for auth in auths.authorizations[:5]:
                current = " (current)" if auth.current else ""
                print(f"    - {auth.app_name} on {auth.device_model}{current}")
            if len(auths.authorizations) > 5:
                print(f"    ... and {len(auths.authorizations) - 5} more")
        except Exception as e:
            print(f"  [WARN] Could not fetch sessions: {e}")

        # Count groups
        group_count = 0
        channel_count = 0
        try:
            async for d in client.iter_dialogs():
                if d.is_group:
                    group_count += 1
                elif d.is_channel:
                    channel_count += 1
        except Exception:
            pass

        print(f"\n  --- Groups & Channels ---")
        print(f"  Groups:        {group_count}")
        print(f"  Channels:      {channel_count}")
        print(f"  {'='*50}")

    finally:
        await client.disconnect()


async def action_groups(account: Account, proxies_map: dict):
    """List all groups/channels the account has joined."""
    print(f"\n  Fetching groups for +{account.phone}...")

    client = await get_client(account, proxies_map)
    if not client:
        return

    try:
        groups = []
        channels = []

        async for dialog in client.iter_dialogs():
            entity = dialog.entity
            info = {
                "id": dialog.id,
                "title": dialog.title or dialog.name,
                "username": getattr(entity, "username", None) or "",
                "members": getattr(entity, "participants_count", 0) or 0,
                "unread": dialog.unread_count,
            }

            if dialog.is_group:
                groups.append(info)
            elif dialog.is_channel:
                channels.append(info)

        print(f"\n  {'='*80}")
        print(f"  GROUPS for +{account.phone}  ({len(groups)} groups, {len(channels)} channels)")
        print(f"  {'='*80}")

        if groups:
            print(f"\n  Groups ({len(groups)}):")
            print(f"  {'ID':<16} {'Title':<30} {'Username':<22} {'Members':<10} {'Unread'}")
            print(f"  {'-'*16} {'-'*30} {'-'*22} {'-'*10} {'-'*6}")
            for g in groups:
                title = g["title"][:28] if g["title"] else "-"
                uname = f"@{g['username']}" if g["username"] else "-"
                print(f"  {g['id']:<16} {title:<30} {uname:<22} {g['members']:<10} {g['unread']}")

        if channels:
            print(f"\n  Channels ({len(channels)}):")
            print(f"  {'ID':<16} {'Title':<30} {'Username':<22} {'Members':<10} {'Unread'}")
            print(f"  {'-'*16} {'-'*30} {'-'*22} {'-'*10} {'-'*6}")
            for c in channels:
                title = c["title"][:28] if c["title"] else "-"
                uname = f"@{c['username']}" if c["username"] else "-"
                print(f"  {c['id']:<16} {title:<30} {uname:<22} {c['members']:<10} {c['unread']}")

        if not groups and not channels:
            print("  No groups or channels found.")

        print(f"  {'='*80}")

    finally:
        await client.disconnect()


async def action_hibernate(account: Account):
    """Set account to hibernating status."""
    print(f"\n  Hibernating +{account.phone}...")

    if account.status == "hibernating":
        print(f"  [INFO] Account is already hibernating.")
        return

    old_status = account.status
    hibernate_days = 7

    async with get_session() as session:
        stmt = select(Account).where(Account.phone == account.phone)
        result = await session.execute(stmt)
        acc = result.scalar_one_or_none()
        if acc:
            acc.status = "hibernating"
            acc.hibernated_until = datetime.utcnow() + timedelta(days=hibernate_days)

    print(f"  [OK] Status changed: {old_status} -> hibernating")
    print(f"  [OK] Hibernated until: {datetime.utcnow() + timedelta(days=hibernate_days):%Y-%m-%d %H:%M} UTC")
    print(f"  [INFO] Account will not be used for any operations during hibernation.")


async def action_activate(account: Account):
    """Set account to active status."""
    print(f"\n  Activating +{account.phone}...")

    if account.status == "active":
        print(f"  [INFO] Account is already active.")
        return

    if account.status == "abandoned":
        print(f"  [WARN] Account was abandoned. Are you sure it's safe to reactivate?")

    old_status = account.status

    async with get_session() as session:
        stmt = select(Account).where(Account.phone == account.phone)
        result = await session.execute(stmt)
        acc = result.scalar_one_or_none()
        if acc:
            acc.status = "active"
            acc.hibernated_until = None
            acc.activated_date = datetime.utcnow()

    print(f"  [OK] Status changed: {old_status} -> active")
    print(f"  [INFO] Account is now available for operations.")


async def main():
    parser = argparse.ArgumentParser(description="Single account management tool")
    parser.add_argument(
        "--phone", required=True,
        help="Phone number of the account to manage",
    )
    parser.add_argument(
        "--action", required=True, choices=VALID_ACTIONS,
        help="Action to perform: check, groups, hibernate, activate",
    )
    args = parser.parse_args()

    phone = args.phone.lstrip("+")
    proxies_map = load_proxies()

    # Fetch account from DB
    async with get_session() as session:
        stmt = select(Account).where(Account.phone == phone)
        result = await session.execute(stmt)
        account = result.scalar_one_or_none()

    if not account:
        print(f"[ERROR] Account +{phone} not found in database.")
        print(f"  Run batch_setup.py first to import accounts.")
        sys.exit(1)

    print(f"\n  Account: +{account.phone} (id={account.id}, role={account.role}, status={account.status})")

    if args.action == "check":
        await action_check(account, proxies_map)
    elif args.action == "groups":
        await action_groups(account, proxies_map)
    elif args.action == "hibernate":
        await action_hibernate(account)
    elif args.action == "activate":
        await action_activate(account)


if __name__ == "__main__":
    asyncio.run(main())
