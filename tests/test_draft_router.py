"""Focused tests for live-board context passed into opponent simulation."""

from types import SimpleNamespace
from typing import Any

from api.engine.params import EngineParams
from api.engine.vor import PoolPlayer
from api.routers import draft
from api.routers.draft import (
    EspnLiveDraftRequest,
    _recent_pick_contexts,
    _team_contexts,
    espn_recommendations,
)
from api.schemas import LeagueSettings


def test_team_contexts_preserve_every_drafters_roster() -> None:
    contexts = _team_contexts(
        ["RB", "WR", "TE", None],
        ["1", "2", "1", "2"],
        ["1", "2", "3"],
    )
    by_id = {context.team_id: context.roster_positions for context in contexts}
    assert by_id == {"1": ("RB", "TE"), "2": ("WR",), "3": ()}


def test_recent_pick_context_includes_room_reach() -> None:
    pool = [
        PoolPlayer("a", "A", "RB", None, 200.0, adp=15.0),
        PoolPlayer("b", "B", "WR", None, 190.0, adp=8.0),
    ]
    picks = _recent_pick_contexts(
        ["a", "b"],
        ["RB", "WR"],
        ["1", "2"],
        pool,
        window=8,
    )
    assert picks[0].adp_delta == 14.0
    assert picks[1].adp_delta == 6.0
    assert [pick.team_id for pick in picks] == ["1", "2"]


def test_espn_live_request_builds_opponent_and_turn_context(monkeypatch: Any) -> None:
    pool = [
        PoolPlayer("1", "Back", "RB", None, 200.0, adp=10.0),
        PoolPlayer("2", "Wideout", "WR", None, 190.0, adp=20.0),
    ]
    settings = LeagueSettings(
        scoring={"rec": 1.0},
        roster_slots={"RB": 1, "WR": 1, "BN": 3},
        total_teams=2,
    )
    captured: dict[str, object] = {}
    sentinel = SimpleNamespace(
        recommended_pick=SimpleNamespace(name="Test Player", position="WR")
    )

    class Result:
        def tuples(self) -> list[tuple[int, str, str]]:
            return [(1, "RB", "KC"), (2, "WR", "DAL")]

    class Session:
        def execute(self, statement: object) -> Result:
            return Result()

    monkeypatch.setattr(draft, "translate_espn_settings", lambda payload: settings)
    monkeypatch.setattr(
        draft,
        "translate_espn_ids",
        lambda session, ids: (["1", "2"], []),
    )
    monkeypatch.setattr(draft, "build_pool", lambda session, settings, platform: pool)

    def capture_recommend(**kwargs: object) -> object:
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr(draft, "recommend", capture_recommend)
    result = espn_recommendations(
        Session(),  # type: ignore[arg-type]
        EspnLiveDraftRequest(
            league_json={},
            picked_player_ids=["e1", "e2"],
            # Consistent with the schedule below; the disagreeing-sources
            # path is covered in test_roster_attribution.
            picked_team_ids=[1, 2],
            schedule={"1": 1, "2": 2, "3": 1, "4": 2, "5": 1},
            my_team_id=1,
        ),
        EngineParams(simulation_trials=1),
    )
    assert result is sentinel
    context = captured["ctx"]
    assert isinstance(context, draft.DraftContext)
    assert context.on_clock
    assert context.my_next_pick == 5
    assert context.upcoming_team_ids == ("2",)
    assert {team.team_id: team.roster_positions for team in context.team_contexts} == {
        "1": ("RB",),
        "2": ("WR",),
    }
