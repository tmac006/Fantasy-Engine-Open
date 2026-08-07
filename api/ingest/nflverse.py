"""Free, idempotent player/team context ingest from nflverse and Sleeper.

The nflverse releases are intentionally read as compressed CSV with the standard
library.  This keeps the production dependency set small and avoids loading wide
datasets through pandas/pyarrow just to retain a compact set of draft features.

Run: ``uv run python -m api.ingest.nflverse [draft-season]``
"""

from __future__ import annotations

import csv
import gzip
import io
import json
import logging
import sys
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.config import get_settings
from api.models import PlayerContext, PlayerIds, TeamContext

log = logging.getLogger("ingest.nflverse")

_TEAM_ALIASES = {"LA": "LAR", "STL": "LAR", "OAK": "LV", "SD": "LAC"}

PLAYER_STATS_URL = (
    "https://github.com/nflverse/nflverse-data/releases/download/"
    "stats_player/stats_player_week_{season}.csv.gz"
)
TEAM_STATS_URL = (
    "https://github.com/nflverse/nflverse-data/releases/download/"
    "stats_team/stats_team_week_{season}.csv.gz"
)
ROSTERS_URL = (
    "https://github.com/nflverse/nflverse-data/releases/download/"
    "weekly_rosters/roster_weekly_{season}.csv.gz"
)
SNAPS_URL = (
    "https://github.com/nflverse/nflverse-data/releases/download/"
    "snap_counts/snap_counts_{season}.csv.gz"
)
SCHEDULES_URL = (
    "https://github.com/nflverse/nflverse-data/releases/download/schedules/games.csv"
)


@dataclass(frozen=True)
class Feed:
    content: bytes
    updated_at: datetime | None


@dataclass
class IngestCounts:
    player_rows: int = 0
    team_rows: int = 0
    sleeper_rows: int = 0
    unmatched_players: int = 0


def _source_timestamp(headers: httpx.Headers) -> datetime | None:
    raw = headers.get("last-modified")
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None


def fetch_optional(client: httpx.Client, url: str) -> Feed | None:
    """Fetch one feed without preventing other independent feeds from loading."""
    try:
        response = client.get(url)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        log.warning("optional feed unavailable: %s (%s)", url, exc)
        return None
    if not response.content:
        log.warning("optional feed was empty: %s", url)
        return None
    return Feed(response.content, _source_timestamp(response.headers))


def decode_csv(content: bytes) -> list[dict[str, str]]:
    """Decode plain or gzip CSV; malformed optional feeds become an empty input."""
    try:
        raw = gzip.decompress(content) if content.startswith(b"\x1f\x8b") else content
        text = io.TextIOWrapper(io.BytesIO(raw), encoding="utf-8-sig", newline="")
        return [
            {str(key): value or "" for key, value in row.items() if key is not None}
            for row in csv.DictReader(text)
        ]
    except (OSError, UnicodeError, csv.Error) as exc:
        log.warning("could not decode CSV feed: %s", exc)
        return []


def _clean(value: object) -> str | None:
    text = str(value or "").strip()
    return text if text and text.upper() not in {"NA", "N/A", "NULL"} else None


def normalize_team(value: object) -> str | None:
    team = _clean(value)
    return _TEAM_ALIASES.get(team, team) if team else None


def _number(row: Mapping[str, str], key: str) -> float | None:
    raw = _clean(row.get(key))
    if raw is None:
        return None
    try:
        return float(raw.removesuffix("%"))
    except ValueError:
        return None


def _sum(rows: Iterable[Mapping[str, str]], key: str) -> float:
    return sum(value for row in rows if (value := _number(row, key)) is not None)


def _mean(rows: Iterable[Mapping[str, str]], key: str) -> float | None:
    values = [value for row in rows if (value := _number(row, key)) is not None]
    return sum(values) / len(values) if values else None


def _rounded(value: float | None, digits: int = 3) -> float | None:
    return round(value, digits) if value is not None else None


def _ratio(numerator: float, denominator: float) -> float | None:
    return numerator / denominator if denominator > 0 else None


