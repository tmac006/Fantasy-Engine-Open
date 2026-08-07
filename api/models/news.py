"""Aggregated player news."""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from api.models.base import Base


class NewsItem(Base):
    __tablename__ = "news_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(32), index=True)
    # Feed-provided guid or the link; used to avoid re-storing the same article.
    external_id: Mapped[str] = mapped_column(String(512), unique=True)
    url: Mapped[str | None] = mapped_column(String(1024))
    title: Mapped[str] = mapped_column(String(512))
    summary: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    # Null when no player could be identified; those are filtered out of feeds
    # but kept so match quality stays visible.
    canonical_id: Mapped[int | None] = mapped_column(
        ForeignKey("player_ids.canonical_id"), index=True
    )
    # injury | depth_chart | camp | transaction | general
    tag: Mapped[str] = mapped_column(String(16), default="general", index=True)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (Index("ix_news_player_published", "canonical_id", "published_at"),)
