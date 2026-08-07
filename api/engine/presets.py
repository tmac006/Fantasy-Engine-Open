"""Scoring presets for drafts with no attached league (Sleeper mocks)."""

_BASE: dict[str, float] = {
    "pass_yd": 0.04, "pass_td": 4.0, "pass_int": -1.0, "pass_2pt": 2.0,
    "rush_yd": 0.1, "rush_td": 6.0, "rush_2pt": 2.0,
    "rec_yd": 0.1, "rec_td": 6.0, "rec_2pt": 2.0,
    "fum_lost": -2.0,
    "fgm": 3.0, "xpm": 1.0,
    "sack": 1.0, "int": 2.0, "fum_rec": 2.0, "def_td": 6.0, "safe": 2.0,
}


def preset_scoring(scoring_type: str) -> dict[str, float]:
    """Sleeper-style scoring dict for 'std' | 'half_ppr' | 'ppr'."""
    rec = {"std": 0.0, "half_ppr": 0.5, "ppr": 1.0}.get(scoring_type, 1.0)
    return {**_BASE, "rec": rec}
