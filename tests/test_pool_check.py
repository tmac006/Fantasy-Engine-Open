"""Pool health gate: the tripwire for silent-absence bugs.

A clean rehearsal cannot catch a player the engine never heard of; this gate
checks the inputs themselves (top-ADP coverage, namesakes, data age, room
size, placeholder clusters).
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker

from api.engine.vor import PoolPlayer
from api.eval.pool_check import pool_health_problems
from api.models import Adp, PlayerIds, Projection
from api.schemas import LeagueSettings


@compiles(JSONB, "sqlite")
def _jsonb_as_json_on_sqlite(type_, compiler, **kw):
    return "JSON"


NOW = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)

SETTINGS = LeagueSettings(
    scoring={"rec": 1.0},
    roster_slots={"QB": 1, "RB": 2, "WR": 2, "TE": 1, "BN": 5},
    total_teams=12,
)


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite://")
    for table in (PlayerIds.__table__, Projection.__table__, Adp.__table__):
        table.create(engine)
    return sessionmaker(bind=engine)()


def seed(session: Session, count: int = 10, fetched_at: datetime | None = None) -> list[PoolPlayer]:
    """count players with ADP 1..count, all present in the returned pool."""
    fetched = fetched_at or NOW - timedelta(hours=6)
    pool = []
    for i in range(1, count + 1):
        session.add(
            PlayerIds(canonical_id=i, full_name=f"Player {i}", merge_name=f"player {i}", position="RB")
        )
        session.add(
            Adp(canonical_id=i, source="sleeper", format="ppr", teams=None, value=float(i), fetched_at=fetched)
        )
        session.add(
            Projection(canonical_id=i, source="sleeper", season="2026", week=None, points=200.0 - i, stats={}, fetched_at=fetched)
        )
        pool.append(
            PoolPlayer(canonical_id=str(i), name=f"Player {i}", position="RB", team="KC", points=200.0 - i, adp=float(i))
        )
    session.commit()
    return pool


def test_healthy_pool_reports_nothing(session: Session) -> None:
    pool = seed(session)
    assert pool_health_problems(session, SETTINGS, pool, top_n=10, now=NOW) == []


def test_missing_top_adp_player_is_reported(session: Session) -> None:
    pool = seed(session)
    short_pool = [p for p in pool if p.canonical_id != "3"]  # ADP 3 vanished
    problems = pool_health_problems(session, SETTINGS, short_pool, top_n=10, now=NOW)
    assert any("Player 3" in p for p in problems), problems


def test_namesake_in_pool_is_reported(session: Session) -> None:
    pool = seed(session)
    pool.append(
        PoolPlayer(canonical_id="99", name="Player 1", position="RB", team="SF", points=100.0)
    )
    problems = pool_health_problems(session, SETTINGS, pool, top_n=10, now=NOW)
    assert any("namesake" in p for p in problems), problems


def test_stale_data_is_reported(session: Session) -> None:
    pool = seed(session, fetched_at=NOW - timedelta(days=20))
    problems = pool_health_problems(session, SETTINGS, pool, top_n=10, now=NOW)
    assert sum("days ago" in p for p in problems) == 2, problems  # projections + ADP


def test_room_size_drift_is_reported(session: Session) -> None:
    pool = seed(session)
    problems = pool_health_problems(
        session, SETTINGS, pool, top_n=10, live_team_count=14, now=NOW
    )
    assert any("14 teams" in p for p in problems), problems


def test_placeholder_cluster_is_reported(session: Session) -> None:
    pool = seed(session)
    for i in range(300):
        session.add(
            PlayerIds(canonical_id=1000 + i, full_name=f"Deep {i}", merge_name=f"deep {i}", position="WR")
        )
        session.add(
            Adp(
                canonical_id=1000 + i, source="espn", format="ppr", teams=None,
                value=170.2, fetched_at=NOW - timedelta(hours=2),
            )
        )
    session.commit()
    problems = pool_health_problems(session, SETTINGS, pool, top_n=10, now=NOW)
    assert any("placeholder" in p and "espn" in p for p in problems), problems
