"""Assembles in-season engine inputs from stored data.

The engines in `api/engine/` are pure and know nothing about the database. This
module is the seam: it loads weekly history, league rosters and game lines, and
hands the engines plain dataclasses.

Everything here takes an explicit `week` and passes it through as the
point-in-time cutoff, so a request for week 9 sees week 8 and earlier whatever
else has since been ingested.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from statistics import median

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.engine.outlook import outlook
from api.engine.startsit import GameEnvironment, LineupCandidate
from api.engine.usage import WeeklyRow, player_form
from api.engine.waivers import Candidate
from api.models import GameLine, League, LeagueRoster, Player, Projection, WeeklyStat

log = logging.getLogger("api.inseason")

# Positions the weekly feed actually carries stats for. Team defenses are absent
# from the per-player feed entirely, and kickers appear in it but with no kicking
# stats -- 269 K rows in 2025, every one with zero fantasy points -- so a ranked
# kicker would score 0.0 and read as droppable. Both are reported via
# UNSUPPORTED_SLOTS in the weekly router instead of being ranked wrongly.
SKILL_POSITIONS = ("QB", "RB", "WR", "TE")

# A player with no game this week is on bye. Distinguishing that from "we have
# no line for his game" matters: one is a zero, the other is missing data.
BYE = object()


@dataclass(frozen=True)
class PlayerRow:
    canonical_id: int
    name: str
    position: str
    team: str | None


def current_week(session: Session, season: str) -> int:
    """First week of the season that has not been fully played.

    Derived from stored results rather than the calendar so that it stays
    correct when ingest is behind, and so a backfilled season reports its true
    end rather than week 1.
    """
    played = session.scalars(
        select(GameLine.week)
        .where(GameLine.season == season, GameLine.home_score.is_not(None))
        .order_by(GameLine.week.desc())
        .limit(1)
    ).first()
    scheduled = session.scalars(
        select(GameLine.week).where(GameLine.season == season).order_by(GameLine.week.desc()).limit(1)
    ).first()
    if played is None:
        return 1
    return min(played + 1, (scheduled or played) + 1)


def load_history(session: Session, season: str) -> dict[int, list[WeeklyRow]]:
    """Every player's weekly rows for the season, keyed by canonical id."""
    history: dict[int, list[WeeklyRow]] = {}
    for stat in session.scalars(select(WeeklyStat).where(WeeklyStat.season == season)):
        history.setdefault(stat.canonical_id, []).append(
            WeeklyRow(week=stat.week, stats=dict(stat.stats or {}))
        )
    return history


def load_players(session: Session, ids: set[int] | None = None) -> dict[int, PlayerRow]:
    query = select(Player).where(Player.position.in_(SKILL_POSITIONS))
    if ids is not None:
        query = query.where(Player.canonical_id.in_(ids))
    return {
        player.canonical_id: PlayerRow(
            canonical_id=player.canonical_id,
            name=player.full_name,
            position=player.position or "",
            team=player.team,
        )
        for player in session.scalars(query)
    }


def rostered_ids(session: Session, league: League, mine_only: bool = False) -> set[int]:
    """Canonical ids owned by any team in the league (or just mine)."""
    query = select(LeagueRoster).where(LeagueRoster.league_id == league.id)
    if mine_only:
        query = query.where(LeagueRoster.is_mine.is_(True))
    owned: set[int] = set()
    for roster in session.scalars(query):
        owned.update(int(pid) for pid in (roster.player_ids or []) if str(pid).isdigit())
    return owned


def waiver_candidates(
    session: Session, league: League, week: int, season: str | None = None
) -> list[Candidate]:
    """Free agents in this league, with form computed as of `week`."""
    season = season or league.season
    history = load_history(session, season)
    owned = rostered_ids(session, league)
    players = load_players(session, set(history) - owned)

    candidates: list[Candidate] = []
    for canonical_id, player in players.items():
        rows = history.get(canonical_id)
        if not rows:
            continue
        candidates.append(
            Candidate(
                canonical_id=canonical_id,
                name=player.name,
                position=player.position,
                team=player.team,
                form=player_form(rows, as_of_week=week),
            )
        )
    return candidates


def _game_index(session: Session, season: str, week: int) -> dict[str, GameLine]:
    """Team -> that team's game this week. Teams absent from the map are on bye."""
    index: dict[str, GameLine] = {}
    for game in session.scalars(
        select(GameLine).where(GameLine.season == season, GameLine.week == week)
    ):
        index[game.home_team] = game
        index[game.away_team] = game
    return index


def _environment(game: GameLine | None, team: str | None) -> GameEnvironment:
    if game is None or team is None:
        return GameEnvironment()
    return GameEnvironment(
        implied_total=game.implied_total_for(team),
        spread=(
            -game.spread_line
            if game.spread_line is not None and team == game.away_team
            else game.spread_line
        ),
        wind=game.wind,
        roof=game.roof,
        opponent=game.opponent_of(team),
        kickoff=game.kickoff.isoformat() if game.kickoff else None,
    )


def _projections(session: Session, season: str, week: int, ids: set[int]) -> dict[int, float]:
    """Weekly projected points, aggregated across sources by median (spec 6).

    Projections are stored per source, so taking whichever row came back last
    would silently pick a source at random. The median also keeps one wild
    outlier from dragging a lineup call.
    """
    if not ids:
        return {}
    by_player: dict[int, list[float]] = {}
    rows = session.scalars(
        select(Projection).where(
            Projection.season == season,
            Projection.week == week,
            Projection.canonical_id.in_(ids),
        )
    )
    for row in rows:
        if row.points is not None:
            by_player.setdefault(row.canonical_id, []).append(row.points)
    return {
        canonical_id: median(points) for canonical_id, points in by_player.items() if points
    }


def lineup_candidates(
    session: Session, league: League, week: int, season: str | None = None
) -> list[LineupCandidate]:
    """My rostered players with a baseline and this week's game environment.

    The baseline prefers a published weekly projection and falls back to the
    player's own scoring rate to date. The fallback is what makes this work
    before any projection source has posted for the week.
    """
    season = season or league.season
    owned = rostered_ids(session, league, mine_only=True)
    if not owned:
        return []

    history = load_history(session, season)
    players = load_players(session, owned)
    games = _game_index(session, season, week)
    projected = _projections(session, season, week, owned)

    candidates: list[LineupCandidate] = []
    for canonical_id, player in players.items():
        rows = history.get(canonical_id, [])
        form = player_form(rows, as_of_week=week)
        baseline = projected.get(canonical_id)
        if baseline is None:
            baseline = form.to_date.ppr_per_game
        if baseline is None:
            # No projection and never played: nothing to rank him on.
            continue
        game = games.get(player.team) if player.team else None
        candidates.append(
            LineupCandidate(
                canonical_id=canonical_id,
                name=player.name,
                position=player.position,
                baseline=round(baseline, 2),
                environment=_environment(game, player.team),
                outlook=outlook(form, player.position),
                is_on_bye=player.team is not None and player.team not in games,
            )
        )
    return candidates
