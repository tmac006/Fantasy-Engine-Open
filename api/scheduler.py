"""Ingest scheduling with catch-up.

Market data decays: ADP moves through August as camp news lands, and the whole
recommendation model is anchored to it. A laptop is not a server, though, so a
plain cron entry silently does nothing while the lid is shut. This scheduler
therefore does two things:

- runs each job on an interval while the API is up, and
- on startup, immediately runs anything whose last success is older than its
  interval, so a machine that was asleep catches up instead of quietly serving
  stale numbers.

Every run is recorded in ``ingest_runs`` with structured counts (spec 10).
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.db import get_sessionmaker
from api.ingest import leagues, lines, news, nflverse, players, projections, weekly
from api.models import IngestRun

log = logging.getLogger("scheduler")

# Counts a job reports back; all optional so jobs stay free to return nothing.
JobResult = dict[str, int] | None
JobFn = Callable[[Session], JobResult]


@dataclass(frozen=True)
class Job:
    name: str
    fn: JobFn
    interval: timedelta
    description: str


def _run_players(session: Session) -> JobResult:
    return players.run(session)


def _run_projections(session: Session) -> JobResult:
    return projections.run(session)


def _run_leagues(session: Session) -> JobResult:
    return leagues.run(session)


def _run_weekly(session: Session) -> JobResult:
    return weekly.run(session)


def _run_lines(session: Session) -> JobResult:
    counts = lines.run(session)
    return {
        "rows_in": counts["rows_in"],
        "rows_written": counts["rows_written"],
        "rows_skipped": counts["rows_skipped"],
    }


def _run_news(session: Session) -> JobResult:
    return news.run(session)


def _run_nflverse(session: Session) -> JobResult:
    counts = nflverse.run(session)
    return {
        "rows_written": counts.player_rows + counts.team_rows,
        "rows_skipped": counts.unmatched_players,
        "rows_in": counts.player_rows + counts.team_rows + counts.unmatched_players,
    }


# Preseason cadence. ADP and projections move daily in August, which is exactly
# when the draft assistant is used; nflverse season data changes far more slowly.
JOBS: tuple[Job, ...] = (
    Job(
        name="players",
        fn=_run_players,
        interval=timedelta(hours=24),
        description="Sleeper player master + ID crosswalk",
    ),
    Job(
        name="projections",
        fn=_run_projections,
        interval=timedelta(hours=12),
        description="Projections and ADP from Sleeper, ESPN and FFC",
    ),
    Job(
        name="leagues",
        fn=_run_leagues,
        interval=timedelta(hours=6),
        description="Registered league settings and rosters",
    ),
    Job(
        name="weekly",
        fn=_run_weekly,
        # Games finish Monday night; a 12h interval plus startup catch-up means
        # the Tuesday waiver window always has fresh usage without cron timing.
        interval=timedelta(hours=12),
        description="In-season weekly usage (snaps, targets, shares)",
    ),
    Job(
        name="lines",
        fn=_run_lines,
        # Lines drift all week and settle near kickoff, so this is worth
        # refreshing more often than the schedule it rides along with.
        interval=timedelta(hours=6),
        description="Game schedule, betting lines and conditions",
    ),
    Job(
        name="news",
        fn=_run_news,
        interval=timedelta(minutes=30),
        description="Player news from verified RSS feeds",
    ),
    Job(
        name="nflverse",
        fn=_run_nflverse,
        interval=timedelta(days=7),
        description="nflverse usage and team context",
    ),
)

JOBS_BY_NAME = {job.name: job for job in JOBS}


def _as_utc(value: datetime | None) -> datetime | None:
    """Normalize to aware UTC; some backends hand back naive timestamps."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def last_success(session: Session, job_name: str) -> datetime | None:
    return _as_utc(
        session.scalar(
            select(IngestRun.finished_at)
            .where(IngestRun.job == job_name, IngestRun.status == "ok")
            .order_by(IngestRun.finished_at.desc())
            .limit(1)
        )
    )


def is_due(session: Session, job: Job, now: datetime | None = None) -> bool:
    previous = last_success(session, job.name)
    if previous is None:
        return True
    return (now or datetime.now(UTC)) - previous >= job.interval


def run_job(job: Job) -> IngestRun:
    """Run one job in its own session and record the outcome either way."""
    with get_sessionmaker()() as session:
        record = IngestRun(job=job.name, status="ok")
        session.add(record)
        session.commit()
        try:
            counts = job.fn(session) or {}
            record.rows_in = counts.get("rows_in", 0)
            record.rows_written = counts.get("rows_written", 0)
            record.rows_skipped = counts.get("rows_skipped", 0)
            record.status = "ok"
            log.info(
                "ingest %s ok: in=%d written=%d skipped=%d",
                job.name, record.rows_in, record.rows_written, record.rows_skipped,
            )
        except Exception as exc:
            record.status = "error"
            record.error = f"{type(exc).__name__}: {exc}"[:2000]
            log.exception("ingest %s failed", job.name)
        record.finished_at = datetime.now(UTC)
        session.commit()
        session.refresh(record)
        return record


def run_due_jobs() -> list[str]:
    """Run every job whose last success is older than its interval."""
    ran: list[str] = []
    with get_sessionmaker()() as session:
        due = [job for job in JOBS if is_due(session, job)]
    for job in due:
        log.info("ingest %s is due (%s)", job.name, job.description)
        run_job(job)
        ran.append(job.name)
    if not ran:
        log.info("ingest: everything is current")
    return ran


def build_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler()
    for job in JOBS:
        scheduler.add_job(
            run_job,
            "interval",
            seconds=job.interval.total_seconds(),
            args=[job],
            id=job.name,
            max_instances=1,
            coalesce=True,
        )
    return scheduler
