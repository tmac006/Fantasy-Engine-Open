"""Per-game yardage bonuses estimated from season projections.

"My 2026 League" pays +1/+2 for 100/200-yd receiving, 300/400 passing and
100/200 rushing games. No projection source carries bonus keys, so every
bonus scored as zero -- a bias aimed precisely at rounds 1-3.
"""

from api.engine.scoring import expected_yardage_bonus_points, score_stats

BONUS_SCORING = {
    "rec": 1.0,
    "rec_yd": 0.1,
    "rush_yd": 0.1,
    "pass_yd": 0.04,
    "bonus_rec_yd_100": 1.0,
    "bonus_rec_yd_200": 2.0,
    "bonus_rush_yd_100": 1.0,
    "bonus_rush_yd_200": 2.0,
    "bonus_pass_yd_300": 1.0,
    "bonus_pass_yd_400": 2.0,
}


def test_no_bonus_keys_means_no_change() -> None:
    stats = {"rec": 90.0, "rec_yd": 1400.0}
    plain = {"rec": 1.0, "rec_yd": 0.1}
    assert score_stats(stats, plain) == 90.0 + 140.0
    assert expected_yardage_bonus_points(stats, plain) == 0.0


def test_elite_receiver_gains_meaningful_bonus_points() -> None:
    # ~82 yds/game: historically ~5-7 games over 100. The audit measured the
    # gap at ~5-10 points for elite volume players.
    bonus = expected_yardage_bonus_points({"rec_yd": 1400.0, "gp": 17.0}, BONUS_SCORING)
    assert 3.0 < bonus < 10.0, bonus


def test_elite_quarterback_gains_meaningful_bonus_points() -> None:
    bonus = expected_yardage_bonus_points({"pass_yd": 4400.0, "gp": 17.0}, BONUS_SCORING)
    assert 2.0 < bonus < 9.0, bonus


def test_low_volume_player_gains_almost_nothing() -> None:
    bonus = expected_yardage_bonus_points({"rec_yd": 300.0, "gp": 17.0}, BONUS_SCORING)
    assert bonus < 0.2, bonus


def test_more_yards_means_more_bonus() -> None:
    low = expected_yardage_bonus_points({"rush_yd": 900.0}, BONUS_SCORING)
    high = expected_yardage_bonus_points({"rush_yd": 1400.0}, BONUS_SCORING)
    assert high > low > 0.0


def test_source_supplied_bonus_stats_are_not_double_counted() -> None:
    # If a source ever ships real bonus projections, the base scoring loop
    # pays them and the estimator must stand down.
    stats = {"rec_yd": 1400.0, "bonus_rec_yd_100": 6.0, "bonus_rec_yd_200": 0.0}
    scoring = {"bonus_rec_yd_100": 1.0, "bonus_rec_yd_200": 2.0}
    assert score_stats(stats, scoring) == 6.0


def test_bands_do_not_overlap() -> None:
    # A 200+ game pays the 200 bonus, not both: expected 100-band games plus
    # expected 200-band games can never exceed total games.
    scoring_100_only = {"bonus_rush_yd_100": 1.0}
    scoring_200_only = {"bonus_rush_yd_200": 1.0}
    both = {"bonus_rush_yd_100": 1.0, "bonus_rush_yd_200": 1.0}
    stats = {"rush_yd": 1800.0, "gp": 17.0}
    banded_100 = expected_yardage_bonus_points(stats, scoring_100_only)
    tail_200 = expected_yardage_bonus_points(stats, scoring_200_only)
    combined = expected_yardage_bonus_points(stats, both)
    assert abs(combined - (banded_100 + tail_200)) < 1e-9
    assert combined < 17.0


def test_scoring_integration_boosts_bonus_league_totals() -> None:
    stats = {"rec": 90.0, "rec_yd": 1400.0, "gp": 17.0}
    without = score_stats(stats, {"rec": 1.0, "rec_yd": 0.1})
    with_bonus = score_stats(stats, {**BONUS_SCORING})
    assert with_bonus > without
