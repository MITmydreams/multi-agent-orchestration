"""One-click batch import + register Telegram accounts.

Scans a directory of tdata folders, converts to Telethon sessions,
verifies accounts via proxy, and writes them to the database.

Usage:
    python scripts/batch_setup.py --accounts-dir /path/to/tg_accounts
"""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")

import socks
from opentele.td import TDesktop
from opentele.api import API, UseCurrentSession
from sqlalchemy import select

from src.models.base import get_session, engine, Base
from src.models.account import Account


# --- Constants ---
PROXIES_FILE = PROJECT_ROOT / "config" / "proxies.json"
SESSIONS_DIR = PROJECT_ROOT / "tdlib_sessions"

# Role assignment: index (1-based) -> role
DEFAULT_ROLE_MAP = {
    1: "executor",
    2: "scout",
    3: "executor",
    4: "content",
    5: "backup",
}

MAX_ACCOUNTS_PER_PROXY = 3


def load_proxies() -> list[dict]:
    if PROXIES_FILE.exists():
        return json.loads(PROXIES_FILE.read_text())
    return []


def make_proxy_tuple(proxy: dict) -> tuple:
    """Build a PySocks proxy tuple for Telethon."""
    proto = proxy.get("protocol", "socks5").lower()
    proxy_type = socks.SOCKS5 if proto.startswith("socks") else socks.HTTP
    if proxy.get("username") and proxy.get("password"):
        return (proxy_type, proxy["host"], proxy["port"], True, proxy["username"], proxy["password"])
    return (proxy_type, proxy["host"], proxy["port"])


def read_2fa(account_dir: Path) -> str:
    fa_file = account_dir / "2fa.txt"
    if fa_file.exists():
        return fa_file.read_text().strip()
    return ""


def assign_proxies(num_accounts: int, proxies: list[dict]) -> list[dict]:
    """Assign proxies to accounts, max MAX_ACCOUNTS_PER_PROXY each.

    Returns a list of proxy dicts (length = num_accounts).
    """
    assignments = []
    usage_count: dict[int, int] = {}
    proxy_idx = 0

    for _ in range(num_accounts):
        assigned = False
        attempts = 0
        while attempts < len(proxies):
            p = proxies[proxy_idx % len(proxies)]
            pid = p["id"]
            if usage_count.get(pid, 0) < MAX_ACCOUNTS_PER_PROXY:
                assignments.append(p)
                usage_count[pid] = usage_count.get(pid, 0) + 1
                assigned = True
                proxy_idx += 1
                break
            proxy_idx += 1
            attempts += 1

        if not assigned:
            # All proxies full, wrap around (allow overflow)
            p = proxies[0]
            assignments.append(p)
            usage_count[p["id"]] = usage_count.get(p["id"], 0) + 1

    return assignments


