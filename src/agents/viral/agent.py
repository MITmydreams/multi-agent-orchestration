"""Layer 5 -- Viral Engine Agent: event-driven viral propagation.

Monitors game events, matches them against trigger rules, and orchestrates
cross-channel promotional bursts.  This is a pure backend system that does
not require its own Telegram account -- it coordinates other agents.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import select, func

from src.agents.base import BaseAgent
from src.ai.content_gen import ContentGenerator
from src.brain.circuit_breaker import CircuitBreaker
from src.brain.risk_engine import RiskEngine
from src.config import settings
from src.models import Account, ContentPiece, DailyMetrics, Group, get_session
from src.tg_clients.user_client import UserClientManager

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Trigger definitions
# ---------------------------------------------------------------------------

class _Trigger:
    """A viral trigger rule."""

    def __init__(
        self,
        name: str,
        description: str,
        condition: str,
        action: str,
        priority: int = 1,
    ) -> None:
        self.name = name
        self.description = description
        self.condition = condition
        self.action = action
        self.priority = priority


_TRIGGERS: dict[str, _Trigger] = {
    "big_win": _Trigger(
        name="big_win",
        description="Single win > 100U",
        condition="prize_amount > 100",
        action="celebrate_all_channels",
        priority=1,
    ),
    "stage5_alert": _Trigger(
        name="stage5_alert",
        description="Any room enters Stage 5",
        condition="current_stage == 5",
        action="urgent_push",
        priority=1,
    ),
    "milestone": _Trigger(
        name="milestone",
        description="User count reaches round thousands/tens of thousands",
        condition="total_users % 1000 == 0",
        action="milestone_celebration",
        priority=2,
    ),
    "dividend_peak": _Trigger(
        name="dividend_peak",
        description="Dividend amount hits new high",
        condition="dividend > previous_max_dividend",
        action="data_post",
        priority=2,
    ),
    "whale_activity": _Trigger(
        name="whale_activity",
        description="Premium room single click > 50U",
        condition="click_price > 50 and room_type == 'premium'",
        action="whale_alert",
        priority=3,
    ),
    "dramatic_ending": _Trigger(
        name="dramatic_ending",
        description=">=3 clicks in the last 10 seconds of a round",
        condition="last_10s_clicks >= 3",
        action="dramatic_story",
        priority=2,
    ),
}


class ViralEngineAgent(BaseAgent):
    """Event-driven viral propagation engine.

    Six triggers:
    - big_win:          Single win > 100U -> celebrate across all channels
    - stage5_alert:     Any room enters Stage 5 -> urgent push
    - milestone:        User count hits round number -> milestone celebration
    - dividend_peak:    Dividend amount new high -> data post
    - whale_activity:   Premium room single click > 50U -> whale alert
    - dramatic_ending:  >=3 clicks in last 10s -> dramatic story

    The viral engine does NOT send messages directly. It generates content
    and delegates distribution to the ContentSeederAgent or directly via
    the user_client.
    """

    name: str = "viral_engine"
    agent_type: str = "viral"

    def __init__(
        self,
        user_client: UserClientManager,
        risk_engine: RiskEngine,
        circuit_breaker: CircuitBreaker,
        content_gen: ContentGenerator,
    ) -> None:
        super().__init__(user_client, risk_engine, circuit_breaker)
        self.content_gen = content_gen
        self._previous_max_dividend: float = 0.0
        self._known_milestones: set[int] = set()

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Main loop: poll game events -> match triggers -> execute actions."""
        self._log.info("viral_engine.run.start")
        while self._running:
            if not await self.should_proceed():
                await self._jittered_sleep(60)
                continue

            try:
                events = await self._poll_game_events()
                for event in events:
                    if not self._running:
                        break

                    triggered = await self.check_triggers(event)
                    for trigger_name in triggered:
                        await self.execute_trigger(trigger_name, event)
                        await self.log_activity("viral_trigger", {
                            "trigger": trigger_name,
                            "event_type": event.get("type"),
                        })

                    await self._jittered_sleep(random.uniform(2, 5))

            except Exception:
                self._log.exception("viral_engine.run.cycle_error")

            # Poll interval (30-60 seconds for near-real-time)
            await self._jittered_sleep(random.uniform(30, 60))

    # ------------------------------------------------------------------
    # Trigger matching
    # ------------------------------------------------------------------

    async def check_triggers(self, event: dict) -> list[str]:
        """Check if an event matches any defined trigger.

        Returns a list of matched trigger names, sorted by priority.
        """
        matched: list[tuple[int, str]] = []
        event_type = event.get("type", "")
        data = event.get("data", {})

        # big_win: single win > 100U
        if event_type in ("big_win", "round_end"):
            prize = data.get("prize_amount", 0)
            if prize > 100:
                matched.append((_TRIGGERS["big_win"].priority, "big_win"))

        # stage5_alert: any room enters stage 5
        if event_type == "stage_change":
            if data.get("current_stage") == 5:
                matched.append((_TRIGGERS["stage5_alert"].priority, "stage5_alert"))

        # milestone: user count round number
        if event_type in ("user_registered", "stats_update"):
            total_users = data.get("total_users", 0)
            if total_users > 0 and total_users % 1000 == 0:
                if total_users not in self._known_milestones:
                    self._known_milestones.add(total_users)
                    matched.append((_TRIGGERS["milestone"].priority, "milestone"))

        # dividend_peak: new dividend high
        if event_type in ("dividend_distributed", "round_end"):
            dividend = data.get("dividend_amount", 0)
            if dividend > self._previous_max_dividend:
                self._previous_max_dividend = dividend
                matched.append((_TRIGGERS["dividend_peak"].priority, "dividend_peak"))

        # whale_activity: premium room click > 50U
        if event_type == "click":
            price = data.get("click_price", 0)
            room = data.get("room_type", "")
            if price > 50 and room == "premium":
                matched.append((_TRIGGERS["whale_activity"].priority, "whale_activity"))

        # dramatic_ending: >=3 clicks in last 10s
        if event_type == "round_end":
            last_10s = data.get("last_10s_clicks", 0)
            if last_10s >= 3:
                matched.append((_TRIGGERS["dramatic_ending"].priority, "dramatic_ending"))

        # Sort by priority (lower number = higher priority)
        matched.sort(key=lambda x: x[0])
        return [name for _, name in matched]

    # ------------------------------------------------------------------
    # Trigger execution
    # ------------------------------------------------------------------

    async def execute_trigger(self, trigger_name: str, event_data: dict) -> None:
        """Execute the promotional action associated with a trigger."""
        self._log.info(
            "viral_engine.trigger_fired",
            trigger=trigger_name,
            event_type=event_data.get("type"),
        )

        data = event_data.get("data", {})
        handler = getattr(self, f"_action_{trigger_name}", None)
        if handler:
            await handler(data)
        else:
            self._log.warning("viral_engine.unknown_trigger", trigger=trigger_name)

    async def _action_big_win(self, data: dict) -> None:
        """Celebrate a big win across all channels."""
        content = await self.content_gen.generate_viral_content(
            trigger="big_win",
            data={
                "winner": data.get("winner_name", "Anonymous"),
                "prize": data.get("prize_amount", 0),
                "room": data.get("room_type", "standard"),
            },
        )
        if content:
            await self._broadcast(content, priority="high")

    async def _action_stage5_alert(self, data: dict) -> None:
        """Send urgent Stage 5 alert to relevant groups."""
        room_type = data.get("room_type", "standard")
        countdown = data.get("countdown", 3)
        content = await self.content_gen.generate_viral_content(
            trigger="stage5_alert",
            data={"room_type": room_type, "countdown": countdown},
        )
        if content:
            await self._broadcast(content, priority="urgent")

    async def _action_milestone(self, data: dict) -> None:
        """Celebrate a user count milestone."""
        total_users = data.get("total_users", 0)
        content = await self.content_gen.generate_viral_content(
            trigger="milestone",
            data={"total_users": total_users},
        )
        if content:
            await self._broadcast(content, priority="medium")

    async def _action_dividend_peak(self, data: dict) -> None:
        """Post data-driven dividend update."""
        content = await self.content_gen.generate_viral_content(
            trigger="dividend_peak",
            data={
                "dividend_amount": data.get("dividend_amount", 0),
                "room_type": data.get("room_type", "standard"),
                "total_clicks": data.get("total_clicks", 0),
            },
        )
        if content:
            await self._broadcast(content, priority="medium")

    async def _action_whale_activity(self, data: dict) -> None:
        """Broadcast a whale alert."""
        content = await self.content_gen.generate_viral_content(
            trigger="whale_activity",
            data={
                "click_price": data.get("click_price", 0),
                "room_type": data.get("room_type", "premium"),
            },
        )
        if content:
            await self._broadcast(content, priority="medium")

    async def _action_dramatic_ending(self, data: dict) -> None:
        """Tell the story of a dramatic round ending."""
        content = await self.content_gen.generate_viral_content(
            trigger="dramatic_ending",
            data={
                "last_10s_clicks": data.get("last_10s_clicks", 0),
                "final_winner": data.get("final_winner", "Anonymous"),
                "room_type": data.get("room_type", "standard"),
                "total_clicks": data.get("total_clicks", 0),
            },
        )
        if content:
            await self._broadcast(content, priority="high")

    # ------------------------------------------------------------------
    # Scheduler-facing API
    # ------------------------------------------------------------------

    async def check_game_events(self) -> list[dict]:
        """Entry point for the CentralBrain scheduler -- poll and return events."""
        return await self._poll_game_events()

    async def handle_events(self, events: list[dict]) -> list[dict]:
        """Process a batch of game events and return action results.

        Called by the CentralBrain scheduler after ``check_game_events``.
        """
        results: list[dict] = []
        for event in events:
            triggered = await self.check_triggers(event)
            actions_taken: list[str] = []
            for trigger_name in triggered:
                await self.execute_trigger(trigger_name, event)
                actions_taken.append(trigger_name)
            results.append({
                "event_type": event.get("type"),
                "actions_taken": actions_taken,
            })
        return results

    # ------------------------------------------------------------------
    # Referral tracking & viral coefficient
    # ------------------------------------------------------------------

    async def track_referral_chain(self, user_id: str) -> dict:
        """Track the referral chain originating from a user.

        Returns a dict with chain depth, total referrals, and conversion rates.
        """
        try:
            # Fetch referral data from game API
            chain_data = await self.user_client.get_referral_chain(user_id)
            if not chain_data:
                return {
                    "user_id": user_id,
                    "depth": 0,
                    "total_referrals": 0,
                    "l1_count": 0,
                    "l2_count": 0,
                    "l3_count": 0,
                    "conversion_rate": 0.0,
                }

            l1 = chain_data.get("l1_referrals", [])
            l2 = chain_data.get("l2_referrals", [])
            l3 = chain_data.get("l3_referrals", [])
            total = len(l1) + len(l2) + len(l3)

            # Conversion rate: referrals who actually played at least one round
            active_referrals = chain_data.get("active_referrals", 0)
            conversion_rate = active_referrals / total if total > 0 else 0.0

            depth = 0
            if l3:
                depth = 3
            elif l2:
                depth = 2
            elif l1:
                depth = 1

            result = {
                "user_id": user_id,
                "depth": depth,
                "total_referrals": total,
                "l1_count": len(l1),
                "l2_count": len(l2),
                "l3_count": len(l3),
                "conversion_rate": round(conversion_rate, 4),
            }

            self._log.info("viral_engine.referral_chain", **result)
            return result

        except Exception:
            self._log.exception("viral_engine.track_referral.error", user_id=user_id)
            return {"user_id": user_id, "depth": 0, "total_referrals": 0}

    async def get_viral_coefficient(self, days: int = 7) -> float:
        """Calculate the viral coefficient K over the specified period.

        K = (invites sent per user) * (conversion rate)

        A K > 1 means exponential organic growth.
        """
        try:
            cutoff = datetime.utcnow() - timedelta(days=days)

            # Fetch metrics from the database
            async with get_session() as session:
                stmt = (
                    select(DailyMetrics)
                    .where(DailyMetrics.date >= cutoff.date())
                    .order_by(DailyMetrics.date.desc())
                )
                result = await session.execute(stmt)
                metrics = result.scalars().all()

            if not metrics:
                return 0.0

            total_registrations = sum(m.new_registrations for m in metrics)
            total_from_referrals = sum(
                m.registrations_from_infiltration + m.registrations_from_bot
                for m in metrics
            )

            if total_registrations == 0:
                return 0.0

            # Approximate: each existing user sends ~N invites, conversion rate = referral_regs / total_regs
            # K = avg_invites_per_user * conversion_rate
            # Simplified: K = total_from_referrals / (total_registrations - total_from_referrals)
            organic = total_registrations - total_from_referrals
            if organic <= 0:
                organic = 1  # avoid division by zero

            k = total_from_referrals / organic
            k = round(k, 4)

            self._log.info(
                "viral_engine.viral_coefficient",
                k=k,
                days=days,
                total_registrations=total_registrations,
                referral_registrations=total_from_referrals,
            )
            return k

        except Exception:
            self._log.exception("viral_engine.viral_coefficient.error")
            return 0.0

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _poll_game_events(self) -> list[dict]:
        """Poll the game API/WebSocket for recent events."""
        try:
            events = await self.user_client.get_game_events()
            return events if isinstance(events, list) else []
        except Exception:
            self._log.debug("viral_engine.poll_events.no_data")
            return []

    async def _broadcast(self, content: str, *, priority: str = "medium") -> None:
        """Broadcast content to target groups based on priority.

        Priority levels:
        - urgent: all S + A grade groups, immediately
        - high:   all S + A grade groups
        - medium: S grade groups only
        """
        if not content:
            return

        try:
            async with get_session() as session:
                if priority in ("urgent", "high"):
                    grades = ["S", "A"]
                else:
                    grades = ["S"]

                stmt = (
                    select(Group)
                    .where(Group.status.in_(["active", "infiltrating"]))
                    .where(Group.grade.in_(grades))
                    .limit(30)
                )
                result = await session.execute(stmt)
                groups = result.scalars().all()

            if not groups:
                self._log.debug("viral_engine.broadcast.no_targets")
                return

            # Pick a content account for posting
            account_id = await self._pick_account()
            if account_id is None:
                self._log.warning("viral_engine.broadcast.no_account")
                return

            for group in groups:
                if not self._running:
                    break
                try:
                    await self.user_client.send_message(
                        account_id, group.tg_group_id, content,
                    )
                    self._log.info(
                        "viral_engine.broadcast.sent",
                        group_id=group.tg_group_id,
                        priority=priority,
                    )
                except Exception:
                    self._log.warning(
                        "viral_engine.broadcast.send_failed",
                        group_id=group.tg_group_id,
                    )

                # Delay between group posts to avoid flood detection
                delay = 5 if priority == "urgent" else random.uniform(15, 45)
                await self._jittered_sleep(delay)

        except Exception:
            self._log.exception("viral_engine.broadcast.error")

    async def _pick_account(self) -> int | None:
        """Pick an active content-role account for viral broadcasts."""
        try:
            async with get_session() as session:
                stmt = (
                    select(Account)
                    .where(Account.role.in_(["content", "infiltrator"]))
                    .where(Account.status == "active")
                )
                result = await session.execute(stmt)
                accounts = result.scalars().all()
                if not accounts:
                    return None
                return random.choice(accounts).id
        except Exception:
            return None
