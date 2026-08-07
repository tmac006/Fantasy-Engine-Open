"""Full-draft rehearsal: the engine drafts a complete team, then gets graded.

Every positional bug this project has shipped -- the round-4 kicker, the second
kicker, the third quarterback, the duplicate defense -- was caught by a human
burning a live mock on it, because nothing ever exercised the engine across a
whole draft. This closes that gap: the engine picks every one of my turns in a
simulated 16-round room whose opponents follow the same calibrated market model
the live simulation uses, and the finished roster is checked against invariants
a human drafter would state flatly.

Doubles as the draft-day rehearsal tool (spec Phase 5):

    uv run python -m api.eval.mockdraft --slot 4 --seed 1

Pure over an injected pool -- tests run it on synthetic boards with no database;
the CLI loads the real pool.
"""

from __future__ import annotations

import random
from collections import Counter
from dataclasses import dataclass, field

from api.adapters.sleeper import remaining_picks_for_slot, snake_slot_for_pick
from api.engine.params import EngineParams
from api.engine.recommend import DraftContext, recommend
from api.engine.simulation import _market_weight, _roster_weight
from api.engine.vor import PoolPlayer
from api.schemas import LeagueSettings


@dataclass(frozen=True)
class SimulatedPick:
    pick_no: int
    round_no: int
    slot: int
    is_mine: bool
    player: PoolPlayer
    reason: str = ""


@dataclass
class DraftResult:
    my_slot: int
    picks: list[SimulatedPick] = field(default_factory=list)

    @property
    def my_picks(self) -> list[SimulatedPick]:
        return [p for p in self.picks if p.is_mine]

    @property
    def my_position_counts(self) -> Counter[str]:
        return Counter(p.player.position for p in self.my_picks)


def _opponent_pick(
    rng: random.Random,
    available: list[PoolPlayer],
    pick_no: int,
    roster: Counter[str],
    settings: LeagueSettings,
    params: EngineParams,
) -> PoolPlayer:
    """One opponent selection from the calibrated market model.

    Same weights the live next-turn simulation uses, so a rehearsal room drafts
    the way the engine already believes rooms draft. Candidates are capped to
    the nearest names by ADP for speed; the tail has ~zero weight anyway.
    """
    candidates = sorted(
        available, key=lambda p: (p.adp is None, p.adp if p.adp is not None else 0.0)
    )[:40]
    weights = [
        _market_weight(p, pick_no, 0.0, params)
        * _roster_weight(p.position, roster, settings.roster_slots, params)
        for p in candidates
    ]
    total = sum(weights)
    if total <= 0:
        return candidates[0]
    return rng.choices(candidates, weights=weights, k=1)[0]


def simulate_draft(
    pool: list[PoolPlayer],
    settings: LeagueSettings,
    my_slot: int,
    rounds: int,
    params: EngineParams | None = None,
    seed: int = 0,
) -> DraftResult:
    """Play a full snake draft; the engine takes every pick at `my_slot`."""
    params = params or EngineParams()
    rng = random.Random(seed)
    teams = settings.total_teams
    total_picks = teams * rounds

    # The engine always receives the FULL pool plus drafted ids, exactly like
    # the live router. Passing only the remaining players instead recomputes
    # tiers and replacement levels over a shrinking board -- every pick reads
    # "tier 1 (1 left)" and VOR inflates as the draft goes on.
    available: dict[str, PoolPlayer] = {p.canonical_id: p for p in pool}
    drafted_order: list[str] = []
    rosters: dict[int, Counter[str]] = {slot: Counter() for slot in range(1, teams + 1)}
    my_positions: list[str] = []
    my_teams: list[str | None] = []
    result = DraftResult(my_slot=my_slot)

    for pick_no in range(1, total_picks + 1):
        slot = snake_slot_for_pick(pick_no, teams)
        round_no = (pick_no - 1) // teams + 1
        if not available:
            break

        if slot == my_slot:
            my_next = next(
                (
                    later
                    for later in range(pick_no + 1, total_picks + 1)
                    if snake_slot_for_pick(later, teams) == my_slot
                ),
                None,
            )
            ctx = DraftContext(
                drafted_ids=frozenset(drafted_order),
                my_roster_positions=tuple(my_positions),
                current_pick=pick_no,
                my_next_pick=my_next,
                recent_positions=tuple(
                    available_position
                    for available_position in _recent_positions(result.picks)
                ),
                my_remaining_picks=remaining_picks_for_slot(pick_no, my_slot, teams, rounds),
                my_roster_nfl_teams=tuple(my_teams),
                simulation_seed=seed * 1000 + pick_no,
            )
            recommendation = recommend(pool, settings, ctx, params)
            if recommendation is None:
                break
            chosen_id = recommendation.recommended_pick.canonical_id
            chosen = available[chosen_id]
            reason = recommendation.recommended_pick.reason
            my_positions.append(chosen.position)
            my_teams.append(chosen.team)
        else:
            chosen = _opponent_pick(
                rng, list(available.values()), pick_no, rosters[slot], settings, params
            )
            reason = ""

        del available[chosen.canonical_id]
        drafted_order.append(chosen.canonical_id)
        rosters[slot][chosen.position] += 1
        result.picks.append(
            SimulatedPick(
                pick_no=pick_no,
                round_no=round_no,
                slot=slot,
                is_mine=slot == my_slot,
                player=chosen,
                reason=reason,
            )
        )

    return result


