"""Download random avatars and set them as Telegram profile photos.

Uses thispersondoesnotexist.com for AI-generated realistic face photos,
or fallback to randomuser.me for real-looking portraits.

Usage:
    .venv/bin/python scripts/setup_avatars.py --accounts 1,2,3,4
"""
import argparse
import asyncio
import json
import os
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

import httpx
import socks
from telethon import TelegramClient, functions, types
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

DB_URL = "postgresql+asyncpg://ops:ops@localhost:5432/ops_orchestrator"
PROXIES_FILE = Path("config/proxies.json")
AVATARS_DIR = Path("data/avatars")
AVATARS_DIR.mkdir(parents=True, exist_ok=True)

# Gender hints based on common name patterns (for better avatar matching)
AVATAR_SOURCES = [
    "https://thispersondoesnotexist.com",  # AI-generated faces
]

# Fallback: pre-curated avatar style seeds for randomuser.me
RANDOMUSER_GENDERS = {
    "Boyce Morris": "male",
    "Justin Foster": "male",
    "Eli Brian": "male",
    "Gordon Patricia": "female",
    "Elton Walker": "male",
}


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


async def download_avatar(name: str, index: int) -> Path | None:
    """Download a random avatar image."""
    avatar_path = AVATARS_DIR / f"avatar_{index}.jpg"

    # Skip if already downloaded this session
    if avatar_path.exists() and avatar_path.stat().st_size > 1000:
        print(f"     Using cached avatar: {avatar_path}")
        return avatar_path

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as http:
        # Try thispersondoesnotexist.com first
        try:
            resp = await http.get("https://thispersondoesnotexist.com")
            if resp.status_code == 200 and len(resp.content) > 5000:
                avatar_path.write_bytes(resp.content)
                print(f"     Downloaded AI avatar ({len(resp.content) // 1024}KB)")
                return avatar_path
        except Exception as e:
            print(f"     AI avatar failed: {e}")

        # Fallback: randomuser.me
        try:
            gender = RANDOMUSER_GENDERS.get(name, "male")
            resp = await http.get(
                f"https://randomuser.me/api/?gender={gender}&nat=us"
            )
            if resp.status_code == 200:
                data = resp.json()
                pic_url = data["results"][0]["picture"]["large"]
                pic_resp = await http.get(pic_url)
                if pic_resp.status_code == 200:
                    avatar_path.write_bytes(pic_resp.content)
                    print(f"     Downloaded randomuser avatar ({len(pic_resp.content) // 1024}KB)")
                    return avatar_path
        except Exception as e:
            print(f"     Randomuser avatar failed: {e}")

    print(f"     ❌ Could not download avatar")
    return None


async def set_avatar(client: TelegramClient, avatar_path: Path, name: str) -> bool:
    """Upload and set profile photo."""
    try:
        # Upload the file
        file = await client.upload_file(avatar_path)

        # Set as profile photo
        await client(functions.photos.UploadProfilePhotoRequest(
            file=file,
        ))
        return True
    except Exception as e:
        print(f"     ❌ Set avatar failed: {e}")
        return False


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--accounts", required=True, help="Comma-separated account IDs (or 'all')")
    args = parser.parse_args()

    proxies = load_proxies()
    engine = create_async_engine(DB_URL, echo=False)

    # Fetch accounts
    async with engine.connect() as conn:
        if args.accounts.lower() == "all":
            rows = await conn.execute(text(
                "SELECT id, phone, display_name, proxy_id, session_string FROM accounts WHERE status='active' ORDER BY id"
            ))
        else:
            account_ids = [int(x.strip()) for x in args.accounts.split(",")]
            rows = await conn.execute(text(
                "SELECT id, phone, display_name, proxy_id, session_string FROM accounts WHERE id = ANY(:ids) ORDER BY id"
            ), {"ids": account_ids})
        accounts = rows.fetchall()

    await engine.dispose()

    if not accounts:
        print("No accounts found")
        return

    print(f"\n{'='*50}")
    print(f"  Setting avatars for {len(accounts)} accounts")
    print(f"{'='*50}")

    success = 0
    for idx, acct in enumerate(accounts):
        acct_id, phone, name, proxy_id, session_path = acct

        print(f"\n  [{idx+1}/{len(accounts)}] {name} (+{phone})")

        # Download avatar
        avatar_path = await download_avatar(name, acct_id)
        if not avatar_path:
            continue

        # Connect Telethon
        session_file = session_path.replace(".session", "") if session_path else None
        if not session_file or not Path(session_file + ".session").exists():
            print(f"     ❌ Session not found: {session_path}")
            continue

        proxy = proxies.get(proxy_id)
        proxy_tuple = make_proxy(proxy) if proxy else None

        try:
            client = TelegramClient(session_file, api_id=int(os.environ["TG_API_ID"]), api_hash=os.environ["TG_API_HASH"])
            if proxy_tuple:
                client.set_proxy(proxy_tuple)

            await client.connect()
            if not await client.is_user_authorized():
                print(f"     ❌ Not authorized")
                await client.disconnect()
                continue

            # Check if already has a photo
            me = await client.get_me()
            if me.photo:
                print(f"     ℹ️  Already has avatar, replacing...")

            ok = await set_avatar(client, avatar_path, name)
            if ok:
                print(f"     ✅ Avatar set!")
                success += 1

            await client.disconnect()

        except Exception as e:
            print(f"     ❌ Error: {e}")

        # Delay between accounts
        if idx < len(accounts) - 1:
            delay = random.uniform(3, 8)
            print(f"     Waiting {delay:.0f}s...")
            await asyncio.sleep(delay)

    print(f"\n{'='*50}")
    print(f"  Done: {success}/{len(accounts)} avatars set")
    print(f"{'='*50}\n")


if __name__ == "__main__":
    asyncio.run(main())
