"""Do advanced stats predict next season beyond a player's own production?

    uv run python -m api.eval.advanced_fit

This is the gate the advanced engine has to pass before it is allowed to move a
pick. The question is deliberately hard: not "do these metrics correlate with
fantasy points" (they trivially do, because good players have good metrics) but
"do they tell you anything last season's scoring did not already say".

So every model here carries prior points per game as a baseline term, and the
advanced features have to earn their keep on top of it. Fitted on 2023 -> 2024
and scored out of sample on 2024 -> 2025, because a fit judged on its own
training data will always look like it works.

Least squares is hand-rolled via normal equations with Gaussian elimination.
The design matrices here are tiny (a handful of columns) and the alternative is
adding numpy to a dependency set this project has kept deliberately small.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from api.db import get_sessionmaker
from api.models import PlayerContext, PlayerIds
from api.models.stats import WeeklyStat

# Features per position, read from the stored PFR payload as (split, key).
FEATURES: dict[str, list[tuple[str, str]]] = {
    "RB": [
        ("rush", "yards_after_contact_per_att"),
        ("rush", "yards_before_contact_per_att"),
        ("rush", "attempts_per_broken_tackle"),
    ],
    "WR": [
        ("rec", "average_depth_of_target"),
        ("rec", "yards_after_catch_per_rec"),
        ("rec", "drop_pct"),
    ],
    "TE": [
        ("rec", "average_depth_of_target"),
        ("rec", "yards_after_catch_per_rec"),
        ("rec", "drop_pct"),
    ],
    "QB": [
        ("pass", "pocket_time"),
        ("pass", "pressure_pct"),
        ("pass", "on_target_pct"),
        ("pass", "intended_air_yards_per_att"),
    ],
}

# A player needs enough of a season for his rates to mean anything.
MIN_GAMES = 8


@dataclass(frozen=True)
class Row:
    position: str
    prior_ppg: float
    next_ppg: float
    features: list[float]


def solve(matrix: list[list[float]], target: list[float]) -> list[float] | None:
    """Ordinary least squares by normal equations; None if singular."""
    width = len(matrix[0])
    normal = [[sum(r[i] * r[j] for r in matrix) for j in range(width)] for i in range(width)]
    rhs = [sum(r[i] * t for r, t in zip(matrix, target, strict=True)) for i in range(width)]
    for col in range(width):
        pivot = max(range(col, width), key=lambda r: abs(normal[r][col]))
        if abs(normal[pivot][col]) < 1e-9:
            return None
        normal[col], normal[pivot] = normal[pivot], normal[col]
        rhs[col], rhs[pivot] = rhs[pivot], rhs[col]
        for r in range(width):
            if r == col:
                continue
            factor = normal[r][col] / normal[col][col]
            for c in range(col, width):
                normal[r][c] -= factor * normal[col][c]
            rhs[r] -= factor * rhs[col]
    return [rhs[i] / normal[i][i] for i in range(width)]


def r_squared(actual: list[float], predicted: list[float]) -> float:
    mean = sum(actual) / len(actual)
    total = sum((a - mean) ** 2 for a in actual)
    residual = sum((a - p) ** 2 for a, p in zip(actual, predicted, strict=True))
    return 1.0 - residual / total if total else 0.0


def load(session: Session, from_season: str, to_season: str) -> list[Row]:
    """One row per player who played enough in both seasons."""
    positions = {r.canonical_id: r.position for r in session.scalars(select(PlayerIds))}

    def season_ppg(season: str) -> dict[int, float]:
        totals: dict[int, list[float]] = defaultdict(list)
        for cid, stats in session.execute(
            select(WeeklyStat.canonical_id, WeeklyStat.stats).where(WeeklyStat.season == season)
        ):
            totals[cid].append(float((stats or {}).get("fantasy_points_ppr") or 0.0))
        return {
            cid: sum(games) / len(games)
            for cid, games in totals.items()
            if len(games) >= MIN_GAMES
        }

    prior, following = season_ppg(from_season), season_ppg(to_season)
    advanced = {
        row.canonical_id: row.data
        for row in session.scalars(
            select(PlayerContext).where(
                PlayerContext.source == "pfr_advstats", PlayerContext.season == from_season
            )
        )
    }

    rows: list[Row] = []
    for cid, payload in advanced.items():
        position = positions.get(cid)
        if position not in FEATURES or cid not in prior or cid not in following:
            continue
        values: list[float] = []
        for split, key in FEATURES[position]:
            value = (payload.get(split) or {}).get(key)
            if value is None:
                break
            values.append(float(value))
        if len(values) != len(FEATURES[position]):
            continue
        rows.append(Row(position, prior[cid], following[cid], values))
    return rows


def main() -> None:
    with get_sessionmaker()() as session:
        train = load(session, "2023", "2024")
        test = load(session, "2024", "2025")

    print("Does last season's advanced profile predict next season's scoring,")
    print("after last season's scoring is already accounted for?\n")
    print(f"{'pos':<5}{'train':>7}{'test':>6}{'baseline R2':>13}{'+advanced R2':>14}{'gain':>8}")
    for position in ("RB", "WR", "TE", "QB"):
        tr = [r for r in train if r.position == position]
        te = [r for r in test if r.position == position]
        if len(tr) < 20 or len(te) < 15:
            print(f"{position:<5}{len(tr):>7}{len(te):>6}   too few players to fit honestly")
            continue

        base_x = [[1.0, r.prior_ppg] for r in tr]
        full_x = [[1.0, r.prior_ppg, *r.features] for r in tr]
        y = [r.next_ppg for r in tr]
        base_w, full_w = solve(base_x, y), solve(full_x, y)
        if base_w is None or full_w is None:
            print(f"{position:<5} singular design matrix")
            continue

        actual = [r.next_ppg for r in te]
        base_pred = [base_w[0] + base_w[1] * r.prior_ppg for r in te]
        full_pred = [
            full_w[0] + full_w[1] * r.prior_ppg + sum(w * f for w, f in zip(full_w[2:], r.features, strict=True))
            for r in te
        ]
        base_r2, full_r2 = r_squared(actual, base_pred), r_squared(actual, full_pred)
        print(
            f"{position:<5}{len(tr):>7}{len(te):>6}{base_r2:>13.3f}{full_r2:>14.3f}"
            f"{full_r2 - base_r2:>+8.3f}"
        )
        for (split, key), weight in zip(FEATURES[position], full_w[2:], strict=True):
            print(f"       {split}.{key:<34} {weight:+.3f}")
    print("\nOut-of-sample R2. A gain at or below zero means the metric adds nothing")
    print("the player's own scoring did not already carry.")


if __name__ == "__main__":
    main()
