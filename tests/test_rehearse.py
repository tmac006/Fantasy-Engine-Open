"""Pre-draft gate checks.

Wrong scoring is the quietest way to ruin a draft. Every projection is rescored
under the league's table, so a misconfigured league produces a board that looks
entirely normal while ranking the wrong players. A real 16-team ESPN league came
back with no yardage scoring at all, which is why this exists.
"""

from api.eval.rehearse import parse_slots, rounds_for, scoring_warnings
from api.schemas import LeagueSettings

FULL_PPR = {
    "rec": 1.0, "pass_td": 4.0, "rush_td": 6.0, "rec_td": 6.0,
    "pass_yd": 0.04, "rush_yd": 0.1, "rec_yd": 0.1,
}
SLOTS = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "K": 1, "DEF": 1, "BN": 7}


def league(scoring: dict[str, float], teams: int = 12) -> LeagueSettings:
    return LeagueSettings(scoring=scoring, roster_slots=SLOTS, total_teams=teams)


class TestScoringWarnings:
    def test_a_normal_table_is_quiet(self) -> None:
        assert scoring_warnings(league(FULL_PPR)) == []

    def test_no_yardage_at_all_is_flagged(self) -> None:
        """The real FUN CREW payload: receptions and touchdowns, no yards."""
        scoring = {k: v for k, v in FULL_PPR.items() if not k.endswith("_yd")}
        warnings = scoring_warnings(league(scoring, teams=16))
        assert len(warnings) == 1
        assert "no yardage at all" in warnings[0]

    def test_partial_yardage_is_flagged_too(self) -> None:
        """Half a table is likelier to be a mistake than a house rule."""
        scoring = {**FULL_PPR}
        del scoring["rec_yd"]
        warnings = scoring_warnings(league(scoring))
        assert warnings and "rec_yd" in warnings[0]

    def test_zero_counts_as_absent(self) -> None:
        """ESPN omits a stat entirely; a hand-built table may write 0.0."""
        scoring = {**FULL_PPR, "pass_yd": 0.0, "rush_yd": 0.0, "rec_yd": 0.0}
        assert scoring_warnings(league(scoring))

    def test_touchdown_only_is_called_out_separately(self) -> None:
        warnings = scoring_warnings(league({"pass_td": 4.0, "rush_td": 6.0, "rec_td": 6.0}))
        assert any("touchdown-only" in w for w in warnings)


class TestSlotParsing:
    def test_round_count_includes_the_bench(self) -> None:
        assert rounds_for(league(FULL_PPR)) == 16

    def test_slots_parse(self) -> None:
        assert parse_slots("QB:1,RB:2,BN:7") == {"QB": 1, "RB": 2, "BN": 7}

    def test_lowercase_is_accepted(self) -> None:
        assert parse_slots("qb:1, rb:2") == {"QB": 1, "RB": 2}

    def test_a_malformed_spec_raises_rather_than_skipping(self) -> None:
        """Skipping a bad entry would rehearse a different league than asked."""
        for bad in ("QB:1,RB", "QB:one", ":3", "QB:1,,RB:2"):
            try:
                parse_slots(bad)
            except ValueError:
                continue
            raise AssertionError(f"{bad!r} should have raised")
