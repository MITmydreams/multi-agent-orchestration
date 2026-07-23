"""Enhanced group discovery strategies for ScoutAgent.

Five new discovery methods that complement the existing keyword search,
seed mining, discussion-group linking, and lyzem web search.

Priority order (highest first):
  2. Forward source tracking
  4. Bio / pinned-message link mining
  1. Member overlap (common chats)
  5. Adaptive keyword evolution
  3. Multi-source web discovery

All public functions accept the same (scout, ...) signature so that
ScoutAgent.collect_intelligence() can call them uniformly.
"""

from __future__ import annotations

import asyncio
import random
import re
import time
from collections import Counter
from datetime import datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import select, func

from src.config import settings
from src.models import Group, GroupAccount, get_session
from src.tg_clients.user_client import UserClientManager

logger = structlog.get_logger(__name__)

# Shared regex for t.me links
_TME_RE = re.compile(
    r"(?:https?://)?t\.me/(?:\+|joinchat/)?([a-zA-Z0-9_\-]{4,})",
    re.IGNORECASE,
)
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")

# Handles that are never real groups
_BLACKLIST_HANDLES = frozenset({
    "share", "joinchat", "addstickers", "proxy", "socks",
    "setlanguage", "addtheme", "iv", "contact", "bot",
    "example_bot", "game",
})


async def _group_exists(tg_group_id: str) -> bool:
    """Check if a group already exists in the DB."""
    try:
        async with get_session() as session:
            stmt = select(Group.id).where(Group.tg_group_id == tg_group_id)
            result = await session.execute(stmt)
            return result.scalar_one_or_none() is not None
    except Exception:
        return False


def _is_valid_handle(handle: str) -> bool:
    """Quick sanity check for a t.me handle."""
    if not handle or len(handle) < 4:
        return False
    if handle.lower() in _BLACKLIST_HANDLES:
        return False
    if handle.lower().endswith("bot"):
        return False
    return True


# ======================================================================
# Strategy 2: Forward Source Tracking (highest priority)
# ======================================================================

async def discover_via_forward_trace(
    user_client: UserClientManager,
    *,
    max_groups_to_scan: int = 0,
    max_results: int = 0,
) -> list[dict[str, Any]]:
    """Scan forwarded messages in joined groups to discover source channels/groups.

    When someone forwards a message from group A into group B, Telethon
    exposes the source via ``msg.fwd_from``.  We resolve that source and
    check whether it's a group worth infiltrating.

    This is the highest-ROI new strategy because forwarded content
    naturally flows from high-activity communities, exactly our target.
    """
    max_groups_to_scan = max_groups_to_scan or settings.forward_trace_max_groups
    max_results = max_results or settings.forward_trace_max_results
    log = logger.bind(strategy="forward_trace")

    discovered: list[dict[str, Any]] = []
    seen_ids: set[int] = set()

    # Get groups where we have accounts joined (freshest data)
    joined_groups: list[tuple[str, int]] = []
    try:
        async with get_session() as session:
            stmt = (
                select(Group.tg_group_id, GroupAccount.account_id)
                .join(GroupAccount, Group.id == GroupAccount.group_id)
                .where(Group.status.in_(["evaluated", "infiltrating", "active"]))
                .order_by(Group.member_count.desc())
                .limit(max_groups_to_scan)
            )
            result = await session.execute(stmt)
            joined_groups = [(r[0], r[1]) for r in result.all()]
    except Exception:
        log.exception("forward_trace.db_error")
        return []

    if not joined_groups:
        log.info("forward_trace.no_joined_groups")
        return []

    log.info("forward_trace.start", groups=len(joined_groups))

    from telethon.tl.types import PeerChannel, PeerChat

    # Get a capable wrapper to read messages directly (need fwd_from header)
    wrappers = await user_client._ensure_capable_wrappers()
    if not wrappers:
        log.warning("forward_trace.no_available_accounts")
        return []

    wrapper = wrappers[0]

    for group_id, account_id in joined_groups:
        if len(discovered) >= max_results:
            break

        try:
            entity = await user_client._resolve_entity(wrapper, group_id)
        except Exception:
            log.debug("forward_trace.resolve_failed", group_id=group_id)
            continue

        try:
            async for msg in wrapper.client.iter_messages(entity, limit=200):
                if len(discovered) >= max_results:
                    break

                # Check for forwarded message header
                if not msg.fwd_from or not msg.fwd_from.from_id:
                    continue

                fwd_peer = msg.fwd_from.from_id

                # Extract the numeric channel/chat ID
                if isinstance(fwd_peer, PeerChannel):
                    fwd_id = fwd_peer.channel_id
                elif isinstance(fwd_peer, PeerChat):
                    fwd_id = fwd_peer.chat_id
                else:
                    continue

                if fwd_id in seen_ids:
                    continue
                seen_ids.add(fwd_id)

                # Resolve the source entity to get metadata
                try:
                    source_entity = await wrapper.client.get_entity(fwd_id)
                except Exception:
                    continue

                username = getattr(source_entity, "username", "") or ""
                title = getattr(source_entity, "title", "") or ""
                tg_id = f"@{username.lower()}" if username else str(fwd_id)

                if _CJK_RE.search(title):
                    continue
                if await _group_exists(tg_id):
                    continue

                member_count = getattr(source_entity, "participants_count", 0) or 0

                discovered.append({
                    "tg_group_id": tg_id,
                    "title": title,
                    "username": username.lower() or None,
                    "member_count": member_count,
                    "source": "forward_trace",
                    "found_in": group_id,
                })
                log.info(
                    "forward_trace.found",
                    source_group=group_id,
                    discovered=tg_id,
                    members=member_count,
                )

        except Exception:
            log.debug("forward_trace.scan_error", group_id=group_id, exc_info=True)

        await asyncio.sleep(random.uniform(2, 5))

    log.info("forward_trace.done", found=len(discovered))
    return discovered


