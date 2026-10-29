"""Season + weekly projection and ADP ingest from three free sources.

Sources (all verified live 2026-08-04; weekly endpoints verified 2026-09-15):
- Sleeper (undocumented `api.sleeper.com`): raw stat projections keyed exactly like
  league scoring_settings, so they can be re-scored under any league's rules. Also
  carries per-format ADP. Primary projection source. `/projections/nfl/{season}/{week}`
  gives the same shape for a single week.
- ESPN kona_player_info (no auth): consensus points projection + live ADP. The one
  payload carries every week's projection for the season (statSourceId 1,
  scoringPeriodId = week), with 0.0 on a bye week.
- FantasyFootballCalculator: community ADP with stdev, per team-count and format.

Weekly rows (Projection.week set) are what the in-season lineup baseline prefers.
Without them week 2 advice ran on one game of actuals, which benched Saquon Barkley
behind Michael Wilson on 2026-09-15.

Idempotent: safe to re-run. Run: `uv run python -m api.ingest.projections`
"""

import logging
import sys
from collections import Counter
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.config import get_settings
from api.ingest.players import make_merge_name
from api.models import Adp, PlayerIds, Projection

log = logging.getLogger("ingest.projections")

SEASON = "2026"
SLEEPER_PROJ_URL = (
    "https://api.sleeper.com/projections/nfl/{season}?season_type=regular"
    "&position[]=QB&position[]=RB&position[]=WR&position[]=TE&position[]=K&position[]=DEF"
    "&order_by=adp_ppr"
)
SLEEPER_WEEK_PROJ_URL = (
    "https://api.sleeper.com/projections/nfl/{season}/{week}?season_type=regular"
    "&position[]=QB&position[]=RB&position[]=WR&position[]=TE&position[]=K&position[]=DEF"
    "&order_by=pts_ppr"
)
FFC_ADP_URL = "https://fantasyfootballcalculator.com/api/v1/adp/{fmt}?teams={teams}&year={season}"
LAST_REGULAR_WEEK = 18

# Sleeper stat keys that are ADP metadata, not projected stats.
_SLEEPER_ADP_KEYS = {"ppr": "adp_ppr", "half_ppr": "adp_half_ppr", "std": "adp_std"}


def _upsert_projection(
    session: Session,
    canonical_id: int,
    source: str,
    points: float | None,
    stats: dict[str, Any],
    *,
    week: int | None = None,
) -> bool:
    """Write one projection row; `week=None` is the full-season row."""
    row = session.scalar(
        select(Projection).where(
            Projection.canonical_id == canonical_id,
            Projection.source == source,
            Projection.season == SEASON,
            Projection.week.is_(None) if week is None else Projection.week == week,
        )
    )
    if row is None:
        session.add(
            Projection(
                canonical_id=canonical_id, source=source, season=SEASON, week=week,
                points=points, stats=stats,
            )
        )
        return True
    row.points = points
    row.stats = stats
    # Upserts only touch fetched_at on INSERT via server_default, so stamp it
    # here too; otherwise a re-ingest leaves the data looking stale forever.
    row.fetched_at = datetime.now(UTC)
    return False


def _upsert_adp(
    session: Session,
    canonical_id: int,
    source: str,
    fmt: str,
    teams: int | None,
    value: float,
    stdev: float | None = None,
) -> None:
    row = session.scalar(
        select(Adp).where(
            Adp.canonical_id == canonical_id,
            Adp.source == source,
            Adp.format == fmt,
            Adp.teams.is_(None) if teams is None else Adp.teams == teams,
        )
    )
    if row is None:
        session.add(
            Adp(canonical_id=canonical_id, source=source, format=fmt, teams=teams, value=value, stdev=stdev)
        )
    else:
        row.value = value
        row.stdev = stdev
        row.fetched_at = datetime.now(UTC)


def ingest_sleeper_projections(session: Session, rows: list[dict[str, Any]]) -> tuple[int, int]:
    by_sleeper = {
        r.sleeper_id: r.canonical_id
        for r in session.scalars(select(PlayerIds).where(PlayerIds.sleeper_id.is_not(None)))
    }
    written = skipped = 0
    for rec in rows:
        sleeper_id = str(rec.get("player_id") or "")
        stats = rec.get("stats") or {}
        canonical = by_sleeper.get(sleeper_id)
        if canonical is None or not stats.get("pts_ppr"):
            skipped += 1
            continue
        proj_stats = {k: v for k, v in stats.items() if not k.startswith("adp_")}
        _upsert_projection(session, canonical, "sleeper", stats.get("pts_ppr"), proj_stats)
        for fmt, key in _SLEEPER_ADP_KEYS.items():
            value = stats.get(key)
            if value and value < 900:  # 999 = "not being drafted"
                _upsert_adp(session, canonical, "sleeper", fmt, None, float(value))
        written += 1
    session.commit()
    return written, skipped


