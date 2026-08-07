"""In-season weekly usage ingest.

Waiver and start/sit advice both rest on *recent* usage — snap share, target
share, route participation over the last week or two — which only exists once
games are played. This job records one row per player-week for the current
season so those deltas can be computed later.

A missed week cannot be reconstructed from season totals, so this runs on a
schedule from Week 1 rather than being backfilled at the end.

Idempotent. Run: `uv run python -m api.ingest.weekly`
"""

import logging
import sys
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.ingest.nflverse import (
    PLAYER_STATS_URL,
    SNAPS_URL,
    decode_csv,
    fetch_optional,
    normalize_team,
)
from api.models import PlayerIds, WeeklyStat

log = logging.getLogger("ingest.weekly")

CURRENT_SEASON = 2026

# nflverse weekly stat columns worth keeping for opportunity analysis. Names are
# verified against the real feed -- a typo here is silent, because a column that
# is absent in a season is skipped rather than raising. `_assert_columns` below
# turns that silence back into a warning.
_STAT_COLUMNS = (
    "attempts", "completions", "passing_yards", "passing_tds", "passing_interceptions",
    "carries", "rushing_yards", "rushing_tds", "rushing_first_downs",
    "targets", "receptions", "receiving_yards", "receiving_tds", "receiving_first_downs",
    "target_share", "air_yards_share", "wopr", "racr", "receiving_air_yards",
    # Efficiency. Opportunity says who gets the ball; EPA says what happens when
    # they do. The two diverging is the signal a waiver target is real.
    "passing_epa", "rushing_epa", "receiving_epa",
    "fantasy_points", "fantasy_points_ppr",
)

# Columns every season should have. Anything here going missing means the feed
# changed shape, which otherwise degrades to silently-null features.
_REQUIRED_COLUMNS = frozenset(
    {"targets", "carries", "target_share", "fantasy_points_ppr", "receiving_epa"}
)


def _assert_columns(rows: list[dict[str, str]], season: int) -> None:
    """Warn when the feed stops carrying a column we score on."""
    if not rows:
        return
    missing = _REQUIRED_COLUMNS - set(rows[0])
    if missing:
        log.warning(
            "weekly feed %s is missing expected columns: %s",
            season, ", ".join(sorted(missing)),
        )


def _num(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def ingest_weekly_rows(
    session: Session,
    season: int,
    stat_rows: list[dict[str, str]],
    snap_rows: list[dict[str, str]],
) -> dict[str, int]:
    """Upsert player-week rows. Matches on gsis_id (nflverse's player id)."""
    _assert_columns(stat_rows, season)
    by_gsis = {
        row.gsis_id: row.canonical_id
        for row in session.scalars(select(PlayerIds).where(PlayerIds.gsis_id.is_not(None)))
    }

    # Snap counts live in a separate feed keyed by pfr_player_id; fold in what
    # we can match by player+week so snap share travels with the usage row.
    snaps_by_key: dict[tuple[str, int], dict[str, Any]] = {}
    for row in snap_rows:
        pfr_id, week_text = row.get("pfr_player_id", ""), row.get("week", "")
        if not pfr_id or not week_text:
            continue
        snaps_by_key[(pfr_id, int(float(week_text)))] = {
            "offense_snaps": _num(row.get("offense_snaps", "")),
            "offense_pct": _num(row.get("offense_pct", "")),
        }
    by_pfr = {
        row.pfr_id: row.canonical_id
        for row in session.scalars(select(PlayerIds).where(PlayerIds.pfr_id.is_not(None)))
    }
    snaps_by_canonical: dict[tuple[int, int], dict[str, Any]] = {}
    for (pfr_id, week), payload in snaps_by_key.items():
        canonical = by_pfr.get(pfr_id)
        if canonical is not None:
            snaps_by_canonical[(canonical, week)] = payload

    existing = {
        (row.canonical_id, row.week): row
        for row in session.scalars(
            select(WeeklyStat).where(WeeklyStat.season == str(season))
        )
    }

    written = skipped = 0
    unmatched: set[str] = set()
    for row in stat_rows:
        if row.get("season_type") not in ("", "REG"):
            skipped += 1
            continue
        canonical = by_gsis.get(row.get("player_id", ""))
        week_raw = row.get("week", "")
        if canonical is None or not week_raw:
            skipped += 1
            if row.get("player_display_name"):
                unmatched.add(row["player_display_name"])
            continue
        week = int(float(week_raw))
        stats: dict[str, Any] = {}
        for column in _STAT_COLUMNS:
            value = _num(row.get(column, ""))
            if value is not None:
                stats[column] = value
        snaps = snaps_by_canonical.get((canonical, week))
        if snaps:
            stats.update({k: v for k, v in snaps.items() if v is not None})
        if not stats:
            skipped += 1
            continue

        record = existing.get((canonical, week))
        if record is None:
            record = WeeklyStat(canonical_id=canonical, season=str(season), week=week)
            session.add(record)
            existing[(canonical, week)] = record
        record.team = normalize_team(row.get("team"))
        record.opponent = normalize_team(row.get("opponent_team"))
        record.stats = stats
        written += 1

    session.commit()
    if unmatched:
        log.warning(
            "%d weekly stat rows had no canonical id (e.g. %s)",
            len(unmatched), ", ".join(sorted(unmatched)[:5]),
        )
    log.info("weekly stats %s: written=%d skipped=%d", season, written, skipped)
    return {"rows_in": len(stat_rows), "rows_written": written, "rows_skipped": skipped}


def run(session: Session, season: int = CURRENT_SEASON) -> dict[str, int]:
    with httpx.Client(timeout=60.0, follow_redirects=True) as client:
        stats_feed = fetch_optional(client, PLAYER_STATS_URL.format(season=season))
        snaps_feed = fetch_optional(client, SNAPS_URL.format(season=season))
    if stats_feed is None:
        # Normal before Week 1: nflverse publishes the file once games exist.
        log.info("no weekly stats published yet for %s", season)
        return {"rows_in": 0, "rows_written": 0, "rows_skipped": 0}
    return ingest_weekly_rows(
        session,
        season,
        decode_csv(stats_feed.content),
        decode_csv(snaps_feed.content) if snaps_feed else [],
    )


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s %(message)s", stream=sys.stderr
    )
    from api.db import get_sessionmaker

    with get_sessionmaker()() as session:
        run(session)
