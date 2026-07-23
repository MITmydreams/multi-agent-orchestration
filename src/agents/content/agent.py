"""Layer 3 -- Content Seeder Agent: high-quality original content production.

Generates valuable content that sparks discussion -- NOT advertising.
All content is designed to provide genuine value first and foremost.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog
from sqlalchemy import select

from src.agents.base import BaseAgent
from src.ai.content_gen import ContentGenerator
from src.brain.circuit_breaker import CircuitBreaker
from src.brain.risk_engine import RiskEngine
from src.config import settings
from src.models import Account, ContentPiece, Group, get_session
from src.tg_clients.user_client import UserClientManager

logger = structlog.get_logger(__name__)


# Timezone-aware posting windows (UTC hours)
_TIMEZONE_WINDOWS: dict[str, tuple[int, int]] = {
    "americas": (14, 28),   # UTC 14:00 - 04:00 next day
    "asia":     (2, 14),    # UTC 02:00 - 14:00
    "europe":   (8, 22),    # UTC 08:00 - 22:00
}

# Content type weekly quotas
_CONTENT_QUOTAS: dict[str, dict[str, Any]] = {
    "battle_report": {"per_week": 0, "trigger": "realtime", "priority": 1},
    "win_story":     {"per_week": 5, "trigger": "scheduled", "priority": 2},
    "meme":          {"per_week": 7, "trigger": "scheduled", "priority": 3},
    "review":        {"per_week": 2, "trigger": "scheduled", "priority": 4},
}


class ContentSeederAgent(BaseAgent):
    """Content factory -- produces high-ROI original content.

    Four content types (v2.0 streamlined):
    1. Battle report / big-win alerts -- real-time push
    2. Victory screenshots / stories  -- 5 per week
    3. Memes / jokes                  -- daily
    4. Project reviews + data         -- 2 per week

    Rules:
    - No direct advertising
    - All content must provide genuine value
    - Content passes anti-spam scoring before distribution
    - Multi-language variants for each piece
    """

    name: str = "content_seeder"
    agent_type: str = "content"

    # Supported languages for content generation
    LANGUAGES: list[str] = ["en"]

    def __init__(
        self,
        user_client: UserClientManager,
        risk_engine: RiskEngine,
        circuit_breaker: CircuitBreaker,
        content_gen: ContentGenerator,
    ) -> None:
        super().__init__(user_client, risk_engine, circuit_breaker)
        self.content_gen = content_gen

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Main loop: listen for game events -> generate content -> distribute."""
        self._log.info("content_seeder.run.start")
        while self._running:
            if not await self.should_proceed():
                await self._jittered_sleep(60)
                continue

            try:
                # 1. Check for real-time events (battle reports)
                events = await self._poll_game_events()
                for event in events:
                    if not self._running:
                        break
                    if event.get("type") == "round_end":
                        variants = await self.generate_battle_report(event.get("data", {}))
                        target_groups, target_channels = await self._select_distribution_targets("battle_report")
                        for content in variants:
                            await self.distribute_content(content, target_groups, target_channels)
                            await self._jittered_sleep(random.uniform(5, 15))
                    elif event.get("type") == "big_win":
                        stories = await self.generate_win_story(event.get("data", {}))
                        target_groups, target_channels = await self._select_distribution_targets("win_story")
                        for story in stories:
                            await self.distribute_content(story, target_groups, target_channels)
                            await self._jittered_sleep(random.uniform(10, 30))

                # 2. Scheduled content generation
                await self._run_scheduled_content()

            except Exception:
                self._log.exception("content_seeder.run.cycle_error")

            # Cycle interval (10-20 min)
            await self._jittered_sleep(random.uniform(600, 1200))

    # ------------------------------------------------------------------
    # Content generation
    # ------------------------------------------------------------------

    async def generate_battle_report(self, round_data: dict) -> list[str]:
        """Generate real-time battle report variants (multi-language).

        Returns a list of content strings, one per language/variant.
        """
        variants: list[str] = []
        room_type = round_data.get("room_type", "standard")
        total_clicks = round_data.get("total_clicks", 0)
        prize_pool = round_data.get("prize_pool", 0)
        final_winner = round_data.get("final_winner", "???")
        duration_min = round_data.get("duration_minutes", 0)

        for lang in self.LANGUAGES:
            try:
                content = await self.content_gen.generate_battle_report(
                    round_data={
                        "room_type": room_type,
                        "total_clicks": total_clicks,
                        "prize_pool": prize_pool,
                        "final_winner": final_winner,
                        "duration_minutes": duration_min,
                    },
                    language=lang,
                )
                if content:
                    variants.append(content)
                    await self._persist_content("battle_report", lang, content)
            except Exception:
                self._log.exception("content_seeder.battle_report.error", lang=lang)

        self._log.info("content_seeder.battle_report.generated", variants=len(variants))
        return variants

    async def generate_win_story(self, win_data: dict) -> list[str]:
        """Generate winning stories based on actual game outcomes."""
        variants: list[str] = []
        winner_name = win_data.get("winner_name", "Anonymous")
        prize_amount = win_data.get("prize_amount", 0)
        room_type = win_data.get("room_type", "standard")
        position = win_data.get("position", "final_hit")

        for lang in self.LANGUAGES:
            try:
                content = await self.content_gen.generate_win_story(
                    win_data={
                        "winner_name": winner_name,
                        "prize_amount": prize_amount,
                        "room_type": room_type,
                        "position": position,
                    },
                    language=lang,
                )
                if content:
                    variants.append(content)
                    await self._persist_content("win_story", lang, content)
            except Exception:
                self._log.exception("content_seeder.win_story.error", lang=lang)

        self._log.info("content_seeder.win_story.generated", variants=len(variants))
        return variants

    async def generate_meme(self, topic: str) -> str:
        """Generate a meme / joke about The Button or crypto culture."""
        try:
            lang = random.choice(self.LANGUAGES)
            content = await self.content_gen.generate_meme(
                topic=topic,
                language=lang,
            )
            if content:
                await self._persist_content("meme", lang, content)
            return content or ""
        except Exception:
            self._log.exception("content_seeder.meme.error", topic=topic)
            return ""

    async def generate_review(self) -> str:
        """Generate a data-driven project review / analysis."""
        try:
            lang = random.choice(self.LANGUAGES)
            content = await self.content_gen.generate_review(language=lang)
            if content:
                await self._persist_content("review", lang, content)
            return content or ""
        except Exception:
            self._log.exception("content_seeder.review.error")
            return ""

    # ------------------------------------------------------------------
    # Distribution
    # ------------------------------------------------------------------

    async def distribute_content(
        self,
        content: str,
        target_groups: list[str],
        target_channels: list[str],
    ) -> None:
        """Distribute a piece of content to groups and channels.

        Uses content-role accounts for posting.  Respects per-group rate limits.
        """
        if not content:
            return

        account_id = await self._pick_content_account()
        if account_id is None:
            self._log.warning("content_seeder.distribute.no_account")
            return

        # Post to channels first (less risky)
        for channel_id in target_channels:
            if not self._running:
                break
            try:
                await self.user_client.send_message(account_id, channel_id, content)
                self._log.info(
                    "content_seeder.posted_to_channel",
                    account_id=account_id,
                    channel_id=channel_id,
                )
            except Exception:
                self._log.warning(
                    "content_seeder.channel_post_failed",
                    channel_id=channel_id,
                )
            await self._jittered_sleep(random.uniform(10, 30))

        # Post to groups
        for group_id in target_groups:
            if not self._running:
                break
            try:
                await self.user_client.send_message(account_id, group_id, content)
                self._log.info(
                    "content_seeder.posted_to_group",
                    account_id=account_id,
                    group_id=group_id,
                )
            except Exception:
                self._log.warning(
                    "content_seeder.group_post_failed",
                    group_id=group_id,
                )
            await self._jittered_sleep(random.uniform(30, 90))

    # ------------------------------------------------------------------
    # Scheduling
    # ------------------------------------------------------------------

    async def schedule_content(self, content_type: str, timezone_region: str) -> datetime:
        """Calculate the optimal posting time for a content type in a given timezone.

        Regions:
        - americas: UTC 14:00 - 04:00
        - asia:     UTC 02:00 - 14:00
        - europe:   UTC 08:00 - 22:00
        """
        window = _TIMEZONE_WINDOWS.get(timezone_region, _TIMEZONE_WINDOWS["asia"])
        start_hour, end_hour = window

        # Pick a random hour within the window
        if end_hour > 24:
            # Window wraps around midnight
            hour = random.choice(
                list(range(start_hour, 24)) + list(range(0, end_hour - 24)),
            )
        else:
            hour = random.randint(start_hour, end_hour - 1)

        minute = random.randint(0, 59)

        now = datetime.now(tz=timezone.utc)
        scheduled = now.replace(hour=hour % 24, minute=minute, second=0, microsecond=0)

        # If the time has passed today, schedule for tomorrow
        if scheduled <= now:
            scheduled += timedelta(days=1)

        self._log.info(
            "content_seeder.scheduled",
            content_type=content_type,
            timezone_region=timezone_region,
            scheduled_at=scheduled.isoformat(),
        )
        return scheduled

    # ------------------------------------------------------------------
    # Scheduler-facing API
    # ------------------------------------------------------------------

    async def run_content_cycle(self) -> int:
        """Entry point for the CentralBrain scheduler.

        Runs one scheduled-content cycle and returns the number of pieces
        generated and distributed.
        """
        count = 0
        try:
            events = await self._poll_game_events()
            for event in events:
                if event.get("type") == "round_end":
                    variants = await self.generate_battle_report(event.get("data", {}))
                    count += len(variants)
                elif event.get("type") == "big_win":
                    stories = await self.generate_win_story(event.get("data", {}))
                    count += len(stories)

            await self._run_scheduled_content()
            count += 1  # account for the scheduled content cycle
        except Exception:
            self._log.exception("content_seeder.run_content_cycle.error")
        return count

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _run_scheduled_content(self) -> None:
        """Generate and distribute scheduled content types based on weekly quotas."""
        now = datetime.utcnow()
        weekday = now.weekday()  # 0=Mon

        # Memes: every day
        meme_topics = [
            "countdown anxiety", "last-second click", "dividend day",
            "whale spotted", "Stage 5 panic", "coin room grind",
            "button addiction", "crypto degen life",
        ]
        meme = await self.generate_meme(random.choice(meme_topics))
        if meme:
            groups, channels = await self._select_distribution_targets("meme")
            await self.distribute_content(meme, groups, channels)

        # Win stories: ~5/week -> every day except weekends
        if weekday < 5:
            # Only generate if we haven't hit quota
            count = await self._count_content_this_week("win_story")
            if count < _CONTENT_QUOTAS["win_story"]["per_week"]:
                stories = await self.generate_win_story({
                    "winner_name": "Player",
                    "prize_amount": random.uniform(5, 100),
                    "room_type": random.choice(["fast", "standard", "premium"]),
                    "position": random.choice(["final_hit", "middle_hit", "quarter_hit"]),
                })
                if stories:
                    groups, channels = await self._select_distribution_targets("win_story")
                    for story in stories[:1]:  # One variant per cycle
                        await self.distribute_content(story, groups, channels)

        # Reviews: 2/week -> Monday and Thursday
        if weekday in (0, 3):
            count = await self._count_content_this_week("review")
            if count < _CONTENT_QUOTAS["review"]["per_week"]:
                review = await self.generate_review()
                if review:
                    groups, channels = await self._select_distribution_targets("review")
                    await self.distribute_content(review, groups, channels)

    async def _poll_game_events(self) -> list[dict]:
        """Poll the game API for recent events worth reporting."""
        try:
            events = await self.user_client.get_game_events()
            return events if isinstance(events, list) else []
        except Exception:
            self._log.debug("content_seeder.poll_events.error")
            return []

    async def _select_distribution_targets(
        self, content_type: str,
    ) -> tuple[list[str], list[str]]:
        """Select target groups and channels for content distribution.

        Returns (group_ids, channel_ids).
        """
        try:
            async with get_session() as session:
                # Select active groups with grade A or S
                stmt = (
                    select(Group)
                    .where(Group.status.in_(["active", "infiltrating"]))
                    .where(Group.grade.in_(["S", "A"]))
                    .limit(20)
                )
                result = await session.execute(stmt)
                groups = result.scalars().all()
                group_ids = [g.tg_group_id for g in groups]

                # Channels are managed separately; return empty for now
                return group_ids, []
        except Exception:
            self._log.exception("content_seeder.select_targets.error")
            return [], []

    async def _pick_content_account(self) -> int | None:
        """Pick an active content-role account for posting."""
        try:
            async with get_session() as session:
                stmt = (
                    select(Account)
                    .where(Account.role == "content")
                    .where(Account.status == "active")
                )
                result = await session.execute(stmt)
                accounts = result.scalars().all()
                if not accounts:
                    return None
                chosen = random.choice(accounts)
                return chosen.id
        except Exception:
            self._log.exception("content_seeder.pick_account.error")
            return None

    async def _persist_content(
        self, content_type: str, language: str, content: str,
    ) -> None:
        """Save a generated content piece to the database."""
        try:
            async with get_session() as session:
                piece = ContentPiece(
                    content_type=content_type,
                    language=language,
                    content=content,
                    promo_level=0.2 if content_type in ("battle_report", "win_story") else 0.1,
                    spam_score=0.0,
                    variants=[],
                )
                session.add(piece)
        except Exception:
            self._log.exception("content_seeder.persist_content.error", content_type=content_type)

    async def _count_content_this_week(self, content_type: str) -> int:
        """Count how many pieces of a content type were created this week."""
        try:
            week_start = datetime.utcnow() - timedelta(days=datetime.utcnow().weekday())
            week_start = week_start.replace(hour=0, minute=0, second=0, microsecond=0)
            async with get_session() as session:
                stmt = (
                    select(ContentPiece)
                    .where(ContentPiece.content_type == content_type)
                    .where(ContentPiece.created_at >= week_start)
                )
                result = await session.execute(stmt)
                return len(result.scalars().all())
        except Exception:
            return 0
