"""The seam between stored rows and the pure engines.

Model instances are built directly rather than round-tripped through the
database: these are the mapping rules, and a sign error here would be silent.
"""

from api.inseason import _environment
from api.models import GameLine


def game(**kwargs: object) -> GameLine:
    defaults: dict[str, object] = {
        "game_id": "2025_09_SF_SEA",
        "season": "2025",
        "week": 9,
        "home_team": "SEA",
        "away_team": "SF",
        "spread_line": 3.5,  # home favoured by 3.5
        "total_line": 45.0,
        "roof": "outdoors",
        "wind": 6.0,
    }
    return GameLine(**{**defaults, **kwargs})  # type: ignore[arg-type]


class TestImpliedTotals:
    def test_home_favourite_gets_the_larger_share(self) -> None:
        line = game()
        assert line.home_implied_total == 24.25
        assert line.away_implied_total == 20.75

    def test_shares_sum_to_the_total(self) -> None:
        line = game(spread_line=-7.5, total_line=51.0)
        assert line.home_implied_total is not None
        assert line.away_implied_total is not None
        assert round(line.home_implied_total + line.away_implied_total, 6) == 51.0

    def test_underdog_home_team_gets_the_smaller_share(self) -> None:
        line = game(spread_line=-6.0)
        assert line.home_implied_total is not None
        assert line.away_implied_total is not None
        assert line.home_implied_total < line.away_implied_total

    def test_missing_line_yields_none_not_zero(self) -> None:
        assert game(total_line=None).home_implied_total is None
        assert game(spread_line=None).away_implied_total is None

    def test_lookup_by_team(self) -> None:
        line = game()
        assert line.implied_total_for("SEA") == line.home_implied_total
        assert line.implied_total_for("SF") == line.away_implied_total
        assert line.implied_total_for("DAL") is None

    def test_opponent_lookup(self) -> None:
        line = game()
        assert line.opponent_of("SEA") == "SF"
        assert line.opponent_of("SF") == "SEA"
        assert line.opponent_of("DAL") is None


class TestEnvironmentMapping:
    def test_spread_is_flipped_for_the_away_team(self) -> None:
        """spread_line is the home margin; an away player's team spread inverts."""
        line = game(spread_line=3.5)
        home = _environment(line, "SEA")
        away = _environment(line, "SF")
        assert home.spread == 3.5
        assert away.spread == -3.5

    def test_each_side_gets_its_own_implied_total(self) -> None:
        line = game()
        assert _environment(line, "SEA").implied_total == 24.25
        assert _environment(line, "SF").implied_total == 20.75

    def test_conditions_carry_through(self) -> None:
        environment = _environment(game(wind=19.0, roof="outdoors"), "SEA")
        assert environment.is_windy
        assert environment.opponent == "SF"

    def test_dome_is_never_windy(self) -> None:
        assert not _environment(game(wind=30.0, roof="dome"), "SEA").is_windy

    def test_missing_game_is_empty_not_an_error(self) -> None:
        environment = _environment(None, "SEA")
        assert environment.implied_total is None
        assert not environment.is_windy

    def test_missing_team_is_empty(self) -> None:
        assert _environment(game(), None).implied_total is None
