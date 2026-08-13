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
    # Chance a drafted starter is unavailable in a given fantasy week: injured,
    # inactive or on bye. Measured over 2025 weeks 4-17 against a sample chosen
    # on weeks 1-3 production alone, so the selector cannot see what it grades.
    # Pooled 0.2214 over 1680 player-weeks, and flat across positions (QB .223,
    # RB .216, WR .222, TE .226 -- every pair inside one standard error), so one
    # rate is the honest model rather than four fitted to noise.
    #
    # This prices bench depth. VOR has no idea only three running backs can ever
    # be in a lineup, so a flat surplus penalty charges a seventh back the same
    # as a fourth, and a room that lets backs fall drafts you into RB7/WR3 with
    # a dead bench.
    starter_unavailable_rate: float = 0.2214
    # Floor on the divisor that demotes sub-replacement players, which bounds
    # how far below zero a deep-bench body can be pushed. Ordering down there is
    # cosmetic (nothing sub-replacement is getting picked over positive value),
    # and an unbounded divisor divides by zero the moment a position is stacked
    # past the point where any backup could be needed.
    depth_penalty_floor: float = 0.25

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
    #
    # Moved 10 -> 14 on measurement. Swept over 27 drafts across a 12- and a
    # 15-team league, finished starters were 1985.1 / 1985.1 / 1985.0 / 1985.0
    # at holds of 10 / 12 / 14 / never, with an identical worst case and no
    # invariant failures anywhere -- while the share of drafts spending a pick
    # on a backup quarterback fell 44% / 30% / 11% / 0%. So the pick was free to
    # skip and the engine was taking it in nearly half of all drafts.
    #
    # Not pushed to "never": the rehearsal grades the best legal lineup from
    # season projections, which structurally cannot see bye-week or injury
    # insurance at a position where only one player starts. 14 keeps a backup
    # available in the endgame, where he competes with the dregs rather than
    # with real starters, without pretending the metric measured his worth.
    duplicate_qb_te_after_round: int = 14
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
