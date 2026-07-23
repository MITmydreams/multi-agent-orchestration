"""Layer 1 -- Scout Agent: intelligence gathering, zero outreach.

The scout joins groups, observes, analyses, and records.  It never sends
outreach messages or links.  Its sole purpose is to discover and evaluate
target groups for downstream agents.
"""

from __future__ import annotations

import asyncio
import random
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import select

from src.agents.base import BaseAgent
from src.brain.circuit_breaker import CircuitBreaker
from src.brain.risk_engine import RiskEngine
from src.config import settings
from src.models import Group, GroupAccount, get_session
from src.tg_clients.user_client import UserClientManager

logger = structlog.get_logger(__name__)


SEARCH_KEYWORDS_TIERED: dict[str, dict[str, list[str]]] = {
    "high": {
        "en": [
            "announcement hunter", "announcement farming", "announcement alpha", "retroactive announcement",
            "referral programs", "telegram tap game", "telegram mini app game",
            "ton game", "ton tap", "play to announcement",
            "community guild", "community product testers", "tech game beta",
            "clicker tech", "telegram clicker",
            "tech chat group", "community community chat", "announcement discussion", "ton chat", "community chat",
            "usdt earning group", "tech earning chat", "play to earn community",
            "community product players", "telegram game earning",
            # Specific projects (high signal — people in these groups are our exact target)
            "hamster kombat chat", "pixelverse community", "catizen chat",
            "blum community", "notcoin chat", "yescoin community",
            "major community chat", "dogs token chat", "memefi community",
            "moonbix chat", "tomarket community", "rocky rabbit chat",
            "vertus community", "dotcoin chat", "seed app community",
            # Exchange / wallet ecosystem
            "binance community wallet", "okx community chat", "bybit community",
            "trust wallet community", "metamask community",
            "tonkeeper chat", "tonhub community",
        ],
        "zh": [],  # Chinese groups excluded by user policy

        "ru": [
            "аирдроп охотник", "фарм аирдропов", "бесплатные аирдропы",
            "telegram кликер", "тап ту ерн", "крипто игры",
            "ton игры", "announcement россия", "ретродроп",
            "p2e игры", "веб3 игры",
            "крипто чат", "аирдроп чат", "обсуждение крипто",
            "заработок usdt", "крипто заработок чат", "играй и зарабатывай",
            # Project-specific
            "hamster kombat россия", "notcoin чат", "blum россия",
            "catizen россия", "dogs токен чат",
        ],
        "vi": [
            "săn announcement", "kèo announcement", "announcement miễn phí",
            "game community", "game kiếm tiền", "referral programs việt nam",
            "ton game việt", "chơi game kiếm coin", "retroactive vietnam",
            "cày announcement", "game nft kiếm tiền",
            "nhóm chat tech", "thảo luận announcement", "cộng đồng community chat",
            "kiếm usdt", "nhóm kiếm tiền tech", "chơi game kiếm usdt",
            # Project-specific
            "hamster kombat việt", "notcoin việt nam", "blum việt nam",
        ],
        "id": [
            "announcement indonesia", "pemburu announcement", "announcement gratis",
            "game kripto", "referral programs indo", "game penghasil tech",
            "game ton indonesia", "main game dapat tech",
            "garap announcement", "game community indo", "retroactive announcement indo",
            # Project-specific
            "hamster kombat indo", "notcoin indonesia", "blum indonesia",
        ],
        # New languages
        "tr": [
            "announcement türkiye", "kripto oyun", "telegram oyun",
            "ton oyun türkiye", "ücretsiz announcement", "kripto kazanç",
            "referral programs türkiye", "community oyun türkçe", "announcement avı",
            "kripto sohbet", "play to earn türkiye",
        ],
        "pt": [
            "announcement brasil", "jogo tech", "telegram game brasil",
            "ton game brasil", "ganhar tech grátis", "caçador de announcement",
            "referral programs brasil", "community jogo", "comunidade tech brasil",
            "community brasil", "play to earn brasil",
        ],
    },
    "medium": {
        "en": [
            "ton ecosystem", "ton builders", "community gaming", "community",
            "tech quests", "zealy questers", "galxe quest",
            "testnet farmers", "analytics gaming", "layerzero farming",
            "zksync farming", "linea farming", "monad testnet",
            "degens lounge", "alpha calls community",
            "saas yield chat", "tech passive income", "staking community chat",
            # Chain ecosystems
            "solana announcement", "sui community", "aptos chat",
            "sei network community", "scroll community", "blast community",
            "starknet chat", "mantle community", "manta network chat",
            "berachain community", "celestia chat",
            # Broader tech gaming
            "idle game tech", "casual game blockchain", "social game community",
            "prediction market tech", "betting tech community",
        ],
        "zh": [],  # Chinese groups excluded by user policy
        "ru": [
            "крипто тестнет", "community геймеры", "крипто квесты",
            "galxe задания", "zealy", "layer2 фарм",
            "альфа крипто", "дегены", "ранние проекты",
            "ton экосистема", "крипто задания",
            "solana аирдроп", "sui чат", "scroll россия",
        ],
        "vi": [
            "hệ sinh thái ton", "testnet vietnam", "layer2 farming",
            "galxe việt", "zealy task", "alpha tech việt",
            "dự án sớm", "nhiệm vụ tech", "degen vietnam",
            "community vietnam",
            "solana việt nam", "sui việt nam",
        ],
        "id": [
            "testnet indo", "layer2 farming indo", "galxe indonesia",
            "zealy indo", "alpha tech indonesia", "ekosistem ton",
            "degen indo", "quest tech", "proyek awal tech",
            "community indonesia",
            "solana indonesia", "sui indonesia",
        ],
        "tr": [
            "kripto testnet türkiye", "community oyuncu türkiye",
            "galxe türkiye", "zealy türkiye", "layer2 türkiye",
            "solana türkiye", "ton ekosistem türkiye",
        ],
        "pt": [
            "testnet brasil", "community productrs brasil",
            "galxe brasil", "zealy brasil", "layer2 brasil",
            "solana brasil", "ton ecosistema brasil",
        ],
    },
    "low": {
        "en": [
            "community community", "tech beta tester", "blockchain gamers",
            "tech newbie", "saas starter", "metaverse players",
            "evm farming", "polygon zkEVM", "base ecosystem", "arbitrum community",
            "nft trading group", "tech signals free", "saas traders chat",
            "community builders", "tech developers chat",
        ],
        "zh": [],  # Chinese groups excluded by user policy
        "ru": [
            "крипто новичок", "community сообщество", "блокчейн игры",
            "arbitrum ru", "base ru", "zksync ru", "метавселенная",
            "saas новичок", "крипто игроки", "evm фарм",
        ],
        "vi": [
            "tech newbie việt", "cộng đồng community", "arbitrum việt",
            "base việt", "zksync việt", "metaverse việt",
            "người chơi blockchain", "saas cơ bản", "evm farming",
            "tech cho người mới",
        ],
        "id": [
            "tech pemula", "komunitas community indo", "arbitrum indo",
            "base indo", "zksync indo", "metaverse indo",
            "pemain blockchain", "saas pemula", "evm farming",
            "tech untuk pemula",
        ],
        "tr": [
            "kripto yeni başlayan", "community topluluk türkiye",
            "arbitrum türkiye", "base türkiye", "blockchain oyuncular",
        ],
        "pt": [
            "tech iniciante", "comunidade community brasil",
            "arbitrum brasil", "base brasil", "jogadores blockchain",
        ],
    },
}

