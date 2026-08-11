"""Full-draft self-play: the engine must build a sane roster start to finish.

One simulated draft exercises the whole positional rulebook at once -- holds,
caps, endgame must-fill, run detection -- the way fifteen unit tests cannot.
Every invariant here is a bug class that shipped and was caught in a live mock;
this is the harness that catches the next one first.
"""

from api.engine.params import EngineParams
from api.engine.vor import PoolPlayer, startable_slots
from api.eval.mockdraft import check_invariants, fill_lineup, simulate_draft
from api.schemas import LeagueSettings

SETTINGS = LeagueSettings(
    scoring={"rec": 1.0},
    roster_slots={"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "K": 1, "DEF": 1, "BN": 7},
    position_limits={"QB": 4, "RB": 8, "WR": 8, "TE": 3, "K": 3, "DEF": 3},
    total_teams=12,
)

# One simulation trial keeps a 192-pick draft fast; the market model still
# shapes opponent behaviour through the analytic weights.
PARAMS = EngineParams(simulation_trials=1)

ROUNDS = 16


def draft_pool(teams: int = 12) -> list[PoolPlayer]:
    """A board sized for a `teams`-sized room, with interleaved ADPs.

    Block ADPs make every positional test vacuous, which is the lesson of the
    survival fixtures. Counts scale with room size for a duller reason: a
    16-team draft spends 256 picks, and a board built for 192 simply runs out
    partway through round 15, which reads as an engine failure and is not one.
    """
    pool: list[PoolPlayer] = []
    cid = 1
    scale = teams / 12
    shape = (
        ("QB", round(28 * scale), 380.0, 5.5, 20.0, 6.8),
        ("RB", round(70 * scale), 340.0, 3.4, 1.0, 2.9),
        ("WR", round(75 * scale), 330.0, 3.1, 2.0, 2.8),
        ("TE", round(26 * scale), 250.0, 6.0, 18.0, 7.5),
    )
    for pos, count, top, drop, first, gap in shape:
        for i in range(count):
            pool.append(
                PoolPlayer(
                    canonical_id=str(cid),
                    name=f"{pos}{i + 1}",
                    position=pos,
                    team=None,
                    points=top - i * drop,
                    adp=first + i * gap,
                    platform_ids={},
                )
            )
            cid += 1
    for pos, count, top, first in (
        ("K", round(16 * scale), 150.0, 165.0),
        ("DEF", round(16 * scale), 130.0, 158.0),
    ):
        for i in range(count):
            pool.append(
                PoolPlayer(
                    canonical_id=str(cid),
                    name=f"{pos}{i + 1}",
                    position=pos,
                    team=None,
                    points=top - i * 4.0,
                    adp=first + i * 0.8,
                    platform_ids={},
                )
            )
            cid += 1
    return pool


class TestFullDraft:
    def test_roster_invariants_hold_across_slots(self) -> None:
        pool = draft_pool()
        for slot in (1, 6, 12):
            result = simulate_draft(
                pool, SETTINGS, my_slot=slot, rounds=ROUNDS, params=PARAMS, seed=3
            )
            assert len(result.my_picks) == ROUNDS
            violations = check_invariants(result, SETTINGS, ROUNDS)
            assert not violations, f"slot {slot}: {violations}"

    def test_same_seed_same_draft(self) -> None:
        pool = draft_pool()
        first = simulate_draft(pool, SETTINGS, my_slot=4, rounds=ROUNDS, params=PARAMS, seed=9)
        second = simulate_draft(pool, SETTINGS, my_slot=4, rounds=ROUNDS, params=PARAMS, seed=9)
        assert [p.player.canonical_id for p in first.picks] == [
            p.player.canonical_id for p in second.picks
        ]

    def test_no_player_drafted_twice(self) -> None:
        pool = draft_pool()
        result = simulate_draft(pool, SETTINGS, my_slot=7, rounds=ROUNDS, params=PARAMS, seed=5)
        ids = [p.player.canonical_id for p in result.picks]
        assert len(ids) == len(set(ids))

    def test_every_starter_slot_fillable(self) -> None:
        """The blunt version of draft success: a legal starting lineup exists."""
        pool = draft_pool()
        result = simulate_draft(pool, SETTINGS, my_slot=2, rounds=ROUNDS, params=PARAMS, seed=11)
        counts = result.my_position_counts
        assert counts.get("QB", 0) >= 1
        assert counts.get("RB", 0) >= 2
        assert counts.get("WR", 0) >= 2
        assert counts.get("TE", 0) >= 1
        assert counts.get("K", 0) == 1
        assert counts.get("DEF", 0) == 1


class TestInvariantChecker:
    """The checker itself must catch the historical failures if they recur."""

    def _base(self) -> tuple[list[PoolPlayer], LeagueSettings]:
        return draft_pool(), SETTINGS

    def test_flags_a_third_quarterback(self) -> None:
        from api.eval.mockdraft import DraftResult, SimulatedPick

        pool, settings = self._base()
        by_name = {p.name: p for p in pool}
        picks = [
            SimulatedPick(i + 1, i + 1, 4, True, by_name[name])
            for i, name in enumerate(
                ["QB1", "QB2", "QB3", "RB1", "RB2", "WR1", "WR2", "TE1",
                 "K1", "DEF1", "RB3", "RB4", "WR3", "WR4", "RB5", "WR5"]
            )
        ]
        result = DraftResult(my_slot=4, picks=picks)
        violations = check_invariants(result, settings, ROUNDS)
        assert any("third quarterback" in v for v in violations)

    def test_flags_an_early_kicker(self) -> None:
        from api.eval.mockdraft import DraftResult, SimulatedPick

        pool, settings = self._base()
        by_name = {p.name: p for p in pool}
        picks = [
            SimulatedPick(i + 1, i + 1, 4, True, by_name[name])
            for i, name in enumerate(
                ["RB1", "RB2", "WR1", "K1", "WR2", "TE1", "QB1", "RB3",
                 "WR3", "RB4", "WR4", "RB5", "WR5", "DEF1", "RB6", "WR6"]
            )
        ]
        result = DraftResult(my_slot=4, picks=picks)
        violations = check_invariants(result, settings, ROUNDS)
        assert any("K taken in round 4" in v for v in violations)

    def test_flags_a_missing_defense(self) -> None:
        from api.eval.mockdraft import DraftResult, SimulatedPick

        pool, settings = self._base()
        by_name = {p.name: p for p in pool}
        picks = [
            SimulatedPick(i + 1, i + 1, 4, True, by_name[name])
            for i, name in enumerate(
                ["RB1", "RB2", "WR1", "WR2", "TE1", "QB1", "RB3", "WR3",
                 "RB4", "WR4", "RB5", "WR5", "RB6", "WR6", "K1", "RB7"]
            )
        ]
        result = DraftResult(my_slot=4, picks=picks)
        violations = check_invariants(result, settings, ROUNDS)
        assert any("DEF: drafted 0" in v for v in violations)


class TestRoomSizes:
    """Room size and roster shape are inputs, not constants.

    Replacement level, startable counts and therefore the bench-depth discount
    all move with them, so the rules have to hold somewhere other than the one
    12-team, single-flex league every other fixture here uses.
    """

    def test_sixteen_team_room_still_builds_a_legal_roster(self) -> None:
        pool = draft_pool(teams=16)
        settings = SETTINGS.model_copy(update={"total_teams": 16})
        for slot in (1, 9, 16):
            result = simulate_draft(
                pool, settings, my_slot=slot, rounds=ROUNDS, params=PARAMS, seed=3
            )
            assert len(result.my_picks) == ROUNDS
            starters, _ = fill_lineup(
                [p.player for p in result.my_picks], settings.roster_slots
            )
            assert len(starters) == 9, f"slot {slot}: lineup short at 16 teams"
            assert not check_invariants(result, settings, ROUNDS, PARAMS)

    def test_two_flex_slots_widen_what_counts_as_depth(self) -> None:
        """A second flex makes a fourth back a starter rather than a backup."""
        two_flex = SETTINGS.model_copy(
            update={
                "roster_slots": {
                    **SETTINGS.roster_slots, "FLEX": 2, "BN": 5,
                }
            }
        )
        assert startable_slots(two_flex, PARAMS)["RB"] == 4
        assert startable_slots(SETTINGS, PARAMS)["RB"] == 3
        rounds = sum(two_flex.roster_slots.values())
        result = simulate_draft(
            draft_pool(), two_flex, my_slot=5, rounds=rounds, params=PARAMS, seed=3
        )
        starters, _ = fill_lineup(
            [p.player for p in result.my_picks], two_flex.roster_slots
        )
        assert len(starters) == 10
        assert not check_invariants(result, two_flex, rounds, PARAMS)
