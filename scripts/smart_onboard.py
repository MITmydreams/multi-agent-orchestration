"""Smart onboarding: import + register + assign role for new accounts.

Detects accounts present on disk but not in DB, assigns them to proxies
(respecting MAX_ACCOUNTS_PER_PROXY) and roles based on a target distribution.

Usage:
    .venv/bin/python scripts/smart_onboard.py
"""
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

import os
import socks
from opentele.td import TDesktop
from opentele.api import API, UseCurrentSession
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

DB_URL = "postgresql+asyncpg://promo:promo@localhost:5432/promo_bot"
ACCOUNTS_DIR = Path("../tg_accounts")
SESSIONS_DIR = Path("tdlib_sessions")
PROXIES_FILE = Path("config/proxies.json")
MAX_ACCOUNTS_PER_PROXY = 3

# Role distribution for new accounts
# 2-account test: 1 infiltrator + 1 scout
NEW_ACCOUNT_ROLES = [
    "infiltrator", "scout",
]


def load_proxies():
    return json.loads(PROXIES_FILE.read_text())


def make_proxy_tuple(proxy):
    proto = proxy.get("protocol", "socks5").lower()
    proxy_type = socks.SOCKS5 if proto.startswith("socks") else socks.HTTP
    if proxy.get("username") and proxy.get("password"):
        return (proxy_type, proxy["host"], proxy["port"], True, proxy["username"], proxy["password"])
    return (proxy_type, proxy["host"], proxy["port"])


def read_2fa(account_dir):
    fa = account_dir / "2fa.txt"
    return fa.read_text().strip() if fa.exists() else ""


async def import_one(phone, proxy, role, api_id, api_hash):
    """Import a single account: tdata -> session -> verify -> register."""
    tdata_dir = ACCOUNTS_DIR / phone
    tdata_path = tdata_dir / "tdata"

    if not tdata_path.exists():
        return {"phone": phone, "status": "error", "error": "tdata not found"}

    print(f"\n{'='*60}")
    print(f"  Importing +{phone}")
    print(f"  Proxy: #{proxy['id']} {proxy['host']} ({proxy.get('city', '')})")
    print(f"  Role:  {role}")
    print(f"{'='*60}")

    try:
        # Step 1: Load tdata
        print("  [1/4] Loading tdata...")
        tdesk = TDesktop(str(tdata_path))
        if not tdesk.isLoaded() or len(tdesk.accounts) == 0:
            return {"phone": phone, "status": "error", "error": "tdata empty"}

        # Step 2: Convert to Telethon session
        print("  [2/4] Converting to session...")
        custom_api = API.TelegramDesktop.Generate(
            system="windows", unique_id=f"promo_bot_{phone}",
        )
        session_path = SESSIONS_DIR / f"account_{proxy['id']}"
        session_path.mkdir(parents=True, exist_ok=True)
        session_file = str(session_path / phone)

        client = await tdesk.ToTelethon(
            session=session_file,
            flag=UseCurrentSession,
            api=custom_api,
        )

        # Step 3: Connect via proxy
        print("  [3/4] Connecting via proxy...")
        client.set_proxy(make_proxy_tuple(proxy))
        await client.connect()

        if not await client.is_user_authorized():
            await client.disconnect()
            return {"phone": phone, "status": "error", "error": "not authorized"}

        # Step 4: Get info
        print("  [4/4] Fetching info...")
        me = await client.get_me()
        # All current batch accounts are user-confirmed 1-year veterans.
        # If the procurement source ever changes, switch this to read a per-account
        # metadata file or CLI override instead of editing this constant again.
        account_age_days = 365

        info = {
            "phone": phone,
            "user_id": me.id,
            "username": me.username or "",
            "display_name": f"{me.first_name or ''} {me.last_name or ''}".strip(),
            "session_file": session_file + ".session",
            "proxy_id": proxy["id"],
            "role": role,
            "status": "active",
            "account_age_days": account_age_days,
        }
        print(f"  ✅ {info['display_name']} (@{info['username'] or 'N/A'})")

        await client.disconnect()
        return info

    except Exception as e:
        print(f"  ❌ {e}")
        import traceback
        traceback.print_exc()
        return {"phone": phone, "status": "error", "error": str(e)}


