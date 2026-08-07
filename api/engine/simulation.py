"""Deterministic live-room simulation through the user's next turn."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from math import exp
from random import Random

from pydantic import BaseModel

from api.engine.params import EngineParams
from api.engine.survival import estimate_stdev
from api.engine.vor import PoolPlayer


@dataclass(frozen=True)
class TeamDraftContext:
    """Known roster state for a team that may pick before the user."""

    team_id: str
    roster_positions: tuple[str, ...] = ()


@dataclass(frozen=True)
class DraftPickContext:
    """One observed room pick, retaining its owner and board position."""

    pick_number: int
    team_id: str
    position: str
    # Positive means the player was selected this many picks earlier than ADP.
    adp_delta: float | None = None


class NextTurnSimulation(BaseModel):
    trials: int
    seed: int
    picks_simulated: int
    disappearance_probability: dict[str, float]
    expected_fallback_value: dict[str, float]
    expected_fallback_player: dict[str, str | None]


def _market_weight(
    player: PoolPlayer, pick_number: int, reach: float, params: EngineParams
) -> float:
    """Relative chance this opponent selects a player from the ADP distribution.

    The decay scale is calibrated, not arbitrary: too wide and the simulated
    room drafts nearly at random, which understates how quickly on-the-clock
    names actually leave the board.
    """
    if player.adp is None:
        return 0.02
    sigma = estimate_stdev(player.adp, player.adp_stdev)
    adjusted_adp = player.adp - reach
    picks_away = adjusted_adp - pick_number
    if picks_away <= 0:
        # Overdue players remain highly selectable instead of falling out of the
        # normal PDF once the room has passed their consensus draft position.
        return 1.0 + min(2.0, -picks_away / max(sigma, 1.0) * 0.15)
    scale = max(params.market_decay_sigma_mult * sigma, params.market_decay_min_picks)
    return max(0.002, exp(-picks_away / scale))


def _roster_weight(
    position: str,
    roster: Counter[str],
    slots: dict[str, int],
    params: EngineParams,
) -> float:
    if position in params.late_fill_positions:
        total_filled = sum(roster.values())
        # Gate against STARTER slots, not the full roster: counting a 7-slot
        # bench in the denominator meant simulated opponents took K/DEF only in
        # their last two picks, while real rooms start in rounds 11-15. That
        # false calm reported "100% to last" for every kicker and made K/DEF
        # runs invisible to the room model.
        starter_slots = sum(
            count for slot, count in slots.items() if slot not in ("BN", "IR", "TAXI")
        )
        if starter_slots and total_filled < starter_slots + params.late_fill_buffer:
            return 0.03

    direct = slots.get(position, 0)
    if direct and roster[position] < direct:
        # TE demand is real but weaker than RB/WR/QB demand simply because its
        # starter is open; elite value must come from its actual tier cliff.
        return 1.12 if position == "TE" else 1.45
    return 0.72


def _room_weight(position: str, recent_positions: tuple[str, ...], params: EngineParams) -> float:
    if not recent_positions:
        return 1.0
    window = recent_positions[-params.run_window :]
    share = Counter(window)[position] / len(window)
    return 1.0 + params.simulation_run_weight * share


def _weighted_choice(
    rng: Random,
    players: list[PoolPlayer],
    weights: list[float],
) -> PoolPlayer:
    total = sum(weights)
    if total <= 0:
        return min(players, key=lambda p: (p.adp is None, p.adp or float("inf"), p.canonical_id))
    needle = rng.random() * total
    cumulative = 0.0
    for player, weight in zip(players, weights, strict=True):
        cumulative += weight
        if needle <= cumulative:
            return player
    return players[-1]


def simulate_next_turn(
    available: list[PoolPlayer],
    values: dict[str, float],
    *,
    current_pick: int,
    next_pick: int | None,
    roster_slots: dict[str, int],
    params: EngineParams,
    teams: tuple[TeamDraftContext, ...] = (),
    upcoming_team_ids: tuple[str, ...] = (),
    recent_picks: tuple[DraftPickContext, ...] = (),
    recent_positions: tuple[str, ...] = (),
    current_pick_is_user: bool = True,
    seed: int | None = None,
    trials: int | None = None,
    excluded_ids: frozenset[str] = frozenset(),
) -> NextTurnSimulation:
    """Simulate opponent selections until ``next_pick`` with a stable seed.

    The same inputs always produce the same output.  ADP supplies the market
    distribution, while known team rosters and observed room picks alter each
    opponent's conditional selection probabilities.
    """
    simulation_seed = params.simulation_seed if seed is None else seed
    simulation_trials = params.simulation_trials if trials is None else trials
    first_simulated_pick = current_pick + 1 if current_pick_is_user else current_pick
    picks_to_simulate = max(0, (next_pick or first_simulated_pick) - first_simulated_pick)
    all_candidates = [p for p in available if p.canonical_id not in excluded_ids]
    # Real draft pools can contain thousands of fringe players. Opponents are
    # realistically choosing from the market's near-term names or our highest
    # values, so bound each rollout to both groups. This keeps live responses
    # comfortably inside the extension's request deadline.
    half_limit = max(1, params.simulation_pool_limit // 2)
    market_candidates = sorted(
        all_candidates,
        key=lambda p: (p.adp is None, p.adp or float("inf"), p.canonical_id),
    )[:half_limit]
    value_candidates = sorted(
        all_candidates,
        key=lambda p: (-values.get(p.canonical_id, 0.0), p.canonical_id),
    )[:half_limit]
    candidates_by_id = {
        player.canonical_id: player
        for player in [*market_candidates, *value_candidates]
    }
    candidates = list(candidates_by_id.values())[: params.simulation_pool_limit]
    positions = sorted({p.position for p in candidates})
    recent = (
        tuple(p.position for p in recent_picks[-params.run_window :])
        if recent_picks
        else recent_positions
    )
    observed_reaches: dict[str, list[float]] = {}
    for pick in recent_picks:
        if pick.adp_delta is not None:
            observed_reaches.setdefault(pick.team_id, []).append(max(0.0, pick.adp_delta))
    all_reaches = [reach for reaches in observed_reaches.values() for reach in reaches]
    room_reach = sum(all_reaches) / len(all_reaches) if all_reaches else 0.0
    teams_by_id = {team.team_id: team for team in teams}
    disappeared: Counter[str] = Counter()
    fallback_totals: dict[str, float] = {position: 0.0 for position in positions}
    fallback_ids: dict[str, Counter[str]] = {position: Counter() for position in positions}

    if not candidates or simulation_trials <= 0:
        return NextTurnSimulation(
            trials=max(0, simulation_trials),
            seed=simulation_seed,
            picks_simulated=picks_to_simulate,
            disappearance_probability={p.canonical_id: 0.0 for p in candidates},
            expected_fallback_value={position: 0.0 for position in positions},
            expected_fallback_player={position: None for position in positions},
        )

    rng = Random(simulation_seed)
    for _ in range(simulation_trials):
        remaining = list(candidates)
        rosters = {
            team.team_id: Counter(team.roster_positions)
            for team in teams
        }
        selected: set[str] = set()
        for offset in range(picks_to_simulate):
            if not remaining:
                break
            team_id = (
                upcoming_team_ids[offset]
                if offset < len(upcoming_team_ids)
                else f"unknown-{offset}"
            )
            team = teams_by_id.get(team_id)
            roster = rosters.setdefault(team_id, Counter(team.roster_positions if team else ()))
            pick_number = first_simulated_pick + offset
            team_reaches = observed_reaches.get(team_id, ())
            observed_reach = (
                sum(team_reaches) / len(team_reaches)
                if team_reaches
                else room_reach
            )
            reach = observed_reach
            weights = [
                _market_weight(player, pick_number, reach, params)
                * _roster_weight(player.position, roster, roster_slots, params)
                * _room_weight(player.position, recent, params)
                for player in remaining
            ]
            chosen = _weighted_choice(rng, remaining, weights)
            remaining.remove(chosen)
            selected.add(chosen.canonical_id)
            roster[chosen.position] += 1

        disappeared.update(selected)
        for position in positions:
            fallback = max(
                (p for p in remaining if p.position == position),
                key=lambda p: (values.get(p.canonical_id, 0.0), p.canonical_id),
                default=None,
            )
            if fallback is not None:
                fallback_totals[position] += values.get(fallback.canonical_id, 0.0)
                fallback_ids[position][fallback.canonical_id] += 1

    denominator = float(simulation_trials)
    return NextTurnSimulation(
        trials=simulation_trials,
        seed=simulation_seed,
        picks_simulated=picks_to_simulate,
        disappearance_probability={
            p.canonical_id: round(disappeared[p.canonical_id] / denominator, 4)
            for p in candidates
        },
        expected_fallback_value={
            position: round(fallback_totals[position] / denominator, 3)
            for position in positions
        },
        expected_fallback_player={
            position: (
                fallback_ids[position].most_common(1)[0][0]
                if fallback_ids[position]
                else None
            )
            for position in positions
        },
    )
