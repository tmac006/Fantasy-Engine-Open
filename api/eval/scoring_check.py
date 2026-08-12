"""Check our ESPN scoring translation against ESPN's own answer key.

    uv run python -m api.eval.scoring_check

`STAT_ID_TO_KEY` is a hand-maintained map, and a hand-maintained map is only as
complete as whoever last edited it. That is not a hypothetical worry: a real
league scored yardage in blocks ("Every 25 passing yards = 1") under statIds the
map had never heard of, so the league came through scoring no yardage at all and
every projection in it was quietly wrong. It was caught by a human reading his
own settings page, which is not a process.

ESPN will grade us. Ask for a league's projections and each player carries both
the raw projected stats and `appliedTotal`, which is ESPN scoring that same
projection under that league's rules. Score the raw stats ourselves, compare to
their number, and any statId we mishandle shows up as a gap in points.

Verified along the way: summing `raw[statId] * points[statId]` over the league's
scoring items reproduces `appliedTotal` to the cent, so ESPN's model really is
plain multiply-and-add and this comparison is exact rather than approximate.
"""

from __future__ import annotations

import argparse
from collections import defaultdict

from sqlalchemy import select

from api.adapters.espn import (
    STAT_ID_TO_KEY,
    STAT_ID_YARDS_PER_BLOCK,
    EspnReadClient,
    translate_espn_settings,
)
from api.db import get_sessionmaker
from api.models.league import League
from api.schemas import LeagueSettings

# ESPN projects the coming season under this stat entry id (source 1 = projected,
# split 0 = season). The 00-prefixed sibling is actual results.
PROJECTION_STAT_ID = "10{season}"

# Points per player-season we are willing to leave unexplained. Truncation alone
# accounts for a fraction of a point (ESPN rounds each game's yardage block down,
# we use the linear rate), so anything above this is a statId, not rounding.
TOLERANCE = 2.0

# Kicking and defense are scored from source totals rather than re-scored from
# stats, so their scoring items are expected to be unaccounted for and are not
# evidence of a bug. Everything else is.
SKILL_POSITION_IDS = frozenset({1, 2, 3, 4})


def our_total(raw: dict[int, float], settings: LeagueSettings) -> float:
    """Score ESPN's raw projected stats the way our engine would.

    Yardage always reads from the per-yard statIds. A league scoring in blocks
    still stores the underlying yards there, and `translate_espn_settings` has
    already converted its block value into a per-yard rate, so the two forms
    converge here rather than needing separate handling.
    """
    total = 0.0
    counted: set[str] = set()
    for stat_id, key in STAT_ID_TO_KEY.items():
        rate = settings.scoring.get(key)
        if rate is None or key in counted:
            continue
        value = raw.get(stat_id)
        if value is None:
            continue
        total += value * rate
        counted.add(key)
    return total


def check_league(league_id: str, season: str, limit: int) -> tuple[int, list[str]]:
    """Compare every sampled player; return (players checked, problems)."""
    client = EspnReadClient()
    payload = client.league_view(league_id, season, "mSettings")
    settings = translate_espn_settings(payload)
    items = {
        item["statId"]: float(item.get("points") or 0.0)
        for item in payload["settings"]["scoringSettings"]["scoringItems"]
    }
    handled = set(STAT_ID_TO_KEY) | set(STAT_ID_YARDS_PER_BLOCK)

    errors: list[float] = []
    unexplained: dict[int, float] = defaultdict(float)
    worst: tuple[float, str] = (0.0, "")
    checked = 0

    for entry in client.projected_players(league_id, season, limit=limit):
        player = entry.get("player") or {}
        if player.get("defaultPositionId") not in SKILL_POSITION_IDS:
            continue
        projection = next(
            (
                stat
                for stat in player.get("stats", [])
                if stat.get("id") == PROJECTION_STAT_ID.format(season=season)
            ),
            None,
        )
        if not projection or not projection.get("stats"):
            continue
        raw = {int(k): float(v) for k, v in projection["stats"].items()}
        espn = float(projection["appliedTotal"])
        mine = our_total(raw, settings)
        error = mine - espn
        errors.append(error)
        checked += 1
        if abs(error) > abs(worst[0]):
            worst = (error, player.get("fullName", "?"))
        # Attribute the gap: which scored statIds does our map not represent?
        for stat_id, points in items.items():
            if points and stat_id not in handled and raw.get(stat_id):
                unexplained[stat_id] += raw[stat_id] * points

    problems: list[str] = []
    if not checked:
        problems.append("no projected skill players returned; cannot verify scoring")
        return checked, problems

    mean_abs = sum(abs(e) for e in errors) / checked
    print(f"  scoring: {checked} players, mean error {mean_abs:+.2f} pts, "
          f"worst {worst[0]:+.2f} ({worst[1]})")
    if mean_abs > TOLERANCE:
        problems.append(
            f"our scoring differs from ESPN's by {mean_abs:.2f} pts per player on "
            f"average (tolerance {TOLERANCE}). A scored statId is likely unmapped."
        )
    # Always list what the map does not cover, even when it is under tolerance.
    # Averaging a quarterback-only stat across sixty mostly-non-quarterbacks
    # buries it, and a tool built to surface unknown statIds must not be the
    # thing that hides one. Only a material gap fails the run.
    ranked = sorted(unexplained.items(), key=lambda kv: -abs(kv[1]))[:5]
    for stat_id, points in ranked:
        per_player = points / checked
        note = f"    unmapped statId {stat_id}: {per_player:+.2f} pts/player"
        if abs(per_player) >= TOLERANCE:
            problems.append(
                f"statId {stat_id} is scored by this league and unmapped, worth "
                f"{per_player:+.2f} pts per player"
            )
        else:
            print(f"{note} (under tolerance)")
    return checked, problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--season", default="2026")
    parser.add_argument("--limit", type=int, default=60, help="players sampled per league")
    parser.add_argument("--league", help="check one league id instead of all registered")
    args = parser.parse_args()

    if args.league:
        league_ids = [args.league]
    else:
        with get_sessionmaker()() as session:
            league_ids = list(
                session.scalars(
                    select(League.platform_league_id).where(
                        League.platform == "espn", League.active.is_(True)
                    )
                )
            )
    if not league_ids:
        print("no registered ESPN leagues")
        return 1

    total_problems = 0
    for league_id in league_ids:
        print(f"\nleague {league_id}")
        try:
            _, problems = check_league(league_id, args.season, args.limit)
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            problems = [f"could not check: {exc}"]
        for problem in problems:
            print(f"  PROBLEM: {problem}")
        total_problems += len(problems)

    print()
    if total_problems:
        print(f"{total_problems} scoring problem(s). Advice in these leagues is wrong.")
        return 1
    print(f"{len(league_ids)} league(s) score as ESPN scores them.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