async def import_and_register(
    tdata_dir: Path,
    proxy: dict,
    role: str,
    api_id: int,
    api_hash: str,
) -> dict | None:
    """Import a single account from tdata, verify via proxy, write to DB.

    Returns a summary dict on success, None on failure.
    """
    phone = tdata_dir.name
    tdata_path = tdata_dir / "tdata"
    two_fa = read_2fa(tdata_dir)

    result = {
        "phone": phone,
        "status": "unknown",
        "error": None,
    }

    if not tdata_path.exists():
        result["status"] = "error"
        result["error"] = "tdata folder not found"
        print(f"  [SKIP] +{phone}: tdata folder not found")
        return result

    print(f"\n{'='*60}")
    print(f"  Account: +{phone}")
    print(f"  Proxy:   #{proxy['id']} {proxy['host']}:{proxy['port']} ({proxy.get('city', '')})")
    print(f"  Role:    {role}")
    print(f"  2FA:     {'yes' if two_fa else 'no'}")
    print(f"{'='*60}")

    # Step 1: Load tdata
    try:
        print(f"  [1/5] Loading tdata...")
        tdesk = TDesktop(str(tdata_path))

        if not tdesk.isLoaded() or len(tdesk.accounts) == 0:
            result["status"] = "error"
            result["error"] = "tdata load failed or no accounts"
            print(f"  [FAIL] tdata load failed or empty")
            return result

        print(f"  [OK]   tdata loaded ({len(tdesk.accounts)} account(s))")
    except Exception as e:
        result["status"] = "error"
        result["error"] = f"tdata load exception: {e}"
        print(f"  [FAIL] tdata load: {e}")
        return result

    # Step 2: Convert to Telethon session
    try:
        print(f"  [2/5] Converting to Telethon session...")
        custom_api = API.TelegramDesktop.Generate(
            system="windows",
            unique_id=f"ops_orchestrator_{phone}",
        )

        session_path = SESSIONS_DIR / f"account_{proxy['id']}"
        session_path.mkdir(parents=True, exist_ok=True)
        session_file = str(session_path / phone)

        client = await tdesk.ToTelethon(
            session=session_file,
            flag=UseCurrentSession,
            api=custom_api,
        )
        print(f"  [OK]   Session file: {session_file}.session")
    except Exception as e:
        result["status"] = "error"
        result["error"] = f"Session convert failed: {e}"
        print(f"  [FAIL] Session convert: {e}")
        return result

    # Step 3: Connect via proxy
    try:
        print(f"  [3/5] Connecting via proxy #{proxy['id']}...")
        proxy_tuple = make_proxy_tuple(proxy)
        client.set_proxy(proxy_tuple)
        await client.connect()

        if not await client.is_user_authorized():
            result["status"] = "error"
            result["error"] = "Not authorized (session expired)"
            print(f"  [FAIL] Not authorized")
            await client.disconnect()
            return result

        print(f"  [OK]   Connected and authorized")
    except Exception as e:
        result["status"] = "error"
        result["error"] = f"Proxy connect failed: {e}"
        print(f"  [FAIL] Connect: {e}")
        # Try without proxy as fallback? No, we want proxy discipline.
        try:
            await client.disconnect()
        except Exception:
            pass
        return result

    # Step 4: Fetch account info
    try:
        print(f"  [4/5] Fetching account info...")
        me = await client.get_me()

        is_restricted = getattr(me, "restricted", False)
        restriction_reason = ""
        if is_restricted and hasattr(me, "restriction_reason"):
            restriction_reason = str(me.restriction_reason)

        user_id = me.id
        username = me.username or ""
        first_name = me.first_name or ""
        last_name = me.last_name or ""
        display_name = f"{first_name} {last_name}".strip()

        print(f"  [OK]   ID: {user_id} | @{username or 'N/A'} | {display_name}")
        if is_restricted:
            print(f"  [WARN] Account is RESTRICTED: {restriction_reason}")

        # Get dialogs count as a rough indicator
        dialogs = await client.get_dialogs(limit=0)
        group_count = 0
        try:
            async for d in client.iter_dialogs():
                if d.is_group or d.is_channel:
                    group_count += 1
                if group_count > 200:
                    break
        except Exception:
            pass

        print(f"  [OK]   Groups/Channels: {group_count}")

        await client.disconnect()
    except Exception as e:
        result["status"] = "error"
        result["error"] = f"Fetch info failed: {e}"
        print(f"  [FAIL] Fetch info: {e}")
        try:
            await client.disconnect()
        except Exception:
            pass
        return result

    # Step 5: Write to database
    try:
        print(f"  [5/5] Writing to database...")

        status = "abandoned" if is_restricted else "active"

        async with get_session() as session:
            # Check if already exists
            existing = await session.execute(
                select(Account).where(Account.phone == phone)
            )
            account = existing.scalar_one_or_none()

            if account:
                # Update existing record
                account.username = username
                account.display_name = display_name
                account.role = role
                account.proxy_id = proxy["id"]
                account.status = status
                account.session_string = f"{session_file}.session"
                print(f"  [OK]   Updated existing record (id={account.id})")
            else:
                # Insert new record
                account = Account(
                    phone=phone,
                    phone_type="physical_sim",
                    username=username,
                    display_name=display_name,
                    role=role,
                    status=status,
                    proxy_id=proxy["id"],
                    session_string=f"{session_file}.session",
                    language="zh",
                )
                session.add(account)
                print(f"  [OK]   Inserted new record")

        result["status"] = status
        result["user_id"] = user_id
        result["username"] = username
        result["display_name"] = display_name
        result["role"] = role
        result["proxy_id"] = proxy["id"]
        result["groups"] = group_count
        result["restricted"] = is_restricted

        status_icon = "[RESTRICTED]" if is_restricted else "[OK]"
        print(f"\n  {status_icon} +{phone} imported as {role} (status={status})")

    except Exception as e:
        result["status"] = "error"
        result["error"] = f"DB write failed: {e}"
        print(f"  [FAIL] DB write: {e}")
        import traceback
        traceback.print_exc()

    return result


