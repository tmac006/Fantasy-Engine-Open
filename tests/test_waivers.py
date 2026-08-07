"""Waiver scoring: the ranking signal, the safety gate, and what stays context."""

from api.engine.usage import WeeklyRow, player_form
from api.engine.waivers import (
    REGRESSION_FLAG_FPOE,
    Candidate,
    expected_points,
    points_over_expected,
    rank_waiver_targets,
    score_candidate,
)


def row(week: int, **stats: float) -> WeeklyRow:
    return WeeklyRow(week=week, stats=dict(stats))


def candidate(
    name: str, position: str, rows: list[WeeklyRow], as_of_week: int = 8, **kwargs: object
) -> Candidate:
    return Candidate(
        canonical_id=abs(hash(name)) % 100_000,
        name=name,
        position=position,
        team="KC",
        form=player_form(rows, as_of_week=as_of_week),
        **kwargs,  # type: ignore[arg-type]
    )


def steady(points: float, snap: float = 0.7, targets: float = 6.0) -> list[WeeklyRow]:
    return [
        row(w, targets=targets, carries=0, offense_pct=snap, fantasy_points_ppr=points)
        for w in range(1, 8)
    ]


class TestExpectedPoints:
    def test_scales_with_opportunity(self) -> None:
        light = player_form(steady(8.0, targets=3.0), as_of_week=8).recent
        heavy = player_form(steady(8.0, targets=9.0), as_of_week=8).recent
        low, high = expected_points(light, "WR"), expected_points(heavy, "WR")
        assert low is not None and high is not None and high > low

    def test_position_rates_differ(self) -> None:
        window = player_form(steady(10.0, targets=6.0), as_of_week=8).recent
        rb, te = expected_points(window, "RB"), expected_points(window, "TE")
        assert rb is not None and te is not None
        assert te > rb, "a target is worth more to a TE than to a RB in PPR"

    def test_unknown_position_uses_the_pooled_rate(self) -> None:
        window = player_form(steady(10.0), as_of_week=8).recent
        assert expected_points(window, "FB") is not None

    def test_quarterbacks_are_valued_on_pass_attempts(self) -> None:
        """Scoring a QB on targets and carries measures only his scrambles."""
        rows = [
            WeeklyRow(
                week=w,
                stats={"attempts": 34.0, "carries": 4.0, "offense_pct": 1.0,
                       "fantasy_points_ppr": 19.0},
            )
            for w in range(1, 8)
        ]
        window = player_form(rows, as_of_week=8).recent
        expected = expected_points(window, "QB")
        assert expected is not None
        # 34 attempts alone is a starter's workload and must dominate the number.
        assert expected > 12.0

    def test_quarterback_fpoe_is_not_wildly_negative(self) -> None:
        """Regression check: with no attempts term, a QB looked +20 over expected."""
        rows = [
            WeeklyRow(
                week=w,
                stats={"attempts": 34.0, "carries": 4.0, "offense_pct": 1.0,
                       "fantasy_points_ppr": 19.0},
            )
            for w in range(1, 8)
        ]
        window = player_form(rows, as_of_week=8).recent
        fpoe = points_over_expected(window, "QB")
        assert fpoe is not None
        assert abs(fpoe) < REGRESSION_FLAG_FPOE, "a typical starting QB is not an outlier"

    def test_no_opportunity_is_none_not_zero(self) -> None:
        window = player_form(
            [row(w, targets=0, carries=0, offense_pct=0.4, fantasy_points_ppr=0.0) for w in range(1, 8)],
            as_of_week=8,
        ).recent
        assert expected_points(window, "WR") is None

    def test_fpoe_signs_the_right_way(self) -> None:
        # Same six targets a game; one scores far more than that usage supports.
        lucky = player_form(steady(22.0, targets=6.0), as_of_week=8).recent
        unlucky = player_form(steady(4.0, targets=6.0), as_of_week=8).recent
        over, under = points_over_expected(lucky, "WR"), points_over_expected(unlucky, "WR")
        assert over is not None and over > 0
        assert under is not None and under < 0