def _nonempty(values: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in values.items()
        if value is not None and value != "" and value != {}
    }


def _nonzero(values: Mapping[str, float | int | None]) -> dict[str, float | int]:
    return {
        key: value for key, value in values.items() if value is not None and value != 0
    }


def _regular_rows(
    rows: Iterable[Mapping[str, str]], season: int
) -> list[Mapping[str, str]]:
    return [
        row
        for row in rows
        if (_clean(row.get("season")) in {None, str(season)})
        and (_clean(row.get("season_type")) in {None, "REG"})
        and (_clean(row.get("game_type")) in {None, "REG"})
    ]


def aggregate_player_stats(
    rows: Iterable[Mapping[str, str]], sample_season: int
) -> dict[str, dict[str, Any]]:
    """Aggregate weekly nflverse stats into small, position-agnostic features."""
    grouped: dict[str, list[Mapping[str, str]]] = defaultdict(list)
    for row in _regular_rows(rows, sample_season):
        if gsis_id := _clean(row.get("player_id")):
            grouped[gsis_id].append(row)

    output: dict[str, dict[str, Any]] = {}
    for gsis_id, player_rows in grouped.items():
        attempts = _sum(player_rows, "attempts")
        carries = _sum(player_rows, "carries")
        targets = _sum(player_rows, "targets")
        receptions = _sum(player_rows, "receptions")
        passing_yards = _sum(player_rows, "passing_yards")
        rushing_yards = _sum(player_rows, "rushing_yards")
        receiving_yards = _sum(player_rows, "receiving_yards")
        game_ids = {_clean(row.get("game_id")) for row in player_rows}
        game_ids.discard(None)

        volume = _nonzero(
            {
                "attempts": round(attempts),
                "carries": round(carries),
                "targets": round(targets),
                "receptions": round(receptions),
                "touches": round(carries + receptions),
            }
        )
        opportunity = _nonempty(
            {
                "target_share": _rounded(_mean(player_rows, "target_share")),
                "air_yards_share": _rounded(_mean(player_rows, "air_yards_share")),
                "wopr": _rounded(_mean(player_rows, "wopr")),
            }
        )
        efficiency = _nonempty(
            {
                "yards_per_carry": _rounded(_ratio(rushing_yards, carries)),
                "yards_per_target": _rounded(_ratio(receiving_yards, targets)),
                "catch_rate": _rounded(_ratio(receptions, targets)),
                "passing_epa_per_attempt": _rounded(
                    _ratio(_sum(player_rows, "passing_epa"), attempts)
                ),
                "rushing_epa_per_carry": _rounded(
                    _ratio(_sum(player_rows, "rushing_epa"), carries)
                ),
                "receiving_epa_per_target": _rounded(
                    _ratio(_sum(player_rows, "receiving_epa"), targets)
                ),
                "passing_cpoe": _rounded(_mean(player_rows, "passing_cpoe")),
            }
        )
        production = _nonzero(
            {
                "passing_yards": round(passing_yards),
                "passing_tds": round(_sum(player_rows, "passing_tds")),
                "interceptions": round(_sum(player_rows, "passing_interceptions")),
                "rushing_yards": round(rushing_yards),
                "rushing_tds": round(_sum(player_rows, "rushing_tds")),
                "receiving_yards": round(receiving_yards),
                "receiving_tds": round(_sum(player_rows, "receiving_tds")),
                "fantasy_points_ppr": _rounded(
                    _sum(player_rows, "fantasy_points_ppr"), 1
                ),
            }
        )
        output[gsis_id] = _nonempty(
            {
                "sample_season": sample_season,
                "games": len(game_ids) or len(player_rows),
                "last_week": max(
                    (int(value) for row in player_rows if (value := _number(row, "week"))),
                    default=None,
                ),
                "team": normalize_team(player_rows[-1].get("team")),
                "position": _clean(player_rows[-1].get("position")),
                "volume": volume,
                "opportunity": opportunity,
                "efficiency": efficiency,
                "production": production,
            }
        )
    return output


