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

# A credible live room: 14 teams, two full snake rounds pre-allocated.
FOURTEEN_TEAM_SCHEDULE: dict[str, int] = {
    **{str(pick): pick for pick in range(1, 15)},
    **{str(15 + offset): 14 - offset for offset in range(14)},
}


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


def test_live_room_size_overrides_stale_stored_team_count(monkeypatch: Any) -> None:
    """Stored settings said 16 teams while the live room had 14 (FUN CREW).
    The room is the ground truth for its own size: warn and prefer it."""
    pool = [
        PoolPlayer("1", "Back", "RB", None, 200.0, adp=10.0),
        PoolPlayer("2", "Wideout", "WR", None, 190.0, adp=20.0),
    ]
    stale = LeagueSettings(
        scoring={"rec": 1.0},
        roster_slots={"RB": 1, "WR": 1, "BN": 3},
        total_teams=16,  # stored; the live room below has only 14
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

    monkeypatch.setattr(draft, "translate_espn_settings", lambda payload: stale)
    monkeypatch.setattr(draft, "translate_espn_ids", lambda session, ids: (["1", "2"], []))
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
            picked_team_ids=[1, 2],
            schedule=FOURTEEN_TEAM_SCHEDULE,
            my_team_id=1,
        ),
        EngineParams(simulation_trials=1),
    )
    settings_used = captured["settings"]
    assert isinstance(settings_used, LeagueSettings)
    assert settings_used.total_teams == 14, "the live room's size must win"
    assert any("live draft room has 14 teams" in w for w in result.warnings)


def test_partial_schedule_does_not_rewrite_the_team_count(monkeypatch: Any) -> None:
    """A half-delivered schedule must never redefine the room.

    total_teams drives replacement level, round math and the ADP table. Trusting
    a schedule that has only reported three picks would rewrite a 12-team league
    to a 3-team one and corrupt every value on the board -- far worse than the
    stale count the override exists to correct.
    """
    pool = [PoolPlayer("1", "Back", "RB", None, 200.0, adp=10.0)]
    stored = LeagueSettings(
        scoring={"rec": 1.0},
        roster_slots={"RB": 1, "WR": 1, "BN": 3},
        total_teams=12,
    )
    captured: dict[str, object] = {}
    sentinel = SimpleNamespace(
        recommended_pick=SimpleNamespace(name="Test Player", position="WR")
    )

    class Result:
        def tuples(self) -> list[tuple[int, str, str]]:
            return [(1, "RB", "KC")]

    class Session:
        def execute(self, statement: object) -> Result:
            return Result()

    monkeypatch.setattr(draft, "translate_espn_settings", lambda payload: stored)
    monkeypatch.setattr(draft, "translate_espn_ids", lambda session, ids: (["1"], []))
    monkeypatch.setattr(draft, "build_pool", lambda session, settings, platform: pool)
    monkeypatch.setattr(draft, "recommend", lambda **kw: captured.update(kw) or sentinel)

    result = espn_recommendations(
        Session(),  # type: ignore[arg-type]
        EspnLiveDraftRequest(
            league_json={},
            picked_player_ids=["e1"],
            picked_team_ids=[1],
            schedule={"1": 1, "2": 2, "3": 3},  # only three picks known
            my_team_id=1,
        ),
        EngineParams(simulation_trials=1),
    )
    settings_used = captured["settings"]
    assert isinstance(settings_used, LeagueSettings)
    assert settings_used.total_teams == 12, "a partial schedule must not redefine the room"
    assert not any("live draft room has" in w for w in result.warnings)
