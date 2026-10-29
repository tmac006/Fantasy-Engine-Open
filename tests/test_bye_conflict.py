"""Bye-stacking cost at draft time.

The engine drafted a roster that was individually optimal and collectively
unstartable: NE, GB, SEA and CLE all share week 11, and seat 11 came away with
seven of them -- only 7 of 9 lineup slots fillable that week, ~33 points below a
normal week. Season projections cannot see this by construction, since every
total is earned across the weeks a player actually plays.
"""

from api.engine.params import EngineParams
from api.engine.recommend import DraftContext, _bye_conflict_adjustment
from api.engine.vor import PoolPlayer

PARAMS = EngineParams()
TEAM_BYES = {"NE": 11, "GB": 11, "SEA": 11, "MIA": 6, "LAC": 7, "PHI": 10}


def player(name: str, position: str, team: str, bye: int | None) -> PoolPlayer:
    return PoolPlayer(
        canonical_id=name, name=name, position=position, team=team,
        points=200.0, bye_week=bye,
    )


def ctx(positions: tuple[str, ...], teams: tuple[str | None, ...]) -> DraftContext:
    return DraftContext(
        drafted_ids=frozenset(),
        my_roster_positions=positions,
        current_pick=50,
        my_next_pick=60,
        recent_positions=(),
        my_roster_nfl_teams=teams,
    )


def test_empty_roster_is_never_penalised() -> None:
    penalty, factors = _bye_conflict_adjustment(
        player("Rookie", "WR", "NE", 11), ctx((), ()), PARAMS, TEAM_BYES
    )
    assert penalty == 0.0 and factors == []


def test_a_player_with_no_known_bye_is_not_penalised() -> None:
    penalty, _ = _bye_conflict_adjustment(
        player("Mystery", "WR", "NE", None), ctx(("RB",), ("NE",)), PARAMS, TEAM_BYES
    )
    assert penalty == 0.0


def test_one_shared_bye_is_free() -> None:
    """Unavoidable in a 12-team room; penalising it would distort every board."""
    penalty, _ = _bye_conflict_adjustment(
        player("Second", "WR", "GB", 11), ctx(("RB",), ("NE",)), PARAMS, TEAM_BYES
    )
    assert penalty == 0.0


def test_the_second_shared_bye_costs() -> None:
    penalty, factors = _bye_conflict_adjustment(
        player("Third", "WR", "SEA", 11),
        ctx(("RB", "TE"), ("NE", "GB")),
        PARAMS,
        TEAM_BYES,
    )
    assert penalty < 0.0
    assert any("Week 11" in f for f in factors)


def test_penalty_grows_with_the_pile() -> None:
    two = _bye_conflict_adjustment(
        player("X", "WR", "SEA", 11), ctx(("RB", "TE"), ("NE", "GB")), PARAMS, TEAM_BYES
    )[0]
    four = _bye_conflict_adjustment(
        player("X", "WR", "SEA", 11),
        ctx(("RB", "TE", "QB", "K"), ("NE", "GB", "NE", "GB")),
        PARAMS,
        TEAM_BYES,
    )[0]
    assert four < two, "each additional week-11 body should cost more"


def test_same_position_bye_clash_costs_more_than_a_cross_position_one() -> None:
    """A backup whose bye matches the starter he backs up provides no cover."""
    cross = _bye_conflict_adjustment(
        player("WR", "WR", "SEA", 11), ctx(("RB", "TE"), ("NE", "GB")), PARAMS, TEAM_BYES
    )[0]
    same = _bye_conflict_adjustment(
        player("WR", "WR", "SEA", 11), ctx(("WR", "TE"), ("NE", "GB")), PARAMS, TEAM_BYES
    )[0]
    assert same < cross


def test_a_different_bye_week_is_not_a_conflict() -> None:
    penalty, _ = _bye_conflict_adjustment(
        player("Clean", "WR", "MIA", 6),
        ctx(("RB", "TE"), ("NE", "GB")),
        PARAMS,
        TEAM_BYES,
    )
    assert penalty == 0.0, "week 6 does not collide with a week 11 pile"


def test_unknown_roster_teams_are_ignored_not_guessed() -> None:
    penalty, _ = _bye_conflict_adjustment(
        player("Guy", "WR", "NE", 11), ctx(("RB", "TE"), (None, None)), PARAMS, TEAM_BYES
    )
    assert penalty == 0.0
