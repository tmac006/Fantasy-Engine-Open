"""Player ingest: team assignment must track the source, including releases.

The stale-team hole: `row.team = p.get("team") or row.team` meant a stored
team could never be cleared, so every player cut after his row was created
kept passing the free-agent pool filter with full projections (Raheem Mostert
sat in the pool as LVR with 46 ESPN points). NFL cutdown day lands days
before the drafts — these tests pin the reverse gear.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from api.ingest.players import ingest_sleeper_players
from api.models import Player, PlayerIds
from api.services import is_free_agent_team


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite://")
    for table in (PlayerIds.__table__, Player.__table__):
        table.create(engine)
    return sessionmaker(bind=engine)()


def sleeper_player(name: str, position: str, team: str | None) -> dict:
    first, _, last = name.partition(" ")
    return {
        "full_name": name,
        "first_name": first,
        "last_name": last,
        "position": position,
        "team": team,
        "status": "Active",
        "search_rank": 50,
    }


def test_release_clears_stored_team(session: Session) -> None:
    session.add(
        PlayerIds(
            full_name="Raheem Mostert",
            merge_name="raheem mostert",
            position="RB",
            team="LVR",
            sleeper_id="s1",
        )
    )
    session.commit()

    ingest_sleeper_players(session, {"s1": sleeper_player("Raheem Mostert", "RB", None)})

    row = session.scalars(sqla_select_ids(session, "s1")).one()
    assert row.team is None, "a released player's stored team must be cleared"
    player = session.get(Player, row.canonical_id)
    assert player is not None and player.team is None


def test_signing_updates_stored_team(session: Session) -> None:
    session.add(
        PlayerIds(
            full_name="Some Guy",
            merge_name="some guy",
            position="WR",
            team=None,
            sleeper_id="s2",
        )
    )
    session.commit()

    ingest_sleeper_players(session, {"s2": sleeper_player("Some Guy", "WR", "KC")})

    row = session.scalars(sqla_select_ids(session, "s2")).one()
    assert row.team == "KC"


def sqla_select_ids(session: Session, sleeper_id: str):
    from sqlalchemy import select

    return select(PlayerIds).where(PlayerIds.sleeper_id == sleeper_id)


# -- free-agent filter -------------------------------------------------------


@pytest.mark.parametrize("team", [None, "", "  ", "FA", "fa", "FA*", "NONE"])
def test_free_agent_codes_are_filtered(team: str | None) -> None:
    assert is_free_agent_team(team), f"{team!r} must read as team-less"


@pytest.mark.parametrize("team", ["KC", "LVR", "SF", "NE", "JAX", "WSH"])
def test_real_teams_pass_the_filter(team: str) -> None:
    assert not is_free_agent_team(team)


# -- namesake collisions -----------------------------------------------------
# make_merge_name strips Jr/Sr/II, so Frank Gore Jr. and Sr. (both RB) share a
# merge name. The old one-row-per-name index silently handed the second
# player's ids to the first and skipped his projections.


def test_namesake_gets_his_own_row_not_merged(session: Session) -> None:
    session.add(
        PlayerIds(
            full_name="Frank Gore",
            merge_name="frank gore",
            position="RB",
            team=None,
            sleeper_id="sr-1",
            gsis_id="00-0001",
        )
    )
    session.commit()

    ingest_sleeper_players(session, {"jr-9": sleeper_player("Frank Gore Jr.", "RB", "BUF")})

    from sqlalchemy import select

    rows = session.scalars(select(PlayerIds)).all()
    assert len(rows) == 2, "the namesake must get his own identity, not be merged"
    senior = next(r for r in rows if r.sleeper_id == "sr-1")
    junior = next(r for r in rows if r.sleeper_id == "jr-9")
    assert senior.gsis_id == "00-0001"
    assert junior.canonical_id != senior.canonical_id


def test_ambiguous_name_refuses_name_joins(session: Session) -> None:
    from api.ingest.players import CrosswalkIndex

    for i, sleeper_id in enumerate(("kw-1", "kw-2")):
        session.add(
            PlayerIds(
                full_name="Kyle Williams",
                merge_name="kyle williams",
                position="WR",
                sleeper_id=sleeper_id,
                canonical_id=i + 1,
            )
        )
    session.commit()

    idx = CrosswalkIndex(session)
    assert ("kyle williams", "WR") in idx.ambiguous
    # A pure name lookup must refuse to guess between them.
    assert idx.find(None, None, None, "kyle williams", "WR") is None
    # An id-anchored lookup still works.
    assert idx.find("kw-2", None, None, "kyle williams", "WR").canonical_id == 2


def test_name_match_contradicted_by_known_id_is_rejected(session: Session) -> None:
    from api.ingest.players import CrosswalkIndex

    session.add(
        PlayerIds(
            full_name="Marvin Harrison",
            merge_name="marvin harrison",
            position="WR",
            sleeper_id="hof-1",
            canonical_id=1,
        )
    )
    session.commit()

    idx = CrosswalkIndex(session)
    # Same name, but the caller carries a different sleeper id: namesake.
    assert idx.find("rookie-2", None, None, "marvin harrison", "WR") is None
