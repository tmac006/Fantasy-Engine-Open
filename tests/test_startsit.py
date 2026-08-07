"""Start/sit: environment adjustments, slot filling, and honest uncertainty."""

from api.engine.startsit import (
    HIGH_WIND_MPH,
    MAX_IMPLIED_TOTAL_ADJUSTMENT,
    GameEnvironment,
    LineupCandidate,
    optimize_lineup,
    score_player,
)


def player(
    name: str, position: str, baseline: float, **environment: object
) -> LineupCandidate:
    return LineupCandidate(
        canonical_id=abs(hash(name)) % 100_000,
        name=name,
        position=position,
        baseline=baseline,
        environment=GameEnvironment(**environment),  # type: ignore[arg-type]
    )


class TestEnvironment:
    def test_high_implied_total_helps_a_little(self) -> None:
        scored = score_player(player("Rich", "WR", 12.0, implied_total=29.0))
        assert scored.projected > 12.0
        assert scored.projected - 12.0 <= MAX_IMPLIED_TOTAL_ADJUSTMENT

    def test_low_implied_total_hurts_a_little(self) -> None:
        scored = score_player(player("Poor", "WR", 12.0, implied_total=16.0))
        assert scored.projected < 12.0
        assert 12.0 - scored.projected <= MAX_IMPLIED_TOTAL_ADJUSTMENT

    def test_implied_total_never_overturns_a_real_gap(self) -> None:
        """The measured edge was 0.56 PPR. It must not flip a two-point call."""
        good = score_player(player("Good", "WR", 14.0, implied_total=17.0))
        worse = score_player(player("Worse", "WR", 12.0, implied_total=30.0))
        assert good.projected > worse.projected

    def test_wind_hurts_pass_catchers(self) -> None:
        calm = score_player(player("Receiver", "WR", 12.0, wind=3.0, roof="outdoors"))
        windy = score_player(player("Receiver", "WR", 12.0, wind=22.0, roof="outdoors"))
        assert windy.projected < calm.projected
        assert any("wind" in a.label for a in windy.adjustments)

    def test_wind_helps_running_backs(self) -> None:
        calm = score_player(player("Back", "RB", 12.0, wind=3.0, roof="outdoors"))
        windy = score_player(player("Back", "RB", 12.0, wind=22.0, roof="outdoors"))
        assert windy.projected > calm.projected

    def test_wind_is_ignored_indoors(self) -> None:
        # A dome reporting a wind number is reporting the weather outside it.
        indoors = score_player(player("Receiver", "WR", 12.0, wind=25.0, roof="dome"))
        assert indoors.projected == 12.0

    def test_missing_wind_is_neutral_not_penalized(self) -> None:
        scored = score_player(player("Receiver", "WR", 12.0, roof="outdoors"))
        assert scored.projected == 12.0

    def test_wind_threshold_is_not_applied_below_the_cutoff(self) -> None:
        below = score_player(
            player("Receiver", "WR", 12.0, wind=HIGH_WIND_MPH - 0.1, roof="outdoors")
        )
        assert below.projected == 12.0

    def test_bye_week_is_zero(self) -> None:
        candidate = LineupCandidate(1, "Resting Rob", "WR", 15.0, is_on_bye=True)
        assert score_player(candidate).projected == 0.0


class TestLineup:
    def roster(self) -> list[LineupCandidate]:
        return [
            player("Elite QB", "QB", 22.0),
            player("Backup QB", "QB", 14.0),
            player("RB1", "RB", 16.0),
            player("RB2", "RB", 12.0),
            player("RB3", "RB", 8.0),
            player("WR1", "WR", 18.0),
            player("WR2", "WR", 13.0),
            player("WR3", "WR", 9.0),
            player("TE1", "TE", 10.0),
        ]

    def test_fills_every_slot_with_the_best_eligible(self) -> None:
        lineup = optimize_lineup(
            self.roster(), {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "BN": 5}
        )
        starters = {slot.slot: slot.starter.name for slot in lineup.slots}
        assert starters["QB"] == "Elite QB"
        assert starters["TE"] == "TE1"
        assert not lineup.unfilled

    def test_flex_takes_the_best_leftover(self) -> None:
        lineup = optimize_lineup(
            self.roster(), {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "BN": 5}
        )
        flex = next(slot for slot in lineup.slots if slot.slot == "FLEX")
        # RB1/RB2 and WR1/WR2 are used; the best remaining flex-eligible is WR3 at 9.0.
        assert flex.starter.name == "WR3"

    def test_bench_slots_are_not_lineup_decisions(self) -> None:
        lineup = optimize_lineup(self.roster(), {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "BN": 6})
        assert all(slot.slot != "BN" for slot in lineup.slots)
        assert "Backup QB" in {player.name for player in lineup.bench}

    def test_superflex_does_not_strand_the_qb_slot(self) -> None:
        """Scarce slots fill first, or a superflex can eat the only startable QB."""
        lineup = optimize_lineup(
            self.roster(), {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "SUPER_FLEX": 1, "BN": 4}
        )
        starters = {slot.slot: slot.starter.name for slot in lineup.slots}
        assert starters["QB"] == "Elite QB"
        assert starters["SUPER_FLEX"] == "Backup QB"

    def test_close_call_is_reported_as_a_coin_flip(self) -> None:
        roster = [player("A", "TE", 10.0), player("B", "TE", 10.3)]
        lineup = optimize_lineup(roster, {"TE": 1, "BN": 1})
        slot = lineup.slots[0]
        assert slot.is_toss_up
        assert any("coin flip" in reason for reason in slot.reasons)

    def test_clear_call_is_not_hedged(self) -> None:
        roster = [player("A", "TE", 4.0), player("B", "TE", 15.0)]
        slot = optimize_lineup(roster, {"TE": 1, "BN": 1}).slots[0]
        assert slot.starter.name == "B"
        assert not slot.is_toss_up
        assert not any("coin flip" in reason for reason in slot.reasons)

    def test_unfillable_slot_is_reported(self) -> None:
        lineup = optimize_lineup([player("Only WR", "WR", 10.0)], {"QB": 1, "WR": 1, "BN": 1})
        assert lineup.unfilled == ["QB"]
        assert len(lineup.slots) == 1

    def test_projected_total_sums_starters_only(self) -> None:
        roster = [player("A", "WR", 10.0), player("B", "WR", 8.0), player("C", "WR", 6.0)]
        lineup = optimize_lineup(roster, {"WR": 2, "BN": 1})
        assert lineup.projected_total == 18.0

    def test_bye_player_loses_his_slot(self) -> None:
        roster = [
            LineupCandidate(1, "Bye Guy", "TE", 15.0, is_on_bye=True),
            player("Available", "TE", 6.0),
        ]
        slot = optimize_lineup(roster, {"TE": 1, "BN": 1}).slots[0]
        assert slot.starter.name == "Available"
