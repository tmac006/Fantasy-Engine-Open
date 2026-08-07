"""Draft-time endpoints: live best-available recommendations."""

import logging
from collections import defaultdict
from collections.abc import Iterator
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.adapters.espn import translate_espn_settings
from api.adapters.sleeper import (
    SleeperAdapter,
    next_pick_for_slot,
    remaining_picks_for_slot,
    snake_slot_for_pick,
)
from api.db import get_sessionmaker
from api.engine.params import EngineParams
from api.engine.presets import preset_scoring
from api.engine.recommend import DraftContext, Recommendation, recommend
from api.engine.simulation import DraftPickContext, TeamDraftContext
from api.engine.vor import PoolPlayer
from api.models import PlayerIds
from api.schemas import LeagueSettings
from api.services import build_pool, translate_espn_ids, translate_prefixed_ids

log = logging.getLogger("api.draft")
router = APIRouter(prefix="/draft", tags=["draft"])

_DEFAULT_PARAMS = EngineParams()


class EspnLiveDraftRequest(BaseModel):
    league_json: dict[str, Any]
    picked_player_ids: list[str]
    picked_team_ids: list[int] | None = None
    schedule: dict[str, int]
    my_team_id: int | None = None
    # Overall pick numbers this user owns, derived by the panel from the draft
    # slot the user typed. In an ESPN *practice* draft the URL's teamId names
    # the user's real-league team, which has nothing to do with the seat they
    # occupy in the practice room -- matching it against the (real-league)
    # schedule silently attributes a stranger's roster to them. The typed slot
    # is the only input that is true in both kinds of room, so when it is
    # present it outranks every id-based inference.
    my_pick_numbers: list[int] | None = None


def db_session() -> Iterator[Session]:
    with get_sessionmaker()() as session:
        yield session


def sleeper_adapter() -> SleeperAdapter:
    return SleeperAdapter()


def _team_contexts(
    positions: list[str | None],
    team_ids: list[str],
    all_team_ids: list[str],
) -> tuple[TeamDraftContext, ...]:
    rosters: dict[str, list[str]] = defaultdict(list)
    for team_id, position in zip(team_ids, positions, strict=False):
        if position is not None:
            rosters[team_id].append(position)
    return tuple(
        TeamDraftContext(team_id=team_id, roster_positions=tuple(rosters[team_id]))
        for team_id in all_team_ids
    )


def _recent_pick_contexts(
    drafted: list[str],
    positions: list[str | None],
    team_ids: list[str],
    pool: list[PoolPlayer],
    window: int,
) -> tuple[DraftPickContext, ...]:
    adp_by_id = {player.canonical_id: player.adp for player in pool}
    start = max(0, len(drafted) - window)
    picks: list[DraftPickContext] = []
    for index in range(start, len(drafted)):
        position = positions[index]
        if position is None or index >= len(team_ids):
            continue
        pick_number = index + 1
        adp = adp_by_id.get(drafted[index])
        picks.append(
            DraftPickContext(
                pick_number=pick_number,
                team_id=team_ids[index],
                position=position,
                adp_delta=adp - pick_number if adp is not None else None,
            )
        )
    return tuple(picks)


def _settings_for_draft(adapter: SleeperAdapter, meta: dict[str, object]) -> LeagueSettings:
    """League settings from the draft's league; presets for league-less mocks."""
    league_id = meta.get("league_id")
    if league_id:
        return adapter.get_league_settings(str(league_id))
    settings = meta.get("settings")
    slots: dict[str, int] = {}
    if isinstance(settings, dict):
        for key, value in settings.items():
            if key.startswith("slots_") and isinstance(value, int):
                slots[key.removeprefix("slots_").upper()] = value
    metadata = meta.get("metadata")
    scoring_type = metadata.get("scoring_type", "ppr") if isinstance(metadata, dict) else "ppr"
    teams = settings.get("teams", 12) if isinstance(settings, dict) else 12
    return LeagueSettings(
        scoring=preset_scoring(str(scoring_type)),
        roster_slots=slots,
        total_teams=int(teams) if isinstance(teams, int) else 12,
    )


