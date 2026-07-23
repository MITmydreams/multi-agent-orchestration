"""DailyMetrics model – aggregated daily operational metrics."""

import datetime as dt

from sqlalchemy import Date, Float, Integer, func
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


class DailyMetrics(Base):
    __tablename__ = "daily_metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    date: Mapped[dt.date] = mapped_column(Date, unique=True, nullable=False, index=True)

    # Account stats
    active_accounts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    hibernating_accounts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    banned_accounts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ban_rate: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    # Group stats
    active_groups: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Message stats
    messages_sent: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    promo_messages: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    promo_ratio: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    # Registration stats
    new_registrations: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    registrations_from_infiltration: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    registrations_from_bot: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    registrations_from_channel: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Engagement stats
    daily_reach: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    content_generated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    avg_engagement_rate: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    avg_risk_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    # Cost stats
    cac: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, comment="Customer acquisition cost")

    created_at: Mapped[dt.datetime] = mapped_column(nullable=False, server_default=func.now())

    def __repr__(self) -> str:
        return f"<DailyMetrics date={self.date} active={self.active_accounts} msgs={self.messages_sent}>"
