"""View all account statuses with live Telegram verification.

Reads accounts from the database, connects to Telegram via Telethon
to check if each account is online/restricted, and prints a summary table.

Usage:
    python scripts/account_status.py
    python scripts/account_status.py --no-live    # Skip live Telegram checks, DB only
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")

import socks
from telethon import TelegramClient
from sqlalchemy import select, func

from src.models.base import get_session
from src.models.account import Account
from src.models.group import GroupAccount


PROXIES_FILE = PROJECT_ROOT / "config" / "proxies.json"


def load_proxies() -> dict[int, dict]:
    """Load proxies indexed by ID."""
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


async def check_account_live(
    session_file: str,
    proxy: dict | None,
    api_id: int,
    api_hash: str,
) -> dict:
    """Connect to Telegram and check account status.

    Returns dict with keys: authorized, restricted, restriction_reason, username
    """
    result = {
        "authorized": False,
        "restricted": False,
        "restriction_reason": "",
        "live_username": "",
        "error": None,
    }

    if not session_file or not Path(session_file).exists():
        result["error"] = "session file missing"
        return result

    # Strip .session extension if present for TelegramClient
    session_name = session_file
    if session_name.endswith(".session"):
        session_name = session_name[:-8]

    try:
        proxy_tuple = make_proxy_tuple(proxy) if proxy else None
        client = TelegramClient(
            session_name,
            api_id,
            api_hash,
            proxy=proxy_tuple,
        )

        await client.connect()

        if not await client.is_user_authorized():
            result["error"] = "not authorized"
            await client.disconnect()
            return result

        result["authorized"] = True

        me = await client.get_me()
        result["live_username"] = me.username or ""
        result["restricted"] = getattr(me, "restricted", False)
        if result["restricted"] and hasattr(me, "restriction_reason"):
            result["restriction_reason"] = str(me.restriction_reason)

        await client.disconnect()

    except Exception as e:
        result["error"] = str(e)[:80]

    return result


async def main():
    parser = argparse.ArgumentParser(description="View all account statuses")
    parser.add_argument(
        "--no-live", action="store_true",
        help="Skip live Telegram checks, show DB info only",
    )
    args = parser.parse_args()

    api_id = int(os.getenv("TG_API_ID", "0"))
    api_hash = os.getenv("TG_API_HASH", "")

    if not args.no_live and (not api_id or not api_hash):
        print("[WARN] TG_API_ID/TG_API_HASH not set. Running in DB-only mode.")
        args.no_live = True

    proxies_map = load_proxies()

    # Fetch all accounts from DB
    async with get_session() as session:
        stmt = select(Account).order_by(Account.id)
        result = await session.execute(stmt)
        accounts = result.scalars().all()

        if not accounts:
            print("\n  No accounts found in database.")
            print("  Run batch_setup.py first to import accounts.")
            return

        # Get group counts per account
        group_counts: dict[int, int] = {}
        gc_stmt = (
            select(GroupAccount.account_id, func.count(GroupAccount.group_id))
            .group_by(GroupAccount.account_id)
        )
        gc_result = await session.execute(gc_stmt)
        for aid, cnt in gc_result:
            group_counts[aid] = cnt

    # Live checks
    live_results: dict[int, dict] = {}
    if not args.no_live:
        print(f"  Checking {len(accounts)} accounts live...")
        for acc in accounts:
            proxy = proxies_map.get(acc.proxy_id)
            live = await check_account_live(
                session_file=acc.session_string,
                proxy=proxy,
                api_id=api_id,
                api_hash=api_hash,
            )
            live_results[acc.id] = live

            status_char = "." if live["authorized"] else "x"
            if live["restricted"]:
                status_char = "!"
            print(f"    {status_char}", end="", flush=True)

            # Brief delay to avoid rate limits
            await asyncio.sleep(1)
        print()

    # Print table
    now = datetime.utcnow()

    print(f"\n{'='*120}")
    print(f"  ACCOUNT STATUS REPORT  ({len(accounts)} accounts)")
    print(f"{'='*120}")

    header = (
        f"  {'ID':<4} "
        f"{'Phone':<16} "
        f"{'Username':<18} "
        f"{'Status':<13} "
        f"{'Risk':<6} "
        f"{'Role':<14} "
        f"{'Proxy':<8} "
        f"{'Groups':<7} "
        f"{'Last Active':<20} "
    )
    if not args.no_live:
        header += f"{'Live':<12}"

    print(header)
    print(f"  {'-'*4} {'-'*16} {'-'*18} {'-'*13} {'-'*6} {'-'*14} {'-'*8} {'-'*7} {'-'*20} ", end="")
    if not args.no_live:
        print(f"{'-'*12}", end="")
    print()

    status_counts = {"active": 0, "hibernating": 0, "nurturing": 0, "abandoned": 0}

    for acc in accounts:
        status_counts[acc.status] = status_counts.get(acc.status, 0) + 1

        # Format last active
        if acc.last_active:
            delta = now - acc.last_active
            if delta.days > 0:
                last_active_str = f"{delta.days}d ago"
            elif delta.seconds > 3600:
                last_active_str = f"{delta.seconds // 3600}h ago"
            else:
                last_active_str = f"{delta.seconds // 60}m ago"
        else:
            last_active_str = "never"

        # Proxy display
        proxy_info = proxies_map.get(acc.proxy_id)
        proxy_str = f"#{acc.proxy_id}" if acc.proxy_id else "none"

        # Risk display
        risk_str = f"{acc.risk_score:.1f}"

        # Group count
        grp_count = group_counts.get(acc.id, 0)

        row = (
            f"  {acc.id:<4} "
            f"{acc.phone:<16} "
            f"{'@' + acc.username if acc.username else '-':<18} "
            f"{acc.status:<13} "
            f"{risk_str:<6} "
            f"{acc.role:<14} "
            f"{proxy_str:<8} "
            f"{grp_count:<7} "
            f"{last_active_str:<20} "
        )

        if not args.no_live:
            live = live_results.get(acc.id, {})
            if live.get("error"):
                live_str = f"ERR: {live['error'][:20]}"
            elif live.get("restricted"):
                live_str = "RESTRICTED"
            elif live.get("authorized"):
                live_str = "OK"
            else:
                live_str = "?"
            row += f"{live_str:<12}"

        print(row)

    # Summary
    print(f"\n{'='*120}")
    print(f"  Summary:")
    for s, c in sorted(status_counts.items()):
        if c > 0:
            print(f"    {s:<14}: {c}")

    if not args.no_live and live_results:
        ok = sum(1 for v in live_results.values() if v.get("authorized") and not v.get("restricted"))
        restricted = sum(1 for v in live_results.values() if v.get("restricted"))
        errors = sum(1 for v in live_results.values() if v.get("error"))
        print(f"\n  Live checks:")
        print(f"    OK:         {ok}")
        print(f"    Restricted: {restricted}")
        print(f"    Errors:     {errors}")

    print(f"{'='*120}\n")


if __name__ == "__main__":
    asyncio.run(main())
