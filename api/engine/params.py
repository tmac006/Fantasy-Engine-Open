"""Tunable engine parameters.

Every knob a user might reasonably want to adjust lives here with a sane default.
Overridable per-request via the recommendations endpoint.
"""

from pydantic import BaseModel, Field


class EngineParams(BaseModel):
    # How FLEX slots are assumed to be filled across the league, by position.
    # Drives replacement-level math; must sum to ~1.0 per flex type.
    # TE share is deliberately small: leagues almost never start a second TE,
    # and a generous TE share pushes TE replacement too deep, which inflates
    # elite-TE value and drafts them a round early.
    flex_split: dict[str, float] = Field(default={"RB": 0.40, "WR": 0.55, "TE": 0.05})
    # Superflex rooms overwhelmingly use a second quarterback. This separate
    # mix prevents generic FLEX assumptions from erasing QB replacement demand.
    superflex_split: dict[str, float] = Field(
        default={"QB": 0.85, "RB": 0.06, "WR": 0.08, "TE": 0.01}
    )

    # Tier detection: a break occurs where the drop between consecutive players
    # exceeds max(tier_gap_abs points, tier_gap_rel x that position's median gap).
    tier_gap_abs: float = 6.0
    tier_gap_rel: float = 2.0

    # Roster-need weighting: boost applied to positions with unfilled starter
    # slots, and penalty applied once starters (and flex) are covered.
    need_boost: float = 0.20
    surplus_penalty: float = 0.15

    # Positional-run detection: flag if >= run_threshold of the last run_window
    # picks were the same position.
    run_window: int = 8
    run_threshold: int = 5

    # Survival probability below which a player is flagged "won't last".
    at_risk_threshold: float = 0.35

    # Seeded opponent simulation. Keeping the seed in the request parameters
    # makes recommendations reproducible for tests, cached boards and clients.
    simulation_trials: int = 64
    simulation_seed: int = 2026
    simulation_run_weight: float = 1.0
    simulation_candidate_limit: int = 12
    simulation_pool_limit: int = 120
    # How fast an opponent's interest decays for players the market drafts
    # later, as a multiple of that player's ADP spread. Calibrated so the
    # simulation reproduces the analytic conditional survival curve; a wider
    # scale makes the simulated room draft closer to random. See
    # tests/test_simulation.py::test_simulated_disappearance_tracks_analytic.
    market_decay_sigma_mult: float = 0.4
    market_decay_min_picks: float = 2.5

    # Expected two-turn utility calibration.
    next_turn_discount: float = 0.85
    projection_uncertainty_weight: float = 0.15
    room_urgency_weight: float = 0.75

    # An unfilled TE slot is not itself an early-round reason to take a TE.
    # The regular need boost starts only after this round; before then an elite
    # TE can still win on projection advantage and a real simulated tier cliff.
    te_need_boost_after_round: int = 6
    qb_need_boost_after_round: int = 7
    # Second QB can wait until the teens; TE depth waits even longer because
    # streaming/waiver TEs are fine once a starter is rostered.
    duplicate_qb_te_after_round: int = 10
    duplicate_te_after_round: int = 13
    # A held backup TE may still win if it clearly crushes every non-held option.
    duplicate_te_exception_margin: float = 8.0
    early_duplicate_qb_te_multiplier: float = 0.35

    # Same-NFL-team correlations on the user's roster. Small additive bumps on
    # adjusted value so projections/VOR still dominate, but stacks matter.
    # QB+WR: positive game-script correlation (both benefit from passing TDs).
    # WR+RB: negative correlation (volume competition / opposing game scripts).
    qb_wr_stack_bonus: float = 2.5
    wr_rb_correlation_penalty: float = 2.0

    # Market timing guard: a high-survival player can usually be taken later.
    # This is a soft opportunity-cost penalty, not a hard ADP reach ban.
    reach_grace_picks: float = 8.0
    reach_penalty_per_pick: float = 0.25
    max_reach_penalty: float = 5.0

    # Hidden gems sit beyond the immediate market window and must beat their
    # market cost by enough picks to justify surfacing.
    hidden_gem_market_window: int = 18
    hidden_gem_min_value: float = 3.0
    hidden_gem_count: int = 1

    # Positions the engine ranks and recommends.
    positions: list[str] = Field(default=["QB", "RB", "WR", "TE", "K", "DEF"])

    # VOR discount for positions whose projections are noisy and whose
    # replacement level effectively lives on waivers (streaming).
    position_discount: dict[str, float] = Field(default={"K": 0.5, "DEF": 0.5})

    # Positions held out of the recommendation until the endgame: suppressed
    # while my remaining picks exceed my unfilled slots there plus this buffer.
    late_fill_positions: list[str] = Field(default=["K", "DEF"])
    late_fill_buffer: int = 2
