"""Fit `EngineParams.starter_unavailable_rate` from a season of weekly stats.

This is the number that prices bench depth: how often a drafted starter is
missing from a lineup in a given week, whether hurt, inactive or on bye. The
draft engine multiplies a surplus player's value by the chance a week actually
calls on him, so getting this wrong reshapes every roster the engine builds.

    uv run python -m api.eval.availability --season 2025

The selector must be blind to what it grades. Ranking by season total picks the
players who stayed healthy, which is precisely the sample that cannot measure
injury, and it reported the rate as roughly half its real value. So the sample
is chosen on early-season production alone and graded on the weeks after.

Refit each offseason once a full season has landed; the README lists this among
the coefficients that want a yearly look.
"""

from __future__ import annotations

import argparse
from collections import defaultdict

from sqlalchemy import select

from api.db import get_sessionmaker
from api.models.player import PlayerIds
from api.models.stats import WeeklyStat

# Roughly the pool a 12-team league drafts as starters at each position.
SAMPLE_SIZE = {"QB": 24, "RB": 36, "WR": 36, "TE": 24}


def measure(
    season: str, select_weeks: frozenset[int], grade_weeks: frozenset[int]
) -> tuple[list[tuple[str, int, float, float]], float, int]:
    """Return (rows of position/count/miss rate/std err, pooled rate, sample size)."""
    with get_sessionmaker()() as session:
        rows = session.execute(
            select(
                WeeklyStat.canonical_id,
                WeeklyStat.week,
                WeeklyStat.stats,
                PlayerIds.position,
            )
            .join(PlayerIds, PlayerIds.canonical_id == WeeklyStat.canonical_id)
            .where(WeeklyStat.season == season)
        ).all()

    graded: dict[int, set[int]] = defaultdict(set)
    selection_points: dict[int, float] = defaultdict(float)
    position_of: dict[int, str] = {}
    for canonical_id, week, stats, position in rows:
        position_of[canonical_id] = position
        if week in select_weeks:
            selection_points[canonical_id] += float(
                (stats or {}).get("fantasy_points_ppr") or 0.0
            )
        elif week in grade_weeks:
            graded[canonical_id].add(week)

    candidates: dict[str, list[int]] = defaultdict(list)
    for canonical_id, position in position_of.items():
        if canonical_id in selection_points:
            candidates[position].append(canonical_id)

    results = []
    pooled_played = pooled_total = 0
    for position, cutoff in SAMPLE_SIZE.items():
        ids = sorted(
            candidates[position], key=lambda c: selection_points[c], reverse=True
        )[:cutoff]
        if not ids:
            continue
        played = sum(len(graded[c]) for c in ids)
        total = len(ids) * len(grade_weeks)
        pooled_played += played
        pooled_total += total
        rate = 1.0 - played / total
        results.append((position, len(ids), rate, (rate * (1 - rate) / total) ** 0.5))
    pooled = 1.0 - pooled_played / pooled_total if pooled_total else 0.0
    return results, pooled, pooled_total


def main() -> None:
    parser = argparse.ArgumentParser(description="Fit the starter unavailability rate")
    parser.add_argument("--season", default="2025")
    parser.add_argument("--select-through", type=int, default=3)
    # Week 18 is not a fantasy week and starters rest, which would book a rest
    # day as an injury and inflate the rate.
    parser.add_argument("--grade-through", type=int, default=17)
    args = parser.parse_args()

    select_weeks = frozenset(range(1, args.select_through + 1))
    grade_weeks = frozenset(range(args.select_through + 1, args.grade_through + 1))
    results, pooled, sample = measure(args.season, select_weeks, grade_weeks)

    print(f"{args.season}: selected on weeks 1-{args.select_through}, "
          f"graded on {args.select_through + 1}-{args.grade_through}\n")
    print(f"{'pos':<6}{'n':>5}{'miss rate':>12}{'std err':>10}")
    for position, count, rate, stderr in results:
        print(f"{position:<6}{count:>5}{rate:>12.3f}{stderr:>10.3f}")
    print(f"\npooled {pooled:.4f} over {sample} player-weeks")
    spread = max(r for _, _, r, _ in results) - min(r for _, _, r, _ in results)
    worst = max(stderr for *_, stderr in results)
    print(
        f"spread across positions {spread:.3f} against a worst standard error of "
        f"{worst:.3f} -- {'one pooled rate' if spread <= worst else 'PER-POSITION RATES'} "
        "is the honest model"
    )


if __name__ == "__main__":
    main()