def _purge_weekly(session: Session, source: str, week: int, keep: set[int]) -> int:
    """Drop a source's rows for the week that it no longer publishes.

    A player ruled out mid-week vanishes from the feed rather than being
    projected at zero, so a row left behind would keep starting him.
    """
    stale = [
        row
        for row in session.scalars(
            select(Projection).where(
                Projection.source == source,
                Projection.season == SEASON,
                Projection.week == week,
            )
        )
        if row.canonical_id not in keep
    ]
    for row in stale:
        session.delete(row)
    return len(stale)


def ingest_sleeper_weekly_projections(
    session: Session, rows: list[dict[str, Any]], week: int
) -> tuple[int, int]:
    """One week of Sleeper projections. Rows without `pts_ppr` are players the
    feed lists but does not project (inactive, buried, or on bye) and are
    skipped so the lineup falls back rather than reading a phantom zero."""
    by_sleeper = {
        r.sleeper_id: r.canonical_id
        for r in session.scalars(select(PlayerIds).where(PlayerIds.sleeper_id.is_not(None)))
    }
    written = skipped = 0
    kept: set[int] = set()
    for rec in rows:
        stats = rec.get("stats") or {}
        canonical = by_sleeper.get(str(rec.get("player_id") or ""))
        points = stats.get("pts_ppr")
        if canonical is None or points is None:
            skipped += 1
            continue
        proj_stats = {k: v for k, v in stats.items() if not k.startswith("adp_")}
        _upsert_projection(session, canonical, "sleeper", float(points), proj_stats, week=week)
        kept.add(canonical)
        written += 1
    purged = _purge_weekly(session, "sleeper", week, kept)
    if purged:
        log.info("sleeper week %d: purged %d rows no longer projected", week, purged)
    session.commit()
    return written, skipped


def ingest_espn_weekly_projections(
    session: Session, players: list[dict[str, Any]], week: int
) -> tuple[int, int]:
    """One week of ESPN projections out of the kona payload.

    Same seasonId discipline as the season row: the prior season's week N
    projection sits in the same array under the same statSourceId/scoringPeriodId.
    """
    by_espn = {
        r.espn_id: r.canonical_id
        for r in session.scalars(select(PlayerIds).where(PlayerIds.espn_id.is_not(None)))
    }
    written = skipped = 0
    kept: set[int] = set()
    for entry in players:
        canonical = by_espn.get(str(entry.get("id") or ""))
        if canonical is None:
            skipped += 1
            continue
        week_proj = next(
            (
                s
                for s in (entry.get("player") or {}).get("stats") or []
                if s.get("statSourceId") == 1
                and s.get("scoringPeriodId") == week
                and str(s.get("seasonId")) == SEASON
            ),
            None,
        )
        points = (week_proj or {}).get("appliedTotal")
        if points is None:
            skipped += 1
            continue
        _upsert_projection(
            session, canonical, "espn", float(points), {"applied_total_ppr": points}, week=week
        )
        kept.add(canonical)
        written += 1
    purged = _purge_weekly(session, "espn", week, kept)
    if purged:
        log.info("espn week %d: purged %d rows no longer projected", week, purged)
    session.commit()
    return written, skipped


def upcoming_weeks(session: Session) -> list[int]:
    """The week being set right now and the one after it.

    The next week is fetched early so the lineup page has a baseline the
    moment `current_week` rolls over on Sunday afternoon, instead of waiting
    up to an interval for the next ingest.
    """
    from api.inseason import current_week  # local: inseason imports the model layer

    week = current_week(session, SEASON)
    return [w for w in (week, week + 1) if 1 <= w <= LAST_REGULAR_WEEK]


def placeholder_adp_values(adps: list[float], min_cluster: int = 25) -> set[int]:
    """Rounded ADP values that are ESPN's "no draft data" placeholder.

    ESPN publishes ~170 (the exact value drifts with room size) for every
    player nobody actually drafts. Real market values spread out; the
    placeholder is one value shared by hundreds of rows (observed: 740 rows at
    round(170) with the next-largest organic cluster at 24). Sleeper marks the
    same condition with 999 and its ingest guards it; this is ESPN's guard.
    """
    counts = Counter(round(a) for a in adps)
    if not counts:
        return set()
    (mode, size), *rest = counts.most_common(2)
    runner_up = rest[0][1] if rest else 0
    if size >= min_cluster and size >= 3 * max(1, runner_up):
        return {mode}
    return set()


