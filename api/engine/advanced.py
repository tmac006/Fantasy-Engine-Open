"""Readable advanced-stat profiles for the player card.

Deliberately read-only. These metrics were gated against the market and did not
predict next season beyond a player's own prior scoring, out of sample, at any
position (`api.eval.advanced_fit`), so they are not allowed to move a pick. What
they are good for is telling you *why* a player produced what he did, which is
the one thing a projection cannot say and a human can act on.

Raw numbers do not carry that on their own. "2.4 yards after contact" means
nothing without knowing that it is a 90th-percentile figure, so everything here
is ranked within position and reported as a percentile against the players the
league would actually draft.

The split that matters most: yards before contact is mostly the offensive line,
yards after contact is the runner. A back who is strong after contact behind
weak blocking is good in spite of his situation; the projection prices the
situation, and you may know something about whether it is changing.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# (payload split, key, label, higher_is_better) per position.
METRICS: dict[str, tuple[tuple[str, str, str, bool], ...]] = {
    "RB": (
        ("rush", "yards_after_contact_per_att", "yards after contact/att", True),
        ("rush", "yards_before_contact_per_att", "yards before contact/att", True),
        ("rush", "attempts_per_broken_tackle", "carries per broken tackle", False),
    ),
    "WR": (
        ("rec", "average_depth_of_target", "average depth of target", True),
        ("rec", "yards_after_catch_per_rec", "yards after catch/rec", True),
        ("rec", "drop_pct", "drop rate", False),
        ("rec", "targets", "targets", True),
    ),
    "TE": (
        ("rec", "average_depth_of_target", "average depth of target", True),
        ("rec", "yards_after_catch_per_rec", "yards after catch/rec", True),
        ("rec", "drop_pct", "drop rate", False),
        ("rec", "targets", "targets", True),
    ),
    "QB": (
        ("pass", "pocket_time", "time in pocket", True),
        ("pass", "pressure_pct", "pressure rate faced", False),
        ("pass", "on_target_pct", "on-target throw rate", True),
        ("pass", "intended_air_yards_per_att", "intended air yards/att", True),
    ),
}

# Minimum volume before a rate is worth reporting; below this the percentile is
# noise dressed as a finding.
MIN_VOLUME: dict[str, tuple[str, str, float]] = {
    "RB": ("rush", "attempts", 50.0),
    "WR": ("rec", "targets", 30.0),
    "TE": ("rec", "targets", 25.0),
    "QB": ("pass", "attempts", 100.0),
}

HIGH, LOW = 70, 30


@dataclass(frozen=True)
class AdvancedProfile:
    """One player's advanced picture, already ranked against his position."""

    season: str
    lines: list[str] = field(default_factory=list)
    summary: str | None = None


def _percentile(value: float, cohort: list[float], higher_is_better: bool) -> int:
    below = sum(1 for other in cohort if other < value)
    rank = round(100.0 * below / len(cohort))
    return rank if higher_is_better else 100 - rank


def _ordinal(rank: int) -> str:
    """11th/12th/13th are the exceptions that make a naive suffix read wrong."""
    if 11 <= rank % 100 <= 13:
        return f"{rank}th"
    return f"{rank}{('th', 'st', 'nd', 'rd')[rank % 10] if rank % 10 < 4 else 'th'}"


def _verdict(position: str, ranks: dict[str, int]) -> str | None:
    """One sentence a human can act on, or nothing.

    Silence is a valid answer. A profile with no strong signal should say so by
    saying nothing rather than manufacturing a narrative from middling numbers.
    """
    if position == "RB":
        runner = ranks.get("yards after contact/att")
        blocking = ranks.get("yards before contact/att")
        if runner is None or blocking is None:
            return None
        if runner >= HIGH and blocking <= LOW:
            return "Creates his own yards behind poor blocking; the projection is pricing the line, not the back."
        if runner <= LOW and blocking >= HIGH:
            return "Production leans on his blocking rather than on him."
        if runner >= HIGH and blocking >= HIGH:
            return "Strong after contact and well blocked."
        return None
    if position in ("WR", "TE"):
        depth = ranks.get("average depth of target")
        yac = ranks.get("yards after catch/rec")
        if depth is None or yac is None:
            return None
        if depth >= HIGH and yac <= LOW:
            return "Downfield role; his yards come from depth rather than after the catch."
        if depth <= LOW and yac >= HIGH:
            return "Short targets he turns into yards himself."
        if depth >= HIGH and yac >= HIGH:
            return "Targeted deep and dangerous after the catch."
        return None
    if position == "QB":
        protection = ranks.get("pressure rate faced")
        accuracy = ranks.get("on-target throw rate")
        if protection is None or accuracy is None:
            return None
        if protection <= LOW and accuracy >= HIGH:
            return "Accurate while under constant pressure; better than his line makes him look."
        if protection >= HIGH and accuracy <= LOW:
            return "Well protected and still inaccurate."
        if protection <= LOW:
            return "Pressured heavily; his floor depends on protection improving."
        return None
    return None


def build_profiles(
    payloads: dict[str, tuple[str, dict[str, dict[str, float]]]],
    positions: dict[str, str],
) -> dict[str, AdvancedProfile]:
    """Rank every player against his own position and render the card text.

    `payloads` maps canonical id -> (season, stored PFR payload). Players with
    no payload are simply absent from the result, which is how a rookie stays
    unjudged rather than being scored as though he had bad numbers.
    """
    cohorts: dict[tuple[str, str, str], list[float]] = {}
    for canonical_id, (_, payload) in payloads.items():
        position = positions.get(canonical_id)
        if position not in METRICS or not _meets_volume(position, payload):
            continue
        for split, key, _, _ in METRICS[position]:
            value = (payload.get(split) or {}).get(key)
            if value is not None:
                cohorts.setdefault((position, split, key), []).append(float(value))

    profiles: dict[str, AdvancedProfile] = {}
    for canonical_id, (season, payload) in payloads.items():
        position = positions.get(canonical_id)
        if position not in METRICS or not _meets_volume(position, payload):
            continue
        lines: list[str] = []
        ranks: dict[str, int] = {}
        for split, key, label, higher in METRICS[position]:
            value = (payload.get(split) or {}).get(key)
            cohort = cohorts.get((position, split, key))
            if value is None or not cohort or len(cohort) < 10:
                continue
            rank = _percentile(float(value), cohort, higher)
            ranks[label] = rank
            lines.append(
                f"{label}: {float(value):g} ({_ordinal(rank)} pct among {position}s)"
            )
        if not lines:
            continue
        profiles[canonical_id] = AdvancedProfile(
            season=season, lines=lines, summary=_verdict(position, ranks)
        )
    return profiles


def _meets_volume(position: str, payload: dict[str, dict[str, float]]) -> bool:
    split, key, floor = MIN_VOLUME[position]
    value = (payload.get(split) or {}).get(key)
    return value is not None and float(value) >= floor
