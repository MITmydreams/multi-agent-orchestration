"""Coordinated dual-account chat strategy for natural-looking outreach.

When two or more of our accounts are present in the same group, this module
orchestrates a scripted but natural-sounding conversation between them.  The
dialogue follows a question-answer pattern that organically introduces the
promoted project/game, making the interaction appear genuine to other group
members and admins.

Safety constraints:
    - Same pair in same group: max 1 coordinated chat per day.
    - Inter-message delay: 3-10 minutes (randomised).
    - Initiator role alternates (A does not always start).
    - Coordinated messages count toward each account's daily message limit.
    - Outreachal links appear in only ~30% of coordinated conversations.
"""

from __future__ import annotations

import hashlib
import random
from datetime import datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import select, and_, func as sa_func

from src.config import settings
from src.models import Account, Group, GroupAccount, MessageLog, get_session

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Conversation templates
# ---------------------------------------------------------------------------
# Each template is a list of message dicts with keys:
#   role: "A" or "B"
#   text: message content (may contain {game_url} placeholder)
#
# Templates are grouped by scenario.  The {game_url} placeholder is
# replaced with settings.product_app_url when a link is included.

_TEMPLATES_EN: list[dict[str, Any]] = [
    # --- Scenario 1: Discovering mini apps ---
    {
        "id": "discover_miniapps",
        "messages": [
            {"role": "A", "text": "Anyone tried those new Telegram mini apps? Looking for something fun"},
            {"role": "B", "text": "Yeah I've been playing Atlas lately, it's pretty addictive lol"},
            {"role": "A", "text": "Oh nice, is it a clicker or strategy game?"},
            {"role": "B", "text": "Kind of both, you tap but there's a social experiment element. Check {game_url}"},
        ],
    },
    # --- Scenario 2: Bored looking for games ---
    {
        "id": "bored_looking",
        "messages": [
            {"role": "A", "text": "so bored rn, anyone got a good mobile game to kill time?"},
            {"role": "B", "text": "try Atlas on telegram, I've been hooked for a week now"},
            {"role": "A", "text": "is it free?"},
            {"role": "B", "text": "yeah totally free to start, you earn coins just by tapping"},
        ],
    },
    # --- Scenario 3: Tech game discussion ---
    {
        "id": "tech_game_discuss",
        "messages": [
            {"role": "A", "text": "are there any legit tech games that aren't just ponzis?"},
            {"role": "B", "text": "I mean most are sketchy but Atlas is pretty transparent about their model"},
            {"role": "A", "text": "what makes it different?"},
            {"role": "B", "text": "it's a social game, the tokenomics are simple and you can actually see how the pool works"},
        ],
    },
    # --- Scenario 4: Opportunity scanning ---
    {
        "id": "announcement_hunting",
        "messages": [
            {"role": "A", "text": "anyone know good announcements coming up? everything I find is already over"},
            {"role": "B", "text": "not exactly an announcement but Atlas gives out free coins daily, been stacking"},
            {"role": "A", "text": "sounds interesting, how much can you actually get?"},
        ],
    },
    # --- Scenario 5: TON ecosystem chat ---
    {
        "id": "ton_ecosystem",
        "messages": [
            {"role": "A", "text": "what's everyone building on TON these days? feels like there's a lot happening"},
            {"role": "B", "text": "mostly mini apps, Atlas is one of the more popular ones recently"},
            {"role": "A", "text": "oh yeah I saw someone mention that, what's the concept?"},
            {"role": "B", "text": "social experiment meets clicker game, pretty unique actually {game_url}"},
        ],
    },
    # --- Scenario 6: Earning discussion ---
    {
        "id": "earning_discussion",
        "messages": [
            {"role": "A", "text": "is it still possible to earn anything from telegram games or is it all hype?"},
            {"role": "B", "text": "depends on the game, some are legit. I've been doing ok with Atlas"},
            {"role": "A", "text": "what's your daily like from it?"},
            {"role": "B", "text": "nothing crazy but consistent, the coin room is free so no risk at least"},
        ],
    },
    # --- Scenario 7: Game recommendation ---
    {
        "id": "game_recommend",
        "messages": [
            {"role": "A", "text": "what games are you guys playing this week?"},
            {"role": "B", "text": "been into Atlas, it's a telegram mini app"},
            {"role": "A", "text": "never heard of it, worth trying?"},
            {"role": "B", "text": "yeah it's fun and you can earn while playing, pretty chill"},
        ],
    },
    # --- Scenario 8: Comparing games ---
    {
        "id": "comparing_games",
        "messages": [
            {"role": "A", "text": "hamster kombat vs notcoin vs all these clickers, which one is actually good?"},
            {"role": "B", "text": "honestly I moved on to Atlas, less grindy and more interesting mechanic"},
            {"role": "A", "text": "less grindy how?"},
            {"role": "B", "text": "you don't need to tap 10k times, it's more about timing and strategy"},
        ],
    },
    # --- Scenario 9: New to tech games ---
    {
        "id": "new_to_tech",
        "messages": [
            {"role": "A", "text": "just got into tech games, where should I start?"},
            {"role": "B", "text": "start with something low risk, Atlas is free to play and teaches you the basics"},
            {"role": "A", "text": "thanks, is it on telegram?"},
            {"role": "B", "text": "yep it's a mini app, super easy to get started"},
        ],
    },
    # --- Scenario 10: Weekend plans ---
    {
        "id": "weekend_plans",
        "messages": [
            {"role": "A", "text": "lazy weekend, need something to do besides scrolling twitter"},
            {"role": "B", "text": "lol same, I've been playing Atlas when I'm bored"},
            {"role": "A", "text": "that fun?"},
            {"role": "B", "text": "surprisingly yeah, it's like a social experiment where you compete with others"},
        ],
    },
    # --- Scenario 11: Discussing dividends ---
    {
        "id": "dividends_talk",
        "messages": [
            {"role": "A", "text": "anyone know projects with actual revenue sharing?"},
            {"role": "B", "text": "Atlas has a pool model, not exactly dividends but you get returns based on timing"},
            {"role": "A", "text": "interesting, is it sustainable though?"},
            {"role": "B", "text": "been running for a while now, the math checks out from what I've seen"},
        ],
    },
    # --- Scenario 12: Skeptical gamer ---
    {
        "id": "skeptical_gamer",
        "messages": [
            {"role": "A", "text": "every tech game I try is a waste of time, prove me wrong"},
            {"role": "B", "text": "haha fair, but Atlas is different because it's actually fun to play even without the tech part"},
            {"role": "A", "text": "ok that's a first, might check it out"},
        ],
    },
    # --- Scenario 13: Mini app trend ---
    {
        "id": "miniapp_trend",
        "messages": [
            {"role": "A", "text": "telegram mini apps are blowing up huh"},
            {"role": "B", "text": "yeah there's a few good ones, Atlas is one I keep coming back to"},
            {"role": "A", "text": "what's the gameplay like?"},
            {"role": "B", "text": "you press a button and try to be the last one, sounds simple but it gets intense"},
        ],
    },
    # --- Scenario 14: Tech winter chat ---
    {
        "id": "tech_winter",
        "messages": [
            {"role": "A", "text": "market is so dead rn, nothing to trade"},
            {"role": "B", "text": "same, I've been filling time with Atlas game on tg"},
            {"role": "A", "text": "at least it's something lol"},
            {"role": "B", "text": "better than staring at red charts all day haha"},
        ],
    },
    # --- Scenario 15: product discussion ---
    {
        "id": "community_discuss",
        "messages": [
            {"role": "A", "text": "is community still a thing or did it die in 2023?"},
            {"role": "B", "text": "it evolved, telegram mini games are the new wave. Atlas is solid"},
            {"role": "A", "text": "never tried any tg games, are they smooth?"},
            {"role": "B", "text": "way better than I expected, no app download needed just open and play"},
        ],
    },
    # --- Scenario 16: Morning chat ---
    {
        "id": "morning_chat",
        "messages": [
            {"role": "A", "text": "gm everyone, what's the move today?"},
            {"role": "B", "text": "gm, probably just grinding Atlas and chilling"},
            {"role": "A", "text": "you play that every day?"},
            {"role": "B", "text": "pretty much yeah, only takes a few minutes and the rewards add up"},
        ],
    },
    # --- Scenario 17: Play to earn fatigue ---
    {
        "id": "p2e_fatigue",
        "messages": [
            {"role": "A", "text": "I'm so tired of play-to-earn games that require 3 hours daily"},
            {"role": "B", "text": "try Atlas then, it's like 5 minutes and you're done"},
            {"role": "A", "text": "wait really? and you still earn?"},
            {"role": "B", "text": "yep, that's the whole point. quick sessions, still get coins"},
        ],
    },
    # --- Scenario 18: Wallet discussion ---
    {
        "id": "wallet_discuss",
        "messages": [
            {"role": "A", "text": "just set up my TON wallet, now what do I do with it?"},
            {"role": "B", "text": "nice, you can try some mini apps. Atlas is a good first one"},
            {"role": "A", "text": "does it connect to the wallet?"},
            {"role": "B", "text": "yeah it works with telegram wallet directly, pretty seamless"},
        ],
    },
    # --- Scenario 19: Group meta discussion ---
    {
        "id": "group_meta",
        "messages": [
            {"role": "A", "text": "this group been quiet lately, what's everyone up to?"},
            {"role": "B", "text": "been busy with Atlas tbh, takes up my gaming time now"},
            {"role": "A", "text": "haha sounds like I should try it"},
        ],
    },
    # --- Scenario 20: Strategy discussion ---
    {
        "id": "strategy_discuss",
        "messages": [
            {"role": "A", "text": "anyone got tips for timing in clicker games? I always miss the good windows"},
            {"role": "B", "text": "in Atlas it's all about patience, don't press too early"},
            {"role": "A", "text": "oh you play that? is timing really that important?"},
            {"role": "B", "text": "huge, the last person to press wins so you gotta read the room"},
        ],
    },
    # --- Scenario 21: Late night chat ---
    {
        "id": "late_night",
        "messages": [
            {"role": "A", "text": "can't sleep, anyone else up?"},
            {"role": "B", "text": "yeah playing Atlas, it's actually better at night less competition"},
            {"role": "A", "text": "smart, less people pressing?"},
            {"role": "B", "text": "exactly, better odds when the crowd is smaller"},
        ],
    },
    # --- Scenario 22: Referral chat ---
    {
        "id": "referral_chat",
        "messages": [
            {"role": "A", "text": "looking for something to do with friends on telegram, any suggestions?"},
            {"role": "B", "text": "Atlas is fun with friends, you can compete against each other"},
            {"role": "A", "text": "oh that sounds cool, group thing?"},
            {"role": "B", "text": "yeah kind of, everyone's in the same pool trying to be last to press"},
        ],
    },
    # --- Scenario 23: Investment comparison ---
    {
        "id": "investment_compare",
        "messages": [
            {"role": "A", "text": "what's giving better returns rn, staking or gaming?"},
            {"role": "B", "text": "gaming if you pick the right one. Atlas pool has been decent"},
            {"role": "A", "text": "interesting, how does the pool work?"},
            {"role": "B", "text": "everyone puts in, last to press takes the biggest share. simple but effective"},
        ],
    },
]

