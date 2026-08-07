"""Platform adapter contract (spec §5): the engine never knows which platform it's on."""

from collections.abc import Callable
from typing import Protocol

from api.schemas import DraftState, LeagueSettings, Matchup

# Translates a platform player ID to a canonical ID string, or None if unmapped.
# Adapters stay pure HTTP + parsing; the crosswalk lives behind this callable.
IdMapper = Callable[[str], str | None]


class PlatformAdapter(Protocol):
    def get_league_settings(self, league_id: str) -> LeagueSettings: ...

    def get_draft_state(self, draft_id: str, my_slot: int | None = None) -> DraftState: ...

    def get_rosters(self, league_id: str) -> dict[str, list[str]]: ...

    def get_matchups(self, league_id: str, week: int) -> list[Matchup]: ...
