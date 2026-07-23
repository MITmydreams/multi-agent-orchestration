"""Account nurturing script - joins normal groups and starts lurking.

Phase 1 of the nurturing process:
- Join 3-5 normal public groups (tech news, tech, general)
- Set a natural bio if missing
- Just lurk - no messages for the first few days
- Build natural group membership history

Usage:
    python scripts/nurture_account.py --phone 10000000001
    python scripts/nurture_account.py --all  # nurture all active accounts
"""

import argparse
import asyncio
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import socks
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.account import UpdateProfileRequest
from telethon.errors import (
    FloodWaitError,
    ChannelPrivateError,
    UserBannedInChannelError,
    InviteRequestSentError,
)

from dotenv import load_dotenv
import os
import json

load_dotenv()

# Normal, legitimate public groups for nurturing
# These are real, active tech/tech groups that won't look suspicious
NURTURE_GROUPS = [
    # Tech news & discussion (large, public, safe)
    {"username": "techcurrency", "name": "Techcurrency", "category": "tech_news"},
    {"username": "bitcoin", "name": "Bitcoin", "category": "tech_news"},
    {"username": "SaaSWorld", "name": "SaaS World", "category": "saas"},
    {"username": "example_announce", "name": "Announcements Hub", "category": "announcement"},

    # Tech channels (read-only, very safe for nurturing)
    {"username": "coindesk", "name": "CoinDesk", "category": "news_channel"},
    {"username": "caborointelegraph", "name": "CoinTelegraph", "category": "news_channel"},
    {"username": "theblock_co", "name": "The Block", "category": "news_channel"},
    {"username": "binanceexchange", "name": "Binance", "category": "exchange"},

    # Gaming / Community (fits our project niche)
    {"username": "communitydaily", "name": "Community Daily", "category": "community"},
    {"username": "techgaming", "name": "Tech Gaming", "category": "gaming"},
]

# Natural bios for different personas
BIOS = {
    "en": [
        "Tech enthusiast since 2021 | SaaS & Gaming",
        "Community explorer | Love trying new projects",
        "Trader & gamer | Building in tech",
        "Tech & tech | Always learning",
        "SaaS degen | digital collectibles collector | 🎮",
    ],
}


def load_proxies():
    path = Path("config/proxies.json")
    if path.exists():
        return json.loads(path.read_text())
    return []


async def get_account_from_db(phone: str) -> dict | None:
    """Load account info from database."""
    from src.models.base import get_session
    from src.models.account import Account
    from sqlalchemy import select

    async with get_session() as session:
        result = await session.execute(
            select(Account).where(Account.phone == phone)
        )
        account = result.scalar_one_or_none()
        if account:
            return {
                "id": account.id,
                "phone": account.phone,
                "proxy_id": account.proxy_id,
                "session_file": account.session_string,
                "username": account.username,
                "display_name": account.display_name,
                "bio": account.bio,
                "role": account.role,
                "language": account.language or "en",
            }
    return None


async def connect_account(phone: str, session_file: str, proxy_config: dict | None) -> TelegramClient | None:
    """Connect to Telegram using existing session."""
    api_id = int(os.environ["TG_API_ID"])
    api_hash = os.environ["TG_API_HASH"]

    # Load session from file
    session_path = Path(session_file)
    if not session_path.exists():
        print(f"  ❌ Session file not found: {session_file}")
        return None

    proxy_tuple = None
    if proxy_config:
        proto = proxy_config.get("protocol", "socks5").lower()
        proxy_type = socks.SOCKS5 if proto.startswith("socks") else socks.HTTP
        proxy_tuple = (
            proxy_type,
            proxy_config["host"],
            proxy_config["port"],
            True,
            proxy_config.get("username", ""),
            proxy_config.get("password", ""),
        )

    client = TelegramClient(
        str(session_path).replace(".session", ""),
        api_id,
        api_hash,
        proxy=proxy_tuple,
    )

    await client.connect()
    if not await client.is_user_authorized():
        print(f"  ❌ Session expired for {phone}")
        await client.disconnect()
        return None

    return client


