"""Operational endpoints: ingest freshness and manual triggers."""

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from api.db import get_sessionmaker
from api.scheduler import JOBS, JOBS_BY_NAME, is_due, last_success, run_job

router = APIRouter(prefix="/admin", tags=["admin"])


def db_session() -> Iterator[Session]:
    with get_sessionmaker()() as session:
        yield session


class JobStatus(BaseModel):
    job: str
    description: str
    interval_hours: float
    last_success: datetime | None
    age_hours: float | None
    due: bool


class IngestStatus(BaseModel):
    jobs: list[JobStatus]
    any_due: bool


@router.get("/ingest-status")
def ingest_status(session: Annotated[Session, Depends(db_session)]) -> IngestStatus:
    """Freshness per job. The draft-day script checks this before you start."""
    now = datetime.now(UTC)
    statuses: list[JobStatus] = []
    for job in JOBS:
        previous = last_success(session, job.name)
        statuses.append(
            JobStatus(
                job=job.name,
                description=job.description,
                interval_hours=job.interval.total_seconds() / 3600,
                last_success=previous,
                age_hours=(now - previous).total_seconds() / 3600 if previous else None,
                due=is_due(session, job, now),
            )
        )
    return IngestStatus(jobs=statuses, any_due=any(s.due for s in statuses))


@router.post("/ingest/{job_name}/run")
def trigger_ingest(job_name: str, background: BackgroundTasks) -> dict[str, str]:
    job = JOBS_BY_NAME.get(job_name)
    if job is None:
        raise HTTPException(
            status_code=404,
            detail=f"unknown job {job_name!r}; known: {sorted(JOBS_BY_NAME)}",
        )
    background.add_task(run_job, job)
    return {"status": "started", "job": job_name}
