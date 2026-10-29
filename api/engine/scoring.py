"""Score raw stat projections under a league's exact scoring settings."""

from collections.abc import Mapping
from math import erf, sqrt

# Per-game yardage bonus bands, ESPN/Sleeper shared semantics: each key pays
# once per game inside its band (100-199 and 200+ are separate awards).
# Season projections carry no bonus_* keys -- Sleeper publishes none and
# ESPN's appliedTotal is default scoring -- so leagues paying game bonuses
# scored them as zero, under-projecting exactly the elite volume players
# rounds 1-3 are made of (~5-10 pts).
#
# Expected bonus games come from a normal per-game yardage model around the
# projected per-game mean. The dispersion constants are fitted, not assumed:
# 2023-2025 nflverse weekly data, volume players only (>=8 games, >=150/40/40
# yds per game). Rushing and receiving use the measured sd/mu and reproduce
# observed threshold rates almost exactly (rush 100+/200+: predicted 245/4 vs
# observed 250/8; rec: 433/8 vs 442/8). Passing uses a tail-calibrated 0.31
# because the raw ratio (0.40) is inflated by blowout short games, which a
# symmetric model converts into 3x too many 400-yard games; calibrated it
# predicts 215/18 vs 242/17 observed.
_YARDAGE_BONUS_BANDS: tuple[
    tuple[str, float, tuple[tuple[str, float, float | None], ...]], ...
] = (
    ("pass_yd", 0.31, (("bonus_pass_yd_300", 300.0, 400.0), ("bonus_pass_yd_400", 400.0, None))),
    ("rush_yd", 0.58, (("bonus_rush_yd_100", 100.0, 200.0), ("bonus_rush_yd_200", 200.0, None))),
    ("rec_yd", 0.64, (("bonus_rec_yd_100", 100.0, 200.0), ("bonus_rec_yd_200", 200.0, None))),
)


def _normal_tail(z: float) -> float:
    """P(Z >= z) for a standard normal."""
    return 0.5 * (1.0 - erf(z / sqrt(2.0)))


def expected_yardage_bonus_points(
    stats: Mapping[str, float], scoring: Mapping[str, float]
) -> float:
    """Expected season points from per-game yardage bonuses the league pays.

    Only bonus keys the league scores AND the projection does not already
    carry are estimated, so a source that someday ships real bonus
    projections is never double-counted.
    """
    games_raw = stats.get("gp")
    games = (
        float(games_raw)
        if isinstance(games_raw, (int, float)) and 0 < float(games_raw) <= 18
        else 17.0
    )
    total = 0.0
    for stat_key, cv, bands in _YARDAGE_BONUS_BANDS:
        season_yards = stats.get(stat_key)
        if not isinstance(season_yards, (int, float)) or season_yards <= 0:
            continue
        relevant = [band for band in bands if band[0] in scoring and band[0] not in stats]
        if not relevant:
            continue
        mu = float(season_yards) / games
        sigma = max(cv * mu, 1e-9)
        for bonus_key, lower, upper in relevant:
            share = _normal_tail((lower - mu) / sigma)
            if upper is not None:
                share -= _normal_tail((upper - mu) / sigma)
            total += float(scoring[bonus_key]) * games * max(0.0, share)
    return total


def score_stats(
    stats: Mapping[str, float],
    scoring: Mapping[str, float],
    *,
    position: str | None = None,
) -> float:
    """Points for a stat line under league scoring: sum over shared stat keys.

    Both sides use Sleeper stat keys (rec, rush_yd, pass_td, bonus_rec_te, ...),
    so a league's scoring_settings dict applies directly to projected stats.
    """
    total = sum(float(value) * scoring[key] for key, value in stats.items() if key in scoring)
    # Sleeper expresses TE premium as extra points per TE reception, while
    # projection feeds generally expose only the player's reception count.
    if (
        position == "TE"
        and "bonus_rec_te" in scoring
        and "bonus_rec_te" not in stats
        and "rec" in stats
    ):
        total += float(stats["rec"]) * scoring["bonus_rec_te"]
    total += expected_yardage_bonus_points(stats, scoring)
    return total
