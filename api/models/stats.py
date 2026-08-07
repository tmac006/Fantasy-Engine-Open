"""Weekly usage/production stats from nflverse."""

from typing import Any

from sqlalchemy import ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from api.models.base import Base, TimestampMixin


class WeeklyStat(Base, TimestampMixin):
    """One row per player-week from nflverse (snap counts, targets, routes, ...)."""

    __tablename__ = "weekly_stats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    canonical_id: Mapped[int] = mapped_column(ForeignKey("player_ids.canonical_id"), index=True)
    season: Mapped[str] = mapped_column(String(8))
    week: Mapped[int] = mapped_column(Integer)
    # Team as of that week, not today: players change teams, and a historical row
    # joined to a current roster would attribute the wrong game to the player.
    team: Mapped[str | None] = mapped_column(String(4), default=None, index=True)
    opponent: Mapped[str | None] = mapped_column(String(4), default=None)
    stats: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    __table_args__ = (
        UniqueConstraint("canonical_id", "season", "week", name="uq_weekly_stat_key"),
    )
