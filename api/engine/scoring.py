"""Score raw stat projections under a league's exact scoring settings."""

from collections.abc import Mapping


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
    return total
