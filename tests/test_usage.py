"""Point-in-time usage features, and the lookahead guard they rest on."""

from api.engine.usage import (
    WeeklyRow,
    player_form,
    summarize,
    weeks_before,
)


def row(week: int, **stats: float) -> WeeklyRow:
    return WeeklyRow(week=week, stats=dict(stats))


def ramping() -> list[WeeklyRow]:
    """A player whose role grows: bit part through week 4, starter from week 5."""
    return [
        row(1, targets=1, carries=0, offense_pct=0.10, fantasy_points_ppr=1.2, receiving_epa=0.1),
        row(2, targets=2, carries=0, offense_pct=0.14, fantasy_points_ppr=2.9, receiving_epa=0.4),
        row(3, targets=2, carries=1, offense_pct=0.18, fantasy_points_ppr=3.4, receiving_epa=0.2),
        row(4, targets=3, carries=0, offense_pct=0.22, fantasy_points_ppr=4.1, receiving_epa=0.6),
        row(5, targets=9, carries=1, offense_pct=0.71, fantasy_points_ppr=18.4, receiving_epa=6.0),
        row(6, targets=11, carries=0, offense_pct=0.80, fantasy_points_ppr=21.0, receiving_epa=7.2),
        row(7, targets=10, carries=2, offense_pct=0.83, fantasy_points_ppr=16.5, receiving_epa=4.8),
    ]


class TestLookaheadGuard:
    """The whole model is worthless if these fail."""

    def test_weeks_before_is_strict(self) -> None:
        kept = weeks_before(ramping(), as_of_week=5)
        assert [r.week for r in kept] == [1, 2, 3, 4], "week 5 itself must not be included"

    def test_form_ignores_every_later_week(self) -> None:
        # Going into week 5 he looks like a nobody, because that is all that was
        # known at the time. His breakout is in weeks 5-7 and must not leak back.
        form = player_form(ramping(), as_of_week=5)
        assert form.to_date.games == 4
        assert form.to_date.targets_per_game == 2.0
        assert form.to_date.ppr_per_game is not None
        assert form.to_date.ppr_per_game < 5.0

    def test_same_history_scored_later_sees_the_breakout(self) -> None:
        early = player_form(ramping(), as_of_week=5)
        late = player_form(ramping(), as_of_week=8)
        assert early.to_date.ppr_per_game is not None
        assert late.to_date.ppr_per_game is not None
        assert late.to_date.ppr_per_game > early.to_date.ppr_per_game

    def test_unplayed_weeks_are_absent_not_zero(self) -> None:
        # A missed week must not drag averages toward zero; it is simply no data.
        played = [row(1, targets=10, fantasy_points_ppr=20.0), row(3, targets=10, fantasy_points_ppr=20.0)]
        window = player_form(played, as_of_week=4).to_date
        assert window.games == 2
        assert window.targets_per_game == 10.0
        assert window.ppr_per_game == 20.0


class TestWindows:
    def test_recent_counts_games_played_not_calendar_weeks(self) -> None:
        # Weeks 1 and 2 played, then out until week 7. Going into week 8 the last
        # three *games* are weeks 1, 2 and 7 -- not an empty window.
        rows = [
            row(1, targets=5, carries=0, offense_pct=0.5, fantasy_points_ppr=8.0),
            row(2, targets=6, carries=0, offense_pct=0.5, fantasy_points_ppr=9.0),
            row(7, targets=7, carries=0, offense_pct=0.5, fantasy_points_ppr=10.0),
        ]
        form = player_form(rows, as_of_week=8, recent_weeks=3)
        assert form.recent.games == 3
        assert form.baseline.is_empty

    def test_role_change_shows_as_opportunity_delta(self) -> None:
        form = player_form(ramping(), as_of_week=8)
        assert form.recent.games == 3  # weeks 5, 6, 7
        assert form.baseline.games == 4  # weeks 1-4
        delta = form.opportunity_delta
        assert delta is not None and delta > 7.0
        assert form.is_emerging

    def test_steady_player_is_not_emerging(self) -> None:
        rows = [
            row(w, targets=6, carries=0, offense_pct=0.62, fantasy_points_ppr=11.0)
            for w in range(1, 8)
        ]
        form = player_form(rows, as_of_week=8)
        assert form.opportunity_delta == 0.0
        assert not form.is_emerging

    def test_delta_is_none_without_a_baseline(self) -> None:
        rows = [row(6, targets=8, fantasy_points_ppr=14.0), row(7, targets=9, fantasy_points_ppr=15.0)]
        form = player_form(rows, as_of_week=8)
        assert form.baseline.is_empty
        assert form.opportunity_delta is None, "a debut is not a role change"


class TestSummarize:
    def test_empty_is_empty(self) -> None:
        assert summarize([]).is_empty

    def test_stdev_needs_two_games(self) -> None:
        assert summarize([row(1, fantasy_points_ppr=10.0)]).ppr_stdev is None
        assert summarize(
            [row(1, fantasy_points_ppr=10.0), row(2, fantasy_points_ppr=20.0)]
        ).ppr_stdev == 5.0

    def test_efficiency_suppressed_on_tiny_denominators(self) -> None:
        # One target on a 4% snap share: any per-opportunity rate here is noise.
        window = summarize([row(1, targets=1, carries=0, offense_pct=0.04, receiving_epa=3.0)])
        assert window.epa_per_opportunity is None

    def test_efficiency_computed_on_real_volume(self) -> None:
        window = summarize(
            [
                row(1, targets=8, carries=0, offense_pct=0.7, receiving_epa=4.0),
                row(2, targets=12, carries=0, offense_pct=0.8, receiving_epa=6.0),
            ]
        )
        assert window.epa_per_opportunity == 0.5  # 10.0 EPA / 20 opportunities
