"""Player master + ID crosswalk ingest (spec §6, revised per docs/findings.md §4).

Seeding order matters: DynastyProcess is the espn_id authority (Sleeper's own
espn_id field is missing for ~59% of top-300 players — Phase 0 finding).

Idempotent: safe to re-run any time. Run: `uv run python -m api.ingest.players`
"""

import csv
import io
import logging
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.adapters.espn import ESPN_PRO_TEAM_ABBREV
from api.config import get_settings
from api.models import Player, PlayerIds

log = logging.getLogger("ingest.players")

DP_IDS_URL = "https://github.com/dynastyprocess/data/raw/master/files/db_playerids.csv"
FANTASY_POSITIONS = {"QB", "RB", "WR", "TE", "K", "DEF"}
_SUFFIXES = re.compile(r"\b(jr|sr|ii|iii|iv|v)\b\.?", re.IGNORECASE)
_NON_ALNUM = re.compile(r"[^a-z0-9 ]")


def make_merge_name(name: str) -> str:
    """Uniform join key: lowercase, accents folded, no punctuation or suffixes."""
    n = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    n = _SUFFIXES.sub("", n.lower())
    n = _NON_ALNUM.sub("", n)
    return " ".join(n.split())


@dataclass
class Counts:
    rows_in: int = 0
    inserted: int = 0
    updated: int = 0
    skipped: dict[str, int] = field(default_factory=dict)

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1


class CrosswalkIndex:
    """In-memory view of player_ids for fast matching; mirrors DB uniqueness."""

    def __init__(self, session: Session) -> None:
        self.session = session
        rows = session.scalars(select(PlayerIds)).all()
        self.by_sleeper = {r.sleeper_id: r for r in rows if r.sleeper_id}
        self.by_gsis = {r.gsis_id: r for r in rows if r.gsis_id}
        self.by_espn = {r.espn_id: r for r in rows if r.espn_id}
        self.by_name = {(r.merge_name, r.position): r for r in rows}

    def find(
        self,
        sleeper_id: str | None,
        gsis_id: str | None,
        espn_id: str | None,
        merge_name: str,
        position: str | None,
    ) -> PlayerIds | None:
        for index, key in (
            (self.by_sleeper, sleeper_id),
            (self.by_gsis, gsis_id),
            (self.by_espn, espn_id),
        ):
            if key and key in index:
                return index[key]
        return self.by_name.get((merge_name, position))

    def add(self, row: PlayerIds) -> None:
        if row.sleeper_id:
            self.by_sleeper[row.sleeper_id] = row
        if row.gsis_id:
            self.by_gsis[row.gsis_id] = row
        if row.espn_id:
            self.by_espn[row.espn_id] = row
        self.by_name[(row.merge_name, row.position)] = row

    def claim_ok(self, row: PlayerIds, attr: str, value: str) -> bool:
        """True if `value` for unique column `attr` isn't already owned by another row."""
        index = {"sleeper_id": self.by_sleeper, "gsis_id": self.by_gsis, "espn_id": self.by_espn}[attr]
        owner = index.get(value)
        return owner is None or owner is row


def _set_ids(idx: CrosswalkIndex, row: PlayerIds, c: Counts, **ids: str | None) -> bool:
    changed = False
    for attr, value in ids.items():
        if not value or getattr(row, attr):
            continue
        if not idx.claim_ok(row, attr, value):
            c.skip(f"conflict_{attr}")
            log.warning("ID conflict: %s=%s already mapped; not assigning to %s", attr, value, row.full_name)
            continue
        setattr(row, attr, value)
        changed = True
    if changed:
        idx.add(row)
    return changed


