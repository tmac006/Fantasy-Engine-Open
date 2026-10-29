"""Rest-of-season valuation and the injury status that feeds it.

The failure this guards against: on 2026-09-29 the trade engine still valued
De'Von Achane at his 257-point preseason total the day after he went on IR, and
every "normal" offer was built around him.
"""

import pytest

from api.adapters.espn import parse_roster_injury_statuses, parse_rosters
from api.engine.restofseason import (
    FINAL_FANTASY_WEEK,
    IR_MIN_GAMES,
    IR_RETURN_FACTOR,
    league_scoring_points,
    misses_this_week,
    normalize_status,
    per_game_rate,
    rest_of_season,
)
from api.engine.startsit import LineupCandidate, optimize_lineup, score_player
from api.engine.usage import WeeklyRow
from api.ingest.leagues import map_injury_statuses
from api.inseason import season_scoring_rates

# -- statuses ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("INJURY_RESERVE", "IR"),  # ESPN
        ("IR", "IR"),  # Sleeper injury_status
        ("Injured Reserve", "IR"),  # Sleeper status
        ("PUP", "IR"),
        ("OUT", "OUT"),
        ("Out", "OUT"),
        ("DOUBTFUL", "DOUBTFUL"),
        ("SUSPENSION", "SUSPENDED"),
        ("Sus", "SUSPENDED"),
        ("QUESTIONABLE", None),
        ("ACTIVE", None),
        ("DAY_TO_DAY", None),
        (None, None),
        ("", None),
    ],
)
def test_statuses_normalise_across_platforms(raw: str | None, expected: str | None) -> None:
    assert normalize_status(raw) == expected


def test_only_certain_absences_sit_this_week() -> None:
    assert misses_this_week("INJURY_RESERVE")
    assert misses_this_week("OUT")
    assert misses_this_week("SUSPENSION")
    assert not misses_this_week("QUESTIONABLE")
    assert not misses_this_week("DOUBTFUL")  # a call the manager makes, not us
    assert not misses_this_week(None)


# -- the schedule ----------------------------------------------------------------


def test_a_healthy_player_is_worth_only_the_weeks_left() -> None:
    ros = rest_of_season(preseason_total=170.0, current_week=4)
    assert sorted(ros.weekly) == list(range(4, FINAL_FANTASY_WEEK + 1))
    assert ros.points == pytest.approx(10.0 * 14)
    assert ros.availability_factor == 1.0


def test_his_bye_week_scores_nothing() -> None:
    ros = rest_of_season(preseason_total=170.0, current_week=4, bye_week=9)
    assert ros.weekly[9] == 0.0
    assert ros.points == pytest.approx(10.0 * 13)


def test_ir_zeroes_the_minimum_stay_and_discounts_the_return() -> None:
    ros = rest_of_season(preseason_total=257.0, current_week=4, injury_status="INJURY_RESERVE")
    rate = 257.0 / 17
    assert [ros.weekly[w] for w in range(4, 4 + IR_MIN_GAMES)] == [0.0] * IR_MIN_GAMES
    assert ros.weekly[8] == pytest.approx(rate * IR_RETURN_FACTOR)
    assert ros.points == pytest.approx(rate * IR_RETURN_FACTOR * (14 - IR_MIN_GAMES))
    assert ros.points < 0.5 * 257.0
    assert ros.games_missed == IR_MIN_GAMES
    assert ros.availability_factor == pytest.approx(IR_RETURN_FACTOR * 10 / 14)


def test_a_bye_does_not_count_toward_the_ir_stay() -> None:
    """IR minimums are games, not weeks."""
    ros = rest_of_season(
        preseason_total=170.0, current_week=4, bye_week=5, injury_status="IR"
    )
    assert [ros.weekly[w] for w in range(4, 9)] == [0.0] * 5
    assert ros.games_missed == IR_MIN_GAMES
    assert ros.weekly[9] > 0.0


def test_out_misses_only_this_week() -> None:
    ros = rest_of_season(preseason_total=170.0, current_week=4, injury_status="OUT")
    assert ros.weekly[4] == 0.0
    assert ros.weekly[5] == pytest.approx(10.0)


def test_questionable_changes_nothing() -> None:
    healthy = rest_of_season(preseason_total=170.0, current_week=4)
    questionable = rest_of_season(
        preseason_total=170.0, current_week=4, injury_status="QUESTIONABLE"
    )
    assert questionable.points == healthy.points


def test_a_stored_weekly_projection_beats_any_rate() -> None:
    ros = rest_of_season(
        preseason_total=170.0, current_week=4,
        weekly_projections={4: 22.0}, season_rate=5.0, games_played=3,
    )
    assert ros.weekly[4] == 22.0
    assert ros.weekly[5] == pytest.approx(ros.per_game)


