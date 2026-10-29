"""What a player is worth from here to the end of the fantasy season.

The draft board prices players on preseason full-season totals, and that is the
right number in August. By week 4 it is the wrong one for anything that trades
future points: three weeks are gone, some players have shown a different role
than projected, and some are on injured reserve. Valued on preseason totals, the
trade engine built every "normal" offer around De'Von Achane the day after he
went on IR -- 257 points of a season he could no longer play.

Everything here reduces to one schedule: expected points for each remaining
week. A trade is valued on its sum; the manual-trade bye view reads it week by
week. Each week's number comes from the most specific source available:

1. A stored weekly projection for that exact week (median across sources).
2. Otherwise a per-game rate: the player's in-season scoring rate, shrunk
   toward his preseason per-game projection while the sample is small.

Availability is applied on top, from the structured injury status (ESPN's
`injuryStatus` where the league is on ESPN, Sleeper's otherwise):

- IR-class designations miss the NFL minimum of four games, and the weeks after
  his return are discounted, because a four-game stint is a floor, not a forecast.
- OUT / DOUBTFUL / SUSPENSION miss this week's game.
- QUESTIONABLE and DAY_TO_DAY change nothing -- the projections already price it.

Pure functions. No database.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Last fantasy week. Week 18 is not a fantasy week in standard leagues and is
# where contenders rest starters -- the backtest grades through 17 for the same
# reason.
FINAL_FANTASY_WEEK = 17
# A full NFL season, for turning a preseason total into a per-game rate.
SEASON_GAMES = 17
# How many games of preseason projection an in-season rate is blended against.
# After this many games played the two carry equal weight; from then on the
# season to date dominates. Not a fitted value: the backtest showed that
# chasing the last week or two loses to a longer sample, so a three-game start
# must not simply overwrite the projection, but by midseason a player's own
# production is the better guide.
PRESEASON_WEIGHT_GAMES = 3.0
# NFL injured reserve keeps a player out for at least four games.
IR_MIN_GAMES = 4
# Share of his rate credited once he is eligible to return. Four games is the
# minimum stay; many run longer and some end the season, so full value on the
# first eligible week would overstate what an IR player is worth.
IR_RETURN_FACTOR = 0.75

# Normalised availability classes.
IR = "IR"
OUT = "OUT"
DOUBTFUL = "DOUBTFUL"
SUSPENDED = "SUSPENDED"
_ALIASES = {
    # ESPN mRoster injuryStatus values.
    "INJURY_RESERVE": IR,
    "OUT": OUT,
    "DOUBTFUL": DOUBTFUL,
    "SUSPENSION": SUSPENDED,
    # Sleeper injury_status / status values.
    "IR": IR,
    "INJURED_RESERVE": IR,
    "PUP": IR,
    "PUP-R": IR,
    "PUP-P": IR,
    "NFI": IR,
    "NFI-R": IR,
    "SUS": SUSPENDED,
    "SUSPENDED": SUSPENDED,
}
# Games missed starting with the current week, per class.
_GAMES_OUT = {IR: IR_MIN_GAMES, OUT: 1, DOUBTFUL: 1, SUSPENDED: 1}
_RETURN_FACTOR = {IR: IR_RETURN_FACTOR}
# Classes that certainly do not play this week, for start/sit.
_SITS_THIS_WEEK = frozenset({IR, OUT, SUSPENDED})


def normalize_status(status: str | None) -> str | None:
    """One availability class for ESPN's and Sleeper's spellings, or None."""
    if not status:
        return None
    key = status.strip().upper().replace(" ", "_")
    return _ALIASES.get(key)


def misses_this_week(status: str | None) -> bool:
    """Whether this designation rules him out of the current week's game."""
    return normalize_status(status) in _SITS_THIS_WEEK


@dataclass(frozen=True)
class RestOfSeason:
    """A player's expected points for each remaining fantasy week."""

    weekly: dict[int, float] = field(default_factory=dict)
    per_game: float = 0.0
    rate_source: str = ""
    availability: str | None = None
    games_missed: int = 0
    # Expected points as a share of what he would score fully healthy. Used to
    # shade his draft-market price: nobody pays full ADP for a player on IR.
    availability_factor: float = 1.0

    @property
    def points(self) -> float:
        return sum(self.weekly.values())


def per_game_rate(
    preseason_total: float,
    season_rate: float | None,
    games_played: int,
) -> tuple[float, str]:
    """Per-game expectation when no weekly projection covers the week."""
    preseason = preseason_total / SEASON_GAMES
    if season_rate is None or games_played <= 0:
        return preseason, "preseason projection"
    weight = games_played / (games_played + PRESEASON_WEIGHT_GAMES)
    blended = weight * season_rate + (1.0 - weight) * preseason
    return blended, f"{games_played} gm scoring rate blended with preseason"


def rest_of_season(
    *,
    preseason_total: float,
    current_week: int,
    bye_week: int | None = None,
    weekly_projections: dict[int, float] | None = None,
    season_rate: float | None = None,
    games_played: int = 0,
    injury_status: str | None = None,
    final_week: int = FINAL_FANTASY_WEEK,
) -> RestOfSeason:
    """Expected points for every week from `current_week` through `final_week`."""
    weekly_projections = weekly_projections or {}
    rate, source = per_game_rate(preseason_total, season_rate, games_played)
    availability = normalize_status(injury_status)
    to_miss = _GAMES_OUT.get(availability or "", 0)
    return_factor = _RETURN_FACTOR.get(availability or "", 1.0)

    weekly: dict[int, float] = {}
    healthy_total = 0.0
    missed = 0
    for week in range(max(1, current_week), final_week + 1):
        if week == bye_week:
            weekly[week] = 0.0
            continue
        healthy = weekly_projections.get(week, rate)
        healthy_total += healthy
        if missed < to_miss:
            # The bye is skipped above, so it never counts toward the stay --
            # matching the NFL rule that IR games are games, not weeks.
            missed += 1
            weekly[week] = 0.0
        elif to_miss and return_factor < 1.0:
            weekly[week] = healthy * return_factor
        else:
            weekly[week] = healthy
    expected = sum(weekly.values())
    return RestOfSeason(
        weekly=weekly,
        per_game=rate,
        rate_source=source,
        availability=availability,
        games_missed=missed,
        availability_factor=(expected / healthy_total) if healthy_total > 0 else 1.0,
    )


def league_scoring_points(stats: dict[str, float], reception_value: float) -> float | None:
    """One weekly stat row's points under the league's per-reception value.

    The weekly feed publishes standard and full-PPR totals and nothing in
    between; their difference is exactly the reception count, so any
    per-reception value interpolates between them. Other bespoke scoring
    (bonuses, TE premium) is not reconstructed -- it is small next to the
    availability error this module exists to remove.
    """
    standard = stats.get("fantasy_points")
    ppr = stats.get("fantasy_points_ppr")
    receptions = float(stats.get("receptions") or 0.0)
    if standard is not None and ppr is not None:
        return float(standard) + reception_value * (float(ppr) - float(standard))
    if ppr is not None:
        return float(ppr) - (1.0 - reception_value) * receptions
    if standard is not None:
        return float(standard) + reception_value * receptions
    return None
