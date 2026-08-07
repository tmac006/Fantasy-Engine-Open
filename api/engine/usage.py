"""Point-in-time usage features.

Everything downstream of this module -- waiver scoring, start/sit -- answers a
question of the form "what should I have done going into week N". The honest
answer may only use what was knowable *before* week N kicked off. Aggregating a
whole season and then scoring week 8 leaks weeks 8-18 into the decision and
produces a model that backtests beautifully and fails in September.

So the cutoff lives here, in one place, rather than in each caller: every
selector takes `as_of_week` and keeps rows strictly before it. Callers never
filter weeks themselves.

Pure functions over plain rows -- no database, no ORM -- so the lookahead guard
is directly testable.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from statistics import pstdev

# Weeks that count as "recent form". Three is short enough to catch a role
# change within a couple of games of it happening and long enough that one
# blowout script does not define the trend.
RECENT_WEEKS = 3

# A player needs this much of a snap share before efficiency numbers mean
# anything; below it the denominators are tiny and the rates are noise.
MIN_SNAP_PCT_FOR_RATES = 0.15


@dataclass(frozen=True)
class WeeklyRow:
    """One player-week. `stats` mirrors the JSONB blob on `weekly_stats`."""

    week: int
    stats: dict[str, float]

    def get(self, key: str) -> float | None:
        value = self.stats.get(key)
        return float(value) if isinstance(value, (int, float)) else None


@dataclass(frozen=True)
class UsageWindow:
    """Averages over a set of weeks. `games` is rows present, not weeks elapsed.

    A player who missed two of three weeks has games=1, and every rate below is
    that single game. Callers weigh thin samples via `games`.
    """

    games: int
    snap_pct: float | None
    target_share: float | None
    targets_per_game: float | None
    carries_per_game: float | None
    attempts_per_game: float | None  # pass attempts; the volume metric for a QB
    opportunities_per_game: float | None
    ppr_per_game: float | None
    ppr_stdev: float | None
    epa_per_opportunity: float | None

    @property
    def is_empty(self) -> bool:
        return self.games == 0


EMPTY_WINDOW = UsageWindow(0, None, None, None, None, None, None, None, None, None)


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _present(rows: Iterable[WeeklyRow], key: str) -> list[float]:
    return [value for row in rows if (value := row.get(key)) is not None]


def weeks_before(rows: Iterable[WeeklyRow], as_of_week: int) -> list[WeeklyRow]:
    """The only place a week cutoff is applied. Strictly before `as_of_week`."""
    return sorted((row for row in rows if row.week < as_of_week), key=lambda row: row.week)


def summarize(rows: Sequence[WeeklyRow]) -> UsageWindow:
    """Collapse already-filtered rows into averages."""
    if not rows:
        return EMPTY_WINDOW

    targets = _present(rows, "targets")
    carries = _present(rows, "carries")
    ppr = _present(rows, "fantasy_points_ppr")

    # Opportunity blends the two ways a skill player touches the ball. Targets
    # are worth more than carries per unit in PPR, but this is a volume proxy,
    # not a projection, so they are counted evenly and the scoring model applies
    # the value weighting.
    opportunities = [
        (row.get("targets") or 0.0) + (row.get("carries") or 0.0)
        for row in rows
        if row.get("targets") is not None or row.get("carries") is not None
    ]

    epa_total = sum(
        (row.get("receiving_epa") or 0.0) + (row.get("rushing_epa") or 0.0) for row in rows
    )
    opportunity_total = sum(opportunities)
    snap = _mean(_present(rows, "offense_pct"))
    rates_trustworthy = (
        opportunity_total >= len(rows) and (snap is None or snap >= MIN_SNAP_PCT_FOR_RATES)
    )

    return UsageWindow(
        games=len(rows),
        snap_pct=snap,
        target_share=_mean(_present(rows, "target_share")),
        targets_per_game=_mean(targets),
        carries_per_game=_mean(carries),
        attempts_per_game=_mean(_present(rows, "attempts")),
        opportunities_per_game=_mean(opportunities),
        ppr_per_game=_mean(ppr),
        # Population stdev: these are all the games he played, not a sample of
        # them. Needs two games to mean anything.
        ppr_stdev=pstdev(ppr) if len(ppr) > 1 else None,
        epa_per_opportunity=(
            epa_total / opportunity_total if rates_trustworthy and opportunity_total else None
        ),
    )


@dataclass(frozen=True)
class PlayerForm:
    """Recent form against earlier-season baseline, as known before `as_of_week`."""

    as_of_week: int
    recent: UsageWindow
    baseline: UsageWindow
    to_date: UsageWindow

    @property
    def opportunity_delta(self) -> float | None:
        """Change in touches per game, recent vs earlier. The role-change signal.

        None when there is no baseline to compare against -- a player with only
        recent games has not changed role, he has simply just appeared, which is
        a different thing and is scored separately.
        """
        if self.recent.is_empty or self.baseline.is_empty:
            return None
        recent, baseline = self.recent.opportunities_per_game, self.baseline.opportunities_per_game
        if recent is None or baseline is None:
            return None
        return recent - baseline

    @property
    def snap_delta(self) -> float | None:
        if self.recent.snap_pct is None or self.baseline.snap_pct is None:
            return None
        return self.recent.snap_pct - self.baseline.snap_pct

    @property
    def target_share_delta(self) -> float | None:
        if self.recent.target_share is None or self.baseline.target_share is None:
            return None
        return self.recent.target_share - self.baseline.target_share

    @property
    def is_emerging(self) -> bool:
        """Recent role materially bigger than the earlier-season one."""
        delta = self.opportunity_delta
        snap = self.snap_delta
        return (delta is not None and delta >= 2.0) or (snap is not None and snap >= 0.15)


def player_form(
    rows: Iterable[WeeklyRow], as_of_week: int, recent_weeks: int = RECENT_WEEKS
) -> PlayerForm:
    """Split a player's history into recent form vs the baseline before it.

    Both windows are drawn only from weeks before `as_of_week`. `recent` is the
    last `recent_weeks` *games played*, not calendar weeks, so a bye or a missed
    game does not silently empty the window.
    """
    history = weeks_before(rows, as_of_week)
    recent = history[-recent_weeks:] if recent_weeks > 0 else []
    baseline = history[: -recent_weeks] if recent_weeks > 0 else list(history)
    return PlayerForm(
        as_of_week=as_of_week,
        recent=summarize(recent),
        baseline=summarize(baseline),
        to_date=summarize(history),
    )
