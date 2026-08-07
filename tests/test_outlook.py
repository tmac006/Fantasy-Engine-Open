"""Risk and reward meters: calibration, positional scaling, and honest labels."""

from api.engine.outlook import (
    CEILING_ANCHORS,
    MAX_BUST_PROBABILITY,
    MIN_BUST_PROBABILITY,
    bust_probability,
    outlook,
    projected_ceiling,
)
from api.engine.usage import WeeklyRow, player_form


def form(points: list[float], snap: float | None = 0.7, as_of: int = 12):
    rows = []
    for index, value in enumerate(points, start=1):
        stats: dict[str, float] = {"fantasy_points_ppr": value, "targets": 6.0}
        if snap is not None:
            stats["offense_pct"] = snap
        rows.append(WeeklyRow(week=index, stats=stats))
    return player_form(rows, as_of_week=as_of)


# Identical totals (68.0 over six games), opposite week-to-week shapes.
STEADY = [11.0, 12.0, 10.0, 11.5, 12.5, 11.0]
SWINGY = [2.0, 24.0, 1.0, 21.0, 3.0, 17.0]


class TestBustProbability:
    def test_low_snap_share_raises_risk(self) -> None:
        buried = bust_probability(form(STEADY, snap=0.15))
        featured = bust_probability(form(STEADY, snap=0.85))
        assert buried > featured

    def test_volatility_raises_risk(self) -> None:
        assert bust_probability(form(SWINGY)) > bust_probability(form(STEADY))

    def test_stays_inside_the_fitted_range(self) -> None:
        worst = bust_probability(form([0.5, 40.0, 0.2, 38.0], snap=0.01))
        best = bust_probability(form([12.0] * 6, snap=1.0))
        assert MIN_BUST_PROBABILITY <= best <= worst <= MAX_BUST_PROBABILITY

    def test_missing_snap_data_does_not_spike_risk(self) -> None:
        unknown = bust_probability(form(STEADY, snap=None))
        assert MIN_BUST_PROBABILITY < unknown < 0.45


class TestCeiling:
    def test_scales_with_scoring_rate(self) -> None:
        low = projected_ceiling(form([4.0] * 6))
        high = projected_ceiling(form([18.0] * 6))
        assert low is not None and high is not None and high > low

    def test_ceiling_exceeds_the_average_week(self) -> None:
        ceiling = projected_ceiling(form([10.0] * 6))
        assert ceiling is not None and ceiling > 10.0

    def test_volatility_does_not_raise_the_ceiling(self) -> None:
        """Measured three ways in 2025: boom/bust does not buy upside."""
        steady = projected_ceiling(form(STEADY))
        swingy = projected_ceiling(form(SWINGY))
        assert steady is not None and swingy is not None
        assert abs(steady - swingy) < 1.0, "same scoring rate, same ceiling"


class TestMeters:
    def test_position_changes_the_reward_reading(self) -> None:
        """12 PPR/gm is an elite tight end and a fringe quarterback."""
        played = form([12.0] * 6)
        te = outlook(played, "TE")
        qb = outlook(played, "QB")
        assert te is not None and qb is not None
        assert te.reward > qb.reward

    def test_every_position_has_anchors_or_a_default(self) -> None:
        played = form([10.0] * 6)
        for position in (*CEILING_ANCHORS, "FB", ""):
            result = outlook(played, position)
            assert result is not None
            assert 0 <= result.reward <= 100

    def test_meters_are_bounded(self) -> None:
        for points, snap in (([0.4] * 6, 0.02), ([34.0] * 6, 1.0)):
            result = outlook(form(points, snap=snap), "RB")
            assert result is not None
            assert 0 <= result.risk <= 100
            assert 0 <= result.reward <= 100

    def test_too_few_games_yields_nothing(self) -> None:
        assert outlook(form([12.0], as_of=2), "RB") is None

    def test_probability_is_reported_alongside_the_bar(self) -> None:
        result = outlook(form(STEADY), "RB")
        assert result is not None
        assert 0.0 < result.bust_probability < 1.0
        assert any("dud week" in d for d in result.drivers)

    def test_buried_player_is_flagged_by_snap_share(self) -> None:
        result = outlook(form(STEADY, snap=0.12), "WR")
        assert result is not None
        assert any("snaps" in d for d in result.drivers)


class TestLabels:
    def test_workhorse_reads_as_a_safe_starter(self) -> None:
        result = outlook(form([17.0, 18.0, 16.0, 19.0, 17.5, 18.5], snap=0.9), "RB")
        assert result is not None
        assert result.label == "safe starter"

    def test_low_usage_scattershot_reads_as_a_dart_throw(self) -> None:
        result = outlook(form([0.0, 9.0, 0.0, 1.0, 0.0, 8.0], snap=0.14), "WR")
        assert result is not None
        assert result.label == "dart throw"

    def test_label_matches_the_meters(self) -> None:
        for points, snap, position in (
            ([17.0] * 6, 0.9, "RB"),
            ([3.0] * 6, 0.2, "WR"),
            ([12.0] * 6, 0.5, "TE"),
        ):
            result = outlook(form(points, snap=snap), position)
            assert result is not None
            high_reward = result.reward >= 50
            high_risk = result.risk >= 50
            expected = {
                (True, False): "safe starter",
                (True, True): "high ceiling, shaky role",
                (False, False): "steady but capped",
                (False, True): "dart throw",
            }[(high_reward, high_risk)]
            assert result.label == expected
