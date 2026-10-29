"""League roster mapping: gaps must surface, never vanish.

Persistence is exercised end-to-end against the real league registry; this
covers the mapping rule itself, which is where a silent miss would originate.
"""

from sqlalchemy import create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from api.ingest.leagues import _store_rosters, map_roster_players
from api.models import League, LeagueRoster


@compiles(JSONB, "sqlite")
def _jsonb_as_json_on_sqlite(type_, compiler, **kw):
    return "JSON"


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


# -- roster persistence ------------------------------------------------------


def test_departed_teams_rosters_are_deleted() -> None:
    """A pure upsert left departed teams' rosters in place forever, so their
    players read as owned in-season and vanished from waiver advice."""
    engine = create_engine("sqlite://")
    for table in (League.__table__, LeagueRoster.__table__):
        table.create(engine)
    session = sessionmaker(bind=engine)()
    league = League(
        id=1, platform="espn", platform_league_id="L1", season="2026", my_team_id="3"
    )
    session.add(league)
    for roster_id in ("3", "15", "16"):
        session.add(
            LeagueRoster(
                league_id=1,
                platform_roster_id=roster_id,
                player_ids=["9"],
                unmapped_player_ids=[],
            )
        )
    session.commit()

    _store_rosters(
        session,
        league,
        rosters={"3": ["e1"], "4": ["e2"]},
        canonical={"e1": "100", "e2": "101"},
    )
    session.commit()

    remaining = {
        row.platform_roster_id
        for row in session.scalars(select(LeagueRoster).where(LeagueRoster.league_id == 1))
    }
    assert remaining == {"3", "4"}, "teams 15/16 left the league; their rosters must go too"
