"""Account model – represents a managed Telegram account."""

from datetime import datetime

from sqlalchemy import Boolean, Float, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    phone: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, index=True)
    phone_type: Mapped[str] = mapped_column(String(20), nullable=False, comment="physical_sim | virtual")
    phone_provider: Mapped[str | None] = mapped_column(String(50), nullable=True, comment="TextNow, Google Voice, etc.")
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    bio: Mapped[str | None] = mapped_column(Text, nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    role: Mapped[str] = mapped_column(String(20), nullable=False, comment="scout | executor | content | backup")
    persona_id: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="FK to persona template")
    language: Mapped[str] = mapped_column(String(8), nullable=False, default="en", comment="en, zh, ja, ko, th")
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="nurturing",
        comment="nurturing | active | hibernating | abandoned",
    )
    proxy_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    risk_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    trust_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    nurture_start_date: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())
    activated_date: Mapped[datetime | None] = mapped_column(nullable=True)
    last_active: Mapped[datetime | None] = mapped_column(nullable=True)

    # Daily counters (reset by scheduler)
    messages_sent_today: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    outreach_messages_today: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    groups_active_today: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    new_groups_today: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    dms_initiated_today: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    links_sent_today: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Lifetime counters
    total_messages: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_outreach_messages: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    kicked_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Risk flags
    reported: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    reported_at: Mapped[datetime | None] = mapped_column(nullable=True)
    hibernated_until: Mapped[datetime | None] = mapped_column(nullable=True)

    # Account age (days since registration when purchased)
    account_age_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    @property
    def age_tier(self) -> str:
        """Return age tier based on account_age_days."""
        if self.account_age_days >= 365:
            return "veteran"
        if self.account_age_days >= 180:
            return "mature"
        if self.account_age_days >= 90:
            return "young"
        return "fresh"

    # Telethon session data
    session_string: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now(), onupdate=func.now(),
    )

    def __repr__(self) -> str:
        return f"<Account id={self.id} phone={self.phone} role={self.role} status={self.status}>"
