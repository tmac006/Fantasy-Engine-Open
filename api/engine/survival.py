"""Draft-market survival probability.

The market is noisy: a player with ADP 45 is not gone at pick 45, he is gone
somewhere around it. Modeling that spread turns "will he last?" from a guess
into a probability, which is what the value-vs-market comparison needs.

The spread model is fitted from FantasyFootballCalculator's published ADP
standard deviations (n=246, 12-team PPR): stdev ~= 0.107 * adp, which tracks
the observed medians closely from the first round (1.3) through pick 200
(16.7). Sources that publish ADP without a stdev get the fitted estimate.
"""

from math import erf, sqrt

# Fitted slope of stdev vs ADP; see module docstring.
ADP_STDEV_RATIO = 0.107
# Even the consensus 1.01 has some spread; keeps early-round math sane.
MIN_ADP_STDEV = 1.0


def estimate_stdev(adp: float, published: float | None = None) -> float:
    """Published stdev when available, else the fitted estimate from ADP."""
    if published is not None and published > 0:
        return max(published, MIN_ADP_STDEV)
    return max(adp * ADP_STDEV_RATIO, MIN_ADP_STDEV)


def survival_probability(adp: float | None, stdev: float | None, pick: int) -> float:
    """P(player is still on the board when `pick` comes around).

    Treats actual draft position as normal(adp, stdev); returns the upper tail
    at `pick`. Players with no ADP at all are assumed available (1.0) — better
    to under-warn than to invent scarcity from missing data.
    """
    if adp is None:
        return 1.0
    sigma = estimate_stdev(adp, stdev)
    z = (pick - adp) / sigma
    return 0.5 * (1.0 - erf(z / sqrt(2.0)))
