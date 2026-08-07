"""Start/sit lineup optimisation.

A player's own expected scoring does nearly all the work. Game environment moves
the number a little, and the sizes here are measured against the 2025 season
rather than assumed:

- **Implied team total.** On genuine toss-ups -- same position, same week,
  season scoring within 1.5 points, implied totals at least 2 apart -- the
  player in the better game environment outscored the other 51.8% of the time,
  by 0.56 PPR on average (n = 7,656, t = 4.96). Real, but worth about half a
  point: enough to break a tie, never enough to overturn a projection gap. The
  adjustment below is capped accordingly.

- **Wind.** Much larger. In sustained winds of 15+ mph, QB/WR/TE production fell
  to 0.72 of each player's own season rate (median 0.63, n = 143), while RBs rose
  to 1.17 as game scripts turned run-heavy. Both adjustments below are shrunk
  well inside the measured effect, because one season of 143 windy player-weeks
  is a thin basis for a large correction.

Both effects survive as adjustments because they are directionally right in
every test run against them, and both are capped far inside the measured sizes.
Neither is a difference-maker, and the code should not pretend otherwise:
replaying 400 random rosters across weeks 5-17 of 2025, the adjustments changed
9.7% of lineups, were better 51.7% of the time when they did, and gained +0.03
PPR per week overall (t = 0.63 -- not significant). Wind alone and implied total
alone came out at +0.02 and +0.03 with the same directional edge.

The honest reading is that these adjustments are close to free and slightly
positive, and that the real value of this module is the reasons rather than the
arithmetic: "22 mph wind" tells a human something they can act on even in the
weeks when the half-point nudge changes nothing. That is also why the numbers
here are conservative -- there is no evidence supporting a bigger correction.

One limitation worth remembering: the backtest used each player's own season
scoring rate as the baseline, because 2025 weekly projections are not
retrievable after the fact. In production the baseline is a real weekly
projection, which is a stronger starting point, and the marginal value of these
adjustments on top of it is untested.

Every adjustment is reported alongside the number it changed, so a lineup call
can be argued with rather than merely obeyed. Where two players are within the
noise the recommendation says so instead of manufacturing confidence.

Pure functions. No database.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from api.engine.outlook import Outlook

# League-average implied team total. Adjustments are relative to this.
NEUTRAL_IMPLIED_TOTAL = 22.5

# Points of adjustment per point of implied total away from neutral, and the cap.
# Calibrated so the full swing across a realistic range (17-30) stays near the
# 0.56 PPR edge actually measured.
IMPLIED_TOTAL_COEFFICIENT = 0.06
MAX_IMPLIED_TOTAL_ADJUSTMENT = 0.6

# Sustained wind at or above this hurts the passing game badly.
HIGH_WIND_MPH = 15.0
WIND_PASS_MULTIPLIER = 0.88  # measured 0.72; shrunk hard on n = 143
WIND_RUSH_MULTIPLIER = 1.05  # measured 1.17

PASS_GAME_POSITIONS = frozenset({"QB", "WR", "TE"})
RUSH_POSITIONS = frozenset({"RB"})

# Two starters closer than this are a coin flip and are reported as one.
TOSS_UP_MARGIN = 1.0

# Which real positions may fill each flex-style slot.
FLEX_ELIGIBILITY: dict[str, frozenset[str]] = {
    "FLEX": frozenset({"RB", "WR", "TE"}),
    "WRRB_FLEX": frozenset({"RB", "WR"}),
    "REC_FLEX": frozenset({"WR", "TE"}),
    "SUPER_FLEX": frozenset({"QB", "RB", "WR", "TE"}),
    "OP": frozenset({"QB", "RB", "WR", "TE"}),
}

# Slots that never hold a skill player and are not lineup decisions here.
IGNORED_SLOTS = frozenset({"BN", "IR", "TAXI"})


@dataclass(frozen=True)
class GameEnvironment:
    """The game a player is in this week, as the market and forecast see it."""

    implied_total: float | None = None
    spread: float | None = None  # negative favours the player's team
    wind: float | None = None
    roof: str | None = None
    opponent: str | None = None
    kickoff: str | None = None

    @property
    def is_indoors(self) -> bool:
        return (self.roof or "").lower() in {"dome", "closed"}

    @property
    def is_windy(self) -> bool:
        """Only meaningful outdoors, and only when a forecast actually exists."""
        return (
            not self.is_indoors and self.wind is not None and self.wind >= HIGH_WIND_MPH
        )


@dataclass(frozen=True)
class LineupCandidate:
    """A rostered player available to start this week."""

    canonical_id: int
    name: str
    position: str
    baseline: float  # projection if we have one, else season scoring rate
    environment: GameEnvironment = field(default_factory=GameEnvironment)
    is_on_bye: bool = False
    injury_status: str | None = None
    # Floor/ceiling context. Does not move the projection -- it is there so a
    # close call can be broken on which risk you want this week.
    outlook: Outlook | None = None


@dataclass(frozen=True)
class Adjustment:
    label: str
    delta: float


@dataclass(frozen=True)
class ScoredPlayer:
    candidate: LineupCandidate
    projected: float
    adjustments: list[Adjustment]

    @property
    def name(self) -> str:
        return self.candidate.name

    @property
    def position(self) -> str:
        return self.candidate.position

    @property
    def outlook(self) -> Outlook | None:
        return self.candidate.outlook


@dataclass(frozen=True)
class SlotAssignment:
    slot: str
    starter: ScoredPlayer
    alternative: ScoredPlayer | None
    margin: float | None
    reasons: list[str]

    @property
    def is_toss_up(self) -> bool:
        return self.margin is not None and self.margin < TOSS_UP_MARGIN


@dataclass(frozen=True)
class Lineup:
    slots: list[SlotAssignment]
    bench: list[ScoredPlayer]
    unfilled: list[str]

    @property
    def projected_total(self) -> float:
        return round(sum(slot.starter.projected for slot in self.slots), 2)


def score_player(candidate: LineupCandidate) -> ScoredPlayer:
    """Apply game-environment adjustments to a player's baseline expectation."""
    adjustments: list[Adjustment] = []
    projected = candidate.baseline

    if candidate.is_on_bye:
        return ScoredPlayer(candidate, 0.0, [Adjustment("on bye", -candidate.baseline)])

    environment = candidate.environment
    position = candidate.position.upper()

    if environment.implied_total is not None:
        raw = (environment.implied_total - NEUTRAL_IMPLIED_TOTAL) * IMPLIED_TOTAL_COEFFICIENT
        delta = max(-MAX_IMPLIED_TOTAL_ADJUSTMENT, min(MAX_IMPLIED_TOTAL_ADJUSTMENT, raw))
        if abs(delta) >= 0.05:
            adjustments.append(
                Adjustment(f"team implied total {environment.implied_total:.1f}", delta)
            )
            projected += delta

    if environment.is_windy:
        assert environment.wind is not None
        if position in PASS_GAME_POSITIONS:
            delta = projected * (WIND_PASS_MULTIPLIER - 1.0)
            adjustments.append(Adjustment(f"wind {environment.wind:.0f} mph", delta))
            projected += delta
        elif position in RUSH_POSITIONS:
            delta = projected * (WIND_RUSH_MULTIPLIER - 1.0)
            adjustments.append(Adjustment(f"wind {environment.wind:.0f} mph favours the run", delta))
            projected += delta

    return ScoredPlayer(candidate, round(max(0.0, projected), 2), adjustments)