def _recent_positions(picks: list[SimulatedPick], window: int = 10) -> list[str]:
    return [p.player.position for p in picks[-window:]]


def check_invariants(result: DraftResult, settings: LeagueSettings, rounds: int) -> list[str]:
    """Rules a finished roster must satisfy. Returns violations; empty is a pass.

    These are deliberately blunt. Each one is a bug class that actually shipped,
    so a violation is a regression, not a style disagreement.
    """
    violations: list[str] = []
    counts = result.my_position_counts
    picks = result.my_picks

    for pos in ("K", "DEF"):
        slot_count = settings.roster_slots.get(pos, 0)
        if slot_count == 0:
            continue
        drafted = counts.get(pos, 0)
        if drafted != slot_count:
            violations.append(f"{pos}: drafted {drafted}, roster needs exactly {slot_count}")
        for pick in picks:
            if pick.player.position == pos and pick.round_no <= rounds - 5:
                violations.append(
                    f"{pos} taken in round {pick.round_no} ({pick.player.name}) -- "
                    "streamer positions belong in the endgame"
                )

    if counts.get("QB", 0) > 2:
        violations.append(f"QB: drafted {counts['QB']} -- a third quarterback is a dead slot")
    if counts.get("TE", 0) > 2:
        violations.append(f"TE: drafted {counts['TE']} -- a third tight end is a dead slot")

    for pos, cap in settings.position_limits.items():
        if counts.get(pos, 0) > cap:
            violations.append(f"{pos}: drafted {counts[pos]}, league cap is {cap}")

    for pos, slot_count in settings.roster_slots.items():
        if pos in ("BN", "IR", "TAXI") or pos not in ("QB", "RB", "WR", "TE", "K", "DEF"):
            continue
        if counts.get(pos, 0) < slot_count:
            violations.append(
                f"{pos}: drafted {counts.get(pos, 0)}, cannot fill {slot_count} starter slot(s)"
            )

    total_mine = len(picks)
    skill_depth = counts.get("RB", 0) + counts.get("WR", 0)
    # Whatever else happens, the bench must be built from the positions you
    # actually flex and churn. 16 rounds minus QB2/TE2/K/DEF leaves 10.
    floor = total_mine - 2 - 2 - settings.roster_slots.get("K", 0) - settings.roster_slots.get("DEF", 0)
    if skill_depth < floor:
        violations.append(
            f"RB+WR depth {skill_depth} below floor {floor} -- bench crowded out by onesie positions"
        )

    return violations


def _print_result(result: DraftResult, settings: LeagueSettings, rounds: int) -> None:
    print(f"\nMy picks (slot {result.my_slot}):")
    for pick in result.my_picks:
        print(
            f"  R{pick.round_no:>2} P{pick.pick_no:>3}  {pick.player.position:3s} "
            f"{pick.player.name:24s} proj {pick.player.points:6.1f}  "
            f"adp {pick.player.adp if pick.player.adp is not None else '-'}"
        )
        if pick.reason:
            print(f"          {pick.reason[:96]}")
    print(f"\nRoster shape: {dict(result.my_position_counts)}")
    violations = check_invariants(result, settings, rounds)
    if violations:
        print("\nINVARIANT VIOLATIONS:")
        for violation in violations:
            print(f"  FAIL {violation}")
    else:
        print("\nAll roster invariants pass.")


if __name__ == "__main__":
    import argparse

    from api.db import get_sessionmaker
    from api.services import build_pool

    parser = argparse.ArgumentParser(description="Rehearse a full draft")
    parser.add_argument("--slot", type=int, default=4)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--rounds", type=int, default=16)
    parser.add_argument("--teams", type=int, default=12)
    parser.add_argument("--platform", choices=("espn", "sleeper"), default="espn")
    args = parser.parse_args()

    league = LeagueSettings(
        scoring={"rec": 1.0, "pass_td": 4.0, "rush_td": 6.0, "rec_td": 6.0,
                 "pass_yd": 0.04, "rush_yd": 0.1, "rec_yd": 0.1},
        roster_slots={"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "K": 1, "DEF": 1,
                      "BN": args.rounds - 9},
        position_limits={"QB": 4, "RB": 8, "WR": 8, "TE": 3, "K": 3, "DEF": 3},
        total_teams=args.teams,
    )
    with get_sessionmaker()() as session:
        full_pool = build_pool(session, league, platform=args.platform)
    outcome = simulate_draft(
        full_pool, league, my_slot=args.slot, rounds=args.rounds, seed=args.seed
    )
    _print_result(outcome, league, args.rounds)