_TEMPLATES_RU: list[dict[str, Any]] = [
    {
        "id": "ru_discover",
        "messages": [
            {"role": "A", "text": "Кто-нибудь пробовал мини-приложения в телеграме? Ищу что-нибудь интересное"},
            {"role": "B", "text": "Да, играю в Atlas последнюю неделю, залипательная штука"},
            {"role": "A", "text": "Что за игра, кликер?"},
            {"role": "B", "text": "Типа кликера но с элементом социального эксперимента, прикольно"},
        ],
    },
    {
        "id": "ru_bored",
        "messages": [
            {"role": "A", "text": "скучно, чем заняться в тг?"},
            {"role": "B", "text": "попробуй Atlas, я там уже неделю сижу"},
            {"role": "A", "text": "это бесплатно?"},
            {"role": "B", "text": "да, монетная комната бесплатная, просто заходишь и играешь"},
        ],
    },
    {
        "id": "ru_earning",
        "messages": [
            {"role": "A", "text": "реально ещё можно заработать на тг играх?"},
            {"role": "B", "text": "зависит от игры, в Atlas вполне нормально выходит"},
            {"role": "A", "text": "сколько примерно в день?"},
            {"role": "B", "text": "немного но стабильно, главное бесплатно и без рисков"},
        ],
    },
]

