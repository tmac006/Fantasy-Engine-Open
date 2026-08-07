"""Game schedule, betting lines and conditions from nflverse.

The market's implied team total is the single best public predictor of team
scoring, which makes it the backbone of start/sit advice. nflverse publishes it
in the same `games.csv` as the schedule, going back to 1999 and forward to games
that have lines posted but have not kicked off -- so this one feed serves both
live advice and backtesting, with no API key and no rate limit.

Idempotent. Run: ``uv run python -m api.ingest.lines [season ...]``
"""

import logging
import sys
from datetime import UTC, datetime

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.ingest.nflverse import SCHEDULES_URL, decode_csv, fetch_optional, normalize_team
from api.models import GameLine

log = logging.getLogger("ingest.lines")

CURRENT_SEASON = 2026


def _num(value: str | None) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _int(value: str | None) -> int | None:
    number = _num(value)
    return None if number is None else int(number)


def _kickoff(row: dict[str, str]) -> datetime | None:
    """Combine nflverse's separate date and time columns.

    `gametime` is missing for older seasons and for games not yet given a slot,
    so the date alone still yields a usable ordering.
    """
    gameday = row.get("gameday", "")
    if not gameday:
        return None
    stamp = f"{gameday} {row.get('gametime') or '00:00'}"
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(stamp, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def ingest_line_rows(
    session: Session, rows: list[dict[str, str]], seasons: set[int]
) -> dict[str, int]:
    """Upsert one row per game for the requested seasons."""
    wanted = {str(season) for season in seasons}
    existing = {
        row.game_id: row
        for row in session.scalars(select(GameLine).where(GameLine.season.in_(wanted)))
    }

    written = skipped = 0
    for row in rows:
        if row.get("season") not in wanted or row.get("game_type") != "REG":
            skipped += 1
            continue
        game_id, week = row.get("game_id", ""), _int(row.get("week"))
        home, away = normalize_team(row.get("home_team", "")), normalize_team(
            row.get("away_team", "")
        )
        if not game_id or week is None or not home or not away:
            skipped += 1
            continue

        record = existing.get(game_id)
        if record is None:
            record = GameLine(game_id=game_id, season=row["season"], week=week)
            session.add(record)
            existing[game_id] = record

        record.week = week
        record.home_team, record.away_team = home, away
        record.kickoff = _kickoff(row)
        record.spread_line = _num(row.get("spread_line"))
        record.total_line = _num(row.get("total_line"))
        record.home_moneyline = _int(row.get("home_moneyline"))
        record.away_moneyline = _int(row.get("away_moneyline"))
        record.roof = row.get("roof") or None
        record.surface = row.get("surface") or None
        record.temp = _num(row.get("temp"))
        record.wind = _num(row.get("wind"))
        record.home_score = _int(row.get("home_score"))
        record.away_score = _int(row.get("away_score"))
        written += 1

    session.commit()
    priced = sum(
        1
        for record in existing.values()
        if record.spread_line is not None and record.total_line is not None
    )
    log.info(
        "game lines %s: written=%d skipped=%d priced=%d",
        ",".join(sorted(wanted)), written, skipped, priced,
    )
    return {
        "rows_in": len(rows),
        "rows_written": written,
        "rows_skipped": skipped,
        "rows_priced": priced,
    }


def run(session: Session, *seasons: int) -> dict[str, int]:
    targets = set(seasons) or {CURRENT_SEASON}
    with httpx.Client(timeout=60.0, follow_redirects=True) as client:
        feed = fetch_optional(client, SCHEDULES_URL)
    if feed is None:
        log.warning("schedules feed unavailable")
        return {"rows_in": 0, "rows_written": 0, "rows_skipped": 0, "rows_priced": 0}
    return ingest_line_rows(session, decode_csv(feed.content), targets)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s %(message)s", stream=sys.stderr
    )
    from api.db import get_sessionmaker

    requested = [int(arg) for arg in sys.argv[1:]] or [CURRENT_SEASON]
    with get_sessionmaker()() as session:
        run(session, *requested)