@router.post("/espn/board")
def espn_board(
    session: Annotated[Session, Depends(db_session)],
    league_json: Annotated[dict[str, Any], Body()],
    params: Annotated[EngineParams, Query()] = _DEFAULT_PARAMS,
) -> Recommendation:
    """Ranked board for an ESPN league, from a raw mSettings payload.

    The extension fetches the payload with the user's own cookies (the server
    holds no ESPN credentials) and re-ranks locally as picks stream in from
    the draft room's WebSocket.
    """
    try:
        settings = translate_espn_settings(league_json)
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"unrecognized ESPN settings payload: {exc}") from exc

    pool = build_pool(session, settings, platform="espn")
    result = recommend(
        pool=pool,
        settings=settings,
        ctx=DraftContext(
            drafted_ids=frozenset(),
            my_roster_positions=(),
            current_pick=1,
            my_next_pick=None,
            recent_positions=(),
            on_clock=False,
        ),
        params=params,
    )
    if result is None:
        raise HTTPException(status_code=404, detail="no players available to rank")
    return result


def _attribute_my_roster(
    my_team_id: int | None,
    picked_team_ids: list[int] | None,
    schedule: dict[str, int],
    positions: list[str | None],
    nfl_teams: list[str | None],
    my_pick_numbers: list[int] | None = None,
) -> tuple[tuple[str, ...], tuple[str | None, ...], list[str], list[str]]:
    """Work out which completed picks are mine, and say so when it cannot.

    Two independent sources describe ownership and they do not always share a
    namespace. `picked_team_ids` comes from the draft room itself (socket frames
    or that draft's REST detail); `schedule` and `my_team_id` come from the
    league. In an ESPN *practice* draft those diverge -- the schedule is the real
    league's pre-allocated one, so it lines up with `my_team_id` and produces a
    correct next-pick number, while the room reports its own team ids, so
    matching picks against `my_team_id` finds nothing.

    An empty roster is not a harmless gap: the engine reads it as "every starter
    slot is open" and confidently recommends a position already filled. So when
    the room's ids attribute nothing to me but the schedule says I have already
    picked, the schedule wins, and anything still unresolved is returned as a
    warning rather than passed off as an empty roster.
    """
    warnings: list[str] = []
    count = len(positions)

    def roster_for(owners: list[str]) -> tuple[tuple[str, ...], tuple[str | None, ...]]:
        pairs = [
            (pos, team)
            for owner, pos, team in zip(owners, positions, nfl_teams, strict=False)
            if pos is not None and my_team_id is not None and owner == str(my_team_id)
        ]
        return tuple(pos for pos, _ in pairs), tuple(team for _, team in pairs)

    def my_pick_indices(owners: list[str]) -> set[int]:
        if my_team_id is None:
            return set()
        return {
            index
            for index, owner in enumerate(owners, start=1)
            if owner == str(my_team_id)
        }

    # An explicit slot-derived pick list needs no id matching at all.
    if my_pick_numbers:
        owned = set(my_pick_numbers)
        pairs = [
            (pos, team)
            for index, (pos, team) in enumerate(zip(positions, nfl_teams, strict=False), start=1)
            if index in owned and pos is not None
        ]
        expected = sum(1 for index in owned if index <= count)
        if expected and len(pairs) < expected:
            warnings.append(
                f"{expected - len(pairs)} of your {expected} picks could not be matched to a "
                "player -- position advice may miss part of your roster"
            )
        return (
            tuple(pos for pos, _ in pairs),
            tuple(team for _, team in pairs),
            [str(schedule.get(str(index), f"unknown-{index}")) for index in range(1, count + 1)],
            warnings,
        )

    from_schedule = [str(schedule.get(str(index), f"unknown-{index}")) for index in range(1, count + 1)]
    use_room_ids = picked_team_ids is not None and len(picked_team_ids) == count
    owners = [str(team_id) for team_id in picked_team_ids] if use_room_ids else from_schedule  # type: ignore[union-attr]

    # The discriminator between the room's ids and the league's is WHICH pick
    # numbers each says are mine, not how many. In a snake draft every seat
    # holds the same count at all times, so a room seat that happens to reuse
    # my league team id produces the right count of the wrong players and a
    # count check waves it through.
    schedule_indices = my_pick_indices(from_schedule)
    room_indices = my_pick_indices(owners)
    if use_room_ids and schedule_indices and room_indices != schedule_indices:
        owners = from_schedule
        warnings.append(
            f"draft room ownership disagrees with the league schedule for team "
            f"{my_team_id} (room says picks {sorted(room_indices)[:6]}, schedule says "
            f"{sorted(schedule_indices)[:6]}); trusting the schedule"
        )

    my_positions, my_teams = roster_for(owners)
    owed = len(schedule_indices) if schedule_indices else len(room_indices)

    if my_team_id is None and count > 0:
        warnings.append(
            "no team id for this draft, so your roster is unknown -- "
            "recommendations will treat every starter slot as open"
        )
    elif owed > 0 and len(my_positions) < owed:
        # Fewer resolved than owned picks means some of my picks had ids we
        # could not map to a player. Silence here is what once made a drafted
        # defense invisible, so partial resolution is said out loud too.
        warnings.append(
            f"{owed - len(my_positions)} of your {owed} picks could not be matched to a "
            "player -- position advice may miss part of your roster"
        )

    return my_positions, my_teams, owners, warnings


