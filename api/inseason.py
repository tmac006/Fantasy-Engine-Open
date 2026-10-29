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
from dataclasses import dataclass, replace
from statistics import median

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.engine.advanced import AdvancedProfile, build_profiles
from api.engine.outlook import outlook
from api.engine.restofseason import RestOfSeason, league_scoring_points, rest_of_season
from api.engine.startsit import GameEnvironment, LineupCandidate
from api.engine.usage import WeeklyRow, player_form
from api.engine.vor import PoolPlayer
from api.engine.waivers import Candidate
from api.models import (
    GameLine,
    League,
    LeagueRoster,
    LeagueSettingsRow,
    Player,
    PlayerContext,
    PlayerIds,
    Projection,
    WeeklyStat,
)
from api.schemas import LeagueSettings

log = logging.getLogger("api.inseason")

# Positions the weekly feed actually carries stats for. Team defenses are absent
# from the per-player feed entirely, and kickers appear in it but with no kicking
# stats -- 269 K rows in 2025, every one with zero fantasy points -- so a ranked
# kicker would score 0.0 and read as droppable. Both are reported via
# UNSUPPORTED_SLOTS in the weekly router instead of being ranked wrongly.
SKILL_POSITIONS = ("QB", "RB", "WR", "TE")
# Kickers and team defenses carry no rows in the nflverse per-player feed, so
# they have no form and were long excluded from start/sit entirely. They are
# startable slots on a real roster, though, and a season-long projection plus
# this week's game environment answers the streaming question the user actually
# has -- which is more than the blank they used to get.
LINEUP_POSITIONS = (*SKILL_POSITIONS, "K", "DEF")

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


def load_players(
    session: Session,
    ids: set[int] | None = None,
    positions: tuple[str, ...] = SKILL_POSITIONS,
) -> dict[int, PlayerRow]:
    query = select(Player).where(Player.position.in_(positions))
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

    profiles = advanced_profiles(session, season)
    candidates: list[Candidate] = []
    for canonical_id, player in players.items():
        rows = history.get(canonical_id)
        if not rows:
            continue
        profile = profiles.get(str(canonical_id))
        candidates.append(
            Candidate(
                canonical_id=canonical_id,
                name=player.name,
                position=player.position,
                team=player.team,
                form=player_form(rows, as_of_week=week),
                advanced=tuple(profile.lines) if profile else (),
                advanced_summary=profile.summary if profile else None,
            )
        )
    return candidates


def advanced_profiles(session: Session, season: str) -> dict[str, AdvancedProfile]:
    """Advanced-stat cards from the most recent completed season.

    Strictly PRIOR seasons. PFR publishes these as one row per completed
    season, so the current season's row is a full-year aggregate -- reading it
    during week 9 would show numbers built partly from weeks 9 to 18 and quietly
    reintroduce the lookahead that `api.engine.usage` exists to prevent. It also
    silently invalidates any backtest run through this path.

    The practical effect is that an in-season waiver card describes last year's
    body of work. That is the honest version: the form numbers beside it already
    cover the present, and this covers how he gets his production.
    """
    payloads: dict[str, tuple[str, dict[str, dict[str, float]]]] = {}
    for row in session.scalars(
        select(PlayerContext)
        .where(PlayerContext.source == "pfr_advstats", PlayerContext.season < season)
        .order_by(PlayerContext.season)
    ):
        payloads[str(row.canonical_id)] = (row.season, row.data or {})
    positions = {
        str(row.canonical_id): row.position
        for row in session.scalars(select(PlayerIds))
        if row.position
    }
    return build_profiles(payloads, positions)


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
    by_week = _projections_by_week(session, season, week, ids)
    return {canonical_id: weeks[week] for canonical_id, weeks in by_week.items() if week in weeks}


