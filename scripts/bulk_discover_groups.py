"""Bulk group discovery via web scraping — no Telegram account needed.

Scrapes public Telegram directories (lyzem.com, tgstat.com, etc.) to find
active, high-member groups. Outputs a report and optionally imports to DB.

Usage:
    # Discover and preview
    .venv/bin/python scripts/bulk_discover_groups.py

    # Discover and import to DB
    .venv/bin/python scripts/bulk_discover_groups.py --import
"""
import argparse
import asyncio
import json
import random
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

DB_URL = "postgresql+asyncpg://promo:promo@localhost:5432/promo_bot"

# ---------------------------------------------------------------------------
# Extraction patterns
# ---------------------------------------------------------------------------
TME_RE = re.compile(r'(?:https?://)?t\.me/([a-zA-Z][a-zA-Z0-9_]{3,30})', re.IGNORECASE)
CJK_RE = re.compile(r'[\u4e00-\u9fff]{3,}')
BOT_RE = re.compile(r'(?i)bot$|_bot$')

SKIP_HANDLES = {
    "share", "joinchat", "addstickers", "proxy", "socks", "setlanguage",
    "addtheme", "iv", "login", "confirmphone", "dns", "bg",
}

NEGATIVE_KEYWORDS = [
    "signal", "shill", "pump", "dump", "1000x", "free usdt",
    "giveaway", "copy trade", "scam", "hack", "mining rig",
    "100x gem", "moonshot", "ponzi", "rugpull", "fake",
    "中文", "chinese", "华人", "链游", "交流群",
    "德州扑克", "返usdt", "副业", "代刷", "卖号",
]


def is_bad_handle(handle: str) -> bool:
    h = handle.lower()
    if h in SKIP_HANDLES:
        return True
    if BOT_RE.search(h):
        return True
    if any(neg in h for neg in NEGATIVE_KEYWORDS):
        return True
    if CJK_RE.search(h):
        return True
    if len(h) < 4 or len(h) > 32:
        return True
    return False


# ---------------------------------------------------------------------------
# Lyzem.com scraper
# ---------------------------------------------------------------------------
LYZEM_KEYWORDS = [
    # High-signal
    "airdrop hunter chat", "airdrop farming group", "airdrop alpha",
    "tap to earn", "telegram tap game", "telegram mini app game",
    "ton game chat", "play to airdrop", "gamefi guild",
    "crypto chat group", "web3 community chat", "ton chat group",
    "usdt earning group", "play to earn community",
    # Project-specific
    "hamster kombat group", "catizen community", "blum crypto chat",
    "notcoin group", "yescoin community", "memefi chat",
    "tomarket chat", "dogs token chat", "pixelverse community",
    # Exchange / wallet
    "binance web3 wallet chat", "okx web3 chat", "tonkeeper chat",
    "trust wallet community", "metamask community",
    # Chain ecosystems
    "solana airdrop group", "sui community chat", "scroll airdrop chat",
    "starknet community", "berachain chat", "base ecosystem group",
    "arbitrum community chat", "blast community chat",
    "ton ecosystem", "ton builders chat",
    # Medium signal
    "web3 gaming community", "gamefi community", "crypto game community",
    "zealy quest group", "galxe quest chat", "testnet farmers",
    "degens lounge", "alpha calls web3", "defi yield chat",
    "nft game chat", "blockchain game chat",
    # Broader
    "crypto earning chat", "crypto passive income group",
    "web3 builders chat", "crypto alpha group", "degen crypto chat",
    "telegram clicker game", "crypto prediction game",
    "social experiment crypto", "countdown game crypto",
    # Russian
    "крипто игра чат", "аирдроп охотник", "фарм аирдропов",
    "крипто заработок", "ton игры", "ретродроп чат",
    "hamster kombat россия", "notcoin чат", "blum россия",
    # Vietnamese
    "săn airdrop nhóm", "game kiếm tiền", "cộng đồng web3",
    "tap to earn việt nam", "nhóm chat crypto",
    # Indonesian
    "airdrop indonesia group", "pemburu airdrop", "game kripto indo",
    "tap to earn indo",
    # Turkish
    "kripto oyun türkiye", "airdrop türkiye", "telegram oyun",
    # Portuguese
    "airdrop brasil grupo", "jogo crypto brasil", "crypto brasil grupo",
]


