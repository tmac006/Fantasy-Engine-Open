"""Pro Football Reference advanced stats, the input to the advanced engine.

The normal engine re-scores market projections and asks who is worth the most
under this league's rules. It cannot see *why* a player produced what he did,
so it cannot tell a back who ran behind a wall from one who created his own
yards, and it prices both at whatever the market already thinks.

These columns can tell them apart:

  ybc_att / yac_att   yards before and after contact per carry. The split is the
                      point: before-contact is mostly the offensive line, after
                      is the runner. A back with poor YBC and strong YAC is good
                      *despite* his blocking, which a raw projection hides.
  pocket_time         seconds a quarterback holds the ball, and with
                      pressure_pct / times_blitzed the closest free proxy for
                      pass protection.
  adot                average depth of target. A receiver's role, not his luck.
  on_tgt_pct          throw accuracy with receiver drops removed.

    uv run python -m api.ingest.advanced [season ...]

Crosswalk is `pfr_id`, already populated for 3918 players; no name matching is
involved, so nothing here can silently attach one player's line to another.

Coverage is ~90% of the top 200 draft-relevant players. Every miss is a rookie
or a missed season, which is exactly why the advanced engine must read absent
data as "no opinion" rather than as a negative: doing otherwise fades every
rookie on the board and calls it analysis.
"""

from __future__ import annotations

import csv
import gzip
import io
import logging
import sys
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.db import get_sessionmaker
from api.models import PlayerContext, PlayerIds

log = logging.getLogger("ingest.advanced")

SOURCE = "pfr_advstats"
BASE = (
    "https://github.com/nflverse/nflverse-data/releases/download/"
    "pfr_advstats/advstats_season_{kind}.csv.gz"
)

# Columns kept per split, named as they land in the stored payload. Everything
# else in these files is either raw counting stats we already carry or context
# the engine has no use for; a narrow set keeps the payload readable.
KEPT: dict[str, dict[str, str]] = {
    "rush": {
        "ybc_att": "yards_before_contact_per_att",
        "yac_att": "yards_after_contact_per_att",
        "brk_tkl": "broken_tackles",
        "att_br": "attempts_per_broken_tackle",
        "att": "attempts",
    },
    "rec": {
        "adot": "average_depth_of_target",
        "ybc_r": "yards_before_catch_per_rec",
        "yac_r": "yards_after_catch_per_rec",
        "brk_tkl": "broken_tackles",
        "drop_percent": "drop_pct",
        "tgt": "targets",
        "rat": "passer_rating_when_targeted",
    },
    "pass": {
        "pocket_time": "pocket_time",
        "pressure_pct": "pressure_pct",
        "times_blitzed": "times_blitzed",
        "times_hit": "times_hit",
        "on_tgt_pct": "on_target_pct",
        "bad_throw_pct": "bad_throw_pct",
        "intended_air_yards_per_pass_attempt": "intended_air_yards_per_att",
        "completed_air_yards_per_completion": "completed_air_yards_per_comp",
        "pass_yards_after_catch_per_completion": "pass_yac_per_comp",
        "pass_attempts": "attempts",
    },
}


def _number(raw: str | None) -> float | None:
    """PFR writes empty strings and stray percent signs; both mean 'no value'."""
    if raw is None:
        return None
    cleaned = raw.strip().replace("%", "")
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _fetch(client: httpx.Client, kind: str) -> tuple[list[dict[str, str]], datetime | None]:
    response = client.get(BASE.format(kind=kind))
    response.raise_for_status()
    text = gzip.decompress(response.content).decode("utf-8", "replace")
    stamp = response.headers.get("last-modified")
    updated = parsedate_to_datetime(stamp) if stamp else None
    return list(csv.DictReader(io.StringIO(text))), updated


def run(session: Session, seasons: tuple[str, ...]) -> dict[str, int]:
    """Store one advanced-stat payload per player-season. Idempotent."""
    by_pfr: dict[str, int] = {
        pfr_id: row.canonical_id
        for row in session.scalars(select(PlayerIds).where(PlayerIds.pfr_id.isnot(None)))
        if (pfr_id := row.pfr_id) is not None
    }
    fetched_at = datetime.now(UTC)
    merged: dict[tuple[int, str], dict[str, Any]] = {}
    updated_at: datetime | None = None
    unmatched = 0

    with httpx.Client(timeout=60.0, follow_redirects=True) as client:
        for kind, columns in KEPT.items():
            rows, stamp = _fetch(client, kind)
            updated_at = updated_at or stamp
            for row in rows:
                season = (row.get("season") or "").strip()
                if season not in seasons:
                    continue
                pfr_id = (row.get("pfr_id") or "").strip()
                canonical_id = by_pfr.get(pfr_id)
                if canonical_id is None:
                    unmatched += 1
                    continue
                values = {
                    name: value
                    for source_name, name in columns.items()
                    if (value := _number(row.get(source_name))) is not None
                }
                if values:
                    merged.setdefault((canonical_id, season), {})[kind] = values

    for (canonical_id, season), data in merged.items():
        existing = session.scalar(
            select(PlayerContext).where(
                PlayerContext.canonical_id == canonical_id,
                PlayerContext.season == season,
                PlayerContext.source == SOURCE,
            )
        )
        if existing is None:
            session.add(
                PlayerContext(
                    canonical_id=canonical_id,
                    season=season,
                    source=SOURCE,
                    data=data,
                    fetched_at=fetched_at,
                    source_updated_at=updated_at,
                )
            )
        else:
            existing.data = data
            existing.fetched_at = fetched_at
            existing.source_updated_at = updated_at
    session.commit()

    counts = {"player_seasons": len(merged), "unmatched_rows": unmatched}
    log.info("pfr advanced stats: %s", counts)
    return counts


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    seasons = tuple(sys.argv[1:]) or ("2023", "2024", "2025")
    with get_sessionmaker()() as session:
        print(run(session, seasons))


if __name__ == "__main__":
    main()
