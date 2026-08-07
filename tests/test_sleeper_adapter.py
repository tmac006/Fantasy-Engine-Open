"""Sleeper adapter tests: pure draft math + normalization over recorded fixtures."""

import json
from pathlib import Path

import httpx
import pytest
import respx

from api.adapters.sleeper import (
    SleeperAdapter,
    merge_picks,
    next_pick_for_slot,
    snake_slot_for_pick,
)

FIXTURES = Path(__file__).parent / "fixtures" / "sleeper"
BASE = "https://api.sleeper.app"


def fx(name: str) -> object:
    return json.loads((FIXTURES / name).read_text())


# -- pure draft math ---------------------------------------------------------


def test_snake_slot_round_trip() -> None:
    # 10 teams: round 1 = slots 1..10, round 2 reverses.
    assert snake_slot_for_pick(1, 10) == 1
    assert snake_slot_for_pick(10, 10) == 10
    assert snake_slot_for_pick(11, 10) == 10
    assert snake_slot_for_pick(20, 10) == 1
    assert snake_slot_for_pick(21, 10) == 1


def test_next_pick_for_slot() -> None:
    # Slot 3 of 10 picks at 3, 18, 23, ...
    assert next_pick_for_slot(1, 3, 10, 15) == 3
    assert next_pick_for_slot(4, 3, 10, 15) == 18
    assert next_pick_for_slot(19, 3, 10, 15) == 23
    # Round 15 (odd round) runs forward: slot 10 owns the very last pick
    assert next_pick_for_slot(149, 10, 10, 15) == 150
    # Drafted out: slot 1's last pick is 141; nothing after that
    assert next_pick_for_slot(142, 1, 10, 15) is None


def test_merge_picks_is_monotonic() -> None:
    a = [{"pick_no": 1, "player_id": "x"}, {"pick_no": 2, "player_id": "y"}]
    stale = [{"pick_no": 1, "player_id": "x"}]
    fresh = [{"pick_no": n, "player_id": p} for n, p in [(1, "x"), (2, "y"), (3, "z")]]
    # Stale response never shrinks state
    assert merge_picks(a, stale) == a
    # New picks append in order
    assert [p["pick_no"] for p in merge_picks(a, fresh)] == [1, 2, 3]
    # First-seen wins (picks are immutable once made)
    assert merge_picks(a, [{"pick_no": 2, "player_id": "OTHER"}])[1]["player_id"] == "y"


# -- adapter over recorded fixtures -----------------------------------------


@pytest.fixture
def adapter() -> SleeperAdapter:
    return SleeperAdapter(client=httpx.Client(base_url=BASE, timeout=5.0))


@respx.mock
def test_get_league_settings(adapter: SleeperAdapter) -> None:
    respx.get(f"{BASE}/v1/league/000000000000000015").respond(json=fx("league.json"))
    s = adapter.get_league_settings("000000000000000015")
    assert s.total_teams == 12
    assert s.scoring["rec"] == 1.0  # full PPR — Trent's league
    assert s.roster_slots["FLEX"] == 2
    assert s.roster_slots["QB"] == 1
    assert s.roster_slots["BN"] == 5


@respx.mock
def test_get_draft_state_complete_draft(adapter: SleeperAdapter) -> None:
    respx.get(f"{BASE}/v1/draft/000000000000000003").respond(json=fx("draft_complete.json"))
    respx.get(f"{BASE}/v1/draft/000000000000000003/picks").respond(
        json=fx("draft_complete_picks.json")
    )
    st = adapter.get_draft_state("000000000000000003", my_slot=1)
    assert st.total_teams == 12
    assert st.rounds == 15
    assert st.status == "complete"
    assert len(st.picks) == 180
    assert st.current_pick == 181
    assert st.my_next_pick is None  # draft over
    assert st.drafted_player_ids[0].startswith("sleeper:")
    # Slot 1 owns 15 picks in a 15-round draft
    assert len(st.my_roster_player_ids) == 15


@respx.mock
def test_draft_state_polls_fresh(adapter: SleeperAdapter) -> None:
    """The live-draft path must cache-bust (Phase 0 finding: 15s CDN cache)."""
    meta_route = respx.get(f"{BASE}/v1/draft/000000000000000003").respond(
        json=fx("draft_complete.json")
    )
    respx.get(f"{BASE}/v1/draft/000000000000000003/picks").respond(
        json=fx("draft_complete_picks.json")
    )
    adapter.get_draft_state("000000000000000003")
    assert "_cb" in dict(meta_route.calls[0].request.url.params)


@respx.mock
def test_get_matchups_pairs_sides(adapter: SleeperAdapter) -> None:
    respx.get(f"{BASE}/v1/league/000000000000000011/matchups/1").respond(
        json=fx("matchups_week1.json")
    )
    ms = adapter.get_matchups("000000000000000011", 1)
    assert ms, "expected at least one matchup"
    for m in ms:
        assert len(m.sides) == 2
        for side in m.sides:
            assert all(p.startswith("sleeper:") for p in side.player_ids)


@respx.mock
def test_retries_on_500_then_succeeds(adapter: SleeperAdapter) -> None:
    route = respx.get(f"{BASE}/v1/league/000000000000000015")
    route.side_effect = [httpx.Response(500), httpx.Response(200, json=fx("league.json"))]
    s = adapter.get_league_settings("000000000000000015")
    assert s.total_teams == 12
    assert route.call_count == 2
