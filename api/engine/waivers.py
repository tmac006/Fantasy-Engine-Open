"""Waiver target scoring.

The design here is backtested, not assumed, and the backtest overturned the
assumption it started from. Replaying the 2025 regular season week by week
(weeks 5-15, top ten adds, graded on the next three weeks) gave:

    season points/game      7.91 PPR/gm    36% returned a startable week
    weighted season+recent  8.04 PPR/gm    34%
    expected points (xFP)   7.41 PPR/gm    27%
    chase last week's pts   6.15 PPR/gm    21%
    random available        3.11 PPR/gm     8%

Two results shaped this module:

1. **Scoring opportunity instead of production made things worse.** The usual
   advice is that opportunity is repeatable and production is not, so xFP should
   beat raw points. It does not, and the reason is that stripping out efficiency
   throws away real skill along with the touchdown luck -- good players genuinely
   do more per target. xFP is kept below, but for what it is actually good at:
   flagging production that outruns its usage.

2. **Chasing breakouts is negative value.** Ranking strategies by how many
   "emerging" players they picked lines up almost perfectly against how well
   they did, and the emergence-first strategy loses across the entire outcome
   distribution -- mean, median, p90 and best case alike (4% of its picks
   returned a 15-point week, against 14% for season points). There is no
   upside-tail argument for it.

So the score is season-to-date points per game, gated on the role still
existing. Role change, regression risk and vacated opportunity are all reported
as context a human can weigh, and none of them move the number.

The weighted blend edged out plain season points by 0.13 PPR/gm across 11 weeks
(t = 0.29, ahead in 4 of 11), which is noise, so the simpler rule wins the tie.

Pure functions over `usage.PlayerForm`. No database.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from api.engine.outlook import Outlook, outlook
from api.engine.usage import PlayerForm, UsageWindow

# PPR points per opportunity, fitted by least squares on 4,621 player-weeks from
# the 2025 regular season (R^2 = 0.61). Used for the expected-points comparison,
# not for ranking -- see the module docstring.
POINTS_PER_TARGET = {"RB": 1.3073, "WR": 1.7073, "TE": 1.8232}
DEFAULT_POINTS_PER_TARGET = 1.7108

# Fitted on RB weeks only. WR and TE carries are jet sweeps and gadget plays --
# rare, high variance, and their fitted coefficients are noise, so every position
# is valued at the rate where the signal actually lives.
POINTS_PER_CARRY = 0.7049

# Quarterbacks need their own model: their volume is pass attempts, and scoring a
# QB on targets and carries measures only his scrambles. Fitted on 628 QB weeks
# from 2025 (R^2 = 0.43 -- lower than the skill positions, because QB scoring
# depends less on raw volume, which is worth remembering before leaning on it).
POINTS_PER_ATTEMPT = 0.3815
POINTS_PER_QB_CARRY = 1.0538

# A player whose snap share has fallen this low is not playing enough for his
# earlier scoring to mean anything, whatever the season average says. This is a
# safety gate rather than a scoring input: it cost nothing measurable in the
# backtest and it stops the obvious embarrassment of recommending a player who
# has been benched.
MIN_SNAP_PCT_FOR_ROLE = 0.35
BENCHED_PENALTY = 0.5

# Points per game above expectation beyond which recent scoring is called out as
# unlikely to hold, and the share of expected points that gap has to represent.
#
# Both bars have to be cleared. An absolute threshold alone does not transfer
# across positions: a starting quarterback's expected points are large enough
# that four points of noise is nothing, and flagging one flagged two thirds of
# them, which is the same as flagging none. Requiring the gap to also be a real
# fraction of the expectation keeps the warning meaningful at every scale.
REGRESSION_FLAG_FPOE = 4.0
REGRESSION_FLAG_SHARE = 0.40

MIN_GAMES_FOR_TREND = 2


def points_per_target(position: str) -> float:
    return POINTS_PER_TARGET.get(position.upper(), DEFAULT_POINTS_PER_TARGET)


def expected_points(window: UsageWindow, position: str) -> float | None:
    """xFP per game: what this usage is worth to an average player at the position."""
    if window.is_empty:
        return None

    carries = window.carries_per_game or 0.0
    if position.upper() == "QB":
        attempts = window.attempts_per_game or 0.0
        if attempts == 0.0 and carries == 0.0:
            return None
        return attempts * POINTS_PER_ATTEMPT + carries * POINTS_PER_QB_CARRY

    targets = window.targets_per_game or 0.0
    if targets == 0.0 and carries == 0.0:
        return None
    return targets * points_per_target(position) + carries * POINTS_PER_CARRY


def points_over_expected(window: UsageWindow, position: str) -> float | None:
    """Actual minus expected, per game.

    Positive means he has scored above what his usage supports, which tends not
    to last. Negative on real volume is the buy-low case. This is the one job
    xFP does well, and it is reported rather than scored.
    """
    expected = expected_points(window, position)
    if expected is None or window.ppr_per_game is None:
        return None
    return window.ppr_per_game - expected


@dataclass(frozen=True)
class WaiverTarget:
    """A scored candidate. `score` is expected PPR per game if added."""

    canonical_id: int
    name: str
    position: str
    team: str | None
    score: float
    season_ppg: float | None
    recent_ppg: float | None
    expected_points_recent: float | None
    points_over_expected: float | None
    snap_delta: float | None
    opportunity_delta: float | None
    is_emerging: bool
    role_intact: bool
    outlook: Outlook | None
    reasons: list[str] = field(default_factory=list)
    context: list[str] = field(default_factory=list)
    # Advanced-stat read, passed straight through from the candidate. Reported
    # beside the score, never inside it: these metrics failed the predictive
    # gate (see api.eval.advanced_fit), so they inform you, not the ranking.
    advanced: list[str] = field(default_factory=list)
    advanced_summary: str | None = None


@dataclass(frozen=True)
class Candidate:
    """Input row: an available player plus his point-in-time form."""

    canonical_id: int
    name: str
    position: str
    team: str | None
    form: PlayerForm
    context_notes: tuple[str, ...] = ()
    advanced: tuple[str, ...] = ()
    advanced_summary: str | None = None


def _reasons(candidate: Candidate, season_ppg: float, role_intact: bool) -> list[str]:
    """Why he is ranked here, in terms checkable against a box score."""
    recent = candidate.form.recent
    out = [f"{season_ppg:.1f} PPR/gm across {candidate.form.to_date.games} games"]

    # Report the volume that actually defines the position: a quarterback's card
    # listing only his scrambles says nothing about his week.
    if candidate.position.upper() == "QB" and recent.attempts_per_game:
        out.append(f"{recent.attempts_per_game:.1f} pass att/gm over last {recent.games}")
    if recent.targets_per_game:
        out.append(f"{recent.targets_per_game:.1f} targets/gm over last {recent.games}")
    if recent.carries_per_game:
        out.append(f"{recent.carries_per_game:.1f} carries/gm over last {recent.games}")
    if recent.snap_pct is not None:
        out.append(f"{recent.snap_pct:.0%} snap share")
    if not role_intact:
        out.append("snap share has collapsed -- discounted")
    return out


def _context(candidate: Candidate, fpoe: float | None) -> list[str]:
    """Things worth knowing that deliberately do not move the score.

    Each of these is either unproven or actively counterproductive as a ranking
    input; they are here so a human can apply judgement the model should not.
    """
    form = candidate.form
    notes: list[str] = []

    if form.is_emerging:
        delta = form.opportunity_delta
        if delta is not None:
            notes.append(f"role growing: {delta:+.1f} touches/gm vs earlier in the season")
        else:
            notes.append("role growing")
        notes.append("note: breakout-chasing underperformed in backtest -- weigh with care")

    snap = form.snap_delta
    if snap is not None and abs(snap) >= 0.10:
        notes.append(f"snaps {'up' if snap > 0 else 'down'} {abs(snap):.0%} vs earlier")

    share = form.target_share_delta
    if share is not None and abs(share) >= 0.05:
        notes.append(f"target share {'up' if share > 0 else 'down'} {abs(share):.0%}")

    # Named window: the score is season-long, this warning is about recent form,
    # and a reader comparing the two numbers should not have to guess which.
    games = form.recent.games
    expected = expected_points(form.recent, candidate.position)
    if fpoe is not None and expected:
        bar = max(REGRESSION_FLAG_FPOE, REGRESSION_FLAG_SHARE * expected)
        if fpoe >= bar:
            notes.append(
                f"last {games}: {fpoe:.1f}/gm above what his usage supports -- expect regression"
            )
        elif fpoe <= -bar:
            notes.append(f"last {games}: {abs(fpoe):.1f}/gm below his usage -- possible buy low")

    efficiency = form.recent.epa_per_opportunity
    if efficiency is not None and efficiency >= 0.20:
        notes.append("efficient on recent volume")

    notes.extend(candidate.context_notes)
    return notes


def score_candidate(candidate: Candidate) -> WaiverTarget | None:
    """Score one candidate, or None when he has not played enough to judge."""
    form = candidate.form
    season_ppg = form.to_date.ppr_per_game
    if season_ppg is None or form.to_date.games == 0:
        return None

    # Season-to-date scoring rate is the whole ranking signal.
    score = season_ppg

    # ...except that it has to still describe a player who is on the field.
    snap = form.recent.snap_pct
    role_intact = snap is None or snap >= MIN_SNAP_PCT_FOR_ROLE
    if not role_intact:
        score *= BENCHED_PENALTY

    fpoe = points_over_expected(form.recent, candidate.position)

    return WaiverTarget(
        canonical_id=candidate.canonical_id,
        name=candidate.name,
        position=candidate.position,
        team=candidate.team,
        score=round(score, 2),
        season_ppg=round(season_ppg, 2),
        recent_ppg=(
            round(form.recent.ppr_per_game, 2) if form.recent.ppr_per_game is not None else None
        ),
        expected_points_recent=(
            round(value, 2)
            if (value := expected_points(form.recent, candidate.position)) is not None
            else None
        ),
        points_over_expected=round(fpoe, 2) if fpoe is not None else None,
        snap_delta=form.snap_delta,
        opportunity_delta=form.opportunity_delta,
        is_emerging=form.is_emerging,
        role_intact=role_intact,
        outlook=outlook(form, candidate.position),
        reasons=_reasons(candidate, season_ppg, role_intact),
        context=_context(candidate, fpoe),
        advanced=list(candidate.advanced),
        advanced_summary=candidate.advanced_summary,
    )


def rank_waiver_targets(candidates: list[Candidate], limit: int = 25) -> list[WaiverTarget]:
    """Best available first. Ties break toward the bigger sample, then by name."""
    scored = [target for candidate in candidates if (target := score_candidate(candidate))]
    scored.sort(key=lambda t: (-t.score, -t.season_ppg if t.season_ppg else 0.0, t.name))
    return scored[:limit]
