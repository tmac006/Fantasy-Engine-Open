"""Player identity (ID crosswalk) and mutable player attributes."""

from sqlalchemy import ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from api.models.base import Base, TimestampMixin


class PlayerIds(Base, TimestampMixin):
    """ID crosswalk (spec §6). One row per canonical player."""

    __tablename__ = "player_ids"

    canonical_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sleeper_id: Mapped[str | None] = mapped_column(String(16), unique=True, index=True)
    espn_id: Mapped[str | None] = mapped_column(String(16), unique=True, index=True)
    gsis_id: Mapped[str | None] = mapped_column(String(16), unique=True, index=True)
    pfr_id: Mapped[str | None] = mapped_column(String(16), index=True)
    full_name: Mapped[str] = mapped_column(String(128))
    # Lowercased, punctuation-stripped name for fuzzy joins (DynastyProcess merge_name)
    merge_name: Mapped[str] = mapped_column(String(128), index=True)
    position: Mapped[str | None] = mapped_column(String(8), index=True)
    team: Mapped[str | None] = mapped_column(String(8))

    player: Mapped["Player | None"] = relationship(back_populates="ids")

    __table_args__ = (Index("ix_player_ids_merge_name_pos", "merge_name", "position"),)


class Player(Base, TimestampMixin):
    """Mutable player attributes, refreshed daily from Sleeper."""

    __tablename__ = "players"

    canonical_id: Mapped[int] = mapped_column(
        ForeignKey("player_ids.canonical_id"), primary_key=True
    )
    full_name: Mapped[str] = mapped_column(String(128))
    position: Mapped[str | None] = mapped_column(String(8), index=True)
    team: Mapped[str | None] = mapped_column(String(8), index=True)
    status: Mapped[str | None] = mapped_column(String(32))
    injury_status: Mapped[str | None] = mapped_column(String(32))
    search_rank: Mapped[int | None] = mapped_column(Integer, index=True)
    years_exp: Mapped[int | None] = mapped_column(Integer)
    age: Mapped[int | None] = mapped_column(Integer)

    ids: Mapped[PlayerIds] = relationship(back_populates="player")
