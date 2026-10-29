"""Scheduler due-logic tests: the catch-up rule is what keeps a laptop honest."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from api.models import IngestRun
from api.scheduler import JOBS, JOBS_BY_NAME, Job, is_due, last_success


@pytest.fixture
def session() -> Session:
    # SQLite is enough here: this exercises scheduling logic, not Postgres types.
    engine = create_engine("sqlite://")
    IngestRun.__table__.create(engine)
    return sessionmaker(bind=engine)()


def record(session: Session, job: str, *, hours_ago: float, status: str = "ok") -> None:
    finished = datetime.now(UTC) - timedelta(hours=hours_ago)
    session.add(
        IngestRun(job=job, status=status, started_at=finished, finished_at=finished)
    )
    session.commit()


JOB = Job(name="players", fn=lambda _s: None, interval=timedelta(hours=24), description="t")


def test_never_run_is_due(session: Session) -> None:
    assert is_due(session, JOB)
    assert last_success(session, "players") is None


def test_recent_success_is_not_due(session: Session) -> None:
    record(session, "players", hours_ago=1)
    assert not is_due(session, JOB)


def test_stale_success_is_due(session: Session) -> None:
    """The laptop-was-shut case: a week later, everything must catch up."""
    record(session, "players", hours_ago=24 * 7)
    assert is_due(session, JOB)


def test_failed_run_does_not_count_as_fresh(session: Session) -> None:
    record(session, "players", hours_ago=1, status="error")
    assert is_due(session, JOB), "a failed ingest must not mark data fresh"


def test_latest_success_wins_over_older_ones(session: Session) -> None:
    record(session, "players", hours_ago=48)
    record(session, "players", hours_ago=2)
    assert not is_due(session, JOB)


def test_jobs_are_registered_and_unique() -> None:
    assert {job.name for job in JOBS} == set(JOBS_BY_NAME)
    assert len(JOBS) == len(JOBS_BY_NAME), "duplicate job names"
    for job in JOBS:
        assert job.interval > timedelta(0)
        assert job.description


def test_unfinished_run_does_not_hide_a_later_success(session: Session) -> None:
    """Observed 2026-09-15: a news run killed mid-flight left status='ok' with no
    finish time. Postgres sorts NULL first in DESC order, so that orphan row was
    returned as the "latest" success and the job read as never run for weeks."""
    started = datetime.now(UTC) - timedelta(days=20)
    session.add(IngestRun(job="players", status="ok", started_at=started, finished_at=None))
    session.commit()
    record(session, "players", hours_ago=1)
    assert last_success(session, "players") is not None
    assert not is_due(session, JOB)


def test_lines_catch_up_before_projections() -> None:
    # The weekly projection window comes from which games have final scores;
    # refreshing projections against a stale schedule skips the current week.
    names = [job.name for job in JOBS]
    assert names.index("lines") < names.index("projections")
