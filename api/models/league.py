"""Leagues and their normalized scoring/roster settings."""

from typing import Any

from sqlalchemy import Boolean, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from api.models.base import Base, TimestampMixin


class League(Base, TimestampMixin):
    """A league this tool actively manages.

    In-season jobs (waivers, start/sit, news filtering) are per-league and run
    on a schedule, so the leagues have to be persisted rather than passed in
    per request the way the draft endpoints do it.
    """

    __tablename__ = "leagues"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    platform: Mapped[str] = mapped_column(String(16))  # 'sleeper' | 'espn'
    platform_league_id: Mapped[str] = mapped_column(String(32))
    name: Mapped[str | None] = mapped_column(String(128))
    season: Mapped[str] = mapped_column(String(8))
    # Which team in this league is mine: Sleeper roster_id, ESPN teamId.
    my_team_id: Mapped[str | None] = mapped_column(String(32))
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    settings: Mapped["LeagueSettingsRow | None"] = relationship(back_populates="league")
    rosters: Mapped[list["LeagueRoster"]] = relationship(back_populates="league")

    __table_args__ = (
        UniqueConstraint("platform", "platform_league_id", "season", name="uq_league_platform_id"),
    )


class LeagueRoster(Base, TimestampMixin):
    """Who owns whom, per league. Drives 'is this player actually a free agent
    in MY league', which no global availability list can answer."""

    __tablename__ = "league_rosters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    league_id: Mapped[int] = mapped_column(ForeignKey("leagues.id"), index=True)
    platform_roster_id: Mapped[str] = mapped_column(String(32))
    owner: Mapped[str | None] = mapped_column(String(128))
    is_mine: Mapped[bool] = mapped_column(Boolean, default=False)
    # Canonical player ids (stringified) currently rostered.
    player_ids: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    # Platform ids we could not map, kept so gaps are visible rather than silent.
    unmapped_player_ids: Mapped[list[Any]] = mapped_column(JSONB, default=list)

    league: Mapped[League] = relationship(back_populates="rosters")

    __table_args__ = (
        UniqueConstraint("league_id", "platform_roster_id", name="uq_roster_league_slot"),
    )


class LeagueSettingsRow(Base, TimestampMixin):
    __tablename__ = "league_settings"

    league_id: Mapped[int] = mapped_column(ForeignKey("leagues.id"), primary_key=True)
    scoring: Mapped[dict[str, Any]] = mapped_column(JSONB)       # {"rec": 1.0, "pass_td": 4, ...}
    roster_slots: Mapped[dict[str, Any]] = mapped_column(JSONB)  # {"QB": 1, "RB": 2, "FLEX": 2, ...}
    total_teams: Mapped[int] = mapped_column(Integer)

    league: Mapped[League] = relationship(back_populates="settings")
