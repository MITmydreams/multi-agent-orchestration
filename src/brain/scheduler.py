"""Central Brain Scheduler - orchestrates all agent layers on a periodic loop.

The ``CentralBrain`` is the top-level coordinator.  It runs a scheduling loop
(default: every 60 seconds) that:

1. Collects intelligence from the Scout agent.
2. Evaluates risk across all active accounts.
3. Checks global circuit-breaker state.
4. Reacts to game events via the Viral Engine.
5. Assigns routine infiltration / content tasks.
6. Collects metrics via Analytics.

A separate daily-maintenance cycle handles counter resets, report generation,
and account replenishment.
"""

from __future__ import annotations

import asyncio
import random
from datetime import datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import delete, func, select, text

from src.config import settings
from src.brain.age_policy import AgePolicy
from src.brain.risk_engine import RiskEngine, RiskLevel
from src.brain.circuit_breaker import CircuitBreaker, SystemState
from src.brain.analytics import Analytics
from src.agents.scout.agent import ScoutAgent
from src.agents.infiltrator.agent import InfiltratorAgent
from src.agents.content.agent import ContentSeederAgent
from src.agents.viral.agent import ViralEngineAgent
from src.tg_clients.user_client import UserClientManager
from src.models.base import get_session
from src.models.account import Account
from src.models.group import Group, GroupAccount
from src.models.message import MessageLog
from src.models.task import AgentTask

logger = structlog.get_logger(__name__)


