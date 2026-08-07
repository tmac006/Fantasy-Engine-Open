"""Boundary models (spec §5). Everything crossing the API surface or a platform
boundary is one of these — raw platform dicts never travel inward."""

from pydantic import BaseModel


class LeagueSettings(BaseModel):
    scoring: dict[str, float]  # e.g. {"rec": 1.0, "pass_td": 4.0}
    roster_slots: dict[str, int]  # e.g. {"QB": 1, "RB": 2, "FLEX": 2, "BN": 5}
    # Hard per-position roster caps the platform enforces at pick time
    # (e.g. {"QB": 4, "K": 3}); empty when the league has none.
    position_limits: dict[str, int] = {}
    total_teams: int


class DraftPick(BaseModel):
    pick_no: int
    round: int
    draft_slot: int
    player_id: str  # canonical ID (stringified); platform ID prefixed "sleeper:"/"espn:" if unmapped
    picked_by_roster: int | None = None


class DraftState(BaseModel):
    drafted_player_ids: list[str]  # canonical IDs, draft order
    my_roster_player_ids: list[str]
    current_pick: int  # 1-based overall pick now on the clock
    my_next_pick: int | None  # None when my slot is unknown or I have no picks left
    total_teams: int
    rounds: int
    status: str  # pre_draft | drafting | paused | complete
    picks: list[DraftPick]


class MatchupSide(BaseModel):
    roster_id: int
    points: float
    player_ids: list[str]  # canonical
    starter_ids: list[str]  # canonical


class Matchup(BaseModel):
    matchup_id: int
    week: int
    sides: list[MatchupSide]


class TrendingPlayer(BaseModel):
    player_id: str  # canonical
    add_count: int