async def scrape_lyzem(http: httpx.AsyncClient, existing: set[str]) -> dict[str, dict]:
    """Scrape lyzem.com for group handles."""
    found = {}
    total = len(LYZEM_KEYWORDS)

    for i, kw in enumerate(LYZEM_KEYWORDS):
        try:
            resp = await http.get(
                "https://lyzem.com/search",
                params={"q": kw, "type": "groups"},
                headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"},
            )
            if resp.status_code != 200:
                continue

            handles = TME_RE.findall(resp.text)
            new_this_kw = 0
            for handle in handles:
                handle = handle.strip().lower()
                if is_bad_handle(handle) or handle in existing or handle in found:
                    continue
                found[handle] = {
                    "username": handle,
                    "tg_group_id": f"@{handle}",
                    "title": handle,
                    "member_count": 0,
                    "source": "lyzem",
                }
                new_this_kw += 1

            print(f"\r  Lyzem [{i+1}/{total}] {kw:<45} +{new_this_kw:<4} total={len(found)}", end="", flush=True)
        except Exception as e:
            print(f"\n  ⚠ {kw}: {type(e).__name__}", flush=True)

        await asyncio.sleep(random.uniform(2, 5))

    print()
    return found


# ---------------------------------------------------------------------------
# TGStat.com scraper (public pages, no API key)
# ---------------------------------------------------------------------------
TGSTAT_CATEGORIES = [
    "crypto", "games", "finance", "technology", "economics",
]

TGSTAT_SEARCH_KEYWORDS = [
    "airdrop", "gamefi", "web3 game", "tap to earn", "ton game",
    "crypto game", "play to earn", "nft game", "defi",
    "hamster kombat", "catizen", "notcoin", "blum",
    "solana", "ton ecosystem", "arbitrum", "base chain",
]


async def scrape_tgstat(http: httpx.AsyncClient, existing: set[str]) -> dict[str, dict]:
    """Scrape tgstat.com search results for group handles."""
    found = {}
    total = len(TGSTAT_SEARCH_KEYWORDS)

    for i, kw in enumerate(TGSTAT_SEARCH_KEYWORDS):
        try:
            resp = await http.get(
                f"https://tgstat.com/search",
                params={"q": kw, "type": "chats"},
                headers={
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                    "Accept": "text/html",
                },
            )
            if resp.status_code != 200:
                continue

            handles = TME_RE.findall(resp.text)
            new_this_kw = 0
            for handle in handles:
                handle = handle.strip().lower()
                if is_bad_handle(handle) or handle in existing or handle in found:
                    continue
                found[handle] = {
                    "username": handle,
                    "tg_group_id": f"@{handle}",
                    "title": handle,
                    "member_count": 0,
                    "source": "tgstat",
                }
                new_this_kw += 1

            print(f"\r  TGStat [{i+1}/{total}] {kw:<45} +{new_this_kw:<4} total={len(found)}", end="", flush=True)
        except Exception as e:
            print(f"\n  ⚠ {kw}: {type(e).__name__}", flush=True)

        await asyncio.sleep(random.uniform(3, 6))

    print()
    return found


# ---------------------------------------------------------------------------
# Combot catalog scraper
# ---------------------------------------------------------------------------
async def scrape_combot(http: httpx.AsyncClient, existing: set[str]) -> dict[str, dict]:
    """Scrape combot.org catalog for top groups by category."""
    found = {}
    categories = ["crypto", "games", "finance", "technology", "investments"]

    for i, cat in enumerate(categories):
        for page in range(1, 4):  # first 3 pages
            try:
                resp = await http.get(
                    f"https://combot.org/top/telegram/chats/{cat}",
                    params={"page": page},
                    headers={
                        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                    },
                )
                if resp.status_code != 200:
                    break

                handles = TME_RE.findall(resp.text)
                for handle in handles:
                    handle = handle.strip().lower()
                    if is_bad_handle(handle) or handle in existing or handle in found:
                        continue
                    found[handle] = {
                        "username": handle,
                        "tg_group_id": f"@{handle}",
                        "title": handle,
                        "member_count": 0,
                        "source": "combot",
                    }
            except Exception:
                break

            await asyncio.sleep(random.uniform(2, 4))

        print(f"\r  Combot [{i+1}/{len(categories)}] {cat:<20} total={len(found)}", end="", flush=True)

    print()
    return found