class CentralBrain:
    """AI Central Scheduling Brain.

    Coordinates the Scout, Infiltrator, Content-Seeder, and Viral-Engine
    agents through a periodic main loop, gated by the RiskEngine and
    CircuitBreaker.
    """

    def __init__(
        self,
        risk_engine: RiskEngine,
        circuit_breaker: CircuitBreaker,
        analytics: Analytics,
        scout: ScoutAgent,
        infiltrator: InfiltratorAgent,
        content_seeder: ContentSeederAgent,
        viral_engine: ViralEngineAgent,
        user_client: UserClientManager,
    ) -> None:
        self._risk_engine = risk_engine
        self._circuit_breaker = circuit_breaker
        self._analytics = analytics
        self._scout = scout
        self._infiltrator = infiltrator
        self._content_seeder = content_seeder
        self._viral_engine = viral_engine
        self._user_client = user_client

        self._running = False
        self._loop_task: asyncio.Task[None] | None = None
        self._daily_task: asyncio.Task[None] | None = None
        self._cycle_count = 0
        self._last_daily_run: datetime | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the scheduler and daily-maintenance background loops."""
        if self._running:
            logger.warning("scheduler.already_running")
            return

        self._running = True
        self._loop_task = asyncio.create_task(self._run_main_loop(), name="brain-main-loop")
        self._daily_task = asyncio.create_task(self._run_daily_loop(), name="brain-daily-loop")
        logger.info(
            "scheduler.started",
            interval_seconds=settings.scheduler_interval_seconds,
        )

    async def stop(self) -> None:
        """Gracefully stop both background loops."""
        self._running = False
        for task in (self._loop_task, self._daily_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._loop_task = None
        self._daily_task = None
        logger.info("scheduler.stopped")

    # ------------------------------------------------------------------
    # Main scheduling loop
    # ------------------------------------------------------------------

    async def _run_main_loop(self) -> None:
        """Wrapper that repeatedly calls ``main_loop`` with the configured interval."""
        while self._running:
            try:
                await self.main_loop()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("scheduler.main_loop.error")
            await asyncio.sleep(settings.scheduler_interval_seconds)

    async def main_loop(self) -> None:
        """Execute one full scheduling cycle.

        Steps:
        1. Collect intelligence (Scout).
        2. Risk-assess all active accounts (RiskEngine).
        3. Check global circuit-breaker state.
        4. React to game events (ViralEngine).
        5. Assign routine tasks (Infiltrator + Content).
        6. Collect cycle metrics (Analytics).
        """
        self._cycle_count += 1
        cycle = self._cycle_count
        logger.info("scheduler.cycle.start", cycle=cycle)

        # 1. Intelligence collection
        try:
            intel = await self._scout.collect_intelligence()
            if intel:
                logger.info("scheduler.intel_collected", groups=len(intel))
        except Exception:
            logger.exception("scheduler.intel.error")

        # 2. Risk evaluation for all active accounts
        try:
            await self.evaluate_all_accounts()
        except Exception:
            logger.exception("scheduler.risk_eval.error")

        # 3. Circuit-breaker global check
        system_state = await self._circuit_breaker.get_system_state()
        speed = await self._circuit_breaker.get_speed_multiplier()
        logger.info(
            "scheduler.circuit_breaker",
            state=system_state.value,
            speed_multiplier=speed,
        )

        if system_state == SystemState.RED:
            logger.critical("scheduler.RED_STOP", msg="System in RED state. Skipping all tasks.")
            return

        # 4. Game event handling
        try:
            events = await self._viral_engine.check_game_events()
            if events:
                await self.handle_game_events(events)
        except Exception:
            logger.exception("scheduler.events.error")

        # 5. Routine task assignment (throttled by speed multiplier)
        if speed > 0:
            try:
                await self.assign_infiltration_tasks()
            except Exception:
                logger.exception("scheduler.infiltration.error")

            try:
                content_count = await self._content_seeder.run_content_cycle()
                if content_count:
                    logger.info("scheduler.content_seeded", count=content_count)
            except Exception:
                logger.exception("scheduler.content.error")

        # 6. Metrics collection
        try:
            metrics = await self._analytics.collect_daily_metrics()
            logger.debug("scheduler.metrics_collected", date=str(metrics.date))
        except Exception:
            logger.exception("scheduler.metrics.error")

        logger.info("scheduler.cycle.done", cycle=cycle)

    # ------------------------------------------------------------------
    # Risk evaluation
    # ------------------------------------------------------------------

    async def evaluate_all_accounts(self) -> None:
        """Risk-assess every active account and trigger hibernate/abandon as needed."""
        async with get_session() as session:
            result = await session.execute(
                select(Account).where(Account.status.in_(["active", "nurturing"]))
            )
            accounts = list(result.scalars().all())

        if not accounts:
            return

        logger.info("scheduler.risk_eval.start", account_count=len(accounts))

        for acct in accounts:
            account_data = {
                "id": acct.id,
                "messages_sent_today": acct.messages_sent_today,
                "promo_messages_today": acct.promo_messages_today,
                "groups_active_today": acct.groups_active_today,
                "new_groups_today": acct.new_groups_today,
                "dms_initiated_today": acct.dms_initiated_today,
                "links_sent_today": acct.links_sent_today,
                "reported": acct.reported,
                "kicked_count": acct.kicked_count,
                "phone_type": acct.phone_type,
                "phone_provider": acct.phone_provider,
            }

            assessment = await self._risk_engine.evaluate(account_data)

            # Persist updated risk score
            async with get_session() as session:
                db_acct = await session.get(Account, acct.id)
                if db_acct:
                    db_acct.risk_score = assessment.risk_score

                    if assessment.risk_level == RiskLevel.ABANDON:
                        db_acct.status = "abandoned"
                        logger.warning(
                            "scheduler.account_abandoned",
                            account_id=acct.id,
                            risk_score=assessment.risk_score,
                        )
                    elif assessment.risk_level == RiskLevel.HIBERNATE:
                        db_acct.status = "hibernating"
                        db_acct.hibernated_until = datetime.utcnow() + timedelta(hours=72)
                        await self._circuit_breaker.hibernate_account(
                            acct.id, reason="risk_threshold",
                        )
                        logger.warning(
                            "scheduler.account_hibernated",
                            account_id=acct.id,
                            risk_score=assessment.risk_score,
                        )

            # Register as active for ban-rate calculation
            await self._circuit_breaker.register_active_account(acct.id)

        logger.info("scheduler.risk_eval.done", evaluated=len(accounts))

    # ------------------------------------------------------------------
    # Task assignment
    # ------------------------------------------------------------------

    async def assign_infiltration_tasks(self) -> None:
        """Match available accounts to target groups and dispatch tasks.

        Two-phase approach:
          Phase 1 – send_message for every (account, group) pair already
                    joined (from group_accounts), skipping frozen accounts.
          Phase 2 – join_group for groups with no account present yet,
                    round-robin across available non-frozen accounts
                    (one join per account per cycle).
        """

        # ----- Monitor: count permanently banned accounts -----
        try:
            async with get_session() as session:
                banned_count = await session.scalar(
                    select(func.count(Account.id)).where(Account.status == "abandoned")
                ) or 0
            if banned_count > 0:
                logger.critical(
                    "scheduler.banned_accounts_detected",
                    count=banned_count,
                )
        except Exception:
            logger.debug("scheduler.banned_count_check_failed", exc_info=True)

        # ----- Auto-cleanup: mark groups with 3+ join failures as readonly -----
        try:
            async with get_session() as session:
                await session.execute(text(
                    "UPDATE groups SET status = 'readonly', "
                    "notes = COALESCE(notes,'') || ' [join-failed-auto]' "
                    "WHERE status = 'evaluated' AND id IN ("
                    "  SELECT group_id FROM agent_tasks "
                    "  WHERE task_type = 'join_failed' "
                    "  GROUP BY group_id HAVING COUNT(*) >= 3"
                    ")"
                ))
                await session.commit()
        except Exception:
            pass

        # ----- Phase 0: leave readonly groups to free up group slots -----
        try:
            async with get_session() as session:
                stmt = (
                    select(
                        GroupAccount.account_id,
                        GroupAccount.group_id,
                        Group.tg_group_id,
                        Group.username,
                    )
                    .join(Group, GroupAccount.group_id == Group.id)
                    .where(Group.status == "readonly")
                    .limit(10)  # cap per cycle to avoid rate-limits
                )
                result = await session.execute(stmt)
                to_leave = list(result.all())

            for account_id, group_id, tg_group_id, username in to_leave:
                try:
                    # Skip frozen/flood-waited accounts — they can't leave
                    wrapper = self._user_client._clients.get(account_id)
                    if wrapper and not wrapper.is_available:
                        # Just delete the DB row, can't actually leave on Telegram
                        async with get_session() as session:
                            await session.execute(
                                delete(GroupAccount).where(
                                    GroupAccount.account_id == account_id,
                                    GroupAccount.group_id == group_id,
                                )
                            )
                            await session.commit()
                        logger.debug("scheduler.leave_readonly.skip_frozen", account_id=account_id)
                        continue

                    target = username if username else tg_group_id
                    await self._user_client.leave_group(account_id, target)
                    async with get_session() as session:
                        await session.execute(
                            delete(GroupAccount).where(
                                GroupAccount.account_id == account_id,
                                GroupAccount.group_id == group_id,
                            )
                        )
                        await session.commit()
                    logger.info(
                        "scheduler.leave_readonly",
                        account_id=account_id,
                        group=tg_group_id,
                    )
                except Exception:
                    logger.debug(
                        "scheduler.leave_readonly.fail",
                        account_id=account_id,
                        group=tg_group_id,
                        exc_info=True,
                    )

            if to_leave:
                logger.info("scheduler.phase0.done", left_count=len(to_leave))
        except Exception:
            logger.exception("scheduler.phase0.error")

        # ----- Fetch available accounts (active, under daily msg limit) -----
        async with get_session() as session:
            acct_result = await session.execute(
                select(Account).where(
                    Account.status == "active",
                    Account.role.in_(["infiltrator", "content"]),
                    Account.messages_sent_today < settings.max_messages_per_day,
                )
            )
            available_accounts = list(acct_result.scalars().all())

            # Target groups: active or infiltrating/evaluated, not on cooldown
            group_result = await session.execute(
                select(Group).where(
                    Group.status.in_(["active", "infiltrating", "evaluated"]),
                    (Group.cooldown_until.is_(None)) | (Group.cooldown_until < datetime.utcnow()),
                )
            )
            target_groups = list(group_result.scalars().all())

        # Filter out bot usernames and random invite hashes — they always fail to join
        import re
        _BOT_RE = re.compile(r"(?i)bot$|_bot$")
        _HASH_RE = re.compile(r"^[A-Za-z0-9_-]{16,}$")
        before_filter = len(target_groups)
        target_groups = [
            g for g in target_groups
            if not (
                g.username and (
                    _BOT_RE.search(g.username)
                    or (_HASH_RE.match(g.username) and not any(c in g.username.lower() for c in "aeiou"))
                )
            )
        ]
        if len(target_groups) < before_filter:
            logger.info(
                "scheduler.filtered_invalid_groups",
                removed=before_filter - len(target_groups),
                remaining=len(target_groups),
            )

        if not available_accounts or not target_groups:
            logger.debug(
                "scheduler.assign.skip",
                accounts=len(available_accounts) if available_accounts else 0,
                groups=len(target_groups) if target_groups else 0,
            )
            return

        # ----- Frozen + FloodWait detection -----
        frozen_accounts: set[int] = set()
        for acct in available_accounts:
            wrapper = self._user_client._clients.get(acct.id)
            if not wrapper:
                continue
            if getattr(wrapper, "_frozen", False):
                frozen_accounts.add(acct.id)
                logger.debug("scheduler.frozen_skip", account_id=acct.id)
            elif hasattr(wrapper, "_flood_until") and wrapper._flood_until:
                import time as _time_mod
                if _time_mod.time() < wrapper._flood_until:
                    frozen_accounts.add(acct.id)
                    logger.debug("scheduler.flood_skip", account_id=acct.id)

        available_account_ids = {a.id for a in available_accounts}

        # ----- Filter accounts eligible for join_group (age quota + 1.5h interval) -----
        today_start = datetime.utcnow().replace(
            hour=0, minute=0, second=0, microsecond=0,
        )
        interval_cutoff = datetime.utcnow() - timedelta(minutes=30)  # 30min between joins per account
        join_eligible_ids: set[int] = set()
        async with get_session() as session:
            for acct in available_accounts:
                if acct.id in frozen_accounts:
                    continue
                tier = AgePolicy.get_tier(acct.account_age_days or 0)
                cap = AgePolicy.get_policy(tier).get("max_new_groups_per_day", 1)
                n = await session.scalar(
                    select(func.count(GroupAccount.account_id)).where(
                        GroupAccount.account_id == acct.id,
                        GroupAccount.joined_at >= today_start,
                    )
                ) or 0
                if n >= cap:
                    continue
                # 1.5h interval since this account's last join (any group)
                last_joined = await session.scalar(
                    select(func.max(GroupAccount.joined_at)).where(
                        GroupAccount.account_id == acct.id,
                    )
                )
                if last_joined is not None and last_joined > interval_cutoff:
                    continue
                join_eligible_ids.add(acct.id)

        # ----- Phase 1: send_message for already-joined (account, group) pairs -----
        # Query all account-group membership pairs for active/evaluated groups.
        target_group_ids = {g.id for g in target_groups}
        async with get_session() as session:
            stmt = (
                select(
                    GroupAccount.account_id,
                    GroupAccount.group_id,
                    Group.tg_group_id,
                )
                .join(Group, GroupAccount.group_id == Group.id)
                .where(
                    Group.status.in_(["active", "evaluated", "infiltrating"]),
                    GroupAccount.account_id.in_(available_account_ids),
                    GroupAccount.group_id.in_(target_group_ids),
                )
                .order_by(Group.member_count.desc())  # big groups first
            )
            result = await session.execute(stmt)
            pairs = list(result.all())

        speed = await self._circuit_breaker.get_speed_multiplier()
        max_assignments = min(len(pairs) + 15, 50)
        assignments_made = 0

        # Collect send_message tasks first, then run in parallel batches
        send_tasks: list[dict] = []
        for account_id, group_id, tg_group_id in pairs:
            if account_id in frozen_accounts:
                continue
            if len(send_tasks) >= max_assignments:
                break
            if not await self._circuit_breaker.should_proceed(account_id):
                continue
            if speed < 1.0 and random.random() > speed:
                continue
            send_tasks.append({
                "task_type": "send_message",
                "account_id": account_id,
                "group_id": group_id,
            })

        # Run in parallel batches of 3 — balances speed vs DB pool pressure.
        BATCH_SIZE = 1  # Sequential per account to avoid SQLite session lock
        for i in range(0, len(send_tasks), BATCH_SIZE):
            batch = send_tasks[i:i + BATCH_SIZE]
            await asyncio.gather(
                *(self._infiltrator.execute_task(t) for t in batch),
                return_exceptions=True,
            )
            assignments_made += len(batch)

        phase1_count = assignments_made
        logger.info(
            "scheduler.assign.phase1_done",
            send_message_tasks=phase1_count,
            pairs_total=len(pairs),
            frozen_skipped=len(frozen_accounts),
        )

        # ----- Phase 2: join_group for groups without any account present -----
        join_tasks: list[dict] = []
        joined_group_ids = {gid for _, gid, _ in pairs}
        unjoin_groups = [g for g in target_groups if g.id not in joined_group_ids]

        # Exclude groups that failed to join in the last 24h — avoids retrying dead groups
        recent_cutoff = datetime.utcnow() - timedelta(hours=24)
        try:
            async with get_session() as session:
                recent_failed_result = await session.execute(
                    select(AgentTask.group_id).where(
                        AgentTask.task_type == "join_failed",
                        AgentTask.completed_at > recent_cutoff,
                        AgentTask.group_id.isnot(None),
                    ).group_by(AgentTask.group_id)
                )
                recent_failed_group_ids = {row[0] for row in recent_failed_result.all()}

            before_filter = len(unjoin_groups)
            unjoin_groups = [g for g in unjoin_groups if g.id not in recent_failed_group_ids]
            if len(unjoin_groups) < before_filter:
                logger.info(
                    "scheduler.phase2.skip_recent_failed",
                    skipped=before_filter - len(unjoin_groups),
                    remaining=len(unjoin_groups),
                )
        except Exception:
            logger.debug("scheduler.phase2.filter_failed", exc_info=True)

        # Prioritize by grade (S > A > B > C) then by member_count desc
        _grade_order = {"S": 0, "A": 1, "B": 2, "C": 3}
        unjoin_groups.sort(
            key=lambda g: (_grade_order.get(g.grade, 9), -(g.member_count or 0))
        )

        # Build a list of accounts eligible for join, excluding frozen
        join_candidates = [
            a for a in available_accounts
            if a.id in join_eligible_ids and a.id not in frozen_accounts
        ]
        # Randomise and pick at most 2 accounts per round — spreads joins across
        # accounts naturally and avoids all accounts joining in the same cycle.
        random.shuffle(join_candidates)
        join_candidates = join_candidates[:10]

        picked_for_join_count: dict[int, int] = {}

        for group in unjoin_groups:
            if assignments_made >= max_assignments:
                break

            # Find the next account not yet used for a join this cycle
            chosen = None
            for acct in join_candidates:
                join_count = picked_for_join_count.get(acct.id, 0)
                if join_count >= 2:  # max 2 joins per account per cycle
                    continue
                if not await self._circuit_breaker.should_proceed(acct.id):
                    continue
                chosen = acct
                break

            if chosen is None:
                break  # no more accounts available for joining

            # 12h same-group interval: no different account should join
            # the same group within 12h of the last join by any account.
            async with get_session() as session:
                last_join = await session.scalar(
                    select(func.max(GroupAccount.joined_at)).where(
                        GroupAccount.group_id == group.id,
                    )
                )
            if last_join and (datetime.utcnow() - last_join).total_seconds() < 600:  # 10min for testing
                continue

            # Speed-multiplier throttle
            if speed < 1.0 and random.random() > speed:
                continue

            task = {
                "task_type": "join_group",
                "account_id": chosen.id,
                "group_id": group.id,
            }
            join_tasks.append(task)
            picked_for_join_count[chosen.id] = picked_for_join_count.get(chosen.id, 0) + 1

        # Run join tasks in parallel batches of 5
        JOIN_BATCH_SIZE = 5
        for i in range(0, len(join_tasks), JOIN_BATCH_SIZE):
            batch = join_tasks[i:i + JOIN_BATCH_SIZE]
        for i in range(0, len(join_tasks), BATCH_SIZE):
            batch = join_tasks[i:i + JOIN_BATCH_SIZE]
            await asyncio.gather(
                *(self._infiltrator.execute_task(t) for t in batch),
                return_exceptions=True,
            )
            assignments_made += len(batch)

        phase2_count = assignments_made - phase1_count
        logger.info(
            "scheduler.assign.done",
            assignments=assignments_made,
            send_message=phase1_count,
            join_group=phase2_count,
        )

        # -- Track per-account daily output vs capacity --
        sum_sent = 0
        async with get_session() as session:
            for acct in available_accounts:
                sent = await session.scalar(
                    select(func.count()).select_from(MessageLog).where(
                        MessageLog.account_id == acct.id,
                        MessageLog.sent_at >= today_start,
                    )
                ) or 0
                sum_sent += sent
                cap = settings.max_messages_per_day  # 30
                utilization = sent / cap if cap > 0 else 0

                if utilization < 0.1 and acct.id not in frozen_accounts:
                    logger.warning(
                        "scheduler.account_underperforming",
                        account_id=acct.id,
                        messages_today=sent,
                        capacity=cap,
                        utilization=f"{utilization:.0%}",
                    )

        logger.info(
            "scheduler.daily_progress",
            total_messages_today=sum_sent,
            target=len(available_accounts) * settings.max_messages_per_day,
            utilization=f"{sum_sent / max(len(available_accounts) * settings.max_messages_per_day, 1):.0%}",
        )

    def select_best_account(
        self,
        group_data: dict,
        available_accounts: list[dict],
    ) -> int | None:
        """Select the best account for a group using a weighted scoring model.

        Score = trust_score * 0.3 + persona_fit * 0.3
                + cooldown_score * 0.2 + history_score * 0.2

        Returns the ``id`` of the best account, or ``None`` if no candidates.
        """
        if not available_accounts:
            return None

        group_lang = group_data.get("language", "en")
        group_topics = group_data.get("topics", [])
        recommended_persona = group_data.get("recommended_persona")

        best_id: int | None = None
        best_score = -1.0

        for acct in available_accounts:
            # 1. Trust score (0-1, directly from account)
            trust = acct.get("trust_score", 0.0)

            # 2. Persona fit (0-1)
            persona_fit = 0.0
            if recommended_persona and acct.get("persona_id") == recommended_persona:
                persona_fit = 1.0
            elif acct.get("language") == group_lang:
                persona_fit = 0.5

            # 3. Cooldown score: accounts that have sent fewer messages today are preferable
            msgs_today = acct.get("messages_sent_today", 0)
            max_msgs = settings.max_messages_per_day
            cooldown = 1.0 - (msgs_today / max_msgs) if max_msgs > 0 else 1.0

            # 4. History score: inverse of risk score (lower risk = better)
            risk = acct.get("risk_score", 0.0)
            history = 1.0 - risk

            composite = (
                trust * 0.3
                + persona_fit * 0.3
                + cooldown * 0.2
                + history * 0.2
            )

            if composite > best_score:
                best_score = composite
                best_id = acct["id"]

        logger.debug(
            "scheduler.select_account",
            group_id=group_data.get("id"),
            best_account=best_id,
            score=round(best_score, 4),
        )
        return best_id

    # ------------------------------------------------------------------
    # Game event handling
    # ------------------------------------------------------------------

    async def handle_game_events(self, events: list[dict]) -> None:
        """Delegate game events to the ViralEngine for time-sensitive reactions."""
        logger.info("scheduler.game_events", count=len(events))
        results = await self._viral_engine.handle_events(events)
        for r in results:
            if r.get("actions_taken"):
                logger.info(
                    "scheduler.viral_action",
                    event_type=r.get("event_type"),
                    actions=r["actions_taken"],
                )

    # ------------------------------------------------------------------
    # Daily maintenance
    # ------------------------------------------------------------------

    async def _run_daily_loop(self) -> None:
        """Background loop that fires ``daily_maintenance`` once per calendar day."""
        while self._running:
            try:
                now = datetime.utcnow()
                # Run daily maintenance if we haven't run today
                if self._last_daily_run is None or self._last_daily_run.date() < now.date():
                    await self.daily_maintenance()
                    self._last_daily_run = now
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("scheduler.daily_loop.error")
            # Check once per hour
            await asyncio.sleep(3600)

    async def daily_maintenance(self) -> None:
        """Daily maintenance tasks executed once per calendar day.

        1. Reset all per-account daily counters.
        2. Generate and persist the daily metrics report.
        3. Refresh account hibernation states.
        4. Log a summary.
        """
        logger.info("scheduler.daily_maintenance.start")

        # 1. Reset daily counters
        try:
            await self._analytics.reset_daily_counters()
            logger.info("scheduler.daily.counters_reset")
        except Exception:
            logger.exception("scheduler.daily.reset_error")

        # 2. Generate daily report
        try:
            report = await self._analytics.generate_weekly_report()
            logger.info("scheduler.daily.report_generated", report_length=len(report))
        except Exception:
            logger.exception("scheduler.daily.report_error")

        # 3. Refresh hibernated accounts - reactivate those whose window expired
        try:
            async with get_session() as session:
                result = await session.execute(
                    select(Account).where(
                        Account.status == "hibernating",
                        Account.hibernated_until.is_not(None),
                        Account.hibernated_until < datetime.utcnow(),
                    )
                )
                expired = list(result.scalars().all())

                for acct in expired:
                    acct.status = "active"
                    acct.hibernated_until = None
                    logger.info(
                        "scheduler.daily.reactivated",
                        account_id=acct.id,
                    )

            if expired:
                logger.info("scheduler.daily.reactivated_count", count=len(expired))
        except Exception:
            logger.exception("scheduler.daily.reactivate_error")

        logger.info("scheduler.daily_maintenance.done")
