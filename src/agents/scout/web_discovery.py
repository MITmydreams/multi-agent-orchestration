"""Web-based group discovery via public Telegram directories."""
import asyncio
import re
import random
from typing import Any

import httpx
import structlog

logger = structlog.get_logger(__name__)

LYZEM_URL = "https://lyzem.com/search"
TME_RE = re.compile(r'href=["\'](?:https?://)?t\.me/([a-zA-Z0-9_]+)["\']', re.IGNORECASE)
CJK_RE = re.compile(r'[\u4e00-\u9fff]')

# Keywords to search on lyzem (different from Telethon search — more specific)
WEB_SEARCH_KEYWORDS = [
    "community product chat", "tech game community", "play to earn group",
    "announcement hunter chat", "ton game chat", "telegram mini app chat",
    "community community", "tech earning group", "nft game chat",
    "referral programs group", "blockchain game community",
    "крипто игра чат", "заработок крипто группа",
    "game kiếm tiền nhóm", "announcement group chat",
    "usdt earning community", "saas game group",
    # Project-specific (high-signal)
    "hamster kombat group", "pixelverse chat", "catizen community",
    "blum tech chat", "notcoin group", "memefi chat",
    "yescoin community", "tomarket chat",
    # Chain ecosystems
    "solana announcement group", "sui community chat", "scroll announcement",
    "starknet community", "berachain chat", "base ecosystem group",
    # Regional
    "kripto oyun türkiye", "announcement türkiye", "tech brasil grupo",
    "announcement indonesia group", "tech vietnam nhóm",
    # Broader
    "telegram clicker game", "tech prediction game",
    "social experiment tech", "countdown game tech",
]


async def discover_from_lyzem(existing_usernames: set[str], max_results: int = 30) -> list[dict]:
    """Search lyzem.com for Telegram groups matching community/game keywords.

    Returns list of dicts with tg_group_id, title, username, source.
    """
    discovered: list[dict] = []
    seen: set[str] = set()

    keywords = random.sample(WEB_SEARCH_KEYWORDS, min(5, len(WEB_SEARCH_KEYWORDS)))

    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        for kw in keywords:
            if len(discovered) >= max_results:
                break
            try:
                resp = await client.get(
                    LYZEM_URL,
                    params={"q": kw, "type": "groups"},
                    headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
                )
                if resp.status_code != 200:
                    logger.debug("web_discovery.lyzem.http_error", status=resp.status_code, keyword=kw)
                    continue

                html = resp.text
                handles = TME_RE.findall(html)

                for handle in handles:
                    handle = handle.strip().lower()
                    if len(handle) < 4:
                        continue
                    if handle in seen or handle in existing_usernames:
                        continue
                    if handle in ("share", "joinchat", "addstickers", "proxy", "socks"):
                        continue

                    seen.add(handle)
                    tg_id = f"@{handle}"

                    discovered.append({
                        "tg_group_id": tg_id,
                        "title": handle,
                        "username": handle,
                        "member_count": 0,
                        "source": "lyzem",
                    })

                logger.info("web_discovery.lyzem.result", keyword=kw, found=len(handles))

            except Exception:
                logger.debug("web_discovery.lyzem.error", keyword=kw, exc_info=True)

            await asyncio.sleep(random.uniform(3, 8))  # polite crawling

    logger.info("web_discovery.lyzem.done", total=len(discovered))
    return discovered