NEGATIVE_KEYWORDS: list[str] = [
    "signal", "shill", "pump", "dump", "1000x", "free usdt",
    "giveaway", "copy trade", "喊单", "跟单", "内幕",
    "代刷", "卖号", "scam", "hack", "mining rig",
    "100x gem", "moonshot",
    # Chinese group markers — user policy: do NOT join Chinese groups
    "中文", "chinese", "华人", "华语", "汉语", "简中", "繁中",
    "中国", "大陆", "台湾", "香港",
    # Chinese content words (groups with Chinese titles)
    "社区", "公会", "交流群", "行业玩家", "发财", "探长",
    "空投社区", "频道", "招聘", "求助", "加速器",
    "德州扑克", "返usdt", "副业",
]

# Regex for detecting 3+ consecutive CJK characters in a title
import re as _re
_CJK_RE = _re.compile(r'[\u4e00-\u9fff]')

# Skip zh in rotation — user does not want Chinese groups
LANGUAGE_ROTATION: list[str] = ["en", "ru", "vi", "id", "tr", "pt"]


class ScoutAgent(BaseAgent):
    """Intelligence collector -- discovers, evaluates, and catalogues groups.

    Daily targets:
    - Discover 20-50 new candidate groups
    - Evaluate group quality (activity, member quality, admin strictness)
    - Identify KOLs and influential members
    - Monitor competitor presence
    - Assess anti-spam severity

    Rules:
    - NEVER send outreach content
    - NEVER interact with executor accounts
    - Only join groups, read messages, and record findings
    """

    name: str = "scout"
    agent_type: str = "scout"

    # ------------------------------------------------------------------
    # Target group search keywords (prioritised)
    # ------------------------------------------------------------------

    # Backward-compat alias: flat list (high-tier English) for any legacy references
    SEARCH_KEYWORDS: list[str] = SEARCH_KEYWORDS_TIERED["high"]["en"]

    # Evaluation weights
    _W_ACTIVITY: float = 0.25
    _W_MEMBER_QUALITY: float = 0.25
    _W_TOPIC_RELEVANCE: float = 0.20
    _W_ADMIN_LENIENCY: float = 0.20
    _W_COMPETITOR_DENSITY: float = 0.10

    # Grade thresholds
    _GRADE_THRESHOLDS: list[tuple[float, str]] = [
        (90.0, "S"),
        (70.0, "A"),
        (50.0, "B"),
        (0.0,  "C"),
    ]

    # Daily caps
    DAILY_DISCOVER_MIN: int = 20
    DAILY_DISCOVER_MAX: int = 50
    MAX_GROUPS_PER_SCAN: int = 10

    # Seed-mining recursive expansion cap (config seeds + DB high-grade groups)
    MAX_MINING_SOURCES: int = 30
    # Concurrency cap for parallel seed mining
    SEED_MINING_CONCURRENCY: int = 3

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._lang_cursor: int = 0
        # Per-cycle dedup set (cleared each cycle) of seeds already mined
        self._mined_recently: set[str] = set()
        # Cross-cycle keyword dedup: skip keywords already searched recently
        self._searched_keywords: set[str] = set()
        # Cycle counter for strategies that run less frequently
        self._cycle_count: int = 0

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Main loop: scan -> evaluate -> persist."""
        self._log.info("scout.run.start")
        while self._running:
            if not await self.should_proceed():
                await self._jittered_sleep(60)
                continue

            try:
                # Phase 1: discover new groups
                keywords = self._pick_keywords()
                discovered = await self.discover_groups(keywords)
                self._log.info("scout.discovered", count=len(discovered))

                # Phase 2: evaluate each discovered group
                for group_info in discovered:
                    if not self._running:
                        break
                    group_id = group_info.get("tg_group_id", "")
                    if not group_id:
                        continue

                    evaluation = await self.evaluate_group(group_id)
                    if evaluation:
                        await self._persist_group(group_info, evaluation)
                        await self.log_activity("evaluate_group", {
                            "group_id": group_id,
                            "grade": evaluation.get("grade"),
                            "score": evaluation.get("score"),
                        })

                    await self._jittered_sleep(random.uniform(30, 90))

                # Phase 3: re-evaluate existing groups periodically
                await self._reevaluate_stale_groups()

            except Exception:
                self._log.exception("scout.run.cycle_error")

            # Sleep between full cycles (30-60 min)
            await self._jittered_sleep(random.uniform(1800, 3600))

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    async def discover_groups(self, keywords: list[str]) -> list[dict]:
        """Search Telegram for groups matching *keywords*.

        Returns a list of dicts with at least ``tg_group_id``, ``title``,
        ``member_count``, and ``username``.
        """
        self._log.info("scout.discover.start", keywords=keywords)
        discovered: list[dict] = []
        for kw in keywords:
            if not self._running:
                break
            try:
                results = await self.user_client.search_groups(kw)
                self._log.info("scout.discover.kw_result", keyword=kw, count=len(results))
                for r in results:
                    tg_id = str(r.get("id", ""))
                    if not tg_id:
                        continue
                    if await self._group_exists(tg_id):
                        continue
                    title_lower = (r.get("title", "") or "").lower()
                    if any(neg in title_lower for neg in NEGATIVE_KEYWORDS) or _CJK_RE.search(title_lower):
                        continue
                    discovered.append({
                        "tg_group_id": tg_id,
                        "title": r.get("title", ""),
                        "username": r.get("username"),
                        "member_count": r.get("member_count", 0),
                    })
                    if len(discovered) >= self.DAILY_DISCOVER_MAX:
                        break
            except Exception:
                self._log.warning("scout.discover.keyword_failed", keyword=kw)
            await self._jittered_sleep(random.uniform(10, 30))

            if len(discovered) >= self.DAILY_DISCOVER_MAX:
                break

        self._log.info("scout.discover.done", total=len(discovered))
        return discovered

    # ------------------------------------------------------------------
    # Evaluation
    # ------------------------------------------------------------------

    async def evaluate_group(self, group_id: str, account_id: int | None = None) -> dict[str, Any]:
        """Evaluate a group across five dimensions and return a scored report.

        Score = W1*activity(25%) + W2*member_quality(25%)
                + W3*topic_relevance(20%) + W4*admin_leniency(20%)
                - W5*competitor_density(10%)

        Grades: S (90+), A (70-89), B (50-69), C (<50)

        If *account_id* is given, that specific account is used for
        ``get_recent_messages`` to distribute load across accounts.
        """
        try:
            messages = await self.user_client.get_recent_messages(group_id, limit=200, account_id=account_id)
            group_info = await self.user_client.get_group_info_any(group_id)
        except Exception:
            self._log.warning("scout.evaluate.fetch_failed", group_id=group_id)
            return {}

        if group_info is None:
            group_info = {}

        activity_score = self._score_activity(messages, group_info)
        member_quality_score = self._score_member_quality(messages)
        topic_score = self._score_topic_relevance(messages, group_info)
        admin_score = self._score_admin_leniency(messages, group_info)
        competitor_score = self._score_competitor_density(messages)

        composite = (
            self._W_ACTIVITY * activity_score
            + self._W_MEMBER_QUALITY * member_quality_score
            + self._W_TOPIC_RELEVANCE * topic_score
            + self._W_ADMIN_LENIENCY * admin_score
            - self._W_COMPETITOR_DENSITY * competitor_score
        )
        composite = max(0.0, min(100.0, composite))

        kols = await self.identify_kols(group_id, messages)
        safety = await self.assess_safety(group_id, messages)

        is_megagroup = group_info.get("is_megagroup", False) if group_info else False

        # Penalise broadcast channels -- we cannot post in them
        if not is_megagroup:
            composite *= 0.3

        composite = max(0.0, min(100.0, composite))
        grade = self._score_to_grade(composite)

        return {
            "score": round(composite, 2),
            "grade": grade,
            "is_megagroup": is_megagroup,
            "activity_score": round(activity_score, 2),
            "member_quality_score": round(member_quality_score, 2),
            "topic_relevance_score": round(topic_score, 2),
            "admin_leniency_score": round(admin_score, 2),
            "competitor_density_score": round(competitor_score, 2),
            "kols": kols,
            "safety": safety,
            "member_count": group_info.get("member_count", 0),
            "language": group_info.get("language", "en"),
            "topics": self._extract_topics(messages, group_info),
            "evaluated_at": datetime.utcnow().isoformat(),
        }

    async def identify_kols(self, group_id: str, messages: list[dict]) -> list[dict]:
        """Identify KOLs and active influencers from message history.

        Criteria: top-10% by message count, reply receivers, non-admin.
        """
        user_stats: dict[str, dict[str, Any]] = {}

        for msg in messages:
            uid = str(msg.get("from_id", ""))
            if not uid:
                continue
            if uid not in user_stats:
                user_stats[uid] = {
                    "user_id": uid,
                    "username": msg.get("from_username", ""),
                    "display_name": msg.get("from_name", ""),
                    "message_count": 0,
                    "replies_received": 0,
                    "is_admin": msg.get("is_admin", False),
                }
            user_stats[uid]["message_count"] += 1

            reply_to = str(msg.get("reply_to_user_id", ""))
            if reply_to and reply_to in user_stats:
                user_stats[reply_to]["replies_received"] += 1

        if not user_stats:
            return []

        all_users = sorted(
            user_stats.values(), key=lambda u: u["message_count"], reverse=True,
        )
        top_n = max(1, len(all_users) // 10)
        kols: list[dict] = []

        for user in all_users[:top_n]:
            if user["is_admin"]:
                continue
            influence_score = user["message_count"] * 0.6 + user["replies_received"] * 0.4
            kols.append({
                "user_id": user["user_id"],
                "username": user["username"],
                "display_name": user["display_name"],
                "message_count": user["message_count"],
                "replies_received": user["replies_received"],
                "influence_score": round(influence_score, 2),
            })

        return sorted(kols, key=lambda k: k["influence_score"], reverse=True)[:20]

    async def assess_safety(self, group_id: str, messages: list[dict]) -> dict[str, Any]:
        """Evaluate the group's anti-spam strictness.

        Signals: anti-spam bots, deletion frequency, admin action rate, link restrictions.
        """
        anti_spam_bots = {
            "rose", "combot", "groupbutler", "shieldy",
            "captchabot", "missrose_bot",
        }
        detected_bots: list[str] = []
        deletion_count = 0
        admin_action_count = 0
        link_messages = 0
        total_messages = len(messages)

        for msg in messages:
            username = (msg.get("from_username") or "").lower()
            if username in anti_spam_bots:
                detected_bots.append(username)
            if msg.get("is_deleted", False):
                deletion_count += 1
            if msg.get("is_admin_action", False):
                admin_action_count += 1
            if msg.get("has_link", False):
                link_messages += 1

        deletion_rate = deletion_count / total_messages if total_messages > 0 else 0
        link_ratio = link_messages / total_messages if total_messages > 0 else 0

        strictness = "low"
        if detected_bots or deletion_rate > 0.1 or admin_action_count > 5:
            strictness = "high"
        elif deletion_rate > 0.03 or admin_action_count > 2:
            strictness = "medium"

        link_tolerance = "high"
        if link_ratio < 0.01 or strictness == "high":
            link_tolerance = "low"
        elif link_ratio < 0.05:
            link_tolerance = "medium"

        return {
            "strictness": strictness,
            "link_tolerance": link_tolerance,
            "anti_spam_bots": list(set(detected_bots)),
            "deletion_rate": round(deletion_rate, 4),
            "admin_action_count": admin_action_count,
            "link_ratio": round(link_ratio, 4),
        }

    # ------------------------------------------------------------------
    # Scoring helpers (each returns 0-100)
    # ------------------------------------------------------------------

    def _score_activity(self, messages: list[dict], group_info: dict) -> float:
        if not messages:
            return 0.0
        unique_senders = len({msg.get("from_id") for msg in messages if msg.get("from_id")})
        member_count = group_info.get("member_count", 1)
        participation_rate = unique_senders / max(member_count, 1)
        mph = len(messages) / max(1, 24)
        score = min(50.0, participation_rate * 500) + min(50.0, mph * 5)
        return min(100.0, score)

    def _score_member_quality(self, messages: list[dict]) -> float:
        if not messages:
            return 0.0
        total_len = sum(len(msg.get("text", "")) for msg in messages)
        substantive = sum(1 for msg in messages if len(msg.get("text", "")) > 20)
        avg_len = total_len / len(messages)
        substantive_ratio = substantive / len(messages)
        return min(100.0, min(50.0, avg_len * 0.5) + substantive_ratio * 50)

    def _score_topic_relevance(self, messages: list[dict], group_info: dict) -> float:
        relevant_terms = {
            "tech", "community", "saas", "nft", "announcement", "blockchain", "token",
            "game", "play", "earn", "ton", "telegram", "mini app",
            "行业", "空投", "社区", "区块链", "代币", "游戏",
        }
        text_blob = f"{(group_info.get('title') or '').lower()} {(group_info.get('description') or '').lower()}"
        for msg in messages[:50]:
            text_blob += f" {(msg.get('text') or '').lower()}"
        hits = sum(1 for term in relevant_terms if term in text_blob)
        return min(100.0, hits * 8.0)

    def _score_admin_leniency(self, messages: list[dict], group_info: dict) -> float:
        """Higher score = more lenient admins (better for engagement)."""
        deletion_count = sum(1 for m in messages if m.get("is_deleted"))
        admin_actions = sum(1 for m in messages if m.get("is_admin_action"))
        total = len(messages) or 1
        strictness_raw = (deletion_count + admin_actions * 2) / total
        return max(0.0, 100.0 - strictness_raw * 500)

    def _score_competitor_density(self, messages: list[dict]) -> float:
        competitors = {
            "hamster kombat", "notcoin", "blum", "tapswap", "catizen",
            "yescoin", "pixelverse", "dogs", "major",
        }
        text_blob = " ".join((m.get("text") or "").lower() for m in messages)
        hits = sum(1 for c in competitors if c in text_blob)
        return min(100.0, hits * 20.0)

    def _score_to_grade(self, score: float) -> str:
        for threshold, grade in self._GRADE_THRESHOLDS:
            if score >= threshold:
                return grade
        return "C"

    def _extract_topics(self, messages: list[dict], group_info: dict) -> list[str]:
        topic_keywords = {
            "saas": ["saas", "swap", "liquidity", "yield"],
            "trading": ["trading", "chart", "bull", "bear", "long", "short"],
            "gaming": ["game", "play", "gaming", "p2e", "社区"],
            "announcement": ["announcement", "空投", "撸毛", "farming"],
            "nft": ["nft", "mint", "opensea", "collection"],
            "general": ["tech", "行业", "blockchain", "区块链"],
        }
        text_blob = f"{(group_info.get('title') or '').lower()} {(group_info.get('description') or '').lower()}"
        for msg in messages[:100]:
            text_blob += f" {(msg.get('text') or '').lower()}"
        topics = [topic for topic, kws in topic_keywords.items() if any(kw in text_blob for kw in kws)]
        return topics or ["general"]

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------

    async def _persist_group(self, group_info: dict, evaluation: dict) -> None:
        """Upsert a group record with evaluation results."""
        try:
            async with get_session() as session:
                tg_id = str(group_info["tg_group_id"])
                stmt = select(Group).where(Group.tg_group_id == tg_id)
                result = await session.execute(stmt)
                group = result.scalar_one_or_none()
                safety = evaluation.get("safety", {})

                channel_type = "megagroup" if evaluation.get("is_megagroup") else "channel"

                if group is None:
                    group = Group(
                        tg_group_id=tg_id,
                        title=group_info.get("title", ""),
                        username=group_info.get("username"),
                        member_count=evaluation.get("member_count", 0),
                        language=evaluation.get("language", "en"),
                        topics=evaluation.get("topics", []),
                        grade=evaluation["grade"],
                        score=evaluation["score"],
                        admin_strictness=safety.get("strictness", "medium"),
                        link_tolerance=safety.get("link_tolerance", "medium"),
                        active_kols=evaluation.get("kols", []),
                        competitor_presence=[],
                        notes=channel_type,
                        status="evaluated",
                        last_activity=datetime.utcnow(),
                    )
                    session.add(group)
                else:
                    group.grade = evaluation["grade"]
                    group.score = evaluation["score"]
                    group.member_count = evaluation.get("member_count", group.member_count)
                    group.topics = evaluation.get("topics", group.topics)
                    group.admin_strictness = safety.get("strictness", group.admin_strictness)
                    group.link_tolerance = safety.get("link_tolerance", group.link_tolerance)
                    group.active_kols = evaluation.get("kols", group.active_kols)
                    group.notes = channel_type
                    group.last_activity = datetime.utcnow()

                self._log.info(
                    "scout.group_persisted",
                    tg_group_id=tg_id,
                    grade=evaluation["grade"],
                    score=evaluation["score"],
                )
        except Exception:
            self._log.exception("scout.persist_group_failed", tg_group_id=group_info.get("tg_group_id"))

    async def _group_exists(self, tg_group_id: str) -> bool:
        try:
            async with get_session() as session:
                stmt = select(Group.id).where(Group.tg_group_id == tg_group_id)
                result = await session.execute(stmt)
                return result.scalar_one_or_none() is not None
        except Exception:
            return False

    async def _reevaluate_stale_groups(self) -> None:
        """Re-evaluate groups not checked in over 7 days."""
        try:
            cutoff = datetime.utcnow() - timedelta(days=7)
            async with get_session() as session:
                stmt = (
                    select(Group)
                    .where(Group.last_activity < cutoff)
                    .where(Group.status != "blacklisted")
                    .limit(10)
                )
                result = await session.execute(stmt)
                stale_groups = result.scalars().all()

            for group in stale_groups:
                if not self._running:
                    break
                evaluation = await self.evaluate_group(group.tg_group_id)
                if evaluation:
                    await self._persist_group(
                        {"tg_group_id": group.tg_group_id, "title": group.title, "username": group.username},
                        evaluation,
                    )
                await self._jittered_sleep(random.uniform(30, 60))
        except Exception:
            self._log.exception("scout.reevaluate_stale_failed")

    # ------------------------------------------------------------------
    # Scheduler-facing API
    # ------------------------------------------------------------------

    async def collect_intelligence(self) -> list[dict]:
        """Entry point for the CentralBrain scheduler.

        Runs one discovery + evaluation cycle and returns the list of
        newly discovered / evaluated group dicts.
        """
        # The scheduler invokes us directly without going through start(),
        # so self._running is False by default. discover_groups()'s
        # `if not self._running: break` would short-circuit the very first
        # iteration. Force-enable for the duration of this call.
        self._running = True
        keywords = self._pick_keywords()
        discovered = await self.discover_groups(keywords)

        # Phase 1 addition: seed-group link mining (highest ROI source)
        try:
            seed_candidates = await self.mine_links_from_seeds()
            discovered.extend(seed_candidates)
        except Exception:
            self._log.exception("scout.seed_mining_error")

        # Phase 2: discover linked discussion groups from readonly channels
        try:
            discussion_candidates = await self.discover_discussion_groups()
            discovered.extend(discussion_candidates)
        except Exception:
            self._log.exception("scout.discussion_error")

        # Phase 3: web-based discovery (lyzem.com)
        try:
            from src.agents.scout.web_discovery import discover_from_lyzem
            # Build set of existing usernames to skip
            async with get_session() as session:
                existing = await session.execute(select(Group.username).where(Group.username.isnot(None)))
                existing_usernames = {r[0].lower() for r in existing if r[0]}

            web_candidates = await discover_from_lyzem(existing_usernames, max_results=20)
            discovered.extend(web_candidates)
        except Exception:
            self._log.exception("scout.web_discovery_error")

        # ---- Enhanced discovery strategies (new) ----
        from src.agents.scout.discovery_strategies import (
            discover_via_forward_trace,
            discover_via_bio_links,
            discover_via_member_overlap,
            discover_via_web_sources,
            evolve_keywords,
        )

        # Phase 4: Forward source tracking (highest ROI new strategy)
        if settings.forward_trace_enabled:
            try:
                fwd_candidates = await discover_via_forward_trace(self.user_client)
                discovered.extend(fwd_candidates)
            except Exception:
                self._log.exception("scout.forward_trace_error")

        # Phase 5: Bio / pinned-message link mining
        if settings.bio_links_enabled:
            try:
                bio_candidates = await discover_via_bio_links(self.user_client)
                discovered.extend(bio_candidates)
            except Exception:
                self._log.exception("scout.bio_links_error")

        # Phase 6: Member overlap (common chats) — run less frequently (every 5th cycle)
        if settings.member_overlap_enabled:
            cycle_count = getattr(self, "_cycle_count", 0)
            self._cycle_count = cycle_count + 1
            if cycle_count % 5 == 0:
                try:
                    overlap_candidates = await discover_via_member_overlap(self.user_client)
                    discovered.extend(overlap_candidates)
                except Exception:
                    self._log.exception("scout.member_overlap_error")

        # Phase 7: Multi-source web discovery (complementary to lyzem)
        if settings.web_discovery_enabled:
            try:
                web_multi = await discover_via_web_sources(existing_usernames)
                discovered.extend(web_multi)
            except Exception:
                self._log.exception("scout.web_multi_error")

        # Phase 8: Adaptive keyword evolution (feed new keywords into next cycle)
        if settings.keyword_evolution_enabled:
            try:
                all_existing_kw = set()
                for tier in SEARCH_KEYWORDS_TIERED.values():
                    for lang_kws in tier.values():
                        all_existing_kw.update(kw.lower() for kw in lang_kws)

                new_kws = await evolve_keywords(self.user_client, all_existing_kw)
                if new_kws:
                    # Inject evolved keywords into the high-tier English pool
                    # so they're picked up by _pick_keywords() in next cycles
                    existing_high_en = SEARCH_KEYWORDS_TIERED["high"]["en"]
                    for kw in new_kws:
                        if kw not in existing_high_en:
                            existing_high_en.append(kw)
                    self._log.info("scout.keywords_evolved", new=len(new_kws), samples=new_kws[:3])
            except Exception:
                self._log.exception("scout.keyword_evolution_error")

        # Round-robin account selection for evaluation calls.
        # Use available (non-frozen, non-flooded) accounts first; fall back
        # to any authorized account so evaluation doesn't stall entirely.
        eval_accounts: list[int] = [
            aid for aid, w in self.user_client._clients.items()
            if w.is_available
        ]
        if not eval_accounts:
            eval_accounts = [
                aid for aid, w in self.user_client._clients.items()
                if w.is_authorized
            ]

        evaluated: list[dict] = []
        eval_idx = 0
        for group_info in discovered:
            group_id = group_info.get("tg_group_id", "")
            if not group_id:
                continue

            # Skip Chinese groups (user policy)
            title_lower = (group_info.get("title", "") or "").lower()
            if any(neg in title_lower for neg in NEGATIVE_KEYWORDS):
                self._log.debug("scout.skip_negative", tg_group_id=group_id, title=title_lower[:40])
                continue

            # Skip broadcast channels -- we can only post in megagroups
            info = await self.user_client.get_group_info_any(group_id)
            if info is None:
                self._log.debug("scout.skip_unresolvable", tg_group_id=group_id)
                continue  # cannot verify — skip, will retry next cycle
            if not info.get("is_megagroup", False):
                self._log.debug("scout.skip_channel", tg_group_id=group_id)
                continue

            # Pick account for this evaluation via round-robin
            eval_acct = eval_accounts[eval_idx % len(eval_accounts)] if eval_accounts else None
            eval_idx += 1

            evaluation = await self.evaluate_group(group_id, account_id=eval_acct)
            if evaluation:
                await self._persist_group(group_info, evaluation)
                evaluated.append({**group_info, **evaluation})
        return evaluated

    # ------------------------------------------------------------------
    # Keyword selection
    # ------------------------------------------------------------------

    def _pick_keywords(self, count: int = 5) -> list[str]:
        """Pick 6 keywords per cycle across multiple languages and tiers.

        Strategy:
            * 3 from ``high`` tier, 2 from ``medium``, 1 from ``low``
            * Each call rotates through ``LANGUAGE_ROTATION`` so two adjacent
              languages provide the pool on each cycle.
            * The ``count`` parameter is accepted for backward compatibility
              but is ignored -- the tiered distribution is fixed at 6 words.
            * Keywords already searched in recent cycles are skipped. If all
              keywords in a tier have been searched, the cache is cleared so
              they can be retried (results may have changed).
        """
        cursor = self._lang_cursor
        self._lang_cursor = (cursor + 1) % len(LANGUAGE_ROTATION)
        langs = [
            LANGUAGE_ROTATION[cursor % len(LANGUAGE_ROTATION)],
            LANGUAGE_ROTATION[(cursor + 1) % len(LANGUAGE_ROTATION)],
        ]

        def _pool(tier: str) -> list[str]:
            bag: list[str] = []
            for lang in langs:
                bag.extend(SEARCH_KEYWORDS_TIERED.get(tier, {}).get(lang, []))
            return bag

        def _pick_unsearched(pool: list[str], n: int) -> list[str]:
            """Pick up to *n* keywords from *pool* that haven't been searched recently."""
            unsearched = [kw for kw in pool if kw not in self._searched_keywords]
            if not unsearched:
                # All exhausted -- reset cache so we can retry
                self._searched_keywords.clear()
                unsearched = pool
            picked = random.sample(unsearched, min(n, len(unsearched)))
            self._searched_keywords.update(picked)
            return picked

        high_pool = _pool("high")
        medium_pool = _pool("medium")
        low_pool = _pool("low")

        pool: list[str] = []
        pool.extend(_pick_unsearched(high_pool, 3))
        pool.extend(_pick_unsearched(medium_pool, 2))
        pool.extend(_pick_unsearched(low_pool, 1))
        random.shuffle(pool)
        return pool

    # ------------------------------------------------------------------
    # Seed-group link mining (Phase 1)
    # ------------------------------------------------------------------

    async def mine_links_from_seeds(self) -> list[dict]:
        """Read seed groups from config/seed_groups.txt + high-grade DB groups,
        fetch recent messages in parallel, extract t.me links, return candidates.

        Recursive expansion: DB groups with grade S/A/B (status evaluated/active/
        infiltrating) are added as additional seeds so each cycle the frontier
        grows. Capped at ``MAX_MINING_SOURCES``. Per-cycle dedup via
        ``self._mined_recently`` (cleared at the start of each call).
        """
        # Reset per-cycle mined marker
        self._mined_recently = set()

        seeds: list[str] = []
        seen_seeds: set[str] = set()

        # Priority 1: DB groups graded S, then A, then B
        try:
            db_seeds = await self._load_db_seed_groups()
        except Exception:
            self._log.exception("scout.seed_db_load_failed")
            db_seeds = []
        for s in db_seeds:
            if s not in seen_seeds:
                seen_seeds.add(s)
                seeds.append(s)

        # Priority 2: config file seeds
        seed_file = Path("config/seed_groups.txt")
        if seed_file.exists():
            for line in seed_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line not in seen_seeds:
                    seen_seeds.add(line)
                    seeds.append(line)
        else:
            self._log.info("scout.seed_file_missing", path=str(seed_file))

        if not seeds:
            return []

        # Cap total sources per cycle
        if len(seeds) > self.MAX_MINING_SOURCES:
            seeds = seeds[: self.MAX_MINING_SOURCES]

        self._log.info(
            "scout.seed_mining_start",
            total_seeds=len(seeds),
            db_seeds=len(db_seeds),
            cap=self.MAX_MINING_SOURCES,
        )

        # Regex to extract t.me links
        tme_re = re.compile(
            r"(?:https?://)?t\.me/(?:joinchat/|\+)?([\w-]+)",
            re.IGNORECASE,
        )

        discovered: list[dict] = []
        seen_handles: set[str] = set()
        sem = asyncio.Semaphore(self.SEED_MINING_CONCURRENCY)
        lock = asyncio.Lock()

        async def _mine_one(seed: str) -> None:
            if seed in self._mined_recently:
                return
            self._mined_recently.add(seed)
            async with sem:
                try:
                    msgs = await self.user_client.get_recent_messages(seed, limit=200)
                except Exception:
                    self._log.warning("scout.seed_fetch_failed", seed=seed)
                    return

                local_candidates: list[dict] = []
                for msg in msgs:
                    text = msg.get("text", "") or ""
                    for m in tme_re.finditer(text):
                        handle = m.group(1)
                        if not handle or len(handle) < 4:
                            continue
                        if handle.lower() in ("share", "joinchat", "addstickers", "iv"):
                            continue
                        async with lock:
                            if handle in seen_handles:
                                continue
                            seen_handles.add(handle)

                        tg_id = "@" + handle
                        if await self._group_exists(tg_id):
                            continue

                        if any(neg in handle.lower() for neg in NEGATIVE_KEYWORDS):
                            continue

                        local_candidates.append({
                            "tg_group_id": tg_id,
                            "title": handle,
                            "username": handle,
                            "member_count": 0,
                            "source": "seed_mining",
                        })

                async with lock:
                    discovered.extend(local_candidates)

                await asyncio.sleep(random.uniform(2, 5))

        await asyncio.gather(*[_mine_one(s) for s in seeds], return_exceptions=True)

        self._log.info(
            "scout.seed_mining_done",
            seeds=len(seeds),
            candidates=len(discovered),
        )
        return discovered

    # ------------------------------------------------------------------
    # Discussion group discovery (Phase 2)
    # ------------------------------------------------------------------

    async def discover_discussion_groups(self) -> list[dict]:
        """Scan readonly channels in DB for linked discussion groups (megagroups).

        This is the highest-ROI discovery path: 659 channels -> potentially
        100+ linked discussion megagroups where we can actually post.
        """
        discovered = []

        try:
            async with get_session() as session:
                # Get readonly channels that we haven't checked yet
                stmt = (
                    select(Group)
                    .where(Group.status == "readonly")
                    .where(~Group.notes.contains("[discussion-checked]"))
                    .order_by(Group.member_count.desc())  # prioritize big channels
                    .limit(20)  # process 20 per cycle
                )
                result = await session.execute(stmt)
                channels = list(result.scalars().all())
        except Exception:
            self._log.exception("scout.discussion.db_error")
            return []

        self._log.info("scout.discussion.start", channels=len(channels))

        for channel in channels:
            try:
                linked = await self.user_client.get_linked_discussion_group(channel.tg_group_id)

                # Mark channel as checked (whether or not it has a discussion group)
                try:
                    async with get_session() as session:
                        stmt = select(Group).where(Group.id == channel.id)
                        result = await session.execute(stmt)
                        g = result.scalar_one_or_none()
                        if g:
                            g.notes = (g.notes or '') + ' [discussion-checked]'
                            await session.commit()
                except Exception:
                    pass

                if linked is None:
                    continue

                if not linked.get("is_megagroup", False):
                    continue

                tg_id = linked.get("username") or linked["id"]
                if isinstance(tg_id, str) and not tg_id.startswith("@") and not tg_id.isdigit():
                    tg_id = f"@{tg_id}"

                if await self._group_exists(tg_id):
                    continue

                discovered.append({
                    "tg_group_id": tg_id,
                    "title": linked.get("title", ""),
                    "username": linked.get("username"),
                    "member_count": linked.get("member_count", 0),
                    "source": "discussion_group",
                    "parent_channel": channel.tg_group_id,
                })

                self._log.info(
                    "scout.discussion.found",
                    channel=channel.tg_group_id,
                    discussion=tg_id,
                    members=linked.get("member_count", 0),
                )

            except Exception:
                self._log.debug("scout.discussion.check_failed", channel=channel.tg_group_id)

            await asyncio.sleep(random.uniform(2, 5))

        self._log.info("scout.discussion.done", found=len(discovered))
        return discovered

    async def _load_db_seed_groups(self) -> list[str]:
        """Return tg_group_id list of high-grade DB groups to use as recursive seeds.

        Filters: grade in (S, A, B); status in (evaluated, active, infiltrating);
        excludes blacklisted. Ordered S > A > B.

        Also includes groups where we actually have accounts joined -- these
        are the freshest link sources since we can read their messages.
        """
        grade_order = {"S": 0, "A": 1, "B": 2}
        seen: set[str] = set()
        ordered: list[str] = []

        async with get_session() as session:
            # Source 1: high-grade evaluated groups
            stmt = (
                select(Group.tg_group_id, Group.grade)
                .where(Group.grade.in_(["S", "A", "B"]))
                .where(Group.status.in_(["evaluated", "active", "infiltrating"]))
            )
            result = await session.execute(stmt)
            rows = result.all()

            rows_sorted = sorted(rows, key=lambda r: grade_order.get(r[1], 99))
            for r in rows_sorted:
                gid = str(r[0]) if r[0] else ""
                if gid and gid not in seen:
                    seen.add(gid)
                    ordered.append(gid)

            # Source 2: groups where we actually have accounts (freshest links)
            stmt2 = (
                select(Group.tg_group_id)
                .join(GroupAccount, Group.id == GroupAccount.group_id)
                .where(Group.status != "readonly")
                .order_by(Group.member_count.desc())
                .limit(15)
            )
            result2 = await session.execute(stmt2)
            for r in result2:
                gid = str(r[0]) if r[0] else ""
                if gid and gid not in seen:
                    seen.add(gid)
                    ordered.append(gid)

        return ordered
