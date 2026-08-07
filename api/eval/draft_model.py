"""Repeatable scenario evaluation for recommendation timing.

Run with ``uv run python -m api.eval.draft_model``. This is not a historical
win-rate backtest; it is a leakage-free regression harness until real,
timestamped platform draft histories are available.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

from api.engine.params import EngineParams
from api.engine.recommend import DraftContext, recommend
from api.engine.vor import PoolPlayer, replacement_points, replacement_ranks, vor
from api.schemas import LeagueSettings


@dataclass(frozen=True)
class ScenarioResult:
    scenario: str
    vor_baseline: str
    recommended: str
    room_aware: str
    picks_agree: bool


def _players(position: str, count: int, top: float, step: float, adp_start: int) -> list[PoolPlayer]:
    return [
        PoolPlayer(
            canonical_id=f"{position}-{index}",
            name=f"{position} {index + 1}",
            position=position,
            team=None,
            points=top - index * step,
            adp=float(adp_start + index * 3),
        )
        for index in range(count)
    ]


def _pool(*, elite_te: bool = False, superflex: bool = False) -> list[PoolPlayer]:
    tight_ends = _players("TE", 20, 215.0, 3.0, 45)
    if elite_te:
        tight_ends[0] = PoolPlayer("elite-te", "Elite TE", "TE", None, 400.0, adp=18.0)
    quarterbacks = _players("QB", 24, 350.0 if superflex else 325.0, 5.0, 20)
    return [
        *_players("RB", 48, 285.0, 3.4, 1),
        *_players("WR", 48, 280.0, 3.1, 2),
        *quarterbacks,
        *tight_ends,
    ]


def evaluate_scenario(
    name: str,
    settings: LeagueSettings,
    context: DraftContext,
    pool: list[PoolPlayer],
) -> ScenarioResult:
    params = EngineParams(simulation_trials=64, simulation_seed=2026)
    result = recommend(pool, settings, context, params)
    if result is None:
        raise ValueError(f"{name}: empty recommendation")
    replacement = replacement_points(pool, replacement_ranks(settings, params))
    available = [player for player in pool if player.canonical_id not in context.drafted_ids]
    baseline = max(available, key=lambda player: vor(player, replacement))
    return ScenarioResult(
        scenario=name,
        vor_baseline=f"{baseline.name} ({baseline.position})",
        recommended=f"{result.recommended_pick.name} ({result.recommended_pick.position})",
        room_aware=f"{result.room_pick.name} ({result.room_pick.position})",
        picks_agree=result.picks_agree,
    )


def run() -> list[ScenarioResult]:
    base_slots = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 2, "BN": 6}
    standard = LeagueSettings(scoring={"rec": 1.0}, roster_slots=base_slots, total_teams=12)
    superflex = LeagueSettings(
        scoring={"rec": 0.5, "pass_td": 4.0},
        roster_slots={**base_slots, "SUPER_FLEX": 1},
        total_teams=12,
    )
    return [
        evaluate_scenario(
            "flat-middle-TE",
            standard,
            DraftContext(frozenset(), ("RB", "WR"), 28, 45, ()),
            _pool(),
        ),
        evaluate_scenario(
            "elite-TE-cliff",
            standard,
            DraftContext(frozenset(), ("RB",), 18, 31, ()),
            _pool(elite_te=True),
        ),
        evaluate_scenario(
            "superflex-QB-demand",
            superflex,
            DraftContext(frozenset(), ("RB", "WR"), 20, 29, ("QB", "QB", "WR", "QB")),
            _pool(superflex=True),
        ),
    ]


if __name__ == "__main__":
    print(json.dumps([asdict(result) for result in run()], indent=2))