def latest_roster_context(
    rows: Iterable[Mapping[str, str]], season: int
) -> dict[str, dict[str, Any]]:
    latest: dict[str, Mapping[str, str]] = {}
    for row in _regular_rows(rows, season):
        gsis_id = _clean(row.get("gsis_id"))
        if gsis_id is None:
            continue
        old_week = _number(latest.get(gsis_id, {}), "week") or -1
        if (_number(row, "week") or -1) >= old_week:
            latest[gsis_id] = row

    return {
        gsis_id: {
            "roster": _nonempty(
                {
                    "team": normalize_team(row.get("team")),
                    "position": _clean(row.get("position")),
                    "depth_chart_position": _clean(row.get("depth_chart_position")),
                    "status": _clean(row.get("status")),
                    "status_detail": _clean(row.get("status_description_abbr")),
                    "years_exp": (
                        round(value) if (value := _number(row, "years_exp")) is not None else None
                    ),
                    "birth_date": _clean(row.get("birth_date")),
                    "week": (
                        round(value) if (value := _number(row, "week")) is not None else None
                    ),
                }
            )
        }
        for gsis_id, row in latest.items()
    }


def aggregate_snaps(
    rows: Iterable[Mapping[str, str]], sample_season: int
) -> dict[str, dict[str, Any]]:
    """Aggregate PFR snap rows; the existing crosswalk supplies PFR IDs."""
    grouped: dict[str, list[Mapping[str, str]]] = defaultdict(list)
    for row in _regular_rows(rows, sample_season):
        if pfr_id := _clean(row.get("pfr_player_id")):
            grouped[pfr_id].append(row)
    return {
        pfr_id: {
            "snaps": _nonempty(
                {
                    "offense": round(_sum(player_rows, "offense_snaps")),
                    "offense_pct": _rounded(_mean(player_rows, "offense_pct")),
                    "games": len(player_rows),
                }
            )
        }
        for pfr_id, player_rows in grouped.items()
    }


def aggregate_team_stats(
    rows: Iterable[Mapping[str, str]], sample_season: int
) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[Mapping[str, str]]] = defaultdict(list)
    for row in _regular_rows(rows, sample_season):
        if team := normalize_team(row.get("team")):
            grouped[team].append(row)

    output: dict[str, dict[str, Any]] = {}
    for team, team_rows in grouped.items():
        attempts = _sum(team_rows, "attempts")
        carries = _sum(team_rows, "carries")
        plays = attempts + carries
        games = len({_clean(row.get("game_id")) for row in team_rows}) or len(team_rows)
        epa = _sum(team_rows, "passing_epa") + _sum(team_rows, "rushing_epa")
        touchdowns = (
            _sum(team_rows, "passing_tds")
            + _sum(team_rows, "rushing_tds")
            + _sum(team_rows, "special_teams_tds")
        )
        output[team] = {
            "sample_season": sample_season,
            "games": games,
            "offense": _nonempty(
                {
                    "plays_per_game": _rounded(_ratio(plays, games), 1),
                    "pass_rate": _rounded(_ratio(attempts, plays)),
                    "yards_per_play": _rounded(
                        _ratio(
                            _sum(team_rows, "passing_yards")
                            + _sum(team_rows, "rushing_yards"),
                            plays,
                        )
                    ),
                    "epa_per_play": _rounded(_ratio(epa, plays)),
                    "touchdowns_per_game": _rounded(_ratio(touchdowns, games)),
                }
            ),
        }
    return output


def build_schedule_context(
    rows: Iterable[Mapping[str, str]], season: int
) -> dict[str, dict[str, Any]]:
    schedules: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in _regular_rows(rows, season):
        away = normalize_team(row.get("away_team"))
        home = normalize_team(row.get("home_team"))
        week_value = _number(row, "week")
        if away is None or home is None or week_value is None:
            continue
        week = round(week_value)
        total = _rounded(_number(row, "total_line"), 1)
        schedules[away].append(
            _nonempty({"week": week, "opponent": home, "home": False, "total": total})
        )
        schedules[home].append(
            _nonempty({"week": week, "opponent": away, "home": True, "total": total})
        )
    return {
        team: {"schedule": sorted(games, key=lambda game: int(game["week"]))}
        for team, games in schedules.items()
    }


