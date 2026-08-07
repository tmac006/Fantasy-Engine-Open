"""League-scoped endpoints: settings and live draft state."""

import logging
from collections.abc import Iterator
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.adapters.sleeper import SleeperAdapter
from api.db import get_sessionmaker
from api.ingest.leagues import sync_league
from api.models import League
from api.schemas import DraftState, LeagueSettings
from api.services import translate_prefixed_ids

log = logging.getLogger("api.leagues")
router = APIRouter(prefix="/leagues", tags=["leagues"])


def db_session() -> Iterator[Session]:
    with get_sessionmaker()() as session:
        yield session


def sleeper_adapter() -> SleeperAdapter:
    return SleeperAdapter()


@router.get("/{platform}/{league_id}/settings")
def league_settings(
    platform: str,
    league_id: str,
    adapter: Annotated[SleeperAdapter, Depends(sleeper_adapter)],
) -> LeagueSettings:
    if platform != "sleeper":
        raise HTTPException(status_code=501, detail=f"platform {platform!r} not implemented yet")
    return adapter.get_league_settings(league_id)


@router.get("/{platform}/{league_id}/draft-state")
def draft_state(
    platform: str,
    league_id: str,
    session: Annotated[Session, Depends(db_session)],
    adapter: Annotated[SleeperAdapter, Depends(sleeper_adapter)],
    my_slot: int | None = None,
) -> DraftState:
    """Normalized live draft state with canonical player IDs (Phase 1 acceptance)."""
    if platform != "sleeper":
        raise HTTPException(status_code=501, detail=f"platform {platform!r} not implemented yet")

    drafts = adapter.drafts_for_league(league_id)
    if not drafts:
        raise HTTPException(status_code=404, detail="league has no drafts")
    # Most recent draft (Sleeper returns newest first; sort defensively by created)
    draft_id = str(max(drafts, key=lambda d: d.get("created", 0))["draft_id"])

    state = adapter.get_draft_state(draft_id, my_slot=my_slot)

    drafted, unmapped = translate_prefixed_ids(session, state.drafted_player_ids)
    mine, _ = translate_prefixed_ids(session, state.my_roster_player_ids)
    if unmapped:
        log.warning("draft %s: %d picks with no canonical mapping: %s", draft_id, len(unmapped), unmapped[:10])
    id_of = dict(zip(state.drafted_player_ids, drafted))
    for pick in state.picks:
        pick.player_id = id_of.get(pick.player_id, pick.player_id)
    return state.model_copy(update={"drafted_player_ids": drafted, "my_roster_player_ids": mine})


class RegisterLeague(BaseModel):
    platform: str  # 'sleeper' | 'espn'
    league_id: str
    season: str = "2026"
    # Sleeper roster_id or ESPN teamId identifying which team is yours.
    my_team_id: str | None = None
    name: str | None = None


class RegisteredLeague(BaseModel):
    id: int
    platform: str
    league_id: str
    season: str
    name: str | None
    my_team_id: str | None
    active: bool
    rostered_players: int
    unmapped_players: int
    # Registration and sync are separate outcomes: a private ESPN league
    # registers fine but cannot sync until cookies are configured.
    sync_error: str | None = None


def _as_registered(league: League, sync_error: str | None = None) -> RegisteredLeague:
    return RegisteredLeague(
        sync_error=sync_error,
        id=league.id,
        platform=league.platform,
        league_id=league.platform_league_id,
        season=league.season,
        name=league.name,
        my_team_id=league.my_team_id,
        active=league.active,
        rostered_players=sum(len(r.player_ids or []) for r in league.rosters),
        unmapped_players=sum(len(r.unmapped_player_ids or []) for r in league.rosters),
    )


@router.get("")
def list_leagues(session: Annotated[Session, Depends(db_session)]) -> list[RegisteredLeague]:
    """Leagues this tool manages. In-season jobs iterate exactly this list."""
    return [_as_registered(league) for league in session.scalars(select(League))]


@router.post("/register")
def register_league(
    payload: RegisterLeague,
    session: Annotated[Session, Depends(db_session)],
) -> RegisteredLeague:
    """Register a league for in-season jobs, then sync it immediately.

    Registration is what makes waivers, start/sit and news filtering possible:
    those run on a schedule and cannot take a league id from a request.
    """
    if payload.platform not in ("sleeper", "espn"):
        raise HTTPException(status_code=422, detail="platform must be 'sleeper' or 'espn'")

    league = session.scalar(
        select(League).where(
            League.platform == payload.platform,
            League.platform_league_id == payload.league_id,
            League.season == payload.season,
        )
    )
    if league is None:
        league = League(
            platform=payload.platform,
            platform_league_id=payload.league_id,
            season=payload.season,
        )
        session.add(league)
    league.name = payload.name or league.name
    league.my_team_id = payload.my_team_id or league.my_team_id
    league.active = True
    session.commit()

    sync_error: str | None = None
    try:
        sync_league(session, league)
    except Exception as exc:  # noqa: BLE001 - surfaced to the caller, not swallowed
        session.rollback()
        sync_error = str(exc)
        log.warning("registered %s league %s but sync failed: %s",
                    payload.platform, payload.league_id, exc)
    session.refresh(league)
    return _as_registered(league, sync_error)
