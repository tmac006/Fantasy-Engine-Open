"""In-season endpoints: waiver targets and start/sit (spec phases 8 and 9)."""

from collections.abc import Iterator
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.db import get_sessionmaker
from api.engine.startsit import optimize_lineup
from api.engine.waivers import rank_waiver_targets
from api.inseason import current_week, lineup_candidates, waiver_candidates
from api.models import League, LeagueSettingsRow

router = APIRouter(prefix="/weekly", tags=["weekly"])

DEFAULT_ROSTER_SLOTS = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1}

# Slots the per-player weekly feed cannot support: team defenses never appear in
# it, and kickers appear with no kicking stats (verified on 2025 -- every K row
# has zero fantasy points). Reporting these separately keeps them from reading
# as "your roster is short a player", which is what an undifferentiated unfilled
# list implies.
UNSUPPORTED_SLOTS = frozenset({"DEF", "DST", "D/ST", "DEF/ST", "K"})


def db_session() -> Iterator[Session]:
    with get_sessionmaker()() as session:
        yield session


class OutlookOut(BaseModel):
    """Two 0-100 meters plus the real quantities they are drawn from."""

    risk: int
    reward: int
    bust_probability: float
    projected_ceiling: float
    label: str
    drivers: list[str]


class WaiverTargetOut(BaseModel):
    canonical_id: int
    name: str
    position: str
    team: str | None
    score: float
    season_ppg: float | None
    recent_ppg: float | None
    expected_points_recent: float | None
    points_over_expected: float | None
    is_emerging: bool
    role_intact: bool
    outlook: OutlookOut | None
    reasons: list[str]
    context: list[str]


class WaiverResponse(BaseModel):
    league_id: int
    season: str
    week: int
    candidates_considered: int
    targets: list[WaiverTargetOut]


class AdjustmentOut(BaseModel):
    label: str
    delta: float


class StarterOut(BaseModel):
    slot: str
    canonical_id: int
    name: str
    position: str
    projected: float
    adjustments: list[AdjustmentOut]
    alternative: str | None
    margin: float | None
    is_toss_up: bool
    outlook: OutlookOut | None
    reasons: list[str]


class BenchOut(BaseModel):
    canonical_id: int
    name: str
    position: str
    projected: float
    outlook: OutlookOut | None


class LineupResponse(BaseModel):
    league_id: int
    season: str
    week: int
    projected_total: float
    starters: list[StarterOut]
    bench: list[BenchOut]
    unfilled: list[str]
    unsupported: list[str]
    toss_ups: int


def _league(session: Session, league_id: int) -> League:
    league = session.get(League, league_id)
    if league is None:
        raise HTTPException(status_code=404, detail=f"league {league_id} not registered")
    return league


@router.get("/{league_id}/waivers")
def waiver_targets(
    league_id: int,
    session: Annotated[Session, Depends(db_session)],
    week: Annotated[int | None, Query(ge=1, le=18)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    season: Annotated[
        str | None,
        Query(description="Override the league's season; useful for reviewing a past year."),
    ] = None,
) -> WaiverResponse:
    """Best free agents in this league, ranked by what they are worth per game.

    Scored on season scoring rate gated by whether the role still exists. Role
    change and regression risk ride along as context because both measured worse
    than useless as ranking inputs -- see `api.engine.waivers`.
    """
    league = _league(session, league_id)
    target_season = season or league.season
    target_week = week if week is not None else current_week(session, target_season)
    candidates = waiver_candidates(session, league, target_week, season=target_season)
    ranked = rank_waiver_targets(candidates, limit=limit)
    return WaiverResponse(
        league_id=league.id,
        season=target_season,
        week=target_week,
        candidates_considered=len(candidates),
        targets=[
            WaiverTargetOut(
                canonical_id=t.canonical_id,
                name=t.name,
                position=t.position,
                team=t.team,
                score=t.score,
                season_ppg=t.season_ppg,
                recent_ppg=t.recent_ppg,
                expected_points_recent=t.expected_points_recent,
                points_over_expected=t.points_over_expected,
                is_emerging=t.is_emerging,
                role_intact=t.role_intact,
                outlook=OutlookOut(**vars(t.outlook)) if t.outlook else None,
                reasons=t.reasons,
                context=t.context,
            )
            for t in ranked
        ],
    )


@router.get("/{league_id}/lineup")
def recommended_lineup(
    league_id: int,
    session: Annotated[Session, Depends(db_session)],
    week: Annotated[int | None, Query(ge=1, le=18)] = None,
    season: Annotated[
        str | None,
        Query(description="Override the league's season; useful for reviewing a past year."),
    ] = None,
) -> LineupResponse:
    """Best starting lineup from my roster for the week.

    Baseline is a published weekly projection where one exists and the player's
    own scoring rate otherwise; game environment nudges it. Genuinely close
    calls are labelled rather than decided.
    """
    league = _league(session, league_id)
    target_season = season or league.season
    target_week = week if week is not None else current_week(session, target_season)

    settings = session.scalar(
        select(LeagueSettingsRow).where(LeagueSettingsRow.league_id == league.id)
    )
    slots = dict(settings.roster_slots) if settings else dict(DEFAULT_ROSTER_SLOTS)

    candidates = lineup_candidates(session, league, target_week, season=target_season)
    if not candidates:
        raise HTTPException(
            status_code=409,
            detail=(
                "no rostered players with usable history -- check that my_team_id is set "
                "and the league has synced"
            ),
        )

    lineup = optimize_lineup(candidates, slots)
    return LineupResponse(
        league_id=league.id,
        season=target_season,
        week=target_week,
        projected_total=lineup.projected_total,
        starters=[
            StarterOut(
                slot=slot.slot,
                canonical_id=slot.starter.candidate.canonical_id,
                name=slot.starter.name,
                position=slot.starter.position,
                projected=slot.starter.projected,
                adjustments=[
                    AdjustmentOut(label=a.label, delta=round(a.delta, 2))
                    for a in slot.starter.adjustments
                ],
                alternative=slot.alternative.name if slot.alternative else None,
                margin=slot.margin,
                is_toss_up=slot.is_toss_up,
                outlook=(
                    OutlookOut(**vars(slot.starter.outlook)) if slot.starter.outlook else None
                ),
                reasons=slot.reasons,
            )
            for slot in lineup.slots
        ],
        bench=[
            BenchOut(
                canonical_id=player.candidate.canonical_id,
                name=player.name,
                position=player.position,
                projected=player.projected,
                outlook=OutlookOut(**vars(player.outlook)) if player.outlook else None,
            )
            for player in lineup.bench
        ],
        unfilled=[s for s in lineup.unfilled if s.upper() not in UNSUPPORTED_SLOTS],
        unsupported=sorted({s for s in lineup.unfilled if s.upper() in UNSUPPORTED_SLOTS}),
        toss_ups=sum(1 for slot in lineup.slots if slot.is_toss_up),
    )
