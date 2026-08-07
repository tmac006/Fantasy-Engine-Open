"""Season projection + ADP ingest from three free sources.

Sources (all verified live 2026-08-04):
- Sleeper (undocumented `api.sleeper.com`): raw stat projections keyed exactly like
  league scoring_settings, so they can be re-scored under any league's rules. Also
  carries per-format ADP. Primary projection source.
- ESPN kona_player_info (no auth): consensus points projection + live ADP.
- FantasyFootballCalculator: community ADP with stdev, per team-count and format.

Idempotent: safe to re-run. Run: `uv run python -m api.ingest.projections`
"""

import logging
import sys
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
FFC_ADP_URL = "https://fantasyfootballcalculator.com/api/v1/adp/{fmt}?teams={teams}&year={season}"

# Sleeper stat keys that are ADP metadata, not projected stats.
_SLEEPER_ADP_KEYS = {"ppr": "adp_ppr", "half_ppr": "adp_half_ppr", "std": "adp_std"}


def _upsert_projection(
    session: Session, canonical_id: int, source: str, points: float | None, stats: dict[str, Any]
) -> bool:
    row = session.scalar(
        select(Projection).where(
            Projection.canonical_id == canonical_id,
            Projection.source == source,
            Projection.season == SEASON,
            Projection.week.is_(None),
        )
    )
    if row is None:
        session.add(
            Projection(canonical_id=canonical_id, source=source, season=SEASON, points=points, stats=stats)
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


def ingest_espn_projections(session: Session, players: list[dict[str, Any]]) -> tuple[int, int]:
    by_espn = {
        r.espn_id: r.canonical_id
        for r in session.scalars(select(PlayerIds).where(PlayerIds.espn_id.is_not(None)))
    }
    written = skipped = 0
    for entry in players:
        canonical = by_espn.get(str(entry.get("id") or ""))
        player = entry.get("player") or {}
        if canonical is None:
            skipped += 1
            continue
        # statSourceId 1 = projection; scoringPeriodId 0 = full season.
        season_proj = next(
            (
                s
                for s in player.get("stats") or []
                if s.get("statSourceId") == 1 and s.get("scoringPeriodId") == 0
            ),
            None,
        )
        points = (season_proj or {}).get("appliedTotal")
        if points is None:
            skipped += 1
            continue
        _upsert_projection(session, canonical, "espn", float(points), {"applied_total_ppr": points})
        adp = ((player.get("ownership") or {}).get("averageDraftPosition"))
        if adp:
            _upsert_adp(session, canonical, "espn", "ppr", None, float(adp))
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
    by_name = {(make_merge_name(r.full_name), r.position): r.canonical_id for r in rows}
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
            canonical = by_name.get((_FFC_NAME_ALIASES.get(merge, merge), position))
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