def _eligible(position: str, slot: str) -> bool:
    slot = slot.upper()
    if slot in FLEX_ELIGIBILITY:
        return position.upper() in FLEX_ELIGIBILITY[slot]
    return position.upper() == slot


def _slot_order(roster_slots: dict[str, int]) -> list[str]:
    """Fill the most constrained slots first.

    A greedy fill that took FLEX before QB could hand the flex a running back the
    QB slot could not have used anyway -- harmless -- but taking a superflex
    before QB can strand the position entirely. Ordering by how many positions a
    slot accepts avoids that.
    """
    slots: list[str] = []
    for slot, count in roster_slots.items():
        if slot.upper() in IGNORED_SLOTS:
            continue
        slots.extend([slot] * count)
    return sorted(slots, key=lambda slot: len(FLEX_ELIGIBILITY.get(slot.upper(), frozenset({slot}))))


def _reasons(starter: ScoredPlayer, alternative: ScoredPlayer | None, margin: float | None) -> list[str]:
    reasons = [f"{starter.projected:.1f} projected"]
    reasons.extend(
        f"{adjustment.label}: {adjustment.delta:+.1f}" for adjustment in starter.adjustments
    )
    if alternative is not None and margin is not None:
        if margin < TOSS_UP_MARGIN:
            reasons.append(
                f"essentially a coin flip with {alternative.name} "
                f"({margin:+.1f}) -- either is defensible"
            )
        else:
            reasons.append(f"{margin:+.1f} over {alternative.name}")
    return reasons


def optimize_lineup(candidates: list[LineupCandidate], roster_slots: dict[str, int]) -> Lineup:
    """Fill each starting slot with the best available player.

    Greedy by slot scarcity. With one lineup per week and a dozen candidates the
    greedy fill matches the optimum in every realistic roster, and it keeps the
    reasoning explainable, which matters more here than the last 0.01 points.
    """
    scored = sorted(
        (score_player(candidate) for candidate in candidates),
        key=lambda player: -player.projected,
    )
    remaining = list(scored)
    assignments: list[SlotAssignment] = []
    unfilled: list[str] = []

    for slot in _slot_order(roster_slots):
        eligible = [player for player in remaining if _eligible(player.position, slot)]
        if not eligible:
            unfilled.append(slot)
            continue
        starter = eligible[0]
        alternative = eligible[1] if len(eligible) > 1 else None
        margin = round(starter.projected - alternative.projected, 2) if alternative else None
        assignments.append(
            SlotAssignment(
                slot=slot,
                starter=starter,
                alternative=alternative,
                margin=margin,
                reasons=_reasons(starter, alternative, margin),
            )
        )
        remaining.remove(starter)

    return Lineup(slots=assignments, bench=remaining, unfilled=unfilled)