async def main():
    parser = argparse.ArgumentParser(description="Batch import + register TG accounts")
    parser.add_argument(
        "--accounts-dir", type=Path, required=True,
        help="Directory containing account folders (folder name = phone number, each with tdata/)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Only scan and show what would be done, don't actually import",
    )
    args = parser.parse_args()

    api_id = int(os.getenv("TG_API_ID", "0"))
    api_hash = os.getenv("TG_API_HASH", "")

    if not api_id or not api_hash:
        print("[ERROR] TG_API_ID and TG_API_HASH must be set in .env")
        sys.exit(1)

    accounts_dir = args.accounts_dir
    if not accounts_dir.exists():
        print(f"[ERROR] Directory not found: {accounts_dir}")
        sys.exit(1)

    # Find account directories (must contain tdata/)
    account_dirs = sorted([
        d for d in accounts_dir.iterdir()
        if d.is_dir() and (d / "tdata").exists()
    ])

    if not account_dirs:
        print(f"[ERROR] No account folders with tdata/ found in {accounts_dir}")
        print(f"  Expected: {accounts_dir}/<phone>/tdata/")
        sys.exit(1)

    proxies = load_proxies()
    if not proxies:
        print("[ERROR] No proxies found in config/proxies.json")
        sys.exit(1)

    print(f"\n{'#'*60}")
    print(f"  BATCH SETUP")
    print(f"{'#'*60}")
    print(f"  Accounts found:  {len(account_dirs)}")
    print(f"  Proxies available: {len(proxies)}")
    print(f"  Max per proxy:   {MAX_ACCOUNTS_PER_PROXY}")
    print(f"  Capacity:        {len(proxies) * MAX_ACCOUNTS_PER_PROXY} accounts")

    # Assign proxies
    proxy_assignments = assign_proxies(len(account_dirs), proxies)

    # Preview
    print(f"\n  Plan:")
    for idx, (d, p) in enumerate(zip(account_dirs, proxy_assignments), 1):
        role = DEFAULT_ROLE_MAP.get(idx, "executor")
        print(f"    {idx}. +{d.name} -> proxy #{p['id']} ({p.get('city', '')}) as {role}")

    if args.dry_run:
        print("\n  [DRY RUN] No changes made.")
        return

    # Ensure sessions dir exists
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

    # Process each account
    results = []
    for idx, (account_dir, proxy) in enumerate(zip(account_dirs, proxy_assignments), 1):
        role = DEFAULT_ROLE_MAP.get(idx, "executor")

        result = await import_and_register(
            tdata_dir=account_dir,
            proxy=proxy,
            role=role,
            api_id=api_id,
            api_hash=api_hash,
        )
        results.append(result)

        # Delay between accounts to avoid rate limiting
        if idx < len(account_dirs):
            print(f"\n  Waiting 3s before next account...")
            await asyncio.sleep(3)

    # Summary report
    success = [r for r in results if r and r["status"] in ("active", "abandoned")]
    errors = [r for r in results if r and r["status"] == "error"]
    restricted = [r for r in results if r and r.get("restricted")]

    print(f"\n{'#'*60}")
    print(f"  SUMMARY REPORT")
    print(f"{'#'*60}")
    print(f"  Total processed: {len(results)}")
    print(f"  Active:          {len([r for r in success if r['status'] == 'active'])}")
    print(f"  Restricted:      {len(restricted)}")
    print(f"  Errors:          {len(errors)}")
    print()

    if success:
        print(f"  {'Phone':<16} {'Username':<20} {'Role':<14} {'Proxy':<6} {'Status':<12} {'Groups'}")
        print(f"  {'-'*16} {'-'*20} {'-'*14} {'-'*6} {'-'*12} {'-'*6}")
        for r in success:
            print(
                f"  {r['phone']:<16} "
                f"@{r.get('username', ''):<19} "
                f"{r.get('role', ''):<14} "
                f"#{r.get('proxy_id', '?'):<5} "
                f"{r['status']:<12} "
                f"{r.get('groups', 0)}"
            )

    if errors:
        print(f"\n  Errors:")
        for r in errors:
            print(f"    +{r['phone']}: {r.get('error', 'unknown')}")

    print(f"\n{'#'*60}")


if __name__ == "__main__":
    asyncio.run(main())
