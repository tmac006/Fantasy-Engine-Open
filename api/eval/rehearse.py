"""Draft-day rehearsal against the leagues you actually play in (spec Phase 5).

`mockdraft` proves the engine on one hardcoded league shape. That is not the
shape of any real league: roster settings drive replacement level, startable
counts and therefore the whole bench-depth discount, so a second FLEX slot moves
every number the engine produces. This runs the same rehearsal against the
registered settings of each league, at every draft slot.

    uv run python -m api.eval.rehearse                 # every league, one seed
    uv run python -m api.eval.rehearse --seeds 3       # before a real draft
    uv run python -m api.eval.rehearse --platform espn

A league you have not registered yet can still be checked, which is the quickest
way to answer whether some unusual room size or roster shape holds up:

    uv run python -m api.eval.rehearse --teams 16
    uv run python -m api.eval.rehearse --teams 16 --slots QB:1,RB:2,WR:3,TE:1,FLEX:2,K:1,DEF:1,BN:6

Run it before every draft and after any engine change. A violation here is a
roster you would have been stuck with.

Caveat worth knowing: per-position caps are read from live platform settings and
are not persisted with the league, so rehearsals run without them. The platform
enforces caps at pick time regardless, and the engine's own positional holds are
what this is really exercising.
"""

from __future__ import annotations

import argparse
import statistics
from collections import Counter

from sqlalchemy import select

from api.db import get_sessionmaker
from api.engine.params import EngineParams
from api.engine.vor import startable_slots
from api.eval.mockdraft import check_invariants, grade_roster, simulate_draft
from api.models.league import League, LeagueSettingsRow
from api.schemas import LeagueSettings
from api.services import build_pool

# Slots that hold nobody who scores, so they add rounds without adding starters.
BENCH_SLOTS = frozenset({"BN", "IR", "TAXI"})


def rounds_for(settings: LeagueSettings) -> int:
    """A draft runs until every roster slot is filled, bench included."""
    return sum(settings.roster_slots.values())


def rehearse_league(
    name: str,
    platform: str,
    settings: LeagueSettings,
    seeds: tuple[int, ...],
    params: EngineParams,
) -> int:
    """Sweep every slot; print the result and return the violation count."""
    rounds = rounds_for(settings)
    startable = startable_slots(settings, params)
    starting_slots = sum(
        count for slot, count in settings.roster_slots.items() if slot not in BENCH_SLOTS
    )
    print(f"\n{'=' * 72}\n{name}  ({platform}, {settings.total_teams} teams)")
    print(f"  {rounds} rounds, {starting_slots} starting slots, "
          f"bench {rounds - starting_slots}")
    print(f"  slots: {settings.roster_slots}")
    print("  startable: " + ", ".join(
        f"{pos} {count}" for pos, count in sorted(startable.items()) if count
    ))

    with get_sessionmaker()() as session:
        pool = build_pool(session, settings, platform=platform)
    print(f"  pool: {len(pool)} players\n")

    scores: list[float] = []
    shapes: Counter[str] = Counter()
    violations: list[str] = []
    for slot in range(1, settings.total_teams + 1):
        for seed in seeds:
            result = simulate_draft(
                pool, settings, my_slot=slot, rounds=rounds, seed=seed, params=params
            )
            grade = grade_roster(result, settings)
            scores.append(grade.starters)
            counts = result.my_position_counts
            shapes["/".join(f"{p}{counts.get(p, 0)}" for p in ("QB", "RB", "WR", "TE"))] += 1
            found = check_invariants(result, settings, rounds, params)
            for problem in found:
                violations.append(f"slot {slot} seed {seed}: {problem}")
            flag = "  FAIL" if found else ""
            if grade.slots_filled < grade.slots_total:
                flag += f"  UNFILLED {grade.slots_filled}/{grade.slots_total}"
            print(f"  slot {slot:>2} seed {seed}: starters {grade.starters:7.1f}"
                  f"  {dict(counts)}{flag}")

    print(f"\n  starters  mean {statistics.mean(scores):.1f}"
          f"  median {statistics.median(scores):.1f}"
          f"  worst {min(scores):.1f}")
    print("  shapes:   " + ",  ".join(f"{s} x{n}" for s, n in shapes.most_common(5)))
    if violations:
        print(f"\n  {len(violations)} VIOLATION(S):")
        for problem in violations:
            print(f"    FAIL {problem}")
    else:
        print("  all roster invariants pass")
    return len(violations)


DEFAULT_SLOTS = "QB:1,RB:2,WR:2,TE:1,FLEX:1,K:1,DEF:1,BN:7"


def parse_slots(spec: str) -> dict[str, int]:
    """Parse "QB:1,RB:2,..." into roster slots, rejecting anything malformed.

    Silently skipping a bad entry would rehearse a different league than the one
    asked about and report it as clean.
    """
    slots: dict[str, int] = {}
    for entry in spec.split(","):
        name, _, count = entry.strip().partition(":")
        if not name or not count.strip().isdigit():
            raise ValueError(f"bad slot spec {entry!r}; expected NAME:COUNT")
        slots[name.strip().upper()] = int(count)
    return slots


def main() -> int:
    parser = argparse.ArgumentParser(description="Rehearse every registered league")
    parser.add_argument("--seeds", type=int, default=1, help="rooms per draft slot")
    parser.add_argument("--platform", choices=("espn", "sleeper"))
    parser.add_argument(
        "--teams",
        type=int,
        help="rehearse an unregistered league of this size instead of the registry",
    )
    parser.add_argument("--slots", default=DEFAULT_SLOTS, help="only with --teams")
    args = parser.parse_args()

    params = EngineParams()
    seeds = tuple(range(1, args.seeds + 1))

    if args.teams:
        settings = LeagueSettings(
            # Standard PPR. An ad-hoc check is about room size and roster shape,
            # which is what moves replacement level and startable counts.
            scoring={"rec": 1.0, "pass_td": 4.0, "rush_td": 6.0, "rec_td": 6.0,
                     "pass_yd": 0.04, "rush_yd": 0.1, "rec_yd": 0.1},
            roster_slots=parse_slots(args.slots),
            total_teams=args.teams,
        )
        failures = rehearse_league(
            f"ad-hoc {args.teams}-team", args.platform or "espn", settings, seeds, params
        )
        print(f"\n{'=' * 72}")
        print("ad-hoc league rehearsed clean." if not failures
              else f"{failures} violation(s). Do not draft on this.")
        return 1 if failures else 0

    with get_sessionmaker()() as session:
        rows = session.execute(
            select(League, LeagueSettingsRow)
            .join(LeagueSettingsRow, LeagueSettingsRow.league_id == League.id)
            .where(League.active.is_(True))
        ).all()

    if args.platform:
        rows = [row for row in rows if row[0].platform == args.platform]
    if not rows:
        print("no registered leagues with settings; register one first")
        return 1

    total = 0
    for league, stored in rows:
        settings = LeagueSettings(
            scoring=stored.scoring,
            roster_slots=stored.roster_slots,
            total_teams=stored.total_teams,
        )
        total += rehearse_league(
            league.name or league.platform_league_id,
            league.platform,
            settings,
            seeds,
            params,
        )

    print(f"\n{'=' * 72}")
    if total:
        print(f"{total} violation(s) across {len(rows)} league(s). Do not draft on this.")
        return 1
    print(f"{len(rows)} league(s) rehearsed clean.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