@router.post("/espn/recommendations")
def espn_recommendations(
    session: Annotated[Session, Depends(db_session)],
    request: EspnLiveDraftRequest,
    params: Annotated[EngineParams, Query()] = _DEFAULT_PARAMS,
) -> Recommendation:
    """Recompute ESPN recommendations from live browser-observed draft state."""
    try:
        settings = translate_espn_settings(request.league_json)
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"unrecognized ESPN settings payload: {exc}") from exc

    drafted, unmapped = translate_espn_ids(session, request.picked_player_ids)
    if unmapped:
        log.warning("espn live draft: %d unmapped picks: %s", len(unmapped), unmapped[:5])

    canonical_ints = [int(c) for c in drafted if c.isdigit()]
    meta_of: dict[int, tuple[str | None, str | None]] = {
        cid: (pos, team)
        for cid, pos, team in session.execute(
            select(PlayerIds.canonical_id, PlayerIds.position, PlayerIds.team).where(
                PlayerIds.canonical_id.in_(canonical_ints)
            )
        ).tuples()
    }
    positions = [
        meta_of[int(c)][0] if c.isdigit() and int(c) in meta_of else None
        for c in drafted
    ]
    nfl_teams = [
        meta_of[int(c)][1] if c.isdigit() and int(c) in meta_of else None
        for c in drafted
    ]
    current_pick = len(drafted) + 1
    scheduled = sorted(
        (int(pick), team_id)
        for pick, team_id in request.schedule.items()
        if pick.isdigit()
    )
    current_team_id = request.schedule.get(str(current_pick))
    my_picks = sorted(request.my_pick_numbers or [])
    if my_picks:
        on_clock = current_pick in set(my_picks)
    else:
        on_clock = (
            request.my_team_id is not None and current_team_id == request.my_team_id
        )
    next_search_pick = current_pick + 1 if on_clock else current_pick
    if my_picks:
        my_next_pick = next((pick for pick in my_picks if pick >= next_search_pick), None)
    else:
        my_next_pick = next(
            (
                pick
                for pick, team_id in scheduled
                if request.my_team_id is not None
                and team_id == request.my_team_id
                and pick >= next_search_pick
            ),
            None,
        )
    my_positions, my_nfl_teams, pick_team_ids, roster_warnings = _attribute_my_roster(
        my_team_id=request.my_team_id,
        picked_team_ids=request.picked_team_ids,
        schedule=request.schedule,
        positions=positions,
        nfl_teams=nfl_teams,
        my_pick_numbers=request.my_pick_numbers,
    )
    for warning in roster_warnings:
        log.warning("espn live draft: %s", warning)
    recent = tuple(pos for pos in positions[-params.run_window :] if pos is not None)
    if my_picks:
        my_remaining: int | None = sum(1 for pick in my_picks if pick >= current_pick)
    elif request.my_team_id is not None:
        my_remaining = sum(
            1
            for pick, team_id in scheduled
            if team_id == request.my_team_id and pick >= current_pick
        )
    else:
        my_remaining = None
    pool = build_pool(session, settings, platform="espn")
    all_team_ids = sorted({str(team_id) for _, team_id in scheduled})
    first_simulated = current_pick + 1 if on_clock else current_pick
    upcoming_team_ids = tuple(
        str(request.schedule.get(str(pick), f"unknown-{pick}"))
        for pick in range(first_simulated, my_next_pick or first_simulated)
    )

    result = recommend(
        pool=pool,
        settings=settings,
        ctx=DraftContext(
            drafted_ids=frozenset(drafted),
            my_roster_positions=my_positions,
            current_pick=current_pick,
            my_next_pick=my_next_pick,
            recent_positions=recent,
            my_remaining_picks=my_remaining,
            my_roster_nfl_teams=my_nfl_teams,
            team_contexts=_team_contexts(positions, pick_team_ids, all_team_ids),
            upcoming_team_ids=upcoming_team_ids,
            recent_picks=_recent_pick_contexts(
                drafted,
                positions,
                pick_team_ids,
                pool,
                params.run_window,
            ),
            on_clock=on_clock,
        ),
        params=params,
    )
    if result is None:
        raise HTTPException(status_code=404, detail="no available players to recommend")
    result.warnings = roster_warnings
    log.info(
        "espn live: pick=%d my_team=%s roster=%s recommended=%s (%s)",
        current_pick,
        request.my_team_id,
        my_positions,
        result.recommended_pick.name,
        result.recommended_pick.position,
    )
    return result