def build_sleeper_status(payload: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Keep only mutable draft-relevant fields from Sleeper's large player map."""
    result: dict[str, dict[str, Any]] = {}
    for sleeper_id, raw in payload.items():
        if not isinstance(raw, Mapping):
            continue
        status = _nonempty(
            {
                "team": normalize_team(raw.get("team")),
                "status": _clean(raw.get("status")),
                "injury_status": _clean(raw.get("injury_status")),
                "injury_start_date": _clean(raw.get("injury_start_date")),
                "practice_participation": _clean(raw.get("practice_participation")),
                "depth_chart_position": _clean(raw.get("depth_chart_position")),
                "depth_chart_order": raw.get("depth_chart_order"),
            }
        )
        if status:
            result[str(sleeper_id)] = status
    return result


def _merge_nested(base: dict[str, Any], update: Mapping[str, Any]) -> None:
    for key, value in update.items():
        if isinstance(value, Mapping) and isinstance(base.get(key), dict):
            base[key].update(value)
        else:
            base[key] = value


def _upsert_player_context(
    session: Session,
    canonical_id: int,
    season: int,
    source: str,
    data: dict[str, Any],
    fetched_at: datetime,
    source_updated_at: datetime | None,
) -> None:
    row = session.scalar(
        select(PlayerContext).where(
            PlayerContext.canonical_id == canonical_id,
            PlayerContext.season == str(season),
            PlayerContext.source == source,
        )
    )
    if row is None:
        session.add(
            PlayerContext(
                canonical_id=canonical_id,
                season=str(season),
                source=source,
                data=data,
                fetched_at=fetched_at,
                source_updated_at=source_updated_at,
            )
        )
        return
    row.data = data
    row.fetched_at = fetched_at
    row.source_updated_at = source_updated_at


def _upsert_team_context(
    session: Session,
    team: str,
    season: int,
    data: dict[str, Any],
    fetched_at: datetime,
    source_updated_at: datetime | None,
) -> None:
    row = session.scalar(
        select(TeamContext).where(
            TeamContext.team == team,
            TeamContext.season == str(season),
            TeamContext.source == "nflverse",
        )
    )
    if row is None:
        session.add(
            TeamContext(
                team=team,
                season=str(season),
                source="nflverse",
                data=data,
                fetched_at=fetched_at,
                source_updated_at=source_updated_at,
            )
        )
        return
    row.data = data
    row.fetched_at = fetched_at
    row.source_updated_at = source_updated_at


def ingest_nflverse_context(
    session: Session,
    draft_season: int,
    player_stats: Iterable[Mapping[str, str]] = (),
    rosters: Iterable[Mapping[str, str]] = (),
    snaps: Iterable[Mapping[str, str]] = (),
    team_stats: Iterable[Mapping[str, str]] = (),
    schedules: Iterable[Mapping[str, str]] = (),
    *,
    fetched_at: datetime | None = None,
    source_updated_at: datetime | None = None,
) -> IngestCounts:
    """Write one nflverse row per mapped player/team, replacing the same key."""
    sample_season = draft_season - 1
    now = fetched_at or datetime.now(UTC)
    by_gsis = aggregate_player_stats(player_stats, sample_season)
    for gsis_id, roster in latest_roster_context(rosters, draft_season).items():
        _merge_nested(by_gsis.setdefault(gsis_id, {}), roster)
    by_pfr = aggregate_snaps(snaps, sample_season)

    counts = IngestCounts()
    crosswalk = session.scalars(select(PlayerIds)).all()
    matched_gsis: set[str] = set()
    matched_pfr: set[str] = set()
    for ids in crosswalk:
        data: dict[str, Any] = {}
        if ids.gsis_id and ids.gsis_id in by_gsis:
            _merge_nested(data, by_gsis[ids.gsis_id])
            matched_gsis.add(ids.gsis_id)
        if ids.pfr_id and ids.pfr_id in by_pfr:
            _merge_nested(data, by_pfr[ids.pfr_id])
            matched_pfr.add(ids.pfr_id)
        if not data:
            continue
        _upsert_player_context(
            session,
            ids.canonical_id,
            draft_season,
            "nflverse",
            data,
            now,
            source_updated_at,
        )
        counts.player_rows += 1
    counts.unmatched_players = len(set(by_gsis) - matched_gsis) + len(
        set(by_pfr) - matched_pfr
    )

    teams = aggregate_team_stats(team_stats, sample_season)
    for team, schedule in build_schedule_context(schedules, draft_season).items():
        _merge_nested(teams.setdefault(team, {}), schedule)
    for team, data in teams.items():
        _upsert_team_context(
            session, team, draft_season, data, now, source_updated_at
        )
        counts.team_rows += 1
    session.commit()
    return counts


def ingest_sleeper_status(
    session: Session,
    draft_season: int,
    payload: Mapping[str, Any],
    *,
    fetched_at: datetime | None = None,
    source_updated_at: datetime | None = None,
) -> int:
    now = fetched_at or datetime.now(UTC)
    statuses = build_sleeper_status(payload)
    crosswalk = session.scalars(
        select(PlayerIds).where(PlayerIds.sleeper_id.is_not(None))
    ).all()
    written = 0
    for ids in crosswalk:
        if ids.sleeper_id is None or ids.sleeper_id not in statuses:
            continue
        _upsert_player_context(
            session,
            ids.canonical_id,
            draft_season,
            "sleeper",
            statuses[ids.sleeper_id],
            now,
            source_updated_at,
        )
        written += 1
    session.commit()
    return written


def _latest_timestamp(feeds: Iterable[Feed | None]) -> datetime | None:
    timestamps = [feed.updated_at for feed in feeds if feed and feed.updated_at]
    return max(timestamps) if timestamps else None


def run(session: Session, draft_season: int = 2026) -> IngestCounts:
    """Fetch every source independently; absent feeds do not block useful data."""
    sample_season = draft_season - 1
    settings = get_settings()
    with httpx.Client(timeout=60.0, follow_redirects=True) as client:
        player_feed = fetch_optional(
            client, PLAYER_STATS_URL.format(season=sample_season)
        )
        team_feed = fetch_optional(
            client, TEAM_STATS_URL.format(season=sample_season)
        )
        roster_feed = fetch_optional(
            client, ROSTERS_URL.format(season=draft_season)
        )
        snap_feed = fetch_optional(client, SNAPS_URL.format(season=sample_season))
        schedule_feed = fetch_optional(client, SCHEDULES_URL)
        sleeper_feed = fetch_optional(
            client, f"{settings.sleeper_base_url}/v1/players/nfl"
        )

    feeds = [player_feed, team_feed, roster_feed, snap_feed, schedule_feed]
    counts = ingest_nflverse_context(
        session,
        draft_season,
        decode_csv(player_feed.content) if player_feed else (),
        decode_csv(roster_feed.content) if roster_feed else (),
        decode_csv(snap_feed.content) if snap_feed else (),
        decode_csv(team_feed.content) if team_feed else (),
        decode_csv(schedule_feed.content) if schedule_feed else (),
        source_updated_at=_latest_timestamp(feeds),
    )

    if sleeper_feed is not None:
        try:
            sleeper_payload = json.loads(sleeper_feed.content)
        except (UnicodeError, json.JSONDecodeError) as exc:
            log.warning("could not decode Sleeper player feed: %s", exc)
        else:
            if isinstance(sleeper_payload, Mapping):
                counts.sleeper_rows = ingest_sleeper_status(
                    session,
                    draft_season,
                    sleeper_payload,
                    source_updated_at=sleeper_feed.updated_at,
                )
            else:
                log.warning("Sleeper player feed was not an object")

    log.info(
        "context ingest: players=%d teams=%d sleeper=%d unmatched=%d",
        counts.player_rows,
        counts.team_rows,
        counts.sleeper_rows,
        counts.unmatched_players,
    )
    return counts


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )
    from api.db import get_sessionmaker

    season = int(sys.argv[1]) if len(sys.argv) > 1 else 2026
    with get_sessionmaker()() as db_session:
        run(db_session, season)