async def nurture_single_account(phone: str):
    """Run nurturing process for a single account."""
    print(f"\n{'='*60}")
    print(f"  Nurturing account: +{phone}")
    print(f"{'='*60}")

    # Load account from DB
    account = await get_account_from_db(phone)
    if not account:
        print(f"  ❌ Account not found in database")
        return False

    # Get proxy
    proxies = load_proxies()
    proxy = None
    for p in proxies:
        if p["id"] == account["proxy_id"]:
            proxy = p
            break

    print(f"  Proxy: #{account['proxy_id']} ({proxy['host'] if proxy else 'direct'})")
    print(f"  Role:  {account['role']}")

    # Connect
    client = await connect_account(phone, account["session_file"], proxy)
    if not client:
        return False

    me = await client.get_me()
    print(f"  Connected as: {me.first_name} (@{me.username or 'N/A'})")

    # Step 1: Update bio if empty
    # Get full user info (includes bio/about)
    from telethon.tl.functions.users import GetFullUserRequest
    full_user = await client(GetFullUserRequest(me))
    current_bio = full_user.full_user.about or ""

    bio = ""
    if not current_bio:
        bio = random.choice(BIOS.get(account["language"], BIOS["en"]))
        try:
            await client(UpdateProfileRequest(about=bio))
            print(f"  ✅ Bio set: \"{bio}\"")
        except Exception as e:
            print(f"  ⚠️ Could not set bio: {e}")
    else:
        bio = current_bio
        print(f"  Bio already set: \"{current_bio}\"")

    # Step 2: Join normal groups (3-5 random ones)
    num_to_join = random.randint(3, 5)
    groups_to_join = random.sample(NURTURE_GROUPS, min(num_to_join, len(NURTURE_GROUPS)))

    print(f"\n  Joining {len(groups_to_join)} groups...")

    joined = 0
    for group in groups_to_join:
        # Random delay between joins (10-30 seconds to look natural)
        if joined > 0:
            delay = random.randint(10, 30)
            print(f"  ⏳ Waiting {delay}s before next join...")
            await asyncio.sleep(delay)

        try:
            entity = await client.get_entity(group["username"])
            await client(JoinChannelRequest(entity))
            print(f"  ✅ Joined: {group['name']} (@{group['username']})")
            joined += 1
        except FloodWaitError as e:
            print(f"  ⚠️ Flood wait: {e.seconds}s - stopping joins")
            break
        except InviteRequestSentError:
            print(f"  ⏳ Join request sent: {group['name']} (awaiting approval)")
        except (ChannelPrivateError, UserBannedInChannelError):
            print(f"  ⚠️ Cannot join: {group['name']} (private/banned)")
        except Exception as e:
            err = str(e)[:60]
            print(f"  ⚠️ Failed to join {group['name']}: {err}")

    # Step 3: Read some messages from joined groups (simulate browsing)
    print(f"\n  Simulating browsing (reading recent messages)...")
    try:
        dialogs = await client.get_dialogs(limit=10)
        for dialog in dialogs[:5]:
            if dialog.is_group or dialog.is_channel:
                msgs = await client.get_messages(dialog, limit=20)
                print(f"  👀 Read {len(msgs)} messages in: {dialog.name}")
                await asyncio.sleep(random.uniform(2, 5))
    except Exception as e:
        print(f"  ⚠️ Browse error: {e}")

    # Step 4: Update database status
    try:
        from src.models.base import get_session
        from src.models.account import Account
        from sqlalchemy import select, update
        from datetime import datetime

        async with get_session() as session:
            await session.execute(
                update(Account)
                .where(Account.phone == phone)
                .values(
                    status="nurturing",
                    last_active=datetime.utcnow(),
                    bio=bio,
                )
            )
            await session.commit()
        print(f"\n  ✅ Database updated: status=nurturing")
    except Exception as e:
        print(f"  ⚠️ DB update error: {e}")

    # Summary
    print(f"\n  {'='*50}")
    print(f"  NURTURING STARTED")
    print(f"  Account:  +{phone} (@{me.username or 'N/A'})")
    print(f"  Groups joined: {joined}")
    print(f"  Status: nurturing")
    print(f"  ")
    print(f"  Next steps (automatic):")
    print(f"    Day 1-7:  Lurk only, no messages")
    print(f"    Day 8-14: Occasional replies in groups")
    print(f"    Day 15+:  Start light engagement")
    print(f"  {'='*50}")

    await client.disconnect()
    return True


async def main():
    parser = argparse.ArgumentParser(description="Nurture TG accounts")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--phone", help="Phone number to nurture")
    group.add_argument("--all", action="store_true", help="Nurture all active accounts")
    args = parser.parse_args()

    if args.phone:
        await nurture_single_account(args.phone)
    elif args.all:
        from src.models.base import get_session
        from src.models.account import Account
        from sqlalchemy import select

        async with get_session() as session:
            result = await session.execute(
                select(Account).where(Account.status.in_(["active", "nurturing"]))
            )
            accounts = result.scalars().all()

        if not accounts:
            print("No active accounts found")
            return

        print(f"Found {len(accounts)} accounts to nurture")
        for account in accounts:
            await nurture_single_account(account.phone)
            if len(accounts) > 1:
                delay = random.randint(20, 60)
                print(f"\n⏳ Waiting {delay}s before next account...")
                await asyncio.sleep(delay)


if __name__ == "__main__":
    asyncio.run(main())
