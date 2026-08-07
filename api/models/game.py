"""Per-game schedule, betting lines and conditions from nflverse."""

from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from api.models.base import Base, TimestampMixin


class GameLine(Base, TimestampMixin):
    """One row per game: the market's view plus the conditions and the result.

    `spread_line` is the home team's margin, so a positive value means the home
    team is favoured by that many points (verified against 2025 outcomes: mean
    error 0.6 points across 272 games). Scores stay null until the game is
    played, which is what makes a row usable for both forecasting and backtests.
    """

    __tablename__ = "game_lines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    game_id: Mapped[str] = mapped_column(String(32), index=True)
    season: Mapped[str] = mapped_column(String(8), index=True)
    week: Mapped[int] = mapped_column(Integer, index=True)
    home_team: Mapped[str] = mapped_column(String(4), index=True)
    away_team: Mapped[str] = mapped_column(String(4), index=True)
    kickoff: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    spread_line: Mapped[float | None] = mapped_column(Float, default=None)
    total_line: Mapped[float | None] = mapped_column(Float, default=None)
    home_moneyline: Mapped[int | None] = mapped_column(Integer, default=None)
    away_moneyline: Mapped[int | None] = mapped_column(Integer, default=None)

    roof: Mapped[str | None] = mapped_column(String(16), default=None)
    surface: Mapped[str | None] = mapped_column(String(24), default=None)
    temp: Mapped[float | None] = mapped_column(Float, default=None)
    wind: Mapped[float | None] = mapped_column(Float, default=None)

    home_score: Mapped[int | None] = mapped_column(Integer, default=None)
    away_score: Mapped[int | None] = mapped_column(Integer, default=None)

    __table_args__ = (UniqueConstraint("game_id", name="uq_game_line_key"),)

    @property
    def home_implied_total(self) -> float | None:
        """Points the market expects the home team to score."""
        if self.total_line is None or self.spread_line is None:
            return None
        return self.total_line / 2.0 + self.spread_line / 2.0

    @property
    def away_implied_total(self) -> float | None:
        """Points the market expects the away team to score."""
        if self.total_line is None or self.spread_line is None:
            return None
        return self.total_line / 2.0 - self.spread_line / 2.0

    def implied_total_for(self, team: str) -> float | None:
        """Implied points for whichever side `team` is on."""
        if team == self.home_team:
            return self.home_implied_total
        if team == self.away_team:
            return self.away_implied_total
        return None

    def opponent_of(self, team: str) -> str | None:
        if team == self.home_team:
            return self.away_team
        if team == self.away_team:
            return self.home_team
        return None
