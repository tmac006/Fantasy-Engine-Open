"""Gap-based tier detection (spec §7.1): breaks come from the value curve,
not fixed bucket sizes. Within a tier, players are interchangeable."""

import statistics

from api.engine.params import EngineParams


def assign_tiers(points_desc: list[float], params: EngineParams) -> list[int]:
    """Tier number (1-based) for each entry of a descending points list.

    A new tier starts where the drop to the next player exceeds
    max(tier_gap_abs, tier_gap_rel x median positive gap in this position).
    """
    if not points_desc:
        return []
    gaps = [points_desc[i] - points_desc[i + 1] for i in range(len(points_desc) - 1)]
    positive = [g for g in gaps if g > 0]
    median_gap = statistics.median(positive) if positive else 0.0
    threshold = max(params.tier_gap_abs, params.tier_gap_rel * median_gap)

    tiers = [1]
    for gap in gaps:
        tiers.append(tiers[-1] + 1 if gap >= threshold else tiers[-1])
    return tiers
