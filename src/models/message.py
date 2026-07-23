"""MessageLog and ContentPiece models – message tracking and content management."""

from datetime import datetime

from sqlalchemy import Boolean, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy import JSON
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base


class MessageLog(Base):
    __tablename__ = "message_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    group_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("groups.id", ondelete="SET NULL"), nullable=True, index=True,
    )
    dm_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True, comment="For similarity detection")
    is_outreach: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    message_type: Mapped[str] = mapped_column(
        String(20), nullable=False, default="chat",
        comment="chat | promo | response | content",
    )
    sent_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())

    def __repr__(self) -> str:
        return f"<MessageLog id={self.id} account_id={self.account_id} type={self.message_type} promo={self.is_outreach}>"


class ContentPiece(Base):
    __tablename__ = "content_pieces"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    content_type: Mapped[str] = mapped_column(
        String(30), nullable=False,
        comment="battle_report | win_screenshot | meme | review | data_viz",
    )
    language: Mapped[str] = mapped_column(String(8), nullable=False, default="en")
    content: Mapped[str] = mapped_column(Text, nullable=False)
    media_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    promo_level: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, comment="0.0 - 1.0")
    spam_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    variants: Mapped[list] = mapped_column(JSON, nullable=False, default=list, comment="JSON array of 5 variants")
    used_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    engagement_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())

    def __repr__(self) -> str:
        return f"<ContentPiece id={self.id} type={self.content_type} lang={self.language}>"
