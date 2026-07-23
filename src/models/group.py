"""Group and GroupAccount models – Telegram group tracking and account-group relations."""

from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy import JSON
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


class Group(Base):
    __tablename__ = "groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tg_group_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    username: Mapped[str | None] = mapped_column(String(128), nullable=True)
    member_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    daily_active: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    language: Mapped[str] = mapped_column(String(8), nullable=False, default="en")
    topics: Mapped[list] = mapped_column(JSON, nullable=False, default=list, comment="JSON array of topic strings")
    grade: Mapped[str] = mapped_column(String(2), nullable=False, default="C", comment="S | A | B | C")
    score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    admin_strictness: Mapped[str] = mapped_column(String(10), nullable=False, default="medium", comment="low | medium | high")
    link_tolerance: Mapped[str] = mapped_column(String(10), nullable=False, default="medium", comment="low | medium | high")
    best_posting_hours: Mapped[list] = mapped_column(JSON, nullable=False, default=list, comment="JSON array of hour strings")
    competitor_presence: Mapped[list] = mapped_column(JSON, nullable=False, default=list, comment="JSON array of competitor dicts")
    active_kols: Mapped[list] = mapped_column(JSON, nullable=False, default=list, comment="JSON array of KOL dicts")
    recommended_approach: Mapped[str | None] = mapped_column(Text, nullable=True)
    recommended_persona: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="discovered",
        comment="discovered | evaluated | infiltrating | active | blacklisted",
    )
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_activity: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=func.now(), onupdate=func.now(),
    )

    def __repr__(self) -> str:
        return f"<Group id={self.id} tg_group_id={self.tg_group_id} grade={self.grade} status={self.status}>"


class GroupAccount(Base):
    __tablename__ = "group_accounts"

    group_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True,
    )
    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True,
    )
    phase: Mapped[str] = mapped_column(
        String(20), nullable=False, default="lurking",
        comment="lurking | trust_building | soft_outreach",
    )
    joined_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    message_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    outreach_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    def __repr__(self) -> str:
        return f"<GroupAccount group_id={self.group_id} account_id={self.account_id} phase={self.phase}>"