def test_in_season_rate_takes_over_as_games_accumulate() -> None:
    preseason = 170.0  # 10 per game
    early, _ = per_game_rate(preseason, season_rate=4.0, games_played=1)
    later, _ = per_game_rate(preseason, season_rate=4.0, games_played=10)
    assert 4.0 < later < early < 10.0
    three, _ = per_game_rate(preseason, season_rate=4.0, games_played=3)
    assert three == pytest.approx(7.0), "equal weight after three games"


def test_no_games_played_falls_back_to_preseason() -> None:
    rate, source = per_game_rate(170.0, season_rate=None, games_played=0)
    assert rate == pytest.approx(10.0)
    assert source == "preseason projection"


def test_nothing_left_after_the_fantasy_final() -> None:
    ros = rest_of_season(preseason_total=170.0, current_week=FINAL_FANTASY_WEEK + 1)
    assert ros.points == 0.0
    assert ros.availability_factor == 1.0


# -- in-season scoring -----------------------------------------------------------


def test_league_scoring_interpolates_between_standard_and_ppr() -> None:
    stats = {"fantasy_points": 10.0, "fantasy_points_ppr": 16.0}
    assert league_scoring_points(stats, 0.0) == 10.0
    assert league_scoring_points(stats, 0.5) == 13.0
    assert league_scoring_points(stats, 1.0) == 16.0
    assert league_scoring_points({}, 1.0) is None


def test_season_rate_uses_only_weeks_already_played() -> None:
    history = {
        7: [
            WeeklyRow(week=1, stats={"fantasy_points": 10.0, "fantasy_points_ppr": 14.0}),
            WeeklyRow(week=2, stats={"fantasy_points": 20.0, "fantasy_points_ppr": 24.0}),
            WeeklyRow(week=4, stats={"fantasy_points": 90.0, "fantasy_points_ppr": 90.0}),
        ]
    }
    rates = season_scoring_rates(history, before_week=4, reception_value=0.5)
    assert rates[7] == (pytest.approx(17.0), 2)


# -- ESPN status ingest ------------------------------------------------------------


MROSTER = {
    "teams": [
        {
            "id": 2,
            "roster": {
                "entries": [
                    {"playerId": 4429795, "playerPoolEntry": {"player": {"injuryStatus": "INJURY_RESERVE"}}},
                    {"playerId": 3116406, "playerPoolEntry": {"player": {"injuryStatus": "ACTIVE"}}},
                    {"playerId": 555, "playerPoolEntry": {"player": {}}},
                ]
            },
        },
        {"id": 3, "roster": {"entries": [{"playerId": 777, "injuryStatus": "OUT"}]}},
    ]
}


def test_espn_roster_statuses_are_read_from_the_player_pool_entry() -> None:
    assert parse_rosters(MROSTER) == {"2": ["4429795", "3116406", "555"], "3": ["777"]}
    assert parse_roster_injury_statuses(MROSTER) == {
        "4429795": "INJURY_RESERVE",
        "3116406": "ACTIVE",
        "777": "OUT",
    }


def test_only_real_designations_are_stored_on_the_roster() -> None:
    statuses = parse_roster_injury_statuses(MROSTER)
    canonical = {"4429795": "101", "3116406": "102", "555": "103"}
    assert map_injury_statuses(["4429795", "3116406", "555"], statuses, canonical) == {
        "101": "INJURY_RESERVE"
    }
    # Unmapped players cannot be keyed, so they are skipped rather than guessed.
    assert map_injury_statuses(["777"], statuses, canonical) == {}


# -- start/sit -------------------------------------------------------------------------


def test_an_ir_player_scores_zero_whatever_his_rate() -> None:
    """The scoring-rate fallback knows nothing about availability."""
    scored = score_player(
        LineupCandidate(1, "Achane", "RB", 21.0, injury_status="INJURY_RESERVE")
    )
    assert scored.projected == 0.0
    assert "ruled out" in scored.adjustments[0].label


def test_an_ir_player_is_never_started_over_an_active_one() -> None:
    lineup = optimize_lineup(
        [
            LineupCandidate(1, "Achane", "RB", 21.0, injury_status="INJURY_RESERVE"),
            LineupCandidate(2, "Backup", "RB", 6.0),
        ],
        {"RB": 1, "BN": 1},
    )
    assert lineup.slots[0].starter.name == "Backup"


def test_questionable_players_are_still_scored() -> None:
    scored = score_player(LineupCandidate(1, "Q", "WR", 12.0, injury_status="QUESTIONABLE"))
    assert scored.projected == 12.0