def _delete_adp(session: Session, canonical_id: int, source: str, fmt: str) -> None:
    row = session.scalar(
        select(Adp).where(
            Adp.canonical_id == canonical_id,
            Adp.source == source,
            Adp.format == fmt,
            Adp.teams.is_(None),
        )
    )
    if row is not None:
        session.delete(row)


def ingest_espn_projections(session: Session, players: list[dict[str, Any]]) -> tuple[int, int]:
    by_espn = {
        r.espn_id: r.canonical_id
        for r in session.scalars(select(PlayerIds).where(PlayerIds.espn_id.is_not(None)))
    }
    placeholders = placeholder_adp_values(
        [
            float(adp)
            for entry in players
            if (adp := ((entry.get("player") or {}).get("ownership") or {}).get("averageDraftPosition"))
        ]
    )
    if placeholders:
        log.info("espn: treating ADP cluster at %s as placeholder (not market data)", sorted(placeholders))
    written = skipped = 0
    for entry in players:
        canonical = by_espn.get(str(entry.get("id") or ""))
        player = entry.get("player") or {}
        if canonical is None:
            skipped += 1
            continue
        # statSourceId 1 = projection; scoringPeriodId 0 = full season.
        #
        # seasonId is NOT optional here. ESPN ships the PRIOR season's
        # full-season projection in the same array under the same
        # (statSourceId=1, scoringPeriodId=0) shape, and it usually comes
        # FIRST -- 91% of the top 300 on 2026-08-23. Without this filter the
        # ingest stored last year's number as this year's projection: James
        # Conner 250.2 (2025) against a real 2026 projection of 0.0, Quinshon
        # Judkins 49.6 against 226.4, and every kicker zeroed (their 2025 slot
        # is 0.0, which build_pool then drops as "no projection"). Mean error
        # across the sampled board was 61.6 points.
        season_proj = next(
            (
                s
                for s in player.get("stats") or []
                if s.get("statSourceId") == 1
                and s.get("scoringPeriodId") == 0
                and str(s.get("seasonId")) == SEASON
            ),
            None,
        )
        points = (season_proj or {}).get("appliedTotal")
        if points is None:
            skipped += 1
            continue
        _upsert_projection(session, canonical, "espn", float(points), {"applied_total_ppr": points})
        adp = ((player.get("ownership") or {}).get("averageDraftPosition"))
        if adp and round(float(adp)) not in placeholders:
            _upsert_adp(session, canonical, "espn", "ppr", None, float(adp))
        elif adp:
            # A previous ingest may have stored the placeholder as real market
            # data; clear it rather than leave the pollution in place.
            _delete_adp(session, canonical, "espn", "ppr")
        written += 1
    session.commit()
    return written, skipped


# FFC quirks: kickers are "PK"; defenses are "<City> Defense" rather than team names.
_FFC_POS = {"PK": "K"}
_FFC_DEF_CITY_TO_ABBR = {
    "arizona": "ARI", "atlanta": "ATL", "baltimore": "BAL", "buffalo": "BUF",
    "carolina": "CAR", "chicago": "CHI", "cincinnati": "CIN", "cleveland": "CLE",
    "dallas": "DAL", "denver": "DEN", "detroit": "DET", "green bay": "GB",
    "houston": "HOU", "indianapolis": "IND", "jacksonville": "JAX", "kansas city": "KC",
    "la chargers": "LAC", "la rams": "LAR", "las vegas": "LV", "miami": "MIA",
    "minnesota": "MIN", "new england": "NE", "new orleans": "NO", "ny giants": "NYG",
    "ny jets": "NYJ", "philadelphia": "PHI", "pittsburgh": "PIT", "san francisco": "SF",
    "seattle": "SEA", "tampa bay": "TB", "tennessee": "TEN", "washington": "WAS",
}
# Nickname aliases observed in FFC data (FFC name -> crosswalk name).
_FFC_NAME_ALIASES = {
    "kenny gainwell": "kenneth gainwell",
    "chig okonkwo": "chigoziem okonkwo",
}


