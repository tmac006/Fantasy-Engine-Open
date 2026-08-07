"""Per-source player projections."""

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from api.models.base import Base


class Projection(Base):
    """Per-source projections (spec §6: store per-source, aggregate with median)."""

    __tablename__ = "projections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    canonical_id: Mapped[int] = mapped_column(ForeignKey("player_ids.canonical_id"), index=True)
    source: Mapped[str] = mapped_column(String(32))
    season: Mapped[str] = mapped_column(String(8))
    week: Mapped[int | None] = mapped_column(Integer)  # NULL = full-season projection
    points: Mapped[float | None] = mapped_column(Float)  # source's default scoring, if any
    stats: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)  # raw stat projections
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("canonical_id", "source", "season", "week", name="uq_projection_key"),
    )