def _projections_by_week(
    session: Session, season: str, from_week: int, ids: set[int] | None
) -> dict[int, dict[int, float]]:
    """Median weekly projection per player per week, for `from_week` onward.

    `ids=None` loads every player; the trade engine values whole rosters.
    """
    if ids is not None and not ids:
        return {}
    query = select(Projection).where(
        Projection.season == season,
        Projection.week.is_not(None),
        Projection.week >= from_week,
    )
    if ids is not None:
        query = query.where(Projection.canonical_id.in_(ids))
    collected: dict[int, dict[int, list[float]]] = {}
    for row in session.scalars(query):
        if row.points is not None and row.week is not None:
            collected.setdefault(row.canonical_id, {}).setdefault(row.week, []).append(row.points)
    return {
        canonical_id: {week: median(points) for week, points in weeks.items()}
        for canonical_id, weeks in collected.items()
    }


def injury_statuses(
    session: Session, league: League, ids: set[int] | None = None
) -> dict[int, str]:
    """Each player's current injury designation, platform first.

    The league's own sync (ESPN mRoster `injuryStatus`) is the freshest view
    and is what the platform itself acts on. Anyone it does not cover -- a
    Sleeper league, a free agent, a league not re-synced since the column was
    added -- falls back to the daily Sleeper player refresh.
    """
    statuses: dict[int, str] = {}
    for player in session.scalars(
        select(Player).where(Player.canonical_id.in_(ids)) if ids is not None else select(Player)
    ):
        status = player.injury_status or player.status
        if status:
            statuses[player.canonical_id] = status
    for roster in session.scalars(
        select(LeagueRoster).where(LeagueRoster.league_id == league.id)
    ):
        for canonical_id, status in (roster.injury_statuses or {}).items():
            if str(canonical_id).isdigit() and status:
                statuses[int(canonical_id)] = str(status)
    return statuses


def season_scoring_rates(
    history: dict[int, list[WeeklyRow]], before_week: int, reception_value: float
) -> dict[int, tuple[float, int]]:
    """(points per game, games) this season, scored at the league's PPR value.

    Only weeks strictly before `before_week` count -- the same point-in-time
    cutoff `api.engine.usage` applies everywhere else.
    """
    rates: dict[int, tuple[float, int]] = {}
    for canonical_id, rows in history.items():
        points = [
            scored
            for row in rows
            if row.week < before_week
            and (scored := league_scoring_points(row.stats, reception_value)) is not None
        ]
        if points:
            rates[canonical_id] = (sum(points) / len(points), len(points))
    return rates


def rest_of_season_pool(
    session: Session,
    league: League,
    settings: LeagueSettings,
    pool: list[PoolPlayer],
    season: str | None = None,
    week: int | None = None,
) -> tuple[list[PoolPlayer], dict[str, RestOfSeason], int]:
    """The draft pool re-valued on what each player is worth from here on.

    Returns (pool with `points` = rest-of-season points, schedules by
    canonical id, current week). `status` is replaced by the resolved
    availability class so downstream notes read the same designation that
    priced him, rather than a stale Sleeper string.
    """
    season = season or league.season
    week = week if week is not None else current_week(session, season)
    projections = _projections_by_week(session, season, week, None)
    rates = season_scoring_rates(
        load_history(session, season), week, float(settings.scoring.get("rec", 0.0))
    )
    statuses = injury_statuses(session, league)

    valued: list[PoolPlayer] = []
    schedules: dict[str, RestOfSeason] = {}
    for player in pool:
        canonical_id = int(player.canonical_id) if player.canonical_id.isdigit() else None
        rate, games = rates.get(canonical_id, (None, 0)) if canonical_id is not None else (None, 0)
        schedule = rest_of_season(
            preseason_total=player.points,
            current_week=week,
            bye_week=player.bye_week,
            weekly_projections=projections.get(canonical_id) if canonical_id is not None else None,
            season_rate=rate,
            games_played=games,
            injury_status=statuses.get(canonical_id) if canonical_id is not None else None,
        )
        schedules[player.canonical_id] = schedule
        valued.append(
            replace(
                player,
                points=schedule.points,
                status=f"injury: {schedule.availability}" if schedule.availability else None,
            )
        )
    return valued, schedules, week