_TEMPLATES_VI: list[dict[str, Any]] = [
    {
        "id": "vi_discover",
        "messages": [
            {"role": "A", "text": "Ai choi mini app tren Telegram chua? Tim cai gi vui vui"},
            {"role": "B", "text": "Minh dang choi Atlas, kha la ghien"},
            {"role": "A", "text": "Game gi vay? Co mien phi khong?"},
            {"role": "B", "text": "Mien phi luon, chi can bam nut va canh tranh voi nguoi khac"},
        ],
    },
    {
        "id": "vi_earning",
        "messages": [
            {"role": "A", "text": "con kiem tien duoc tren Telegram khong nhi?"},
            {"role": "B", "text": "Atlas cho coin moi ngay, minh choi duoc mot thoi gian roi"},
            {"role": "A", "text": "nghe hay do, co kho khong?"},
            {"role": "B", "text": "de lam, chi can vai phut moi ngay thoi"},
        ],
    },
]

_ALL_TEMPLATES: dict[str, list[dict[str, Any]]] = {
    "en": _TEMPLATES_EN,
    "ru": _TEMPLATES_RU,
    "vi": _TEMPLATES_VI,
}


# ---------------------------------------------------------------------------
# Core strategy class
# ---------------------------------------------------------------------------

class CoordinatedChatStrategy:
    """Orchestrates dual-account conversations in shared groups.

    Typical usage inside ``ExecutorAgent``::

        strategy = CoordinatedChatStrategy(user_client)
        pairs = await strategy.find_coordinated_pairs()
        for group_id, account_a, account_b in pairs:
            if await strategy.should_execute(group_id, account_a, account_b):
                script = strategy.generate_conversation(group_id, account_a, account_b)
                await strategy.execute_conversation(group_id, account_a, account_b, script)
    """

    def __init__(self, user_client: Any) -> None:
        self.user_client = user_client
        self._log = logger.bind(component="coordinated_chat")

    # ------------------------------------------------------------------
    # 1. Find eligible pairs
    # ------------------------------------------------------------------

    async def find_coordinated_pairs(self) -> list[tuple[int, int, int]]:
        """Return ``(db_group_id, account_a_id, account_b_id)`` tuples.

        Scans ``group_accounts`` for groups containing 2+ active executor
        accounts in ``trust_building`` or ``soft_outreach`` phase.
        """
        pairs: list[tuple[int, int, int]] = []
        try:
            async with get_session() as session:
                # Find groups with 2+ active executor accounts
                subq = (
                    select(
                        GroupAccount.group_id,
                        sa_func.count(GroupAccount.account_id).label("cnt"),
                    )
                    .join(Account, GroupAccount.account_id == Account.id)
                    .where(
                        and_(
                            Account.role == "executor",
                            Account.status == "active",
                            GroupAccount.phase.in_(["trust_building", "soft_outreach"]),
                        ),
                    )
                    .group_by(GroupAccount.group_id)
                    .having(sa_func.count(GroupAccount.account_id) >= 2)
                    .subquery()
                )

                # For each qualifying group, fetch the account ids
                stmt = (
                    select(GroupAccount.group_id, GroupAccount.account_id)
                    .join(Account, GroupAccount.account_id == Account.id)
                    .join(subq, GroupAccount.group_id == subq.c.group_id)
                    .where(
                        and_(
                            Account.role == "executor",
                            Account.status == "active",
                            GroupAccount.phase.in_(["trust_building", "soft_outreach"]),
                        ),
                    )
                    .order_by(GroupAccount.group_id, GroupAccount.account_id)
                )
                result = await session.execute(stmt)
                rows = result.all()

                # Group accounts by group_id
                group_accounts: dict[int, list[int]] = {}
                for row in rows:
                    gid = row.group_id
                    aid = row.account_id
                    group_accounts.setdefault(gid, []).append(aid)

                # Pick one pair per group (random selection if 3+ accounts)
                for gid, accounts in group_accounts.items():
                    if len(accounts) < 2:
                        continue
                    chosen = random.sample(accounts, 2)
                    # Randomise who initiates (A vs B)
                    if random.random() < 0.5:
                        chosen.reverse()
                    pairs.append((gid, chosen[0], chosen[1]))

        except Exception:
            self._log.exception("coordinated_chat.find_pairs.error")

        self._log.info("coordinated_chat.pairs_found", count=len(pairs))
        return pairs

    # ------------------------------------------------------------------
    # 2. Guard: should we execute today?
    # ------------------------------------------------------------------

    async def should_execute(
        self, group_id: int, account_a: int, account_b: int,
    ) -> bool:
        """Return True if this pair has not done a coordinated chat today.

        Also checks that both accounts still have daily message budget.
        """
        try:
            today_start = datetime.utcnow().replace(
                hour=0, minute=0, second=0, microsecond=0,
            )
            async with get_session() as session:
                # Check if either account already participated in a coordinated
                # chat in this group today (message_type = 'coordinated').
                stmt = (
                    select(sa_func.count())
                    .select_from(MessageLog)
                    .where(
                        and_(
                            MessageLog.group_id == group_id,
                            MessageLog.account_id.in_([account_a, account_b]),
                            MessageLog.message_type == "coordinated",
                            MessageLog.sent_at >= today_start,
                        ),
                    )
                )
                result = await session.execute(stmt)
                coord_count = int(result.scalar_one() or 0)
                if coord_count > 0:
                    self._log.debug(
                        "coordinated_chat.already_done_today",
                        group_id=group_id,
                        account_a=account_a,
                        account_b=account_b,
                    )
                    return False

                # Check daily message budget for both accounts
                for aid in (account_a, account_b):
                    msg_count = await session.scalar(
                        select(sa_func.count())
                        .select_from(MessageLog)
                        .where(
                            and_(
                                MessageLog.account_id == aid,
                                MessageLog.sent_at >= today_start,
                            ),
                        ),
                    ) or 0
                    if int(msg_count) >= settings.max_messages_per_day:
                        self._log.debug(
                            "coordinated_chat.daily_limit_hit",
                            account_id=aid,
                            msg_count=msg_count,
                        )
                        return False

                return True

        except Exception:
            self._log.exception("coordinated_chat.should_execute.error")
            return False

    # ------------------------------------------------------------------
    # 3. Generate conversation script
    # ------------------------------------------------------------------

    def generate_conversation(
        self,
        group_id: int,
        account_a: int,
        account_b: int,
        language: str = "en",
    ) -> list[dict[str, Any]]:
        """Pick a random template and resolve it into a send-ready script.

        Returns a list of dicts::

            [
                {"account_id": 123, "text": "...", "is_outreach": False},
                {"account_id": 456, "text": "...", "is_outreach": False},
                ...
            ]

        The outreach link is included in only ~30% of conversations.
        """
        templates = _ALL_TEMPLATES.get(language, _TEMPLATES_EN)
        template = random.choice(templates)
        messages = template["messages"]

        include_link = random.random() < 0.30
        game_url = settings.product_app_url

        script: list[dict[str, Any]] = []
        for msg in messages:
            account_id = account_a if msg["role"] == "A" else account_b
            text = msg["text"]

            if include_link:
                text = text.replace("{game_url}", game_url)
            else:
                # Remove link placeholders (and surrounding "Check " etc.)
                text = text.replace("{game_url}", "")
                # Clean up trailing whitespace left by placeholder removal
                text = text.rstrip()
                # If removal made the message empty or too short, use a filler
                if len(text) < 5:
                    text = "yeah sounds cool, I'll look into it"

            is_outreach = include_link and game_url in text
            script.append({
                "account_id": account_id,
                "text": text,
                "is_outreach": is_outreach,
                "template_id": template["id"],
            })

        self._log.info(
            "coordinated_chat.script_generated",
            group_id=group_id,
            template_id=template["id"],
            messages=len(script),
            include_link=include_link,
        )
        return script

    # ------------------------------------------------------------------
    # 4. Execute conversation
    # ------------------------------------------------------------------

    async def execute_conversation(
        self,
        group_id: int,
        account_a: int,
        account_b: int,
        script: list[dict[str, Any]],
    ) -> bool:
        """Send the scripted messages with realistic delays between them.

        Each message is logged to ``message_logs`` with
        ``message_type='coordinated'`` so the daily-budget and
        once-per-day guards work correctly.

        Returns True if all messages were sent successfully.
        """
        if not script:
            return False

        # Resolve tg_group_id from DB group id
        tg_group_id: str | None = None
        try:
            async with get_session() as session:
                stmt = select(Group.tg_group_id, Group.username).where(Group.id == group_id)
                result = await session.execute(stmt)
                row = result.one_or_none()
                if row:
                    if row.username:
                        tg_group_id = (
                            f"@{row.username}"
                            if not row.username.startswith("@")
                            else row.username
                        )
                    elif row.tg_group_id:
                        tg_group_id = row.tg_group_id
        except Exception:
            self._log.exception(
                "coordinated_chat.resolve_group.error", group_id=group_id,
            )
            return False

        if not tg_group_id:
            self._log.warning(
                "coordinated_chat.no_tg_group_id", group_id=group_id,
            )
            return False

        self._log.info(
            "coordinated_chat.execute.start",
            group_id=group_id,
            tg_group_id=tg_group_id,
            account_a=account_a,
            account_b=account_b,
            msg_count=len(script),
        )

        import asyncio

        all_ok = True
        for i, step in enumerate(script):
            aid = step["account_id"]
            text = step["text"]
            is_outreach = step.get("is_outreach", False)

            # Inter-message delay (skip before the first message)
            if i > 0:
                delay_minutes = random.uniform(
                    settings.coordinated_chat_min_interval_minutes,
                    settings.coordinated_chat_max_interval_minutes,
                )
                delay_seconds = delay_minutes * 60
                # Add some jitter
                delay_seconds += random.uniform(-30, 30)
                delay_seconds = max(60, delay_seconds)  # at least 1 minute

                self._log.debug(
                    "coordinated_chat.waiting",
                    delay_seconds=round(delay_seconds, 1),
                    step=i,
                )
                await asyncio.sleep(delay_seconds)

            # Simulate typing delay
            typing_delay = len(text) * random.uniform(0.03, 0.08)
            await asyncio.sleep(min(typing_delay, 10.0))

            # Send message
            try:
                success = await self.user_client.send_message(aid, tg_group_id, text)
            except Exception:
                self._log.exception(
                    "coordinated_chat.send.error",
                    account_id=aid,
                    group_id=group_id,
                    step=i,
                )
                success = False

            if not success:
                self._log.warning(
                    "coordinated_chat.send.failed",
                    account_id=aid,
                    group_id=group_id,
                    step=i,
                )
                all_ok = False
                break  # Abort remaining messages to avoid suspicious partial convos

            # Log the message to message_logs
            await self._log_message(aid, group_id, text, is_outreach=is_outreach)

            self._log.info(
                "coordinated_chat.send.ok",
                account_id=aid,
                group_id=group_id,
                step=i,
                text_len=len(text),
            )

        if all_ok:
            self._log.info(
                "coordinated_chat.execute.done",
                group_id=group_id,
                account_a=account_a,
                account_b=account_b,
            )
        return all_ok

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _log_message(
        self, account_id: int, group_id: int, content: str, *, is_outreach: bool,
    ) -> None:
        """Persist a coordinated message to ``message_logs`` and bump counters."""
        try:
            content_hash = hashlib.sha256(content.encode()).hexdigest()[:16]
            async with get_session() as session:
                log_entry = MessageLog(
                    account_id=account_id,
                    group_id=group_id,
                    content=content,
                    content_hash=content_hash,
                    is_outreach=is_outreach,
                    message_type="coordinated",
                )
                session.add(log_entry)

                # Increment daily counters on accounts table
                acct = await session.get(Account, account_id)
                if acct is not None:
                    acct.messages_sent_today = (acct.messages_sent_today or 0) + 1
                    if is_outreach:
                        acct.outreach_messages_today = (acct.outreach_messages_today or 0) + 1
                    await session.commit()
        except Exception:
            self._log.exception(
                "coordinated_chat.log_message.error",
                account_id=account_id,
                group_id=group_id,
            )

    async def try_coordinated_chat(
        self,
        account_id: int,
        group_id: int,
        tg_group_id: str,
        language: str = "en",
    ) -> bool:
        """High-level entry point for the executor agent.

        Called when the agent decides to attempt a coordinated chat instead of
        a regular single-account message.  Finds a partner account in the same
        group, generates a script, and executes it.

        Returns True if a coordinated chat was executed (even partially).
        Returns False if no partner was found or guards blocked execution.
        """
        if not settings.coordinated_chat_enabled:
            return False

        # Find a partner in the same group
        partner_id: int | None = None
        try:
            async with get_session() as session:
                stmt = (
                    select(GroupAccount.account_id)
                    .join(Account, GroupAccount.account_id == Account.id)
                    .where(
                        and_(
                            GroupAccount.group_id == group_id,
                            GroupAccount.account_id != account_id,
                            Account.role == "executor",
                            Account.status == "active",
                            GroupAccount.phase.in_(["trust_building", "soft_outreach"]),
                        ),
                    )
                )
                result = await session.execute(stmt)
                candidates = [row[0] for row in result.all()]
                if candidates:
                    partner_id = random.choice(candidates)
        except Exception:
            self._log.exception(
                "coordinated_chat.find_partner.error",
                account_id=account_id,
                group_id=group_id,
            )
            return False

        if partner_id is None:
            self._log.debug(
                "coordinated_chat.no_partner",
                account_id=account_id,
                group_id=group_id,
            )
            return False

        # Randomise who starts
        if random.random() < 0.5:
            account_a, account_b = account_id, partner_id
        else:
            account_a, account_b = partner_id, account_id

        # Check daily guard
        if not await self.should_execute(group_id, account_a, account_b):
            return False

        # Generate and execute
        script = self.generate_conversation(group_id, account_a, account_b, language=language)
        return await self.execute_conversation(group_id, account_a, account_b, script)
