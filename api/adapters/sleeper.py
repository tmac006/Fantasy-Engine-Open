"""Sleeper platform adapter (spec §5).

Phase 0 findings that shape this code (docs/findings.md §1.1):
- Sleeper reads sit behind Cloudflare with s-maxage=15 + stale-while-revalidate=300.
  During a live draft, responses can be ~40s stale and pick counts NON-MONOTONIC
  across polls. So: cache-bust when freshness matters, and merge picks
  monotonically — never replace a longer view of the draft with a shorter one.
- Rate limit: stay well under 1000 req/min.
"""

import time
from typing import Any

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from api.config import get_settings
from api.schemas import DraftPick, DraftState, LeagueSettings, Matchup, MatchupSide


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code >= 500


_retry = retry(
    retry=retry_if_exception(_is_retryable),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.5, max=4),
    reraise=True,
)


def merge_picks(existing: list[dict[str, Any]], incoming: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Monotonic merge by pick_no: union, never lose a pick to a stale response."""
    by_no = {p["pick_no"]: p for p in existing}
    for p in incoming:
        by_no.setdefault(p["pick_no"], p)
    return [by_no[n] for n in sorted(by_no)]


def snake_slot_for_pick(pick_no: int, teams: int) -> int:
    """1-based draft slot on the clock for a 1-based overall pick in a snake draft."""
    round_idx, pos = divmod(pick_no - 1, teams)
    return pos + 1 if round_idx % 2 == 0 else teams - pos


def next_pick_for_slot(current_pick: int, slot: int, teams: int, rounds: int) -> int | None:
    """First overall pick >= current_pick belonging to `slot`, or None if drafted out."""
    for pick_no in range(current_pick, teams * rounds + 1):
        if snake_slot_for_pick(pick_no, teams) == slot:
            return pick_no
    return None


def remaining_picks_for_slot(current_pick: int, slot: int, teams: int, rounds: int) -> int:
    """How many picks `slot` still owns from current_pick to the end of the draft."""
    return sum(
        1
        for pick_no in range(current_pick, teams * rounds + 1)
        if snake_slot_for_pick(pick_no, teams) == slot
    )


class SleeperAdapter:
    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client or httpx.Client(
            base_url=get_settings().sleeper_base_url, timeout=10.0
        )

    @_retry
    def _get(self, path: str, *, fresh: bool = False) -> Any:
        params = {"_cb": str(time.monotonic_ns())} if fresh else None
        resp = self._client.get(path, params=params)
        resp.raise_for_status()
        return resp.json()

    # -- raw platform reads -------------------------------------------------

    def league(self, league_id: str) -> dict[str, Any]:
        data: dict[str, Any] = self._get(f"/v1/league/{league_id}")
        return data

    def drafts_for_league(self, league_id: str) -> list[dict[str, Any]]:
        data: list[dict[str, Any]] = self._get(f"/v1/league/{league_id}/drafts")
        return data

    def draft(self, draft_id: str, *, fresh: bool = False) -> dict[str, Any]:
        data: dict[str, Any] = self._get(f"/v1/draft/{draft_id}", fresh=fresh)
        return data

    def draft_picks(self, draft_id: str, *, fresh: bool = False) -> list[dict[str, Any]]:
        data: list[dict[str, Any]] = self._get(f"/v1/draft/{draft_id}/picks", fresh=fresh)
        return data

    def rosters(self, league_id: str) -> list[dict[str, Any]]:
        data: list[dict[str, Any]] = self._get(f"/v1/league/{league_id}/rosters")
        return data

    def matchups(self, league_id: str, week: int) -> list[dict[str, Any]]:
        data: list[dict[str, Any]] = self._get(f"/v1/league/{league_id}/matchups/{week}")
        return data

    def trending_adds(self, lookback_hours: int = 24, limit: int = 50) -> list[dict[str, Any]]:
        data: list[dict[str, Any]] = self._get(
            f"/v1/players/nfl/trending/add?lookback_hours={lookback_hours}&limit={limit}"
        )
        return data

    # -- normalized (PlatformAdapter) ---------------------------------------

    def get_league_settings(self, league_id: str) -> LeagueSettings:
        raw = self.league(league_id)
        positions: list[str] = raw.get("roster_positions") or []
        slots: dict[str, int] = {}
        for p in positions:
            slots[p] = slots.get(p, 0) + 1
        return LeagueSettings(
            scoring={k: float(v) for k, v in (raw.get("scoring_settings") or {}).items()},
            roster_slots=slots,
            total_teams=int(raw["settings"]["num_teams"]),
        )

    def get_draft_state(self, draft_id: str, my_slot: int | None = None) -> DraftState:
        # fresh=True: this is the live-draft path; stale data here is wrong data.
        meta = self.draft(draft_id, fresh=True)
        raw_picks = self.draft_picks(draft_id, fresh=True)

        teams = int(meta["settings"]["teams"])
        rounds = int(meta["settings"]["rounds"])
        picks = [
            DraftPick(
                pick_no=int(p["pick_no"]),
                round=int(p["round"]),
                draft_slot=int(p["draft_slot"]),
                player_id=f"sleeper:{p['player_id']}",
                picked_by_roster=p.get("roster_id"),
            )
            for p in sorted(raw_picks, key=lambda p: int(p["pick_no"]))
        ]
        current_pick = len(picks) + 1
        my_next = (
            next_pick_for_slot(current_pick, my_slot, teams, rounds)
            if my_slot is not None and current_pick <= teams * rounds
            else None
        )
        return DraftState(
            drafted_player_ids=[p.player_id for p in picks],
            my_roster_player_ids=[p.player_id for p in picks if my_slot is not None and p.draft_slot == my_slot],
            current_pick=current_pick,
            my_next_pick=my_next,
            total_teams=teams,
            rounds=rounds,
            status=str(meta.get("status", "unknown")),
            picks=picks,
        )

    def get_rosters(self, league_id: str) -> dict[str, list[str]]:
        return {
            str(r["roster_id"]): [f"sleeper:{pid}" for pid in (r.get("players") or [])]
            for r in self.rosters(league_id)
        }

    def get_matchups(self, league_id: str, week: int) -> list[Matchup]:
        sides_by_matchup: dict[int, list[MatchupSide]] = {}
        for m in self.matchups(league_id, week):
            if m.get("matchup_id") is None:
                continue
            sides_by_matchup.setdefault(int(m["matchup_id"]), []).append(
                MatchupSide(
                    roster_id=int(m["roster_id"]),
                    points=float(m.get("points") or 0.0),
                    player_ids=[f"sleeper:{p}" for p in (m.get("players") or [])],
                    starter_ids=[f"sleeper:{p}" for p in (m.get("starters") or [])],
                )
            )
        return [
            Matchup(matchup_id=mid, week=week, sides=sides)
            for mid, sides in sorted(sides_by_matchup.items())
        ]