def ingest_dynastyprocess(session: Session, csv_text: str) -> Counts:
    c = Counts()
    idx = CrosswalkIndex(session)

    def clean(value: str | None) -> str | None:
        # DynastyProcess is an R export: missing values are the literal string "NA".
        v = (value or "").strip()
        return v if v and v.upper() != "NA" else None

    for rec in csv.DictReader(io.StringIO(csv_text)):
        c.rows_in += 1
        position = (rec.get("position") or "").upper() or None
        name = (rec.get("name") or "").strip()
        if not name or position not in FANTASY_POSITIONS:
            c.skip("not_fantasy_relevant")
            continue
        sleeper_id = clean(rec.get("sleeper_id"))
        gsis_id = clean(rec.get("gsis_id"))
        espn_id = clean(rec.get("espn_id"))
        pfr_id = clean(rec.get("pfr_id"))
        # Always our normalization — DP's merge_name keeps hyphens, ours must be uniform.
        merge_name = make_merge_name(name)

        row = idx.find(sleeper_id, gsis_id, espn_id, merge_name, position)
        if row is None:
            row = PlayerIds(
                full_name=name, merge_name=merge_name, position=position,
                team=(rec.get("team") or None),
            )
            session.add(row)
            idx.add(row)
            _set_ids(idx, row, c, sleeper_id=sleeper_id, gsis_id=gsis_id, espn_id=espn_id)
            row.pfr_id = row.pfr_id or pfr_id
            c.inserted += 1
        else:
            if _set_ids(idx, row, c, sleeper_id=sleeper_id, gsis_id=gsis_id, espn_id=espn_id):
                c.updated += 1
            row.pfr_id = row.pfr_id or pfr_id
    session.commit()
    return c


def ingest_sleeper_players(session: Session, payload: dict[str, Any]) -> Counts:
    c = Counts()
    idx = CrosswalkIndex(session)
    existing_players = {p.canonical_id: p for p in session.scalars(select(Player)).all()}
    unmatched_relevant: list[str] = []

    for sleeper_id, p in payload.items():
        if not isinstance(p, dict):
            continue
        c.rows_in += 1
        position = p.get("position")
        if position not in FANTASY_POSITIONS:
            c.skip("not_fantasy_relevant")
            continue
        name = p.get("full_name") or f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
        if not name:
            c.skip("no_name")
            continue
        merge_name = make_merge_name(name)
        gsis_id = (p.get("gsis_id") or "").strip() or None
        espn_id = str(p["espn_id"]) if p.get("espn_id") else None

        row = idx.find(sleeper_id, gsis_id, espn_id, merge_name, position)
        if row is None:
            row = PlayerIds(full_name=name, merge_name=merge_name, position=position, team=p.get("team"))
            session.add(row)
            idx.add(row)
            c.inserted += 1
            search_rank = p.get("search_rank")
            if search_rank is not None and search_rank < 300:
                unmatched_relevant.append(f"{name} ({position}, rank {search_rank})")
        else:
            c.updated += 1
        _set_ids(idx, row, c, sleeper_id=sleeper_id, gsis_id=gsis_id, espn_id=espn_id)
        row.team = p.get("team") or row.team
        session.flush()

        player = existing_players.get(row.canonical_id)
        if player is None:
            player = Player(canonical_id=row.canonical_id, full_name=name)
            session.add(player)
            existing_players[row.canonical_id] = player
        player.full_name = name
        player.position = position
        player.team = p.get("team")
        player.status = p.get("status")
        player.injury_status = p.get("injury_status")
        player.search_rank = p.get("search_rank")
        player.years_exp = p.get("years_exp")
        player.age = p.get("age")

    session.commit()
    if unmatched_relevant:
        # Loud, per spec: silent join failures make recommendations subtly wrong.
        log.warning(
            "%d fantasy-relevant Sleeper players had NO crosswalk row (created fresh, no espn_id): %s",
            len(unmatched_relevant),
            "; ".join(unmatched_relevant[:20]),
        )
    return c


# ESPN defaultPositionId -> position (players); D/ST use negative IDs and slot 16.
_ESPN_POS = {1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 16: "DEF"}


