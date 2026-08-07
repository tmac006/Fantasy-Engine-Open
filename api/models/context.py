"""Compact, source-aware player and team context used by the draft model."""

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from api.models.base import Base


class PlayerContext(Base):
    """A provider snapshot for one player and draft season."""

    __tablename__ = "player_context"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    canonical_id: Mapped[int] = mapped_column(
        ForeignKey("player_ids.canonical_id"), index=True
    )
    season: Mapped[str] = mapped_column(String(8), index=True)
    source: Mapped[str] = mapped_column(String(32))
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "canonical_id", "season", "source", name="uq_player_context_key"
        ),
    )


class TeamContext(Base):
    """A provider snapshot for one NFL team and draft season."""

    __tablename__ = "team_context"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    team: Mapped[str] = mapped_column(String(8), index=True)
    season: Mapped[str] = mapped_column(String(8), index=True)
    source: Mapped[str] = mapped_column(String(32))
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("team", "season", "source", name="uq_team_context_key"),
    )
