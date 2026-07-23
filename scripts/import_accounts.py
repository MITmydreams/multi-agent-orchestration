"""Batch import Telegram accounts from tdata folders.

Converts tdata (Telegram Desktop format) to TDLib sessions via opentele,
assigns proxies, and registers accounts in the database.

Usage:
    # Import a single account
    python scripts/import_accounts.py --tdata-dir /path/to/10000000001

    # Batch import all accounts in a directory
    python scripts/import_accounts.py --batch-dir /path/to/tg_accounts

Directory structure expected:
    tg_accounts/
    ├── 10000000001/        # phone number as folder name
    │   ├── tdata/          # Telegram Desktop session data
    │   └── 2fa.txt         # (optional) 2FA password
    ├── 15551234567/
    │   ├── tdata/
    │   └── 2fa.txt
    ...
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from opentele.td import TDesktop
from opentele.tl import TelegramClient
from opentele.api import API, CreateNewSession, UseCurrentSession


# Proxy config
PROXIES_FILE = Path("config/proxies.json")
SESSIONS_DIR = Path("tdlib_sessions")
ACCOUNTS_DIR = Path("tg_accounts_imported")


def load_proxies() -> list[dict]:
    if PROXIES_FILE.exists():
        return json.loads(PROXIES_FILE.read_text())
    return []


def read_2fa(account_dir: Path) -> str:
    """Read 2FA password from account directory."""
    fa_file = account_dir / "2fa.txt"
    if fa_file.exists():
        return fa_file.read_text().strip()
    return ""


async def import_single_account(
    tdata_dir: Path,
    proxy_id: int,
    api_id: int,
    api_hash: str,
    role: str = "infiltrator",
) -> dict | None:
    """Import a single account from tdata directory.

    Returns account info dict on success, None on failure.
    """
    phone = tdata_dir.name  # folder name = phone number
    tdata_path = tdata_dir / "tdata"
    two_fa = read_2fa(tdata_dir)

    if not tdata_path.exists():
        print(f"  ❌ {phone}: tdata folder not found")
        return None

    proxies = load_proxies()
    proxy = None
    for p in proxies:
        if p["id"] == proxy_id:
            proxy = p
            break

    print(f"\n{'='*60}")
    print(f"  Importing: +{phone}")
    print(f"  Proxy:     #{proxy_id} ({proxy['host'] if proxy else 'direct'})")
    print(f"  2FA:       {'set' if two_fa else 'not set'}")
    print(f"{'='*60}")

    try:
        # Step 1: Load tdata
        print(f"  [1/4] Loading tdata...")
        tdesk = TDesktop(str(tdata_path))

        if not tdesk.isLoaded():
            print(f"  ❌ Failed to load tdata - file may be corrupted")
            return None

        print(f"  ✅ tdata loaded. Accounts found: {len(tdesk.accounts)}")

        if len(tdesk.accounts) == 0:
            print(f"  ❌ No accounts found in tdata")
            return None

        # Step 2: Convert tdata to Telethon session (as intermediate step)
        print(f"  [2/4] Converting tdata → Telethon session...")

        # Use custom API to match our device fingerprint
        custom_api = API.TelegramDesktop.Generate(
            system="windows",
            unique_id=f"promo_bot_{phone}"
        )

        session_path = SESSIONS_DIR / f"account_{proxy_id}"
        session_path.mkdir(parents=True, exist_ok=True)
        session_file = str(session_path / f"{phone}")

        # Convert tdata to telethon client (this extracts the auth key)
        client = await tdesk.ToTelethon(
            session=session_file,
            flag=UseCurrentSession,
            api=custom_api,
        )

        print(f"  ✅ Session converted")

        # Step 3: Connect and verify account
        print(f"  [3/4] Connecting to Telegram...")

        # Setup proxy for telethon
        proxy_tuple = None
        if proxy:
            import socks
            proto = proxy.get("protocol", "socks5").lower()
            if proto.startswith("socks"):
                proxy_type = socks.SOCKS5
            else:
                proxy_type = socks.HTTP

            if proxy.get("username") and proxy.get("password"):
                proxy_tuple = (proxy_type, proxy["host"], proxy["port"], True, proxy["username"], proxy["password"])
            else:
                proxy_tuple = (proxy_type, proxy["host"], proxy["port"])

        client.set_proxy(proxy_tuple)

        await client.connect()

        if not await client.is_user_authorized():
            print(f"  ❌ Account not authorized - session may be expired")
            await client.disconnect()
            return None

        # Step 4: Get account info
        print(f"  [4/4] Fetching account info...")
        me = await client.get_me()

        account_info = {
            "phone": phone,
            "user_id": me.id,
            "first_name": me.first_name or "",
            "last_name": me.last_name or "",
            "username": me.username or "",
            "proxy_id": proxy_id,
            "proxy_host": proxy["host"] if proxy else "",
            "role": role,
            "two_fa": two_fa,
            "session_file": session_file + ".session",
            "status": "active",
        }

        print(f"\n  ✅ Account imported successfully!")
        print(f"     ID:       {me.id}")
        print(f"     Name:     {me.first_name or ''} {me.last_name or ''}")
        print(f"     Username: @{me.username or 'N/A'}")
        print(f"     Phone:    +{phone}")
        print(f"     Session:  {session_file}.session")

        await client.disconnect()
        return account_info

    except Exception as e:
        print(f"  ❌ Import failed: {e}")
        import traceback
        traceback.print_exc()
        return None


async def batch_import(
    batch_dir: Path,
    api_id: int,
    api_hash: str,
    roles: dict[int, str] | None = None,
):
    """Import all account folders in batch_dir."""

    # Find all account directories (folders with tdata subdirectory)
    account_dirs = sorted([
        d for d in batch_dir.iterdir()
        if d.is_dir() and (d / "tdata").exists()
    ])

    if not account_dirs:
        print(f"❌ No account folders found in {batch_dir}")
        print(f"   Expected structure: {batch_dir}/<phone>/tdata/")
        return

    proxies = load_proxies()
    print(f"\nFound {len(account_dirs)} account(s) to import")
    print(f"Available proxies: {len(proxies)}")

    if len(account_dirs) > len(proxies):
        print(f"⚠️  Warning: More accounts than proxies! Some accounts will share proxies.")

    results = []
    for idx, account_dir in enumerate(account_dirs):
        proxy_id = proxies[idx % len(proxies)]["id"] if proxies else 0
        role_map = roles or {}
        role = role_map.get(idx + 1, "infiltrator")

        result = await import_single_account(
            tdata_dir=account_dir,
            proxy_id=proxy_id,
            api_id=api_id,
            api_hash=api_hash,
            role=role,
        )
        results.append(result)

        # Small delay between accounts to avoid rate limits
        if idx < len(account_dirs) - 1:
            print(f"\n  Waiting 3 seconds before next account...")
            await asyncio.sleep(3)

    # Summary
    success = [r for r in results if r is not None]
    failed = len(results) - len(success)

    print(f"\n{'='*60}")
    print(f"  IMPORT SUMMARY")
    print(f"{'='*60}")
    print(f"  Total:     {len(results)}")
    print(f"  Success:   {len(success)}")
    print(f"  Failed:    {failed}")
    print(f"{'='*60}")

    if success:
        # Save results to file
        output_file = Path("config/imported_accounts.json")
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_text(json.dumps(success, indent=2, ensure_ascii=False))
        print(f"\n  Results saved to: {output_file}")

    return success


async def main():
    parser = argparse.ArgumentParser(description="Import TG accounts from tdata")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--tdata-dir", type=Path, help="Single account tdata directory")
    group.add_argument("--batch-dir", type=Path, help="Directory containing multiple account folders")
    parser.add_argument("--proxy-id", type=int, default=1, help="Proxy ID (for single import)")
    parser.add_argument("--role", default="infiltrator", help="Account role: scout|infiltrator|content|backup")
    parser.add_argument("--api-id", type=int, default=0)
    parser.add_argument("--api-hash", default="")
    args = parser.parse_args()

    # Load from .env
    if not args.api_id or not args.api_hash:
        from dotenv import load_dotenv
        import os
        load_dotenv()
        if not args.api_id:
            args.api_id = int(os.getenv("TG_API_ID", "0"))
        if not args.api_hash:
            args.api_hash = os.getenv("TG_API_HASH", "")

    if not args.api_id or not args.api_hash:
        print("❌ TG_API_ID and TG_API_HASH required!")
        print("   Set in .env or pass via --api-id / --api-hash")
        print("   Get from: https://my.telegram.org/apps")
        sys.exit(1)

    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

    if args.tdata_dir:
        # Single import
        await import_single_account(
            tdata_dir=args.tdata_dir,
            proxy_id=args.proxy_id,
            api_id=args.api_id,
            api_hash=args.api_hash,
            role=args.role,
        )
    else:
        # Batch import
        await batch_import(
            batch_dir=args.batch_dir,
            api_id=args.api_id,
            api_hash=args.api_hash,
        )


if __name__ == "__main__":
    asyncio.run(main())
