"""Set each account's Telegram bio based on their assigned persona.

Different personas -> different English bios that match their personality.
"""
import os
import asyncio
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

import socks
from telethon import TelegramClient
from telethon.tl.functions.account import UpdateProfileRequest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

DB_URL = "postgresql+asyncpg://promo:promo@localhost:5432/promo_bot"
PROXIES_FILE = Path("config/proxies.json")

# Distinct English bios per persona
PERSONA_BIOS = {
    "crypto_veteran": [
        "Since 2017 | DeFi & GameFi | dyor",
        "Crypto OG | Survived every cycle | 🤔",
        "Long-term thinker | DeFi native | nfa",
        "Been here since NEO days | mechanism nerd",
    ],
    "game_newbie": [
        "Gamer exploring Web3 | lots of ??? 🎮",
        "Mobile gaming addict | trying crypto games",
        "Genshin > everything | learning Web3 lol",
        "Just here for fun 🎮✨",
    ],
    "airdrop_hunter": [
        "Full-time airdrop farmer | alpha hunter",
        "Farming since 2023 | multi-wallet | 📌",
        "New project radar ON 🔥 | zero-cost ops",
        "Alpha hunter | LP degen | gas optimizer",
    ],
    "data_analyst": [
        "Math/CS | tokenomics nerd | 🧮",
        "Game theory enthusiast | ex-quant",
        "Numbers don't lie | mechanism design",
        "Probability theory > vibes | 📊",
    ],
    "community_active": [
        "Lurking in 50+ groups 😂 | always down to chat",
        "Crypto Twitter refugee | vibes only ✨",
        "Group chat main character 🔥 | be nice",
        "Friendly neighborhood degen | ❤️",
    ],
}


def load_proxies():
    return {p["id"]: p for p in json.loads(PROXIES_FILE.read_text())}


def make_proxy(proxy):
    proto = proxy.get("protocol", "socks5").lower()
    proxy_type = socks.SOCKS5 if proto.startswith("socks") else socks.HTTP
    if proxy.get("username") and proxy.get("password"):
        return (proxy_type, proxy["host"], proxy["port"], True, proxy["username"], proxy["password"])
    return (proxy_type, proxy["host"], proxy["port"])


async def main():
    proxies = load_proxies()
    engine = create_async_engine(DB_URL, echo=False)

    async with engine.connect() as conn:
        rows = await conn.execute(text("""
            SELECT id, phone, display_name, persona_id, proxy_id, session_string
            FROM accounts WHERE status IN ('active', 'nurturing') ORDER BY id
        """))
        accounts = rows.fetchall()

    print(f"\nUpdating bios for {len(accounts)} accounts based on persona...\n")

    for idx, (acc_id, phone, name, persona_id, proxy_id, session_str) in enumerate(accounts):
        bio_options = PERSONA_BIOS.get(persona_id, PERSONA_BIOS["community_active"])
        # Deterministic-ish: pick bio based on account id so same account gets same bio
        bio = bio_options[acc_id % len(bio_options)]

        session_file = session_str.replace(".session", "") if session_str else None
        if not session_file or not Path(session_file + ".session").exists():
            print(f"  ❌ {name}: session missing")
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

            await client(UpdateProfileRequest(about=bio))
            print(f"  ✅ {name:<18} [{persona_id:<16}] -> \"{bio}\"")

            # Also update DB
            async with engine.begin() as conn:
                await conn.execute(
                    text("UPDATE accounts SET bio = :bio WHERE id = :id"),
                    {"bio": bio, "id": acc_id},
                )

            await client.disconnect()

        except Exception as e:
            print(f"  ❌ {name}: {e}")

        if idx < len(accounts) - 1:
            await asyncio.sleep(random.uniform(3, 6))

    print(f"\n✅ All bios updated!\n")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
