"""Send a message to a Telegram group with a specified account.

Usage:
    .venv/bin/python scripts/send_message.py --account 1 --group thebuttongroup --message "Hey!"
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

import socks
from telethon import TelegramClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

DB_URL = "postgresql+asyncpg://promo:promo@localhost:5432/promo_bot"
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
    parser.add_argument("--account", type=int, required=True, help="Account ID")
    parser.add_argument("--group", required=True, help="Group username (without @)")
    parser.add_argument("--message", required=True, help="Message to send")
    args = parser.parse_args()

    proxies = load_proxies()
    engine = create_async_engine(DB_URL, echo=False)

    async with engine.connect() as conn:
        row = await conn.execute(text(
            "SELECT id, phone, display_name, proxy_id, session_string FROM accounts WHERE id = :id"
        ), {"id": args.account})
        acct = row.fetchone()

    await engine.dispose()

    if not acct:
        print(f"Account {args.account} not found")
        return

    acct_id, phone, name, proxy_id, session_path = acct
    session_file = session_path.replace(".session", "")
    proxy = proxies.get(proxy_id)
    proxy_tuple = make_proxy(proxy) if proxy else None

    client = TelegramClient(session_file, api_id=2040, api_hash="b18441a1ff607e10a989891a5462e627")
    if proxy_tuple:
        client.set_proxy(proxy_tuple)

    await client.connect()
    if not await client.is_user_authorized():
        print(f"❌ {name} not authorized")
        await client.disconnect()
        return

    group = args.group.lstrip("@")
    try:
        entity = await client.get_entity(group)
        await client.send_message(entity, args.message)
        print(f"✅ {name} sent message to @{group}")
    except Exception as e:
        print(f"❌ Failed: {e}")

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
