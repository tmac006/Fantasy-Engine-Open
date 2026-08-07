"""League registry sync: settings and rosters for every managed league.

In-season features are per-league and scheduled, so the leagues have to live in
the database rather than arriving as request parameters the way the draft
endpoints work. This job keeps each registered league's scoring, roster slots
and current rosters current.

Idempotent. Run: `uv run python -m api.ingest.leagues`
"""

import logging
import sys

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.adapters.espn import EspnReadClient, translate_espn_settings
from api.adapters.sleeper import SleeperAdapter
from api.models import League, LeagueRoster, LeagueSettingsRow
from api.schemas import LeagueSettings
from api.services import canonical_map_for_espn, canonical_map_for_sleeper

log = logging.getLogger("ingest.leagues")


def _store_settings(session: Session, league: League, settings: LeagueSettings) -> None:
    row = session.get(LeagueSettingsRow, league.id)
    if row is None:
        session.add(
            LeagueSettingsRow(
                league_id=league.id,
                scoring=dict(settings.scoring),
                roster_slots=dict(settings.roster_slots),
                total_teams=settings.total_teams,
            )
        )
    else:
        row.scoring = dict(settings.scoring)
        row.roster_slots = dict(settings.roster_slots)
        row.total_teams = settings.total_teams


def map_roster_players(
    platform_player_ids: list[str], canonical: dict[str, str]
) -> tuple[list[str], list[str]]:
    """Split a roster into (canonical ids, unmappable platform ids).

    Pure so it can be tested directly: a silently dropped player makes waiver
    and start/sit advice wrong in ways that are miserable to debug, so the
    misses are returned rather than skipped.
    """
    matched: list[str] = []
    missing: list[str] = []
    for pid in platform_player_ids:
        target = canonical.get(str(pid))
        if target is None:
            missing.append(str(pid))
        else:
            matched.append(target)
    return matched, missing


def _store_rosters(
    session: Session,
    league: League,
    rosters: dict[str, list[str]],
    canonical: dict[str, str],
) -> tuple[int, int]:
    """Replace this league's rosters. Returns (mapped, unmapped) player counts."""
    existing = {
        row.platform_roster_id: row
        for row in session.scalars(
            select(LeagueRoster).where(LeagueRoster.league_id == league.id)
        )
    }
    mapped = unmapped = 0
    for platform_roster_id, platform_player_ids in rosters.items():
        canonical_ids, missing = map_roster_players(platform_player_ids, canonical)
        mapped += len(canonical_ids)
        unmapped += len(missing)

        row = existing.get(platform_roster_id)
        if row is None:
            row = LeagueRoster(league_id=league.id, platform_roster_id=platform_roster_id)
            session.add(row)
        row.player_ids = canonical_ids
        row.unmapped_player_ids = missing
        row.is_mine = (
            league.my_team_id is not None and str(league.my_team_id) == platform_roster_id
        )
    return mapped, unmapped


def sync_league(session: Session, league: League) -> tuple[int, int]:
    """Refresh one league's name, settings and rosters. Returns (mapped, unmapped)."""
    if league.platform == "sleeper":
        adapter = SleeperAdapter()
        raw_league = adapter.league(league.platform_league_id)
        platform_name = raw_league.get("name")
        _store_settings(session, league, adapter.get_league_settings(league.platform_league_id))
        raw = adapter.get_rosters(league.platform_league_id)
        rosters = {rid: [p.removeprefix("sleeper:") for p in ids] for rid, ids in raw.items()}
        canonical = canonical_map_for_sleeper(
            session, [pid for ids in rosters.values() for pid in ids]
        )
    elif league.platform == "espn":
        client = EspnReadClient()
        payload = client.league_view(league.platform_league_id, league.season, "mSettings")
        platform_name = (payload.get("settings") or {}).get("name")
        _store_settings(
            session, league, translate_espn_settings(payload)
        )
        rosters = client.get_rosters(league.platform_league_id, league.season)
        canonical = canonical_map_for_espn(
            session, [pid for ids in rosters.values() for pid in ids]
        )
    else:
        raise ValueError(f"unknown platform {league.platform!r}")

    # Platforms know their own league name; only fall back to a manual one.
    if platform_name and not league.name:
        league.name = str(platform_name)[:128]

    mapped, unmapped = _store_rosters(session, league, rosters, canonical)
    session.commit()
    return mapped, unmapped


def run(session: Session) -> dict[str, int]:
    leagues = list(session.scalars(select(League).where(League.active.is_(True))))
    if not leagues:
        log.info("no leagues registered; nothing to sync")
        return {"rows_in": 0, "rows_written": 0, "rows_skipped": 0}

    total_mapped = total_unmapped = 0
    failed = 0
    for league in leagues:
        try:
            mapped, unmapped = sync_league(session, league)
        except Exception as exc:  # noqa: BLE001 - one bad league must not stop the others
            failed += 1
            session.rollback()
            log.warning(
                "league sync failed for %s/%s: %s",
                league.platform, league.platform_league_id, exc,
            )
            continue
        total_mapped += mapped
        total_unmapped += unmapped
        log.info(
            "synced %s league %s: %d players mapped, %d unmapped",
            league.platform, league.platform_league_id, mapped, unmapped,
        )
        if unmapped:
            # Silent join failures make waiver/start-sit advice subtly wrong.
            log.warning(
                "league %s has %d rostered players with no canonical id",
                league.platform_league_id, unmapped,
            )
    return {
        "rows_in": total_mapped + total_unmapped,
        "rows_written": total_mapped,
        "rows_skipped": total_unmapped + failed,
    }


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s %(message)s", stream=sys.stderr
    )
    from api.db import get_sessionmaker

    with get_sessionmaker()() as session:
        run(session)