class TestScoring:
    def test_score_is_season_scoring_rate(self) -> None:
        target = score_candidate(candidate("Steady Sam", "WR", steady(12.0)))
        assert target is not None
        assert target.score == 12.0

    def test_benched_player_is_discounted(self) -> None:
        # Good early scoring, then buried: weeks 5-7 at a 6% snap share.
        rows = steady(14.0)[:4] + [
            row(w, targets=0, carries=0, offense_pct=0.06, fantasy_points_ppr=0.4)
            for w in range(5, 8)
        ]
        benched = score_candidate(candidate("Buried Ben", "WR", rows))
        playing = score_candidate(candidate("Playing Pete", "WR", steady(8.4)))
        assert benched is not None and playing is not None
        assert not benched.role_intact
        assert benched.score < playing.score, "season average must not outrank an actual role"

    def test_missing_snap_data_does_not_penalize(self) -> None:
        rows = [row(w, targets=6, carries=0, fantasy_points_ppr=11.0) for w in range(1, 8)]
        target = score_candidate(candidate("No Snaps Nick", "WR", rows))
        assert target is not None
        assert target.role_intact
        assert target.score == 11.0

    def test_unplayed_candidate_is_dropped(self) -> None:
        assert score_candidate(candidate("Ghost", "WR", [], as_of_week=8)) is None

    def test_ranking_orders_by_score(self) -> None:
        ranked = rank_waiver_targets(
            [
                candidate("Low", "WR", steady(5.0)),
                candidate("High", "WR", steady(15.0)),
                candidate("Mid", "WR", steady(10.0)),
            ]
        )
        assert [t.name for t in ranked] == ["High", "Mid", "Low"]

    def test_limit_is_respected(self) -> None:
        pool = [candidate(f"P{i}", "WR", steady(float(i))) for i in range(1, 20)]
        assert len(rank_waiver_targets(pool, limit=5)) == 5


class TestContextStaysContext:
    """Backtested as unhelpful or harmful for ranking -- reported, never scored."""

    def _emerging(self) -> list[WeeklyRow]:
        quiet = [
            row(w, targets=1, carries=0, offense_pct=0.12, fantasy_points_ppr=1.5)
            for w in range(1, 5)
        ]
        breakout = [
            row(w, targets=10, carries=0, offense_pct=0.82, fantasy_points_ppr=17.0)
            for w in range(5, 8)
        ]
        return quiet + breakout

    def test_emergence_is_surfaced(self) -> None:
        target = score_candidate(candidate("Breakout Brad", "WR", self._emerging()))
        assert target is not None
        assert target.is_emerging
        assert any("role growing" in note for note in target.context)

    def test_emergence_does_not_inflate_the_score(self) -> None:
        # Identical season scoring rate, one arrived at by a breakout. The
        # breakout must not rank higher for being a breakout.
        emerging = score_candidate(candidate("Breakout Brad", "WR", self._emerging()))
        assert emerging is not None
        flat = score_candidate(
            candidate("Flat Frank", "WR", steady(emerging.season_ppg or 0.0))
        )
        assert flat is not None
        assert emerging.score == flat.score

    def test_regression_warning_appears_without_changing_score(self) -> None:
        target = score_candidate(candidate("Lucky Luke", "WR", steady(22.0, targets=4.0)))
        assert target is not None
        assert target.score == 22.0
        assert any("regression" in note for note in target.context)

    def test_regression_bar_scales_with_expectation(self) -> None:
        """A flag that fires on most starters carries no information.

        The same 5-point gap is an outlier on a low-usage receiver and noise on
        a quarterback whose usage already projects near 20.
        """
        small = score_candidate(candidate("Low Usage Lou", "WR", steady(9.0, targets=2.0)))
        assert small is not None
        assert any("regression" in note for note in small.context)

        qb_rows = [
            WeeklyRow(
                week=w,
                stats={"attempts": 36.0, "carries": 4.0, "offense_pct": 1.0,
                       "fantasy_points_ppr": 23.0},
            )
            for w in range(1, 8)
        ]
        starter = score_candidate(
            Candidate(1, "Volume QB", "QB", "KC", player_form(qb_rows, as_of_week=8))
        )
        assert starter is not None
        assert starter.points_over_expected is not None
        assert starter.points_over_expected > 4.0, "absolute gap alone would have flagged him"
        assert not any("regression" in note for note in starter.context)

    def test_supplied_notes_pass_through_uncounted(self) -> None:
        target = score_candidate(
            candidate(
                "Backup Bill",
                "RB",
                steady(6.0),
                context_notes=("starter ahead of him is on IR",),
            )
        )
        assert target is not None
        assert target.score == 6.0
        assert "starter ahead of him is on IR" in target.context
