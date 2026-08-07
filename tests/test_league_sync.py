"""League roster mapping: gaps must surface, never vanish.

Persistence is exercised end-to-end against the real league registry; this
covers the mapping rule itself, which is where a silent miss would originate.
"""

from api.ingest.leagues import map_roster_players


def test_maps_every_known_player() -> None:
    matched, missing = map_roster_players(
        ["s10", "s11", "s12"], {"s10": "100", "s11": "101", "s12": "102"}
    )
    assert matched == ["100", "101", "102"]
    assert missing == []


def test_unmapped_players_are_returned_not_dropped() -> None:
    matched, missing = map_roster_players(["known", "ghost"], {"known": "100"})
    assert matched == ["100"]
    assert missing == ["ghost"], "an unmappable player must be reported, not skipped"


def test_roster_order_is_preserved() -> None:
    canonical = {"a": "1", "b": "2", "c": "3"}
    matched, _ = map_roster_players(["c", "a", "b"], canonical)
    assert matched == ["3", "1", "2"]


def test_non_string_platform_ids_are_coerced() -> None:
    """Sleeper hands back strings, ESPN hands back ints; both must map."""
    matched, missing = map_roster_players([4429795], {"4429795": "500"})  # type: ignore[list-item]
    assert matched == ["500"]
    assert missing == []


def test_empty_roster_is_not_an_error() -> None:
    assert map_roster_players([], {"a": "1"}) == ([], [])


def test_all_missing_reports_all() -> None:
    matched, missing = map_roster_players(["x", "y"], {})
    assert matched == []
    assert missing == ["x", "y"]