@router.get("/{platform}/{draft_id}/recommendations")
def recommendations(
    platform: str,
    draft_id: str,
    session: Annotated[Session, Depends(db_session)],
    adapter: Annotated[SleeperAdapter, Depends(sleeper_adapter)],
    params: Annotated[EngineParams, Query()] = _DEFAULT_PARAMS,
    my_slot: int | None = None,
) -> Recommendation:
    """Best available now, per spec §7.1. Engine knobs overridable as query params."""
    if platform != "sleeper":
        raise HTTPException(status_code=501, detail=f"platform {platform!r} not implemented yet")

    meta = adapter.draft(draft_id, fresh=True)
    settings = _settings_for_draft(adapter, dict(meta))
    state = adapter.get_draft_state(draft_id, my_slot=my_slot)

    drafted, unmapped = translate_prefixed_ids(session, state.drafted_player_ids)
    if unmapped:
        log.warning("draft %s: %d unmapped picks: %s", draft_id, len(unmapped), unmapped[:5])

    # Positions/NFL teams of my picks and of the recent picks, for need/run/stacks.
    canonical_ints = [int(c) for c in drafted if c.isdigit()]
    meta_of: dict[int, tuple[str | None, str | None]] = {
        cid: (pos, team)
        for cid, pos, team in session.execute(
            select(PlayerIds.canonical_id, PlayerIds.position, PlayerIds.team).where(
                PlayerIds.canonical_id.in_(canonical_ints)
            )
        ).tuples()
    }
    my_translated, _ = translate_prefixed_ids(session, state.my_roster_player_ids)
    my_roster = tuple(
        meta_of[int(c)]
        for c in my_translated
        if c.isdigit() and int(c) in meta_of and meta_of[int(c)][0] is not None
    )
    my_positions = tuple(pos for pos, _ in my_roster if pos is not None)
    my_nfl_teams = tuple(team for pos, team in my_roster if pos is not None)
    positions = [
        meta_of[int(c)][0] if c.isdigit() and int(c) in meta_of else None
        for c in drafted
    ]
    recent = tuple(
        pos for pos in positions[-params.run_window :] if pos is not None
    )

    on_clock = (
        my_slot is not None
        and snake_slot_for_pick(state.current_pick, state.total_teams) == my_slot
    )
    decision_next_pick = (
        next_pick_for_slot(
            state.current_pick + 1 if on_clock else state.current_pick,
            my_slot,
            state.total_teams,
            state.rounds,
        )
        if my_slot is not None
        else None
    )
    my_remaining = (
        remaining_picks_for_slot(state.current_pick, my_slot, state.total_teams, state.rounds)
        if my_slot is not None
        else None
    )
    pool = build_pool(session, settings, platform="sleeper")
    pick_team_ids = [str(pick.draft_slot) for pick in state.picks]
    first_simulated = state.current_pick + 1 if on_clock else state.current_pick
    upcoming_team_ids = tuple(
        str(snake_slot_for_pick(pick, state.total_teams))
        for pick in range(first_simulated, decision_next_pick or first_simulated)
    )
    result = recommend(
        pool=pool,
        settings=settings,
        ctx=DraftContext(
            drafted_ids=frozenset(drafted),
            my_roster_positions=my_positions,
            current_pick=state.current_pick,
            my_next_pick=decision_next_pick,
            recent_positions=recent,
            my_remaining_picks=my_remaining,
            my_roster_nfl_teams=my_nfl_teams,
            team_contexts=_team_contexts(
                positions,
                pick_team_ids,
                [str(slot) for slot in range(1, state.total_teams + 1)],
            ),
            upcoming_team_ids=upcoming_team_ids,
            recent_picks=_recent_pick_contexts(
                drafted,
                positions,
                pick_team_ids,
                pool,
                params.run_window,
            ),
            on_clock=on_clock,
        ),
        params=params,
    )
    if result is None:
        raise HTTPException(status_code=404, detail="no available players to recommend")
    return result
