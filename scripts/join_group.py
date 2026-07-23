"""Join a Telegram group with specified accounts.

Usage:
    .venv/bin/python scripts/join_group.py --group example_group --accounts 1,2,3,4
"""
import os
import argparse
import asyncio
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

import socks
from telethon import TelegramClient, functions
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

DB_URL = "postgresql+asyncpg://ops:ops@localhost:5432/ops_orchestrator"
PROXIES_FILE = Path("config/proxies.json")


def load_proxies():
    if PROXIES_FILE.exists():
        return {p["id"]: p for p in json.loads(PROXIES_FILE.read_text())}
    return {}


def make_proxy(proxy: dict):
    proto = proxy.get("protocol", "socks5").lower()
    proxy_type = socks.SOCKS5 if proto.startswith("socks") else socks.HTTP
    if proxy.get("username") and proxy.get("password"):
        return (proxy_type, proxy["host"], proxy["port"], True, proxy["username"], proxy["password"])
    return (proxy_type, proxy["host"], proxy["port"])


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--group", required=True, help="Group username (without @)")
    parser.add_argument("--accounts", required=True, help="Comma-separated account IDs")
    args = parser.parse_args()

    group = args.group.lstrip("@")
    account_ids = [int(x.strip()) for x in args.accounts.split(",")]
    proxies = load_proxies()

    engine = create_async_engine(DB_URL, echo=False)

    # Fetch account info
    async with engine.connect() as conn:
        rows = await conn.execute(text(
            "SELECT id, phone, display_name, proxy_id, session_string FROM accounts WHERE id = ANY(:ids)"
        ), {"ids": account_ids})
        accounts = rows.fetchall()

    await engine.dispose()

    if not accounts:
        print("No accounts found")
        return

    print(f"\nJoining @{group} with {len(accounts)} accounts...\n")

    for acct in accounts:
        acct_id, phone, name, proxy_id, session_path = acct

        # Find session file
        session_file = session_path.replace(".session", "") if session_path else None
        if not session_file or not Path(session_file + ".session").exists():
            print(f"  ❌ {name}: session file not found ({session_path})")
            continue

        proxy = proxies.get(proxy_id)
        proxy_tuple = make_proxy(proxy) if proxy else None

        try:
            client = TelegramClient(session_file, api_id=int(os.environ["TG_API_ID"]), api_hash=os.environ["TG_API_HASH"])
            if proxy_tuple:
                client.set_proxy(proxy_tuple)

            await client.connect()

            if not await client.is_user_authorized():
                print(f"  ❌ {name}: not authorized")
                await client.disconnect()
                continue

            # Join the group
            try:
                entity = await client.get_entity(group)
                await client(functions.channels.JoinChannelRequest(entity))
                print(f"  ✅ {name} (@{phone}) joined @{group}")
            except Exception as e:
                if "already" in str(e).lower() or "USER_ALREADY_PARTICIPANT" in str(e):
                    print(f"  ✅ {name} already in @{group}")
                else:
                    print(f"  ❌ {name} failed: {e}")

            await client.disconnect()

        except Exception as e:
            print(f"  ❌ {name} error: {e}")

        # Random delay between joins (5-15s)
        if acct != accounts[-1]:
            delay = random.uniform(5, 15)
            print(f"     Waiting {delay:.0f}s...")
            await asyncio.sleep(delay)

    print(f"\nDone!")


if __name__ == "__main__":
    asyncio.run(main())