# A full NFL season. Turns a season-long projection into a per-game baseline.
SEASON_GAMES = 17
# Past the last real week, so `weeks_before` keeps a whole prior season.
_PRIOR_SEASON_END = 99


def _season_projection_rate(
    session: Session, league: League, season: str, ids: set[int]
) -> dict[int, float]:
    """Per-game baseline from this season's long-range projections.

    Week 1 has no form and no weekly projection, and refusing to answer is the
    wrong response when a season-long forecast exists: it already prices team
    changes, role and depth-chart moves, which last season's rate cannot. Built
    through `build_pool` rather than read raw so it is scored under this
    league's own rules, exactly like the draft board.
    """
    if not ids:
        return {}
    row = session.scalar(
        select(LeagueSettingsRow).where(LeagueSettingsRow.league_id == league.id)
    )
    if row is None:
        return {}
    settings = LeagueSettings(
        scoring=dict(row.scoring),
        roster_slots=dict(row.roster_slots),
        total_teams=row.total_teams,
    )
    from api.services import build_pool  # local: avoids an import cycle

    rates: dict[int, float] = {}
    for player in build_pool(session, settings, season=season, platform=league.platform):
        if not player.canonical_id.isdigit():
            continue
        canonical_id = int(player.canonical_id)
        if canonical_id in ids and player.points > 0:
            rates[canonical_id] = player.points / SEASON_GAMES
    return rates


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
    players = load_players(session, owned, positions=LINEUP_POSITIONS)
    games = _game_index(session, season, week)
    projected = _projections(session, season, week, owned)
    season_rate = _season_projection_rate(session, league, season, owned)
    profiles = advanced_profiles(session, season)
    statuses = injury_statuses(session, league, owned)
    prior_season = str(int(season) - 1) if season.isdigit() else None
    prior_history = load_history(session, prior_season) if prior_season else {}

    candidates: list[LineupCandidate] = []
    for canonical_id, player in players.items():
        rows = history.get(canonical_id, [])
        form = player_form(rows, as_of_week=week)
        prior_rows = prior_history.get(canonical_id, [])
        prior_form = (
            player_form(prior_rows, as_of_week=_PRIOR_SEASON_END) if prior_rows else None
        )

        # Most specific source first. Each step down is a genuinely weaker
        # claim, so the one actually used is recorded and shown on the card.
        baseline = projected.get(canonical_id)
        source = f"week {week} projection"
        if baseline is None:
            baseline = form.to_date.ppr_per_game
            source = f"{season} scoring rate ({form.to_date.games} gm)"
        if baseline is None:
            baseline = season_rate.get(canonical_id)
            source = f"{season} season projection"
        if baseline is None and prior_form is not None:
            baseline = prior_form.to_date.ppr_per_game
            source = f"{prior_season} scoring rate ({prior_form.to_date.games} gm)"
        if baseline is None:
            # No projection, no history either season: nothing to rank him on.
            continue

        # The floor/ceiling read needs games played. Before this season has any,
        # last season's spread is the honest stand-in -- blank would suggest the
        # player has no variance rather than that we have not measured it yet.
        meter_form = form if form.to_date.games else (prior_form or form)
        game = games.get(player.team) if player.team else None
        candidates.append(
            LineupCandidate(
                canonical_id=canonical_id,
                name=player.name,
                position=player.position,
                baseline=round(baseline, 2),
                baseline_source=source,
                environment=_environment(game, player.team),
                outlook=outlook(meter_form, player.position),
                advanced=tuple(profile.lines) if (profile := profiles.get(str(canonical_id))) else (),
                advanced_summary=profile.summary if profile else None,
                is_on_bye=player.team is not None and player.team not in games,
                injury_status=statuses.get(canonical_id),
            )
        )
    return candidates
