"""Analytics - Daily metrics collection, account health summaries, and reports.

Queries the PostgreSQL database (via SQLAlchemy async) and Redis to produce
actionable insights for the promo-bot operations team.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import date, datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.account import Account
from src.models.group import Group, GroupAccount
from src.models.message import ContentPiece, MessageLog
from src.models.metrics import DailyMetrics

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pydantic models for structured output
# ---------------------------------------------------------------------------

class AccountHealthSummary(BaseModel):
    """Snapshot of overall account fleet health."""

    total_accounts: int = 0
    active_accounts: int = 0
    nurturing_accounts: int = 0
    hibernating_accounts: int = 0
    abandoned_accounts: int = 0
    avg_risk_score: float = 0.0
    high_risk_count: int = 0          # risk_score >= 0.5
    accounts_at_limit: int = 0        # messages_sent_today >= max
    generated_at: datetime = Field(default_factory=datetime.utcnow)


class PerformanceSummary(BaseModel):
    """Persona / group-type performance data."""

    label: str
    total_messages: int = 0
    promo_messages: int = 0
    engagement_score: float = 0.0
    conversion_events: int = 0


class WeeklyReportData(BaseModel):
    """Structured data backing the weekly report text."""

    period_start: date
    period_end: date
    total_messages_sent: int = 0
    total_promo_messages: int = 0
    avg_daily_ban_rate: float = 0.0
    avg_daily_risk_score: float = 0.0
    best_persona: str = "unknown"
    best_group_type: str = "unknown"
    active_accounts: int = 0
    banned_accounts: int = 0
    new_registrations: int = 0
    health: AccountHealthSummary | None = None


# ---------------------------------------------------------------------------
# Analytics class
# ---------------------------------------------------------------------------

class Analytics:
    """Daily metrics collection and analysis engine.

    Parameters
    ----------
    db_session:
        An ``AsyncSession`` (or an async session factory / context manager).
    redis_client:
        An ``aioredis``-compatible async Redis client.
    """

    def __init__(self, db_session: AsyncSession | Any = None, redis_client: Any = None, session_factory: Any = None) -> None:
        self._db = db_session
        self._redis = redis_client
        self._session_factory = session_factory

    # ------------------------------------------------------------------
    # Daily metrics
    # ------------------------------------------------------------------

    async def collect_daily_metrics(self) -> DailyMetrics:
        """Aggregate today's metrics from all accounts and persist a row."""
        today = date.today()

        # --- Account counts ---
        active_count = await self._scalar(
            select(func.count()).where(Account.status == "active"),
        )
        hibernating_count = await self._scalar(
            select(func.count()).where(Account.status == "hibernating"),
        )
        abandoned_count = await self._scalar(
            select(func.count()).where(Account.status == "abandoned"),
        )
        total_active_or_nurturing = await self._scalar(
            select(func.count()).where(Account.status.in_(["active", "nurturing"])),
        )
        ban_rate = (
            abandoned_count / total_active_or_nurturing
            if total_active_or_nurturing > 0
            else 0.0
        )

        # --- Group counts ---
        active_groups = await self._scalar(
            select(func.count()).where(Group.status == "active"),
        )

        # --- Message counts ---
        messages_sent = await self._scalar(
            select(func.coalesce(func.sum(Account.messages_sent_today), 0)),
        )
        promo_messages = await self._scalar(
            select(func.coalesce(func.sum(Account.promo_messages_today), 0)),
        )
        promo_ratio = promo_messages / messages_sent if messages_sent > 0 else 0.0

        # --- Risk ---
        avg_risk = await self._scalar(
            select(func.coalesce(func.avg(Account.risk_score), 0.0)).where(
                Account.status == "active",
            ),
        )

        # --- Content ---
        content_generated = await self._scalar(
            select(func.count()).select_from(ContentPiece).where(
                func.date(ContentPiece.created_at) == today,
            ),
        )

        # --- Engagement (average from content pieces created today) ---
        avg_engagement = await self._scalar(
            select(func.coalesce(func.avg(ContentPiece.engagement_score), 0.0)).where(
                func.date(ContentPiece.created_at) == today,
            ),
        )

        # --- Persist ---
        metrics = DailyMetrics(
            date=today,
            active_accounts=active_count or 0,
            hibernating_accounts=hibernating_count or 0,
            banned_accounts=abandoned_count or 0,
            ban_rate=round(ban_rate, 4),
            active_groups=active_groups or 0,
            messages_sent=messages_sent or 0,
            promo_messages=promo_messages or 0,
            promo_ratio=round(promo_ratio, 4),
            new_registrations=0,  # populated externally
            registrations_from_infiltration=0,
            registrations_from_bot=0,
            registrations_from_channel=0,
            daily_reach=messages_sent or 0,
            content_generated=content_generated or 0,
            avg_engagement_rate=round(float(avg_engagement or 0), 4),
            avg_risk_score=round(float(avg_risk or 0), 4),
        )

        if self._session_factory:
            async with self._session_factory() as session:
                session.add(metrics)
                await session.flush()
        else:
            self._db.add(metrics)
            await self._db.flush()
        logger.info("Daily metrics collected for %s: msgs=%d ban_rate=%.2f%%",
                     today, metrics.messages_sent, metrics.ban_rate * 100)
        return metrics

    # ------------------------------------------------------------------
    # Best-performing queries
    # ------------------------------------------------------------------

    async def get_best_performing_persona(self, days: int = 7) -> str:
        """Return the persona_id with the highest engagement in the last N days.

        Engagement is proxied by total message count from active accounts
        that have not been kicked or reported.
        """
        since = datetime.utcnow() - timedelta(days=days)

        stmt = (
            select(Account.persona_id, func.sum(Account.total_messages).label("total"))
            .where(
                Account.status == "active",
                Account.persona_id.is_not(None),
                Account.kicked_count <= 1,
                Account.reported == False,  # noqa: E712
                Account.last_active >= since,
            )
            .group_by(Account.persona_id)
            .order_by(func.sum(Account.total_messages).desc())
            .limit(1)
        )

        result = await self._scalar_row(stmt)
        return result[0] if result else "unknown"

    async def get_best_performing_group_type(self, days: int = 7) -> str:
        """Return the group grade (S/A/B/C) with best promo-to-kick ratio.

        A group type is "good" if it yields many promo messages without
        getting accounts kicked.
        """
        since = datetime.utcnow() - timedelta(days=days)

        stmt = (
            select(
                Group.grade,
                func.sum(GroupAccount.promo_count).label("promos"),
                func.count(GroupAccount.account_id).label("accounts"),
            )
            .join(GroupAccount, Group.id == GroupAccount.group_id)
            .where(
                Group.status == "active",
                GroupAccount.last_message_at >= since,
            )
            .group_by(Group.grade)
            .order_by(func.sum(GroupAccount.promo_count).desc())
            .limit(1)
        )

        result = await self._scalar_row(stmt)
        return f"Grade {result[0]}" if result else "unknown"

    # ------------------------------------------------------------------
    # Account health
    # ------------------------------------------------------------------

    async def get_account_health_summary(self) -> AccountHealthSummary:
        """Return a real-time account fleet health snapshot."""
        total = await self._scalar(select(func.count()).select_from(Account))
        active = await self._scalar(
            select(func.count()).where(Account.status == "active"),
        )
        nurturing = await self._scalar(
            select(func.count()).where(Account.status == "nurturing"),
        )
        hibernating = await self._scalar(
            select(func.count()).where(Account.status == "hibernating"),
        )
        abandoned = await self._scalar(
            select(func.count()).where(Account.status == "abandoned"),
        )
        avg_risk = await self._scalar(
            select(func.coalesce(func.avg(Account.risk_score), 0.0)).where(
                Account.status == "active",
            ),
        )
        high_risk = await self._scalar(
            select(func.count()).where(
                Account.status == "active",
                Account.risk_score >= 0.5,
            ),
        )
        at_limit = await self._scalar(
            select(func.count()).where(
                Account.status == "active",
                Account.messages_sent_today >= 30,
            ),
        )

        return AccountHealthSummary(
            total_accounts=total or 0,
            active_accounts=active or 0,
            nurturing_accounts=nurturing or 0,
            hibernating_accounts=hibernating or 0,
            abandoned_accounts=abandoned or 0,
            avg_risk_score=round(float(avg_risk or 0), 4),
            high_risk_count=high_risk or 0,
            accounts_at_limit=at_limit or 0,
        )

    # ------------------------------------------------------------------
    # Weekly report
    # ------------------------------------------------------------------

    async def generate_weekly_report(self) -> str:
        """Generate a plain-text weekly operations report."""
        today = date.today()
        period_start = today - timedelta(days=7)

        # Fetch the last 7 daily metrics rows
        stmt = (
            select(DailyMetrics)
            .where(DailyMetrics.date >= period_start, DailyMetrics.date <= today)
            .order_by(DailyMetrics.date.asc())
        )
        if self._session_factory:
            async with self._session_factory() as session:
                result = await session.execute(stmt)
                rows: list[DailyMetrics] = list(result.scalars().all())
        else:
            result = await self._db.execute(stmt)
            rows: list[DailyMetrics] = list(result.scalars().all())

        total_msgs = sum(r.messages_sent for r in rows)
        total_promo = sum(r.promo_messages for r in rows)
        total_banned = sum(r.banned_accounts for r in rows)
        avg_ban_rate = (
            sum(r.ban_rate for r in rows) / len(rows) if rows else 0.0
        )
        avg_risk = (
            sum(r.avg_risk_score for r in rows) / len(rows) if rows else 0.0
        )
        total_regs = sum(r.new_registrations for r in rows)

        best_persona = await self.get_best_performing_persona(days=7)
        best_group = await self.get_best_performing_group_type(days=7)
        health = await self.get_account_health_summary()

        report_lines = [
            "=" * 60,
            f"  WEEKLY OPERATIONS REPORT  {period_start} -> {today}",
            "=" * 60,
            "",
            "--- Account Fleet ---",
            f"  Total accounts:      {health.total_accounts}",
            f"  Active:              {health.active_accounts}",
            f"  Nurturing:           {health.nurturing_accounts}",
            f"  Hibernating:         {health.hibernating_accounts}",
            f"  Abandoned:           {health.abandoned_accounts}",
            f"  High-risk (>=0.5):   {health.high_risk_count}",
            f"  Avg risk score:      {health.avg_risk_score:.4f}",
            "",
            "--- Messaging ---",
            f"  Total messages:      {total_msgs:,}",
            f"  Promo messages:      {total_promo:,}",
            f"  Promo ratio:         {total_promo / total_msgs * 100:.1f}%" if total_msgs > 0 else "  Promo ratio:         N/A",
            "",
            "--- Risk ---",
            f"  Avg daily ban rate:  {avg_ban_rate * 100:.2f}%",
            f"  Avg daily risk:      {avg_risk:.4f}",
            f"  Accounts banned (wk):{total_banned}",
            "",
            "--- Performance ---",
            f"  Best persona:        {best_persona}",
            f"  Best group type:     {best_group}",
            f"  New registrations:   {total_regs:,}",
            "",
            "--- Recommendations ---",
        ]

        # Auto-generate recommendations
        if avg_ban_rate > 0.05:
            report_lines.append("  [!] Ban rate > 5%. Review account behaviour and slow down.")
        if health.high_risk_count > health.active_accounts * 0.2:
            report_lines.append("  [!] >20% of active accounts are high-risk. Hibernate or rotate.")
        if total_promo > 0 and total_msgs > 0 and total_promo / total_msgs > 0.25:
            report_lines.append("  [!] Promo ratio > 25%. Increase organic content generation.")
        if avg_ban_rate <= 0.02 and health.high_risk_count == 0:
            report_lines.append("  [OK] Fleet is healthy. Consider scaling up account activations.")

        report_lines.extend(["", "=" * 60])
        return "\n".join(report_lines)

    # ------------------------------------------------------------------
    # Daily reset
    # ------------------------------------------------------------------

    async def reset_daily_counters(self) -> None:
        """Reset all per-account daily counters. Run at midnight UTC."""
        stmt = (
            update(Account)
            .values(
                messages_sent_today=0,
                promo_messages_today=0,
                groups_active_today=0,
                new_groups_today=0,
                dms_initiated_today=0,
                links_sent_today=0,
            )
        )
        if self._session_factory:
            async with self._session_factory() as session:
                await session.execute(stmt)
                await session.commit()
        else:
            await self._db.execute(stmt)
            await self._db.flush()
        logger.info("Daily counters reset for all accounts.")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _scalar_row(self, stmt: Any) -> Any:
        """Execute a statement and return the first row (or None)."""
        if self._session_factory:
            async with self._session_factory() as session:
                result = await session.execute(stmt)
                return result.first()
        result = await self._db.execute(stmt)
        return result.first()

    async def _scalar(self, stmt: Any) -> Any:
        """Execute a statement and return a single scalar value.

        Uses the session factory (short-lived sessions) if available,
        falling back to the long-lived db session.
        """
        if self._session_factory:
            async with self._session_factory() as session:
                result = await session.execute(stmt)
                return result.scalar()
        result = await self._db.execute(stmt)
        return result.scalar()