# ======================================================================
# Strategy 4: Bio / Pinned Message Link Mining
# ======================================================================

async def discover_via_bio_links(
    user_client: UserClientManager,
    *,
    max_groups_to_scan: int = 0,
    max_results: int = 0,
) -> list[dict[str, Any]]:
    """Extract t.me links from group descriptions (about) and pinned messages.

    Many tech groups recommend sister groups, partner communities, or
    project-specific chats in their bio/about text and pinned messages.
    These are higher-signal than random message links because they
    represent deliberate community connections.
    """
    max_groups_to_scan = max_groups_to_scan or settings.bio_links_max_groups
    max_results = max_results or settings.bio_links_max_results
    log = logger.bind(strategy="bio_links")

    discovered: list[dict[str, Any]] = []
    seen_handles: set[str] = set()

    # Scan groups that haven't been bio-checked yet
    groups_to_scan: list[tuple[str, str | None]] = []
    try:
        async with get_session() as session:
            stmt = (
                select(Group.tg_group_id, Group.notes)
                .where(Group.status.in_(["evaluated", "infiltrating", "active", "discovered"]))
                .where(~Group.notes.contains("[bio-checked]"))
                .order_by(Group.score.desc())  # best groups first
                .limit(max_groups_to_scan)
            )
            result = await session.execute(stmt)
            groups_to_scan = [(r[0], r[1]) for r in result.all()]
    except Exception:
        log.exception("bio_links.db_error")
        return []

    if not groups_to_scan:
        log.info("bio_links.no_groups_to_scan")
        return []

    log.info("bio_links.start", groups=len(groups_to_scan))

    for group_id, _ in groups_to_scan:
        if len(discovered) >= max_results:
            break

        text_sources: list[str] = []

        # 1. Get group info (includes about/description)
        try:
            info = await user_client.get_group_info_any(group_id)
            if info:
                about = info.get("about", "") or info.get("description", "")
                if about:
                    text_sources.append(about)
        except Exception:
            log.debug("bio_links.info_failed", group_id=group_id)

        # 2. Get pinned message (try via recent messages with pinned flag)
        try:
            # Telethon: get pinned messages via iter_messages with filter
            wrappers = await user_client._ensure_capable_wrappers()
            for wrapper in wrappers:
                try:
                    from telethon import functions, types
                    entity = await user_client._resolve_entity(wrapper, group_id)
                    # Get pinned messages
                    async for msg in wrapper.client.iter_messages(
                        entity, filter=types.InputMessagesFilterPinned(), limit=5,
                    ):
                        if msg.text:
                            text_sources.append(msg.text)
                    break  # success, no need to try other wrappers
                except Exception:
                    continue
        except Exception:
            log.debug("bio_links.pinned_failed", group_id=group_id)

        # Mark as checked
        try:
            async with get_session() as session:
                stmt = select(Group).where(Group.tg_group_id == group_id)
                result = await session.execute(stmt)
                g = result.scalar_one_or_none()
                if g:
                    g.notes = (g.notes or "") + " [bio-checked]"
        except Exception:
            pass

        # Extract t.me links from all gathered text
        full_text = "\n".join(text_sources)
        for match in _TME_RE.finditer(full_text):
            if len(discovered) >= max_results:
                break

            handle = match.group(1).lower()
            if not _is_valid_handle(handle):
                continue
            if handle in seen_handles:
                continue
            seen_handles.add(handle)

            tg_id = f"@{handle}"
            if await _group_exists(tg_id):
                continue

            discovered.append({
                "tg_group_id": tg_id,
                "title": handle,
                "username": handle,
                "member_count": 0,
                "source": "bio_links",
                "found_in": group_id,
            })
            log.info("bio_links.found", group=group_id, handle=handle)

        await asyncio.sleep(random.uniform(2, 5))

    log.info("bio_links.done", found=len(discovered))
    return discovered