# ---------------------------------------------------------------------------
# Import to DB
# ---------------------------------------------------------------------------
async def import_to_db(engine, groups):
    count = 0
    skipped = 0
    for info in groups.values():
        try:
            async with engine.begin() as conn:
                await conn.execute(text("""
                    INSERT INTO groups (
                        tg_group_id, title, username, member_count, daily_active,
                        language, topics, grade, score,
                        admin_strictness, link_tolerance,
                        best_posting_hours, competitor_presence, active_kols,
                        status, notes
                    ) VALUES (
                        :tg_id, :title, :username, :members, 0,
                        'en', '[]', 'C', 5.0,
                        'unknown', 'unknown',
                        '[]', '[]', '[]',
                        'evaluated', :notes
                    ) ON CONFLICT (tg_group_id) DO NOTHING
                """), {
                    "tg_id": info["tg_group_id"],
                    "title": info["title"],
                    "username": info.get("username", ""),
                    "members": info["member_count"],
                    "notes": f"bulk_discover:{info.get('source', '')}",
                })
            count += 1
        except Exception:
            skipped += 1
    if skipped:
        print(f"  ⚠ Skipped {skipped} groups (conflict or error)")
    return count


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def main():
    parser = argparse.ArgumentParser(description="Bulk discover Telegram groups (web only)")
    parser.add_argument("--import", dest="do_import", action="store_true",
                        help="Import discovered groups to database")
    args = parser.parse_args()

    engine = create_async_engine(DB_URL, echo=False)

    # Load existing groups
    print("\n📊 Loading existing groups from database...")
    existing = set()
    async with engine.connect() as conn:
        rows = await conn.execute(text("SELECT tg_group_id, username FROM groups"))
        for row in rows:
            existing.add(str(row[0]).lower().lstrip("@"))
            if row[1]:
                existing.add(row[1].lower().lstrip("@"))
    print(f"  Already in DB: {len(existing)} entries")

    # Scrape all sources
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as http:
        print(f"\n🔍 Source 1: Lyzem.com ({len(LYZEM_KEYWORDS)} keywords)...")
        lyzem = await scrape_lyzem(http, existing)

        print(f"\n🔍 Source 2: TGStat.com ({len(TGSTAT_SEARCH_KEYWORDS)} keywords)...")
        # Merge existing to avoid duplicates across sources
        combined_existing = existing | set(lyzem.keys())
        tgstat = await scrape_tgstat(http, combined_existing)

        print(f"\n🔍 Source 3: Combot.org (top group catalog)...")
        combined_existing2 = combined_existing | set(tgstat.keys())
        combot = await scrape_combot(http, combined_existing2)

    # Merge all
    all_groups = {**lyzem, **tgstat, **combot}

    # Report
    print(f"\n{'='*70}")
    print(f"  DISCOVERY REPORT")
    print(f"{'='*70}")
    print(f"  Already in DB:      {len(existing)}")
    print(f"  From Lyzem:         {len(lyzem)}")
    print(f"  From TGStat:        {len(tgstat)}")
    print(f"  From Combot:        {len(combot)}")
    print(f"  ─────────────────────────────")
    print(f"  Total NEW groups:   {len(all_groups)}")
    print(f"{'='*70}")

    if all_groups:
        # Group by source
        by_source = {}
        for g in all_groups.values():
            src = g["source"]
            by_source[src] = by_source.get(src, 0) + 1

        print(f"\n  By source: {by_source}")

        # Show all
        sorted_groups = sorted(all_groups.values(), key=lambda g: g["username"])
        print(f"\n  {'#':<5} {'Username':<35} Source")
        print(f"  {'─'*5} {'─'*35} {'─'*10}")
        for i, g in enumerate(sorted_groups, 1):
            print(f"  {i:<5} @{g['username']:<34} {g['source']}")

        # Save to file
        outfile = Path("data/discovered_groups.json")
        outfile.parent.mkdir(parents=True, exist_ok=True)
        outfile.write_text(json.dumps(sorted_groups, ensure_ascii=False, indent=2))
        print(f"\n  📄 Full list saved to: {outfile}")

    # Import
    if args.do_import and all_groups:
        print(f"\n💾 Importing {len(all_groups)} groups to database...")
        imported = await import_to_db(engine, all_groups)
        print(f"  ✅ Imported {imported} groups")

        async with engine.connect() as conn:
            result = await conn.execute(text(
                "SELECT status, count(*) FROM groups GROUP BY status ORDER BY count DESC"
            ))
            print(f"\n  📊 Database stats:")
            for row in result:
                print(f"     {row[0]:<15} {row[1]:>6}")
    elif not args.do_import and all_groups:
        print(f"\n  💡 Run with --import to add these to the database")

    await engine.dispose()
    print("\n✅ Done!\n")


if __name__ == "__main__":
    asyncio.run(main())