def ingest_espn_pool(session: Session, players: list[dict[str, Any]]) -> Counts:
    """Backfill espn_id only (never creates rows) from the no-auth kona_player_info pool.
    Catches what DynastyProcess misses — kickers, notably (Phase 1 finding)."""
    c = Counts()
    idx = CrosswalkIndex(session)
    def_by_team = {
        row.team: row
        for (_, pos), row in idx.by_name.items()
        if pos == "DEF" and row.team
    }
    for entry in players:
        c.rows_in += 1
        p = entry.get("player") or {}
        espn_id = str(entry.get("id") or "") or None
        name = p.get("fullName") or ""
        position = _ESPN_POS.get(p.get("defaultPositionId", 0))
        if not espn_id or not name or position is None:
            c.skip("not_fantasy_relevant")
            continue
        if espn_id in idx.by_espn:
            c.skip("already_mapped")
            continue
        if position == "DEF":
            # ESPN names defenses "Texans D/ST" while ours carry the full city
            # name, so name matching finds nothing and every drafted D/ST used
            # to resolve to no player -- which meant a rostered defense never
            # counted as filled and the engine kept recommending another one.
            # Team abbreviation is the stable join.
            abbrev = ESPN_PRO_TEAM_ABBREV.get(p.get("proTeamId", 0))
            row = def_by_team.get(abbrev) if abbrev else None
        else:
            row = idx.by_name.get((make_merge_name(name), position))
        if row is None:
            c.skip("no_name_match")
            continue
        if _set_ids(idx, row, c, espn_id=espn_id):
            c.updated += 1
    session.commit()
    return c


def fetch_espn_pool(client: httpx.Client, limit: int = 2000) -> list[dict[str, Any]]:
    resp = client.get(
        f"{get_settings().espn_base_url}/seasons/2026/segments/0/leaguedefaults/3",
        params={"view": "kona_player_info"},
        headers={
            "X-Fantasy-Filter": (
                f'{{"players":{{"limit":{limit},"sortPercOwned":{{"sortAsc":false,"sortPriority":1}}}}}}'
            )
        },
    )
    resp.raise_for_status()
    data: list[dict[str, Any]] = resp.json().get("players", [])
    return data


def missing_espn_report(session: Session, top_n_rank: int = 300) -> list[str]:
    """Players relevant enough to draft but lacking an espn_id — the miserable-to-debug case."""
    rows = session.execute(
        select(PlayerIds.full_name, PlayerIds.position, Player.search_rank)
        .join(Player, Player.canonical_id == PlayerIds.canonical_id)
        .where(PlayerIds.espn_id.is_(None), Player.search_rank < top_n_rank, Player.team.is_not(None))
        .order_by(Player.search_rank)
    ).all()
    return [f"{name} ({pos}, rank {rank})" for name, pos, rank in rows]


def run(session: Session) -> dict[str, int]:
    settings = get_settings()
    with httpx.Client(timeout=60.0, follow_redirects=True) as client:
        log.info("fetching DynastyProcess ID map…")
        dp_csv = client.get(DP_IDS_URL).raise_for_status().text
        log.info("fetching Sleeper player DB (~14MB)…")
        sleeper = client.get(f"{settings.sleeper_base_url}/v1/players/nfl").raise_for_status().json()
        log.info("fetching ESPN player pool…")
        espn_pool = fetch_espn_pool(client)

    c1 = ingest_dynastyprocess(session, dp_csv)
    log.info("dynastyprocess: in=%d inserted=%d updated=%d skipped=%s", c1.rows_in, c1.inserted, c1.updated, c1.skipped)
    c2 = ingest_sleeper_players(session, sleeper)
    log.info("sleeper: in=%d inserted=%d updated=%d skipped=%s", c2.rows_in, c2.inserted, c2.updated, c2.skipped)
    c3 = ingest_espn_pool(session, espn_pool)
    log.info("espn_pool: in=%d espn_id_backfilled=%d skipped=%s", c3.rows_in, c3.updated, c3.skipped)

    missing = missing_espn_report(session)
    if missing:
        log.warning("MISSING espn_id for %d rostered top-300 players: %s", len(missing), "; ".join(missing[:15]))
    else:
        log.info("espn_id coverage: complete for all rostered top-300 players")

    written = c1.inserted + c1.updated + c2.inserted + c2.updated + c3.updated
    skipped = sum(c1.skipped.values()) + sum(c2.skipped.values()) + sum(c3.skipped.values())
    return {
        "rows_in": c1.rows_in + c2.rows_in + c3.rows_in,
        "rows_written": written,
        "rows_skipped": skipped,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s", stream=sys.stderr)
    from api.db import get_sessionmaker

    with get_sessionmaker()() as session:
        run(session)