# ======================================================================
# Strategy 1: Member Overlap Discovery (Common Chats)
# ======================================================================

async def discover_via_member_overlap(
    user_client: UserClientManager,
    *,
    max_groups: int = 0,
    max_members_per_group: int = 0,
    max_results: int = 0,
) -> list[dict[str, Any]]:
    """Discover groups by finding what other groups active members belong to.

    From high-quality (grade S/A) groups, sample a few visible members,
    then use ``GetCommonChatsRequest`` to see their other groups.
    These "neighbour groups" are likely also relevant communities.

    IMPORTANT: This is aggressive on the API. Rate-limit strictly:
    - Max 5-10 groups per day
    - 30-60s between API calls
    - Only use scout-role accounts
    """
    max_groups = max_groups or settings.member_overlap_max_groups
    max_members_per_group = max_members_per_group or settings.member_overlap_max_members
    max_results = max_results or settings.member_overlap_max_results
    log = logger.bind(strategy="member_overlap")

    discovered: list[dict[str, Any]] = []
    seen_chats: set[str] = set()

    # Only process high-quality groups
    source_groups: list[str] = []
    try:
        async with get_session() as session:
            stmt = (
                select(Group.tg_group_id)
                .where(Group.grade.in_(["S", "A"]))
                .where(Group.status.in_(["evaluated", "infiltrating", "active"]))
                .where(~Group.notes.contains("[overlap-checked]"))
                .order_by(Group.score.desc())
                .limit(max_groups)
            )
            result = await session.execute(stmt)
            source_groups = [r[0] for r in result.all()]
    except Exception:
        log.exception("member_overlap.db_error")
        return []

    if not source_groups:
        log.info("member_overlap.no_source_groups")
        return []

    log.info("member_overlap.start", groups=len(source_groups))

    from telethon import functions, types, errors as tg_errors

    wrappers = await user_client._ensure_capable_wrappers()
    if not wrappers:
        log.warning("member_overlap.no_available_accounts")
        return []

    wrapper = wrappers[0]  # Use first available

    for group_id in source_groups:
        if len(discovered) >= max_results:
            break

        try:
            entity = await user_client._resolve_entity(wrapper, group_id)
            if not isinstance(entity, types.Channel):
                continue

            # Get a sample of recent participants
            try:
                participants = await wrapper.client(
                    functions.channels.GetParticipantsRequest(
                        channel=entity,
                        filter=types.ChannelParticipantsRecent(),
                        offset=0,
                        limit=max_members_per_group,
                        hash=0,
                    )
                )
            except tg_errors.ChatAdminRequiredError:
                log.debug("member_overlap.admin_required", group=group_id)
                continue
            except tg_errors.FloodWaitError as e:
                log.warning("member_overlap.flood_wait", seconds=e.seconds)
                await asyncio.sleep(min(e.seconds, 300))
                break  # stop processing more groups this cycle
            except Exception:
                log.debug("member_overlap.get_participants_failed", group=group_id)
                continue

            users = getattr(participants, "users", [])
            # Filter to real users (not bots, not deleted)
            real_users = [
                u for u in users
                if not getattr(u, "bot", False)
                and not getattr(u, "deleted", False)
                and getattr(u, "id", 0)
            ]

            # Sample a subset to avoid too many API calls
            sample_size = min(5, len(real_users))
            sampled = random.sample(real_users, sample_size) if real_users else []

            for user in sampled:
                if len(discovered) >= max_results:
                    break

                try:
                    common = await wrapper.client(
                        functions.messages.GetCommonChatsRequest(
                            user_id=user,
                            max_id=0,
                            limit=20,
                        )
                    )
                except tg_errors.FloodWaitError as e:
                    log.warning("member_overlap.common_chats_flood", seconds=e.seconds)
                    await asyncio.sleep(min(e.seconds, 300))
                    break
                except Exception:
                    continue

                for chat in getattr(common, "chats", []):
                    username = getattr(chat, "username", "") or ""
                    title = getattr(chat, "title", "") or ""
                    tg_id = f"@{username.lower()}" if username else str(getattr(chat, "id", ""))

                    if tg_id in seen_chats:
                        continue
                    seen_chats.add(tg_id)

                    if _CJK_RE.search(title):
                        continue
                    if await _group_exists(tg_id):
                        continue

                    is_megagroup = bool(getattr(chat, "megagroup", False))
                    member_count = getattr(chat, "participants_count", 0) or 0

                    discovered.append({
                        "tg_group_id": tg_id,
                        "title": title,
                        "username": username.lower() or None,
                        "member_count": member_count,
                        "is_megagroup": is_megagroup,
                        "source": "member_overlap",
                        "found_in": group_id,
                    })
                    log.info("member_overlap.found", group=group_id, discovered=tg_id)

                # Strict rate limit between users
                await asyncio.sleep(random.uniform(30, 60))

            # Mark as checked
            try:
                async with get_session() as session:
                    stmt = select(Group).where(Group.tg_group_id == group_id)
                    result = await session.execute(stmt)
                    g = result.scalar_one_or_none()
                    if g:
                        g.notes = (g.notes or "") + " [overlap-checked]"
            except Exception:
                pass

        except Exception:
            log.exception("member_overlap.group_error", group=group_id)

        # Long pause between groups
        await asyncio.sleep(random.uniform(30, 60))

    log.info("member_overlap.done", found=len(discovered))
    return discovered