async def main():
    api_id = int(os.getenv("TG_API_ID", "2040"))
    api_hash = os.getenv("TG_API_HASH", "b18441a1ff607e10a989891a5462e627")

    engine = create_async_engine(DB_URL, echo=False)

    # Find new accounts (on disk but not in DB)
    print("\n🔍 Detecting new accounts...")
    on_disk = sorted([d.name for d in ACCOUNTS_DIR.iterdir() if d.is_dir() and (d / "tdata").exists()])

    async with engine.connect() as conn:
        rows = await conn.execute(text("SELECT phone, proxy_id FROM accounts ORDER BY id"))
        existing = {r[0]: r[1] for r in rows}

    new_phones = [p for p in on_disk if p not in existing]
    print(f"  On disk: {len(on_disk)} | In DB: {len(existing)} | New: {len(new_phones)}")

    if not new_phones:
        print("✅ No new accounts to import")
        return

    # Build proxy capacity map
    proxies = load_proxies()
    proxy_load = {p["id"]: 0 for p in proxies}
    for proxy_id in existing.values():
        if proxy_id in proxy_load:
            proxy_load[proxy_id] += 1

    print(f"\n📊 Current proxy load:")
    for p in proxies:
        slot = MAX_ACCOUNTS_PER_PROXY - proxy_load[p["id"]]
        print(f"  #{p['id']} {p.get('city', ''):<14} {proxy_load[p['id']]}/{MAX_ACCOUNTS_PER_PROXY}  ({slot} slots)")

    total_slots = sum(MAX_ACCOUNTS_PER_PROXY - v for v in proxy_load.values())
    if total_slots < len(new_phones):
        print(f"\n⚠️  Only {total_slots} slots available for {len(new_phones)} accounts!")

    # Assign each new phone to a proxy
    assignments = []
    for idx, phone in enumerate(new_phones):
        # Find proxy with capacity, round-robin
        target_proxy = None
        for p in proxies:
            if proxy_load[p["id"]] < MAX_ACCOUNTS_PER_PROXY:
                target_proxy = p
                proxy_load[p["id"]] += 1
                break
        if not target_proxy:
            print(f"  ❌ No proxy slot for {phone}")
            continue
        role = NEW_ACCOUNT_ROLES[idx % len(NEW_ACCOUNT_ROLES)]
        assignments.append((phone, target_proxy, role))

    print(f"\n📋 Plan ({len(assignments)} accounts):")
    for phone, p, role in assignments:
        print(f"  +{phone}  ->  #{p['id']} ({p.get('city', ''):<14}) as {role}")

    # Import each
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    results = []
    for idx, (phone, proxy, role) in enumerate(assignments):
        result = await import_one(phone, proxy, role, api_id, api_hash)
        results.append(result)
        if idx < len(assignments) - 1:
            await asyncio.sleep(2)

    # Insert successful imports into DB
    success = [r for r in results if r.get("status") == "active"]
    failed = [r for r in results if r.get("status") != "active"]

    if success:
        print(f"\n💾 Writing {len(success)} accounts to database...")
        async with engine.begin() as conn:
            for r in success:
                await conn.execute(text("""
                    INSERT INTO accounts
                    (phone, phone_type, username, display_name, role, status, proxy_id, language,
                     session_string, risk_score, trust_score, messages_sent_today, promo_messages_today,
                     groups_active_today, new_groups_today, dms_initiated_today, links_sent_today,
                     total_messages, total_promo_messages, kicked_count, reported, account_age_days)
                    VALUES (:phone, 'virtual', :username, :display_name, :role, 'active', :proxy_id, 'en',
                            :session, 0.0, 0.0, 0, 0, 0, 0, 0, 0, 0, 0, 0, false, :age)
                    ON CONFLICT (phone) DO NOTHING
                """), {
                    "phone": r["phone"],
                    "username": r["username"] or None,
                    "display_name": r["display_name"] or None,
                    "role": r["role"],
                    "proxy_id": r["proxy_id"],
                    "session": r["session_file"],
                    "age": r.get("account_age_days", 0),
                })

    # Summary
    print(f"\n{'='*60}")
    print(f"  IMPORT SUMMARY")
    print(f"{'='*60}")
    print(f"  ✅ Success: {len(success)}")
    print(f"  ❌ Failed:  {len(failed)}")
    if failed:
        for r in failed:
            print(f"     +{r['phone']}: {r.get('error', '?')}")
    print(f"{'='*60}\n")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
