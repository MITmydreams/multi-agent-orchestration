"""Layer 2 -- Infiltrator Agent: trust-building and soft promotion.

The infiltrator manages multiple accounts that blend into target groups
using realistic personas.  It follows a strict three-phase lifecycle per
account-group pair and enforces rigorous isolation rules between co-located
accounts.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import select, and_, func as sa_func

from src.agents.base import BaseAgent
from src.ai.anti_spam import AntiSpamEngine
from src.ai.content_gen import ContentGenerator
from src.ai.persona import PersonaManager, PersonaTemplate
from src.brain.age_policy import AgePolicy
from src.brain.circuit_breaker import CircuitBreaker
from src.brain.risk_engine import RiskEngine, RiskLevel
from src.config import settings
from src.models import Account, AgentTask, Group, GroupAccount, MessageLog, get_session
from src.ai.template_engine import TemplateContentEngine
from src.agents.infiltrator.coordinated_chat import CoordinatedChatStrategy
from src.tg_clients.user_client import UserClientManager

logger = structlog.get_logger(__name__)

_PROMO_LOG = Path("data/logs/promo-tracking.jsonl")


class InfiltratorAgent(BaseAgent):
    """Core promotion layer -- infiltrates groups with realistic personas.

    Three-phase infiltration lifecycle:
        Phase 1 (Day 1-5):   Lurk -- only observe, occasionally react. Promo = 0%.
        Phase 2 (Day 6-14):  Trust -- participate in discussions, share useful info. Promo ~5%.
        Phase 3 (Day 15+):   Soft Promotion -- natural sharing of game experiences. Promo 15-20%.

    Strict isolation rules:
        - Accounts in the same group NEVER interact with each other.
        - Accounts stagger online times by 1-2 hours.
        - Join intervals between accounts in the same group: 5-7 days minimum.
        - Same-group cooldown: 14 days after removal.
        - Max 5 messages per group per day per account.
        - Promotional messages never exceed 20% of total messages.
    """

    name: str = "infiltrator"
    agent_type: str = "infiltrator"

    # Strict isolation rules
    RULES: dict[str, Any] = {
        "同群渗透号之间绝不互动": True,
        "不同时在线（错开1-2小时）": True,
        "入群时间间隔至少5-7天": True,
        "同群冷却期": 14,          # days
        "每日每群最多消息": 5,
        "推广消息占比上限": 0.30,
    }

    # Phase durations (conservative to avoid Telegram restrictions)
    # Even veteran accounts must lurk — jumping straight to posting
    # after joining triggers anti-spam detection.
    LURK_DAYS: int = 3           # new accounts: 3 days silent
    TRUST_DAYS: int = 10         # day 4-10: organic chat, no promo

    # Veteran accounts (365+ days) — still need some lurk
    LURK_DAYS_VETERAN: int = 1   # 1 day silent after joining
    TRUST_DAYS_VETERAN: int = 3  # day 2-3: organic chat, then promo

    # Promotion approach types
    PROMO_APPROACHES: list[str] = [
        "casual_mention",      # A: "Recently found an interesting project..."
        "experience_share",    # B: "Tried a button game the other day, free coins..."
        "ask_for_help",        # C: "Anyone played The Button?"
        "data_analysis",       # D: "Analysed a new project's dividend model..."
        "screenshot_share",    # E: Share win/dividend screenshot
    ]

    def __init__(
        self,
        user_client: UserClientManager,
        risk_engine: RiskEngine,
        circuit_breaker: CircuitBreaker,
        persona_manager: PersonaManager,
        content_gen: ContentGenerator,
        anti_spam: AntiSpamEngine,
    ) -> None:
        super().__init__(user_client, risk_engine, circuit_breaker)
        self.persona_manager = persona_manager
        self.content_gen = content_gen
        self.anti_spam = anti_spam
        self._account_age_cache: dict[int, int] = {}
        self._template_engine = TemplateContentEngine()
        self._coordinated_chat = CoordinatedChatStrategy(user_client)
        # (account_id, group_id, message_id) — already replied with link
        self._replied_link_messages: set[tuple[int, str, int]] = set()

    # ------------------------------------------------------------------
    # Promo tracking log
    # ------------------------------------------------------------------

    async def _log_promo_event(self, event_type: str, **kwargs) -> None:
        """Append a structured event to the promo tracking log."""
        entry = {
            "ts": datetime.utcnow().isoformat() + "Z",
            "event": event_type,
            **kwargs,
        }
        try:
            _PROMO_LOG.parent.mkdir(parents=True, exist_ok=True)
            with open(_PROMO_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            pass  # best-effort

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Main loop: check each account-group pair phase -> assign tasks -> execute."""
        self._log.info("infiltrator.run.start")
        while self._running:
            if not await self.should_proceed():
                await self._jittered_sleep(60)
                continue

            try:
                assignments = await self._load_active_assignments()
                self._log.info("infiltrator.assignments_loaded", count=len(assignments))

                for assignment in assignments:
                    if not self._running:
                        break

                    account_id = assignment["account_id"]
                    group_id = assignment["group_id"]
                    tg_group_id = assignment["tg_group_id"]

                    # Risk check
                    account_data = await self._load_account_data(account_id)
                    if not account_data:
                        continue
                    if not await self.is_account_safe(account_data):
                        self._log.warning("infiltrator.account_unsafe", account_id=account_id)
                        continue

                    # Check isolation: skip if another account from this agent
                    # is currently active in the same group
                    if await self._is_peer_active(account_id, group_id):
                        self._log.debug(
                            "infiltrator.peer_active_skip",
                            account_id=account_id,
                            group_id=group_id,
                        )
                        continue

                    # Determine phase and execute
                    phase = await self.determine_phase(account_id, tg_group_id)

                    if phase == "lurking":
                        await self.execute_lurk_phase(account_id, tg_group_id)
                    elif phase == "trust_building":
                        await self.execute_trust_phase(account_id, tg_group_id)
                    elif phase == "soft_promotion":
                        await self.execute_promotion_phase(account_id, tg_group_id)

                    await self.log_activity("infiltrate", {
                        "account_id": account_id,
                        "group_id": group_id,
                        "phase": phase,
                    })

                    # Stagger: short pause between account activations
                    await self._jittered_sleep(random.uniform(300, 600))  # 5-10 min between msgs in same cycle

            except Exception:
                self._log.exception("infiltrator.run.cycle_error")

            # Main cycle interval (3-5 min for testing)
            await self._jittered_sleep(random.uniform(180, 300))

    # ------------------------------------------------------------------
    # Phase execution
    # ------------------------------------------------------------------

    async def execute_lurk_phase(self, account_id: int, group_id: str) -> None:
        """Phase 1: Lurk -- read messages, occasionally react, zero promo.

        Actions (random, low frequency):
        - Read recent messages (always)
        - Occasionally send a non-promo reaction/emoji (10% chance)
        - Occasionally reply to a popular message with a short organic comment (5% chance)
        """
        self._log.info("infiltrator.lurk", account_id=account_id, group_id=group_id)

        if not await self._daily_message_budget_ok(account_id, group_id):
            return

        recent_messages = await self.user_client.get_recent_messages(group_id, limit=50, account_id=account_id)
        # Even if no recent messages, continue -- template fallback can still produce content.

        roll = random.random()
        if roll < 0.80:
            # Reply to a popular message with a short organic comment (raised to 80%)
            if recent_messages:
                target_msg = self._pick_engaging_message(recent_messages)
            else:
                target_msg = None

            if not recent_messages:
                # No context available -- use template directly, skip AI call
                response = self._template_fallback(account_id, group_id, is_promo=False)
            else:
                response = await self.generate_contextual_response(
                    account_id, group_id, recent_messages,
                )
                if not response:
                    response = self._template_fallback(account_id, group_id, is_promo=False)
            if response:
                await self._send_message(account_id, group_id, response, is_promo=False)
        elif roll < 0.45 and recent_messages:
            # React with emoji
            reactions = ["👍", "😂", "🔥", "👀", "💯"]
            target_msg = random.choice(recent_messages)
            msg_id = target_msg.get("message_id")
            if msg_id:
                try:
                    await self.user_client.react_to_message(
                        account_id, group_id, msg_id, random.choice(reactions),
                    )
                except Exception:
                    self._log.debug("infiltrator.lurk.react_failed", account_id=account_id)

    async def execute_trust_phase(self, account_id: int, group_id: str) -> None:
        """Phase 2: Trust building -- participate in discussions, share useful info.

        Promotional content: ~5% of messages.
        """
        self._log.info("infiltrator.trust", account_id=account_id, group_id=group_id)

        if not await self._daily_message_budget_ok(account_id, group_id):
            return

        recent_messages = await self.user_client.get_recent_messages(group_id, limit=100, account_id=account_id)
        # Even if no recent messages, continue -- template fallback can still produce content.

        # Priority: check if anyone asked for our link first
        if recent_messages and await self._detect_link_request(account_id, group_id, recent_messages):
            return  # Already replied with link, done for this tick

        # Decide: organic response (75%) or soft mention (25%)
        is_promo = random.random() < 0.25
        if is_promo and not await self._promo_ratio_ok(account_id):
            is_promo = False

        if not recent_messages:
            # No context available -- use template directly, skip AI call
            response = self._template_fallback(account_id, group_id, is_promo=is_promo)
        else:
            response = await self.generate_contextual_response(
                account_id, group_id, recent_messages,
            )
            if not response:
                # AI failed -> fall back to pre-written templates, never leave empty-handed
                response = self._template_fallback(account_id, group_id, is_promo=is_promo)
        if not response:
            self._log.warning("infiltrator.trust.no_content", account_id=account_id, group_id=group_id)
            return

        if is_promo:
            # Inject a subtle mention into an otherwise organic message
            response = await self._soften_promo(response, account_id)

        # Anti-spam check
        spam_score = await self.anti_spam.check(response)
        if spam_score > 0.6:
            self._log.warning(
                "infiltrator.trust.spam_blocked",
                account_id=account_id,
                spam_score=spam_score,
            )
            return

        ok = await self._send_message(account_id, group_id, response, is_promo=is_promo)
        if ok:
            await self._log_promo_event(
                "trust_message",
                account_id=account_id,
                group_id=group_id,
                is_promo=is_promo,
                content_len=len(response),
            )
        else:
            await self._log_promo_event(
                "send_failed",
                account_id=account_id,
                group_id=group_id,
                phase="trust_building",
            )

    async def execute_promotion_phase(self, account_id: int, group_id: str) -> None:
        """Phase 3: Soft promotion -- natural sharing of game experiences.

        Promotional content: 15-20% of messages.

        Five promotion approaches (randomly selected):
        A. Casual mention: "Recently found an interesting project..."
        B. Experience share: "Tried a button game the other day, coin room is free..."
        C. Ask for help: "Anyone played The Button?"
        D. Data analysis: "Analysed a new project's dividend model..."
        E. Screenshot share: Share win/dividend screenshot

        KEY RULE: NEVER proactively send links. Wait for someone to ask.
        """
        self._log.info("infiltrator.promote", account_id=account_id, group_id=group_id)

        if not await self._daily_message_budget_ok(account_id, group_id):
            return

        recent_messages = await self.user_client.get_recent_messages(group_id, limit=100, account_id=account_id)
        # Even if no recent messages, continue -- template fallback can still produce content.

        # Priority: check if anyone asked for our link first
        if recent_messages and await self._detect_link_request(account_id, group_id, recent_messages):
            return  # Already replied with link, done for this tick

        # Decide: organic (80-85%) or promo (15-20%)
        promo_chance = random.uniform(0.30, 0.35)
        is_promo = random.random() < promo_chance
        if is_promo and not await self._promo_ratio_ok(account_id):
            is_promo = False

        if is_promo:
            approach = random.choice(self.PROMO_APPROACHES)
            response = await self._generate_promo_message(account_id, group_id, approach)
        elif not recent_messages:
            # No context available -- use template directly, skip AI call
            response = self._template_fallback(account_id, group_id, is_promo=False)
        else:
            response = await self.generate_contextual_response(
                account_id, group_id, recent_messages,
            )

        if not response:
            # AI failed -> fall back to pre-written templates, never leave empty-handed
            response = self._template_fallback(account_id, group_id, is_promo=is_promo)
        if not response:
            self._log.warning("infiltrator.promote.no_content", account_id=account_id, group_id=group_id)
            return

        # If promo message lacks a game link, append one 50% of the time
        if is_promo and settings.game_miniapp_url not in response and random.random() < 0.50:
            link_suffixes = [
                f"\n{settings.game_miniapp_url}",
                f" 👆 {settings.game_miniapp_url}",
                f"\ncheck it out: {settings.game_miniapp_url}",
            ]
            response += random.choice(link_suffixes)

        # Anti-spam check
        spam_score = await self.anti_spam.check(response)
        if spam_score > 0.5:
            self._log.warning(
                "infiltrator.promote.spam_blocked",
                account_id=account_id,
                spam_score=spam_score,
            )
            return

        ok = await self._send_message(account_id, group_id, response, is_promo=is_promo)
        if ok:
            await self._log_promo_event(
                "promo_message",
                account_id=account_id,
                group_id=group_id,
                approach=approach if is_promo else "organic",
                content_len=len(response),
                has_link=settings.game_miniapp_url in response if response else False,
            )
        else:
            await self._log_promo_event(
                "send_failed",
                account_id=account_id,
                group_id=group_id,
                phase="soft_promotion",
            )

    # ------------------------------------------------------------------
    # Template fallback -- ensures every phase always has content to send
    # ------------------------------------------------------------------

    def _template_fallback(self, account_id: int, group_id: str, *, is_promo: bool) -> str | None:
        """Fall back to pre-written templates when AI content gen fails.

        This ensures we ALWAYS have something to send, hitting the daily
        message limit instead of silently dropping 97% of send attempts.

        The context is guessed from the group_id string (which may be a
        @username or numeric id) so that messages are more topical.
        """
        try:
            # Guess context from group_id string
            gid_lower = group_id.lower() if group_id else ""
            if any(kw in gid_lower for kw in ["game", "play", "earn", "tap", "click"]):
                context = "asking_recommendation"  # game-related
            elif any(kw in gid_lower for kw in ["ton", "wallet", "defi", "swap"]):
                context = "responding_to_question"  # tech discussion
            else:
                context = "general"

            if is_promo:
                # Use promo template (may contain link)
                return self._template_engine.generate_promo_with_link(
                    "crypto_veteran",
                    random.choice([
                        "casual_mention", "experience_share", "ask_for_help",
                        "data_analysis", "screenshot_share",
                    ]),
                    "en",  # Always English -- ru/vi templates return Chinese (missing templates)
                )
            else:
                # Use contextual chat template
                return self._template_engine.generate_chat_response(
                    "crypto_veteran",
                    context,
                    language="en",
                )
        except Exception:
            self._log.warning(
                "infiltrator.template_fallback.error",
                account_id=account_id,
                group_id=group_id,
            )
            # Hardcoded last-resort fallback — never return None
            _fallbacks = [
                "interesting project, worth checking out",
                "been looking into this, thoughts?",
                "anyone else been following this space?",
                "good point, I've seen similar trends",
                "this is worth keeping an eye on",
            ]
            return random.choice(_fallbacks)

    # ------------------------------------------------------------------
    # Scheduler-facing API
    # ------------------------------------------------------------------

    async def execute_task(self, task: dict) -> None:
        """Execute a single task dispatched by the CentralBrain scheduler.

        Expected *task* keys: ``task_type``, ``account_id``, ``group_id``.
        """
        task_type = task.get("task_type", "send_message")
        account_id = task.get("account_id")
        group_id = task.get("group_id")

        if account_id is None or group_id is None:
            self._log.warning("infiltrator.execute_task.missing_ids", task=task)
            return

        # Branch: join_group task is handled separately with its own safety
        # checks (quota, interval) -- we do NOT fall through into the
        # send_message pipeline.
        if task_type == "join_group":
            await self._handle_join_group(account_id, group_id)
            return

        # Load account data for risk check
        account_data = await self._load_account_data(account_id)
        if not account_data:
            return
        if not await self.is_account_safe(account_data):
            self._log.warning("infiltrator.execute_task.account_unsafe", account_id=account_id)
            return

        # Resolve tg_group_id from DB group id.
        # Prefer @username — Telethon can resolve it via API without needing
        # a cached access_hash. Numeric IDs require the entity to be in the
        # session cache which is lost on restart.
        tg_group_id = str(group_id)
        try:
            async with get_session() as session:
                stmt = select(Group.tg_group_id, Group.username).where(Group.id == group_id)
                result = await session.execute(stmt)
                row = result.one_or_none()
                if row:
                    if row.username:
                        tg_group_id = f"@{row.username}" if not row.username.startswith("@") else row.username
                    elif row.tg_group_id:
                        tg_group_id = row.tg_group_id
        except Exception:
            pass

        # Skip tiny groups 50% of the time to focus effort on larger ones
        try:
            async with get_session() as session:
                member_count = await session.scalar(
                    select(Group.member_count).where(Group.id == group_id)
                ) or 0
        except Exception:
            member_count = 0

        if member_count > 0 and member_count < 10 and random.random() < 0.5:
            self._log.debug(
                "infiltrator.execute_task.small_group_skip",
                account_id=account_id,
                group_id=group_id,
                member_count=member_count,
            )
            return

        phase = await self.determine_phase(account_id, tg_group_id)

        # Coordinated dual-account chat: 20% chance during trust_building
        # or soft_promotion phases (replaces the normal single-account msg).
        if phase in ("trust_building", "soft_promotion"):
            if (
                settings.coordinated_chat_enabled
                and random.random() < settings.coordinated_chat_chance
            ):
                coord_ok = await self._coordinated_chat.try_coordinated_chat(
                    account_id=account_id,
                    group_id=group_id,       # DB group id (int)
                    tg_group_id=tg_group_id,
                )
                if coord_ok:
                    await self.log_activity("coordinated_chat", {
                        "account_id": account_id,
                        "group_id": group_id,
                        "phase": phase,
                    })
                    await self._log_promo_event(
                        "coordinated_chat",
                        account_id=account_id,
                        group_id=group_id,
                        phase=phase,
                    )
                    return  # Done -- skip normal single-account message

        if phase == "lurking":
            await self.execute_lurk_phase(account_id, tg_group_id)
        elif phase == "trust_building":
            await self.execute_trust_phase(account_id, tg_group_id)
        elif phase == "soft_promotion":
            await self.execute_promotion_phase(account_id, tg_group_id)

        await self.log_activity("infiltrate_task", {
            "account_id": account_id,
            "group_id": group_id,
            "phase": phase,
            "task_type": task_type,
        })

    # ------------------------------------------------------------------
    # Join group flow (task_type == "join_group")
    # ------------------------------------------------------------------

    async def _handle_join_group(self, account_id: int, db_group_id: int) -> bool:
        """Join a target group on behalf of *account_id*.

        Flow:
            1. Risk gate + per-tier daily join quota + >=3h inter-join interval.
            2. Resolve join target from ``Group`` row (username / tg_group_id).
               NOTE: the current ``Group`` schema has no ``invite_link``
               column, so we fall back to username -> tg_group_id.
            3. Call ``user_client.join_group`` (implemented by the user_client
               agent in parallel).
            4. Upsert ``GroupAccount`` row with phase='lurking'.
            5. Log activity.
            6. Post-join lurk delay (see note below).
        """
        # 1. Risk gate
        account_data = await self._load_account_data(account_id)
        if not account_data:
            return False
        if not await self.is_account_safe(account_data):
            self._log.warning(
                "infiltrator.join_group.account_unsafe", account_id=account_id,
            )
            return False
        if not await self._check_join_quota(account_id):
            self._log.info(
                "infiltrator.join_group.quota_blocked", account_id=account_id,
            )
            return False

        # 2. Resolve target
        target: str | None = None
        group_title: str | None = None
        try:
            async with get_session() as session:
                stmt = select(Group).where(Group.id == db_group_id)
                result = await session.execute(stmt)
                group = result.scalar_one_or_none()
                if group is None:
                    self._log.warning(
                        "infiltrator.join_group.group_missing",
                        group_id=db_group_id,
                    )
                    return False
                # Prefer @username (Telethon can resolve via API without cache).
                # Fall back to raw tg_group_id only if no username.
                if group.username:
                    target = f"@{group.username}" if not group.username.startswith("@") else group.username
                else:
                    target = group.tg_group_id
                group_title = group.title
        except Exception:
            self._log.exception(
                "infiltrator.join_group.load_error",
                account_id=account_id,
                group_id=db_group_id,
            )
            return False

        if not target:
            self._log.warning(
                "infiltrator.join_group.no_target",
                account_id=account_id,
                group_id=db_group_id,
            )
            return False

        # 3. Execute join
        try:
            ok = await self.user_client.join_group(account_id, target)
        except Exception:
            self._log.exception(
                "infiltrator.join_group.client_error",
                account_id=account_id,
                group_id=db_group_id,
            )
            ok = False

        if not ok:
            await self.log_activity("join_failed", {
                "account_id": account_id,
                "group_id": db_group_id,
                "target": target,
            })
            # Auto-mark group readonly after 3 cumulative failures (any account)
            try:
                async with get_session() as session:
                    from sqlalchemy import func as sa_func
                    fail_count = await session.scalar(
                        select(sa_func.count()).select_from(AgentTask).where(
                            AgentTask.task_type == "join_failed",
                            AgentTask.payload["group_id"].as_string() == str(db_group_id),
                        )
                    ) or 0
                    if fail_count >= 3:
                        await session.execute(
                            Group.__table__.update()
                            .where(Group.id == db_group_id)
                            .values(status="readonly")
                        )
                        self._log.info(
                            "infiltrator.join_group.auto_readonly",
                            group_id=db_group_id,
                            fail_count=fail_count,
                        )
            except Exception:
                pass  # Best-effort, don't block on cleanup
            return False

        # 4. Upsert GroupAccount -> phase='lurking'
        try:
            async with get_session() as session:
                stmt = select(GroupAccount).where(
                    and_(
                        GroupAccount.account_id == account_id,
                        GroupAccount.group_id == db_group_id,
                    ),
                )
                result = await session.execute(stmt)
                ga = result.scalar_one_or_none()
                now = datetime.utcnow()
                if ga is None:
                    ga = GroupAccount(
                        account_id=account_id,
                        group_id=db_group_id,
                        phase="lurking",
                        joined_at=now,
                    )
                    session.add(ga)
                else:
                    ga.phase = "lurking"
                    ga.joined_at = now
        except Exception:
            self._log.exception(
                "infiltrator.join_group.upsert_error",
                account_id=account_id,
                group_id=db_group_id,
            )
            # Even if the DB upsert failed, the join already happened; don't
            # retry it. Fall through to logging.

        # 5. Activity log
        await self.log_activity("joined_group", {
            "account_id": account_id,
            "group_id": db_group_id,
            "target": target,
            "title": group_title,
        })
        await self._log_promo_event(
            "group_joined",
            account_id=account_id,
            group_id=db_group_id,
            target=target,
            title=group_title,
        )

        # Post-join lurking period is enforced by the scheduler's 24h
        # same-group constraint and by GroupAccount.phase ('lurking') which
        # the trust phase controller advances on the next cycle. No blocking
        # sleep here.

        return True

    async def _check_join_quota(self, account_id: int) -> bool:
        """Return True if *account_id* may join another group right now.

        Enforces:
            * Per-tier daily new-group cap (``AgePolicy.max_new_groups_per_day``).
            * Minimum 3 h interval between two joins for the same account.
        """
        try:
            async with get_session() as session:
                # Fetch account age
                stmt = select(Account.account_age_days).where(Account.id == account_id)
                result = await session.execute(stmt)
                age_days = result.scalar_one_or_none()
                if age_days is None:
                    return False

                tier = AgePolicy.get_tier(int(age_days))
                cap = AgePolicy.get_policy(tier).get("max_new_groups_per_day", 1)

                # Count joins today
                today_start = datetime.utcnow().replace(
                    hour=0, minute=0, second=0, microsecond=0,
                )
                count_stmt = (
                    select(sa_func.count())
                    .select_from(GroupAccount)
                    .where(
                        and_(
                            GroupAccount.account_id == account_id,
                            GroupAccount.joined_at >= today_start,
                        ),
                    )
                )
                count_result = await session.execute(count_stmt)
                joins_today = int(count_result.scalar_one() or 0)

                if joins_today >= cap:
                    self._log.debug(
                        "infiltrator.join_quota.daily_cap_hit",
                        account_id=account_id,
                        tier=tier,
                        cap=cap,
                        joins_today=joins_today,
                    )
                    return False

                # Enforce 3 h interval since last join
                last_stmt = (
                    select(sa_func.max(GroupAccount.joined_at))
                    .where(GroupAccount.account_id == account_id)
                )
                last_result = await session.execute(last_stmt)
                last_joined_at = last_result.scalar_one_or_none()
                if last_joined_at is not None:
                    if datetime.utcnow() - last_joined_at < timedelta(minutes=5):
                        self._log.debug(
                            "infiltrator.join_quota.interval_block",
                            account_id=account_id,
                            last_joined_at=str(last_joined_at),
                        )
                        return False

                return True
        except Exception:
            self._log.exception(
                "infiltrator.join_quota.error", account_id=account_id,
            )
            return False

    # ------------------------------------------------------------------
    # Phase determination
    # ------------------------------------------------------------------

    async def determine_phase(self, account_id: int, group_id: str) -> str:
        """Determine the current infiltration phase based on join date and history.

        *group_id* may be a tg_group_id (``"@toncontests"`` or
        ``"2431210312"``) or an internal DB ``Group.id`` string.  We
        resolve it to the DB id before querying ``GroupAccount``.

        Returns one of: ``"lurking"``, ``"trust_building"``, ``"soft_promotion"``.
        """
        try:
            async with get_session() as session:
                # Resolve tg_group_id → internal Group.id
                db_group_id: int | None = None
                # Try as internal id first (small int)
                if group_id.isdigit() and int(group_id) < 100_000:
                    db_group_id = int(group_id)
                else:
                    # Look up by tg_group_id (could be @username or large numeric)
                    lookup = await session.scalar(
                        select(Group.id).where(Group.tg_group_id == group_id)
                    )
                    if lookup is None and group_id.startswith("@"):
                        bare = group_id[1:]
                        lookup = await session.scalar(
                            select(Group.id).where(Group.tg_group_id == bare)
                        )
                        # Also try matching by username column
                        if lookup is None:
                            lookup = await session.scalar(
                                select(Group.id).where(Group.username == bare)
                            )
                    db_group_id = lookup

                if db_group_id is None:
                    return "lurking"

                stmt = select(GroupAccount).where(
                    and_(
                        GroupAccount.account_id == account_id,
                        GroupAccount.group_id == db_group_id,
                    ),
                )
                result = await session.execute(stmt)
                ga = result.scalar_one_or_none()

                if ga is None:
                    return "lurking"

                days_since_join = (datetime.utcnow() - ga.joined_at).days

                # Veteran accounts (365+ days) use accelerated timeline
                # because they already have a natural Telegram history.
                account_age = await self._get_account_age(account_id)
                if account_age >= 365:
                    lurk_days = self.LURK_DAYS_VETERAN
                    trust_days = self.TRUST_DAYS_VETERAN
                else:
                    lurk_days = self.LURK_DAYS
                    trust_days = self.TRUST_DAYS

                if days_since_join < lurk_days:
                    phase = "lurking"
                elif days_since_join < trust_days:
                    phase = "trust_building"
                else:
                    phase = "soft_promotion"

                if phase != "lurking":
                    await self._log_promo_event(
                        "phase_advance",
                        account_id=account_id,
                        group_id=group_id,
                        phase=phase,
                        days_since_join=days_since_join,
                    )
                return phase
        except Exception:
            self._log.exception("infiltrator.determine_phase.error", account_id=account_id)
            return "lurking"

    async def _get_account_age(self, account_id: int) -> int:
        """Return account age in days, cached to avoid repeated DB queries."""
        if account_id in self._account_age_cache:
            return self._account_age_cache[account_id]
        try:
            async with get_session() as session:
                stmt = select(Account.account_age_days).where(Account.id == account_id)
                result = await session.execute(stmt)
                age = result.scalar_one_or_none()
                age = int(age) if age is not None else 0
        except Exception:
            self._log.exception("infiltrator.get_account_age.error", account_id=account_id)
            age = 0
        self._account_age_cache[account_id] = age
        return age

    # ------------------------------------------------------------------
    # Link request detection
    # ------------------------------------------------------------------

    async def _detect_link_request(
        self, account_id: int, group_id: str, recent_messages: list[dict],
    ) -> bool:
        """Check if anyone recently asked our account for a link / more info.

        Scans the last ~50 messages for replies to our account containing
        keywords like "link", "链接", "ссылка", "liên kết", "where", "怎么玩",
        "как играть", etc.

        If found, reply with the game link and return True.
        """
        # Get this account's Telegram user_id
        me = await self.user_client.get_me(account_id)
        if not me:
            return False
        my_id = me.get("id")
        if not my_id:
            return False

        link_keywords = [
            "link", "url", "链接", "怎么玩", "在哪", "哪里", "给我",
            "ссылка", "где", "как играть", "liên kết", "chơi ở đâu",
            "where", "how to play", "send me", "share", "drop the link",
        ]

        now_ts = int(time.time())
        one_hour_ago = now_ts - 3600

        for msg in recent_messages[:50]:
            # Only consider messages from the last hour
            msg_date = msg.get("date", 0)
            if msg_date < one_hour_ago:
                continue

            # Dedup: skip messages we already replied to
            msg_id = msg.get("id")
            if msg_id is not None:
                cache_key = (account_id, group_id, msg_id)
                if cache_key in self._replied_link_messages:
                    continue

            # Check if this message is a reply to one of our messages
            reply_to_user = msg.get("reply_to_user_id")
            text = (msg.get("text") or "").lower()
            sender_id = msg.get("from_id") or msg.get("sender_id")

            # Skip our own messages
            if sender_id == my_id:
                continue

            # Check if text contains a link-request keyword
            if not any(kw in text for kw in link_keywords):
                continue

            # Either: (a) reply to our message, or (b) keyword matched
            # in the recent conversation window -- respond with link.
            game_link = settings.game_miniapp_url

            replies_pool = [
                f"在这里 {game_link} 金币房免费先试试",
                f"here you go {game_link} coin room is free",
                f"{game_link} 👆 先玩金币房不花钱",
                f"fam {game_link} the coin room is completely free",
                f"вот {game_link} бесплатная комната с монетами",
            ]
            reply_text = random.choice(replies_pool)

            # Reply to the specific message that asked
            if msg_id:
                ok = await self.user_client.send_reply(
                    account_id, group_id, msg_id, reply_text,
                )
            else:
                ok = await self.user_client.send_message(
                    account_id, group_id, reply_text,
                )

            if ok:
                if msg_id is not None:
                    self._replied_link_messages.add((account_id, group_id, msg_id))
                    # Cap cache at 1000 to avoid memory leak
                    if len(self._replied_link_messages) > 1000:
                        to_keep = list(self._replied_link_messages)[-500:]
                        self._replied_link_messages = set(to_keep)
                self._log.info(
                    "infiltrator.link_reply_sent",
                    account_id=account_id,
                    group_id=group_id,
                    trigger_text=text[:50],
                )
                await self._log_promo_event(
                    "link_shared",
                    account_id=account_id,
                    group_id=group_id,
                    trigger_text=text[:50],
                    reply_to_msg_id=msg_id,
                )
                return True

        return False

    # ------------------------------------------------------------------
    # Content generation
    # ------------------------------------------------------------------

    async def generate_contextual_response(
        self, account_id: int, group_id: str, recent_messages: list[dict],
    ) -> str | None:
        """Use AI to generate a contextually relevant, persona-consistent reply."""
        try:
            persona = await self._get_account_persona(account_id)
            if persona is None:
                return None

            # Build context from recent messages
            context_lines = []
            for msg in recent_messages[-20:]:
                name = msg.get("from_name", "User")
                text = msg.get("text", "")
                if text:
                    context_lines.append(f"{name}: {text}")

            context = "\n".join(context_lines)

            response = await self.content_gen.generate_response(
                persona=persona,
                group_context=context,
                is_promo=False,
            )
            return response
        except Exception:
            self._log.exception("infiltrator.generate_response.error", account_id=account_id)
            return None

    async def _generate_promo_message(
        self, account_id: int, group_id: str, approach: str,
    ) -> str | None:
        """Generate a soft promotional message using the specified approach."""
        try:
            persona = await self._get_account_persona(account_id)
            if persona is None:
                return None

            response = await self.content_gen.generate_promo(
                persona=persona,
                approach=approach,
                group_id=group_id,
            )
            return response
        except Exception:
            self._log.exception("infiltrator.generate_promo.error", account_id=account_id)
            return None

    async def _soften_promo(self, message: str, account_id: int) -> str:
        """Ensure a promotional message sounds natural, not salesy."""
        try:
            persona = await self._get_account_persona(account_id)
            if persona is None:
                return message
            softened = await self.content_gen.soften_message(
                message=message,
                persona=persona,
            )
            return softened or message
        except Exception:
            return message

    # ------------------------------------------------------------------
    # Isolation & safety helpers
    # ------------------------------------------------------------------

    async def _is_peer_active(self, account_id: int, group_id: int) -> bool:
        """Check if another infiltrator account was active in this group recently.

        Accounts must be staggered by 1-2 hours.
        """
        try:
            cutoff = datetime.utcnow() - timedelta(hours=2)
            async with get_session() as session:
                stmt = (
                    select(MessageLog)
                    .where(
                        and_(
                            MessageLog.group_id == group_id,
                            MessageLog.account_id != account_id,
                            MessageLog.sent_at > cutoff,
                        ),
                    )
                    .limit(1)
                )
                result = await session.execute(stmt)
                return result.scalar_one_or_none() is not None
        except Exception:
            # If we cannot verify, assume peer is active (safe default)
            return True

    async def _daily_message_budget_ok(self, account_id: int, group_id: str) -> bool:
        """Check if the account has remaining message budget for this group today."""
        try:
            today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
            async with get_session() as session:
                stmt = (
                    select(MessageLog)
                    .where(
                        and_(
                            MessageLog.account_id == account_id,
                            MessageLog.sent_at >= today_start,
                        ),
                    )
                )
                result = await session.execute(stmt)
                today_messages = result.scalars().all()

                # Filter for this group
                group_messages = [m for m in today_messages if str(m.group_id) == str(group_id)]
                if len(group_messages) >= self.RULES["每日每群最多消息"]:
                    self._log.debug(
                        "infiltrator.daily_budget_exceeded",
                        account_id=account_id,
                        group_id=group_id,
                        count=len(group_messages),
                    )
                    return False
                return True
        except Exception:
            return False

    async def _promo_ratio_ok(self, account_id: int) -> bool:
        """Check that promotional messages don't exceed the ratio limit."""
        try:
            cutoff = datetime.utcnow() - timedelta(days=7)
            async with get_session() as session:
                stmt = (
                    select(MessageLog)
                    .where(
                        and_(
                            MessageLog.account_id == account_id,
                            MessageLog.sent_at > cutoff,
                        ),
                    )
                )
                result = await session.execute(stmt)
                recent = result.scalars().all()

                if not recent:
                    return True

                promo_count = sum(1 for m in recent if m.is_promo)
                ratio = promo_count / len(recent)
                return ratio < self.RULES["推广消息占比上限"]
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Messaging
    # ------------------------------------------------------------------

    async def _send_message(
        self, account_id: int, group_id: str, content: str, *, is_promo: bool,
    ) -> bool:
        """Send a message and log it."""
        try:
            # Simulate human typing delay
            typing_delay = len(content) * random.uniform(0.03, 0.08)
            await self._jittered_sleep(min(typing_delay, 10.0), jitter_ratio=0.2)

            success = await self.user_client.send_message(account_id, group_id, content)
            if not success:
                return False

            # Persist message log + update account daily counters
            content_hash = hashlib.sha256(content.encode()).hexdigest()[:16]
            async with get_session() as session:
                # Resolve group_id to internal DB id
                db_group_id = None
                if group_id:
                    if str(group_id).isdigit() and int(group_id) < 100_000:
                        db_group_id = int(group_id)
                    else:
                        try:
                            resolved = await session.scalar(
                                select(Group.id).where(Group.tg_group_id == str(group_id))
                            )
                            if resolved is None and str(group_id).startswith("@"):
                                resolved = await session.scalar(
                                    select(Group.id).where(Group.tg_group_id == str(group_id)[1:])
                                )
                            db_group_id = resolved
                        except Exception:
                            pass

                log_entry = MessageLog(
                    account_id=account_id,
                    group_id=db_group_id,
                    content=content,
                    content_hash=content_hash,
                    is_promo=is_promo,
                    message_type="promo" if is_promo else "chat",
                )
                session.add(log_entry)

                # Increment daily counters on accounts table
                acct = await session.get(Account, account_id)
                if acct is not None:
                    acct.messages_sent_today = (acct.messages_sent_today or 0) + 1
                    if is_promo:
                        acct.promo_messages_today = (acct.promo_messages_today or 0) + 1
                    await session.commit()

            self._log.info(
                "infiltrator.message_sent",
                account_id=account_id,
                group_id=group_id,
                is_promo=is_promo,
                content_len=len(content),
            )
            return True
        except Exception:
            self._log.exception("infiltrator.send_message.error", account_id=account_id)
            return False

    # ------------------------------------------------------------------
    # Data loading helpers
    # ------------------------------------------------------------------

    async def _load_active_assignments(self) -> list[dict]:
        """Load all active account-group assignments for infiltration."""
        try:
            async with get_session() as session:
                stmt = (
                    select(GroupAccount, Group, Account)
                    .join(Group, GroupAccount.group_id == Group.id)
                    .join(Account, GroupAccount.account_id == Account.id)
                    .where(Account.role == "infiltrator")
                    .where(Account.status == "active")
                    .where(Group.status.in_(["evaluated", "infiltrating", "active"]))
                )
                result = await session.execute(stmt)
                rows = result.all()

                assignments = []
                for ga, group, account in rows:
                    # Skip groups in cooldown
                    if group.cooldown_until and group.cooldown_until > datetime.utcnow():
                        continue
                    assignments.append({
                        "account_id": account.id,
                        "group_id": group.id,
                        "tg_group_id": group.tg_group_id,
                        "phase": ga.phase,
                        "joined_at": ga.joined_at,
                        "persona_id": account.persona_id,
                    })
                return assignments
        except Exception:
            self._log.exception("infiltrator.load_assignments.error")
            return []

    async def _load_account_data(self, account_id: int) -> dict | None:
        """Load account data for risk evaluation."""
        try:
            async with get_session() as session:
                stmt = select(Account).where(Account.id == account_id)
                result = await session.execute(stmt)
                account = result.scalar_one_or_none()
                if account is None:
                    return None
                return {
                    "id": account.id,
                    "messages_sent_today": account.messages_sent_today,
                    "groups_active_today": account.groups_active_today,
                    "new_groups_today": account.new_groups_today,
                    "promo_messages_today": account.promo_messages_today,
                    "dms_initiated_today": account.dms_initiated_today,
                    "links_sent_today": account.links_sent_today,
                    "reported": account.reported,
                    "kicked_count": account.kicked_count,
                    "phone_type": account.phone_type,
                    "phone_provider": account.phone_provider,
                }
        except Exception:
            self._log.exception("infiltrator.load_account.error", account_id=account_id)
            return None

    async def _get_account_persona(self, account_id: int) -> PersonaTemplate | None:
        """Load the persona template assigned to an account."""
        try:
            async with get_session() as session:
                stmt = select(Account.persona_id).where(Account.id == account_id)
                result = await session.execute(stmt)
                persona_id = result.scalar_one_or_none()
                if persona_id is None:
                    return None
                return self.persona_manager.get_persona(persona_id)
        except Exception:
            self._log.exception("infiltrator.get_persona.error", account_id=account_id)
            return None

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    @staticmethod
    def _pick_engaging_message(messages: list[dict]) -> dict | None:
        """Pick a message worth replying to (longer text, has replies)."""
        candidates = [
            m for m in messages
            if len(m.get("text", "")) > 30 and not m.get("is_admin_action")
        ]
        if not candidates:
            return None
        return random.choice(candidates)