# ======================================================================
# Strategy 5: Adaptive Keyword Evolution
# ======================================================================

async def evolve_keywords(
    user_client: UserClientManager,
    existing_keywords: set[str],
    *,
    max_groups: int = 0,
    max_new_keywords: int = 0,
) -> list[str]:
    """Generate new search keywords from high-quality groups' titles and descriptions.

    Instead of hardcoding keywords, we learn from the groups that already
    scored well:
    1. Collect titles + about text from S/A/B graded groups
    2. Tokenise and count word frequencies
    3. Filter out common stopwords and existing keywords
    4. Return top candidates as new search terms

    These can be fed back into discover_groups() for the next cycle.
    """
    max_groups = max_groups or settings.keyword_evolution_max_groups
    max_new_keywords = max_new_keywords or settings.keyword_evolution_max_keywords
    log = logger.bind(strategy="keyword_evolution")

    # Stopwords that are too generic to be useful search terms
    stopwords = {
        "the", "and", "for", "with", "from", "that", "this", "are", "was",
        "have", "has", "been", "will", "can", "not", "all", "but", "our",
        "your", "their", "chat", "group", "channel", "official", "community",
        "join", "here", "about", "just", "more", "new", "free", "best",
        "get", "how", "you", "out", "now", "one", "also", "use", "via",
        "telegram", "https", "http", "www", "com",
    }

    texts: list[str] = []
    try:
        async with get_session() as session:
            stmt = (
                select(Group.title, Group.notes)
                .where(Group.grade.in_(["S", "A", "B"]))
                .where(Group.status.in_(["evaluated", "infiltrating", "active"]))
                .order_by(Group.score.desc())
                .limit(max_groups)
            )
            result = await session.execute(stmt)
            for title, notes in result.all():
                if title:
                    texts.append(title.lower())
    except Exception:
        log.exception("keyword_evolution.db_error")
        return []

    # Also collect about/description from top groups via Telethon
    top_groups: list[str] = []
    try:
        async with get_session() as session:
            stmt = (
                select(Group.tg_group_id)
                .where(Group.grade.in_(["S", "A"]))
                .where(Group.status.in_(["evaluated", "infiltrating", "active"]))
                .order_by(Group.score.desc())
                .limit(min(10, max_groups))
            )
            result = await session.execute(stmt)
            top_groups = [r[0] for r in result.all()]
    except Exception:
        pass

    for gid in top_groups:
        try:
            info = await user_client.get_group_info_any(gid)
            if info:
                about = info.get("about", "") or info.get("description", "")
                if about:
                    texts.append(about.lower())
        except Exception:
            continue
        await asyncio.sleep(random.uniform(1, 3))

    if not texts:
        log.info("keyword_evolution.no_texts")
        return []

    # Tokenise: split on non-alphanumeric, keep tokens 3+ chars
    word_counts: Counter = Counter()
    for text in texts:
        tokens = re.findall(r"[a-z0-9]{3,}", text)
        word_counts.update(tokens)

    # Filter: remove stopwords, existing keywords, and rare words
    existing_lower = {kw.lower() for kw in existing_keywords}
    candidates: list[tuple[str, int]] = []
    for word, count in word_counts.most_common(200):
        if count < 2:
            break
        if word in stopwords:
            continue
        if word in existing_lower:
            continue
        if len(word) < 4:
            continue
        candidates.append((word, count))

    # Build bigrams from titles for more specific search terms
    bigrams: Counter = Counter()
    for text in texts:
        tokens = re.findall(r"[a-z0-9]{3,}", text)
        for i in range(len(tokens) - 1):
            bigram = f"{tokens[i]} {tokens[i+1]}"
            if bigram not in existing_lower:
                bigrams[bigram] += 1

    # Merge: single words + bigrams, sorted by frequency
    all_candidates: list[str] = []
    for bigram, count in bigrams.most_common(50):
        if count >= 2:
            all_candidates.append(bigram)
    for word, count in candidates:
        all_candidates.append(word)

    new_keywords = all_candidates[:max_new_keywords]
    log.info(
        "keyword_evolution.done",
        source_texts=len(texts),
        candidates=len(all_candidates),
        new_keywords=len(new_keywords),
        samples=new_keywords[:5],
    )
    return new_keywords