def ingest_ffc_adp(
    session: Session, payload: dict[str, Any], fmt: str, teams: int
) -> tuple[int, int]:
    rows = session.scalars(select(PlayerIds)).all()
    # Recompute names through our normalizer so both sides of the join agree.
    # Names claimed by two players (Gore Jr./Sr., the Kyle Williamses) are
    # ambiguous: attributing FFC's row to either would be a guess, so refuse.
    by_name: dict[tuple[str, str | None], int] = {}
    ambiguous: set[tuple[str, str | None]] = set()
    for r in rows:
        key = (make_merge_name(r.full_name), r.position)
        if key in by_name and by_name[key] != r.canonical_id:
            ambiguous.add(key)
        else:
            by_name[key] = r.canonical_id
    def_by_team = {r.team: r.canonical_id for r in rows if r.position == "DEF" and r.team}

    written = skipped = 0
    for p in payload.get("players", []):
        raw_pos = p.get("position", "")
        position = _FFC_POS.get(raw_pos, raw_pos)
        name = p.get("name", "")
        if position == "DEF":
            city = make_merge_name(name.removesuffix(" Defense"))
            canonical = def_by_team.get(_FFC_DEF_CITY_TO_ABBR.get(city, ""))
        else:
            merge = make_merge_name(name)
            key = (_FFC_NAME_ALIASES.get(merge, merge), position)
            if key in ambiguous:
                skipped += 1
                log.warning("ffc: %s (%s) is a namesake shared by multiple players; skipping", name, raw_pos)
                continue
            canonical = by_name.get(key)
        if canonical is None:
            skipped += 1
            log.warning("ffc: no crosswalk match for %s (%s)", name, raw_pos)
            continue
        _upsert_adp(session, canonical, "ffc", fmt, teams, float(p["adp"]), p.get("stdev"))
        written += 1
    session.commit()
    return written, skipped


def run(session: Session, teams_variants: tuple[int, ...] = (10, 12)) -> dict[str, int]:
    settings = get_settings()
    with httpx.Client(timeout=60.0, follow_redirects=True) as client:
        log.info("fetching Sleeper projections…")
        sleeper_rows = client.get(SLEEPER_PROJ_URL.format(season=SEASON)).raise_for_status().json()

        log.info("fetching ESPN kona projections…")
        espn_resp = client.get(
            f"{settings.espn_base_url}/seasons/{SEASON}/segments/0/leaguedefaults/3",
            params={"view": "kona_player_info"},
            headers={
                "X-Fantasy-Filter": '{"players":{"limit":1500,"sortPercOwned":{"sortAsc":false,"sortPriority":1}}}'
            },
        )
        espn_players = espn_resp.raise_for_status().json().get("players", [])

        weeks = upcoming_weeks(session)
        sleeper_weekly: dict[int, list[dict[str, Any]]] = {}
        for week in weeks:
            log.info("fetching Sleeper week %d projections…", week)
            sleeper_weekly[week] = (
                client.get(SLEEPER_WEEK_PROJ_URL.format(season=SEASON, week=week))
                .raise_for_status()
                .json()
            )

        ffc_payloads = {}
        for teams in teams_variants:
            log.info("fetching FFC ADP (ppr, %d teams)…", teams)
            ffc_payloads[teams] = (
                client.get(FFC_ADP_URL.format(fmt="ppr", teams=teams, season=SEASON))
                .raise_for_status()
                .json()
            )

    written = skipped = 0
    w, s = ingest_sleeper_projections(session, sleeper_rows)
    log.info("sleeper projections: written=%d skipped=%d", w, s)
    written, skipped = written + w, skipped + s
    w, s = ingest_espn_projections(session, espn_players)
    log.info("espn projections: written=%d skipped=%d", w, s)
    written, skipped = written + w, skipped + s
    for week, rows in sleeper_weekly.items():
        w, s = ingest_sleeper_weekly_projections(session, rows, week)
        log.info("sleeper week %d projections: written=%d skipped=%d", week, w, s)
        written, skipped = written + w, skipped + s
        w, s = ingest_espn_weekly_projections(session, espn_players, week)
        log.info("espn week %d projections: written=%d skipped=%d", week, w, s)
        written, skipped = written + w, skipped + s
    for teams, payload in ffc_payloads.items():
        w, s = ingest_ffc_adp(session, payload, "ppr", teams)
        log.info("ffc adp (%d teams): written=%d skipped=%d", teams, w, s)
        written, skipped = written + w, skipped + s
    return {"rows_in": written + skipped, "rows_written": written, "rows_skipped": skipped}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s", stream=sys.stderr)
    from api.db import get_sessionmaker

    with get_sessionmaker()() as session:
        run(session)
