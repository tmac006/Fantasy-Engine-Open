"""Filling a legal starting lineup from a set of rostered players.

Shared deliberately: the draft rehearsal grades finished rosters with it, and
the trade engine values every player by how much he changes it. Two copies
would let those two answers drift, and a trade is only worth making if it moves
the same number the rehearsal grades.
"""

from api.engine.vor import FLEX_ELIGIBILITY, PoolPlayer

# Slots that never score: bench and the injury/taxi shelves.
NON_SCORING_SLOTS = frozenset({"BN", "IR", "TAXI"})


def fill_lineup(
    players: list[PoolPlayer], roster_slots: dict[str, int]
) -> tuple[list[PoolPlayer], list[PoolPlayer]]:
    """The best legal starting lineup, plus everyone left over.

    Direct slots claim their own position's best first, then flex slots take the
    best of whatever remains eligible. Greedy is exact here because every flex
    slot's eligibility is a superset of the direct slots it competes with.
    """
    remaining = sorted(players, key=lambda p: p.points, reverse=True)
    starters: list[PoolPlayer] = []

    def claim(eligible: tuple[str, ...], count: int) -> None:
        for _ in range(count):
            pick = next((p for p in remaining if p.position in eligible), None)
            if pick is None:
                return
            starters.append(pick)
            remaining.remove(pick)

    for slot, count in roster_slots.items():
        if slot in NON_SCORING_SLOTS or slot in FLEX_ELIGIBILITY:
            continue
        claim((slot,), count)
    for slot, count in roster_slots.items():
        if slot in FLEX_ELIGIBILITY:
            claim(tuple(FLEX_ELIGIBILITY[slot]), count)
    return starters, remaining


def lineup_points(players: list[PoolPlayer], roster_slots: dict[str, int]) -> float:
    """Projected points of the best legal lineup these players can field."""
    starters, _ = fill_lineup(players, roster_slots)
    return sum(p.points for p in starters)