# ======================================================================
# Strategy 3: Multi-Source Web Discovery
# ======================================================================

async def discover_via_web_sources(
    existing_usernames: set[str],
    *,
    max_results: int = 0,
) -> list[dict[str, Any]]:
    """Search multiple web directories for Telegram groups.

    Sources:
    - tgstat.com (search page)
    - telemetr.io (category pages)
    - combot.org (group rankings)
    - Google site:t.me dorking (via Bing/DuckDuckGo as fallback)

    Each source runs as an independent async task. Results are merged
    and deduplicated before return.
    """
    max_results = max_results or settings.web_discovery_max_results
    log = logger.bind(strategy="web_sources")

    import httpx

    headers = {
        "User-Agent": _random_user_agent(),
        "Accept-Language": "en-US,en;q=0.9",
    }

    async def _search_tgstat(client: httpx.AsyncClient) -> list[dict]:
        """Scrape tgstat.com search results."""
        results: list[dict] = []
        queries = random.sample(settings.web_discovery_queries, min(4, len(settings.web_discovery_queries)))

        for query in queries:
            try:
                resp = await client.get(
                    "https://tgstat.com/search",
                    params={"q": query, "type": "group"},
                    headers=headers,
                )
                if resp.status_code != 200:
                    continue

                for match in _TME_RE.finditer(resp.text):
                    handle = match.group(1).lower()
                    if _is_valid_handle(handle) and handle not in existing_usernames:
                        results.append({
                            "tg_group_id": f"@{handle}",
                            "title": handle,
                            "username": handle,
                            "member_count": 0,
                            "source": "tgstat",
                        })
            except Exception:
                log.debug("web.tgstat.error", query=query, exc_info=True)

            await asyncio.sleep(random.uniform(3, 8))
        return results

    async def _search_telemetr(client: httpx.AsyncClient) -> list[dict]:
        """Scrape telemetr.io category pages."""
        results: list[dict] = []
        categories = ["techcurrency", "games", "blockchain", "nft"]

        for cat in categories:
            for kind in ("groups", "channels"):
                try:
                    resp = await client.get(
                        f"https://telemetr.io/en/{kind}/{cat}",
                        headers=headers,
                    )
                    if resp.status_code != 200:
                        continue

                    for match in _TME_RE.finditer(resp.text):
                        handle = match.group(1).lower()
                        if _is_valid_handle(handle) and handle not in existing_usernames:
                            results.append({
                                "tg_group_id": f"@{handle}",
                                "title": handle,
                                "username": handle,
                                "member_count": 0,
                                "source": f"telemetr:{cat}",
                            })
                except Exception:
                    log.debug("web.telemetr.error", category=cat, exc_info=True)

                await asyncio.sleep(random.uniform(3, 8))
        return results

    async def _search_combot(client: httpx.AsyncClient) -> list[dict]:
        """Scrape combot.org top groups."""
        results: list[dict] = []
        try:
            resp = await client.get(
                "https://combot.org/telegram/top/groups",
                headers=headers,
            )
            if resp.status_code == 200:
                for match in _TME_RE.finditer(resp.text):
                    handle = match.group(1).lower()
                    if _is_valid_handle(handle) and handle not in existing_usernames:
                        results.append({
                            "tg_group_id": f"@{handle}",
                            "title": handle,
                            "username": handle,
                            "member_count": 0,
                            "source": "combot",
                        })
        except Exception:
            log.debug("web.combot.error", exc_info=True)
        return results

    async def _search_engine_dork(client: httpx.AsyncClient) -> list[dict]:
        """Search via Bing + DuckDuckGo for site:t.me links."""
        results: list[dict] = []
        queries = random.sample(settings.web_discovery_queries, min(5, len(settings.web_discovery_queries)))

        for query in queries:
            dork = f'site:t.me "{query}"'
            found = False

            # Try Bing first (less aggressive rate limiting)
            try:
                resp = await client.get(
                    "https://www.bing.com/search",
                    params={"q": dork, "count": 20},
                    headers=headers,
                )
                if resp.status_code == 200:
                    for match in _TME_RE.finditer(resp.text):
                        handle = match.group(1).lower()
                        if _is_valid_handle(handle) and handle not in existing_usernames:
                            results.append({
                                "tg_group_id": f"@{handle}",
                                "title": handle,
                                "username": handle,
                                "member_count": 0,
                                "source": "bing_dork",
                            })
                            found = True
            except Exception:
                pass

            if not found:
                # Fallback: DuckDuckGo HTML
                try:
                    resp = await client.get(
                        "https://html.duckduckgo.com/html/",
                        params={"q": dork},
                        headers=headers,
                    )
                    if resp.status_code == 200:
                        for match in _TME_RE.finditer(resp.text):
                            handle = match.group(1).lower()
                            if _is_valid_handle(handle) and handle not in existing_usernames:
                                results.append({
                                    "tg_group_id": f"@{handle}",
                                    "title": handle,
                                    "username": handle,
                                    "member_count": 0,
                                    "source": "ddg_dork",
                                })
                except Exception:
                    pass

            await asyncio.sleep(random.uniform(5, 12))
        return results

    # Run all sources concurrently
    log.info("web_sources.start")
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        tasks = [
            _search_tgstat(client),
            _search_telemetr(client),
            _search_combot(client),
            _search_engine_dork(client),
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    # Merge and dedup
    all_groups: list[dict] = []
    seen: set[str] = set()
    for batch in results:
        if isinstance(batch, Exception):
            log.warning("web_sources.source_failed", error=str(batch))
            continue
        for g in batch:
            handle = g.get("username", "").lower()
            if handle and handle not in seen:
                seen.add(handle)
                all_groups.append(g)

    log.info("web_sources.done", total=len(all_groups))
    return all_groups[:max_results]


def _random_user_agent() -> str:
    """Return a random desktop browser User-Agent string."""
    agents = [
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
    ]
    return random.choice(agents)
