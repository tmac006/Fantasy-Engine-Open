"""Risk and reward, as two numbers a meter can draw.

Both are fitted to the 2025 season rather than invented, because a bar that does
not track outcomes is decoration that looks like analysis.

**Risk** is a calibrated probability that the player lays an egg -- a week under
half his own scoring rate. Fitted by least squares on 3,484 point-in-time
player-weeks:

    P(bust) = 0.125 + 0.263*(1 - snap share) + 0.132*volatility

R^2 is only 0.11, which is the honest ceiling for predicting one NFL week, but
the *calibration* is what a meter needs and it is close to exact:

    predicted   20%   30%   40%   50%   60%
    actual      22%   29%   38%   46%   67%

So the number under the bar is a real probability and is shown as one.

**Reward** is the ceiling -- what a good week looks like -- normalised against
what the position offers, since 12 points is an elite week for a tight end and a
flex start for a running back. Fitted on the same sample:

    ceiling = 2.86 + 1.09 * scoring rate

Volatility was in that fit too and came out at -0.0155, which is nothing. That is
the third independent time this data has said the same thing: **past volatility
does not buy upside.** Within equal-quality bands, volatile players had lower
ceilings, lower floors and lower means than steady ones.

Which brings up the honest caveat about the whole pairing. Risk and reward here
correlate at -0.78. There is no meaningful population of high-risk/high-reward
players: sorted into quadrants, "high reward / high risk" held 8% of players and
their average ceiling (11.6) was *below* the low-risk group's (17.9). Good
players are both safer and better. The two meters answer two real questions --
how good can this be, and how often does it flop -- but they are not a tradeoff
dial, and the UI should not imply you are buying upside with risk.

Pure functions over `usage.PlayerForm`. No database.
"""

from __future__ import annotations

from dataclasses import dataclass

from api.engine.usage import PlayerForm

# P(bust) coefficients. See module docstring for the fit and its calibration.
BUST_INTERCEPT = 0.1250
BUST_PER_MISSING_SNAP = 0.2629
BUST_PER_VOLATILITY = 0.1318

# The fit is only meaningful inside the range it was fitted over.
MIN_BUST_PROBABILITY = 0.05
MAX_BUST_PROBABILITY = 0.85
MAX_VOLATILITY = 2.0

# Snap share assumed when a player has no snap data (roughly the sample median).
ASSUMED_SNAP_PCT = 0.55

# ceiling = intercept + slope * scoring rate.
CEILING_INTERCEPT = 2.8578
CEILING_PER_POINT = 1.0937

# Where the risk bar reads empty and full. Anchored to the range the fit can
# actually produce -- the intercept is the best case a perfect role gives you --
# so the bar uses its whole length instead of clipping every workhorse to zero.
RISK_FLOOR = 0.12
RISK_CAP = 0.60

# Ceiling that reads as empty / full, per position: roughly 40% of the positional
# median, and the best ceiling the position produced in 2025. Anchoring the top
# to the 90th percentile instead pegged every startable player at 100, which is
# no more useful than no meter at all.
CEILING_ANCHORS: dict[str, tuple[float, float]] = {
    "QB": (8.2, 30.0),
    "RB": (4.2, 33.0),
    "WR": (4.0, 33.0),
    "TE": (3.3, 23.0),
}
DEFAULT_ANCHORS = (4.0, 30.0)

# A player needs this many games before either number means anything.
MIN_GAMES = 2


@dataclass(frozen=True)
class Outlook:
    """Two 0-100 meters plus the real quantities behind them."""

    risk: int
    reward: int
    bust_probability: float
    projected_ceiling: float
    label: str
    drivers: list[str]


def _scale(value: float, low: float, high: float) -> int:
    if high <= low:
        return 0
    return round(100.0 * max(0.0, min(1.0, (value - low) / (high - low))))


def bust_probability(form: PlayerForm) -> float:
    """Calibrated chance of a week under half his own scoring rate."""
    rate = form.to_date.ppr_per_game
    stdev = form.to_date.ppr_stdev or 0.0
    volatility = min(MAX_VOLATILITY, stdev / rate) if rate else 0.0
    snap = form.recent.snap_pct
    if snap is None:
        snap = ASSUMED_SNAP_PCT
    raw = (
        BUST_INTERCEPT
        + BUST_PER_MISSING_SNAP * (1.0 - snap)
        + BUST_PER_VOLATILITY * volatility
    )
    return max(MIN_BUST_PROBABILITY, min(MAX_BUST_PROBABILITY, raw))


def projected_ceiling(form: PlayerForm) -> float | None:
    """What a good week looks like, in PPR points."""
    rate = form.to_date.ppr_per_game
    if rate is None:
        return None
    return max(0.0, CEILING_INTERCEPT + CEILING_PER_POINT * rate)


def _label(risk: int, reward: int) -> str:
    """Plain words for the quadrant, without implying risk buys upside."""
    high_reward, high_risk = reward >= 50, risk >= 50
    if high_reward and not high_risk:
        return "safe starter"
    if high_reward and high_risk:
        return "high ceiling, shaky role"
    if not high_reward and not high_risk:
        return "steady but capped"
    return "dart throw"


def _drivers(form: PlayerForm, probability: float) -> list[str]:
    """What is actually moving the risk number."""
    out: list[str] = [f"{probability:.0%} chance of a dud week"]
    snap = form.recent.snap_pct
    if snap is not None:
        if snap < 0.35:
            out.append(f"only {snap:.0%} of snaps -- the biggest single risk factor")
        elif snap >= 0.75:
            out.append(f"{snap:.0%} of snaps; the role is secure")
    rate = form.to_date.ppr_per_game
    stdev = form.to_date.ppr_stdev
    if rate and stdev is not None:
        volatility = stdev / rate
        if volatility >= 0.8:
            out.append("week to week his scoring swings hard")
        elif volatility < 0.4:
            out.append("scores in a narrow band most weeks")
    return out


def outlook(form: PlayerForm, position: str) -> Outlook | None:
    """Risk and reward meters, or None when he has not played enough to judge."""
    if form.to_date.games < MIN_GAMES:
        return None
    ceiling = projected_ceiling(form)
    if ceiling is None:
        return None

    probability = bust_probability(form)
    low, high = CEILING_ANCHORS.get(position.upper(), DEFAULT_ANCHORS)
    risk = _scale(probability, RISK_FLOOR, RISK_CAP)
    reward = _scale(ceiling, low, high)

    return Outlook(
        risk=risk,
        reward=reward,
        bust_probability=round(probability, 3),
        projected_ceiling=round(ceiling, 1),
        label=_label(risk, reward),
        drivers=_drivers(form, probability),
    )
