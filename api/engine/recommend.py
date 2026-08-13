"""Live, league-specific two-turn draft recommendations."""

from collections import Counter
from dataclasses import dataclass
from math import comb

from pydantic import BaseModel

from api.engine.params import EngineParams
from api.engine.simulation import (
    DraftPickContext,
    NextTurnSimulation,
    TeamDraftContext,
    simulate_next_turn,
)
from api.engine.survival import survival_probability
from api.engine.tiers import assign_tiers
from api.engine.vor import (
    FLEX_ELIGIBILITY,
    PoolPlayer,
    replacement_points,
    replacement_ranks,
    startable_slots,
    vor,
)
from api.schemas import LeagueSettings


class PlayerAnalysis(BaseModel):
    """Expandable, source-honest context for a recommendation."""

    headline: str
    projection_summary: str
    projection_consensus: float
    projection_low: float
    projection_high: float
    projection_is_placeholder: bool
    roster_fit: str
    timing: str
    model_factors: list[str]
    usage: list[str]
    team_context: list[str]
    status: str | None
    risks: list[str]
    data_freshness: str | None


class RankedPlayer(BaseModel):
    canonical_id: str
    name: str
    position: str
    team: str | None
    points: float
    vor: float
    value: float  # VOR after roster-need weighting
    score: float  # expected two-turn utility; the ranking key
    tier: int  # tier within position
    tier_remaining: int  # undrafted players left in that tier
    adp: float | None
    survival: float  # P(still on the board at my next pick), 0-1
    disappearance_probability: float
    at_risk: bool  # survival below the params threshold
    opportunity_cost: float
    expected_next_turn_value: float
    reach_penalty: float
    reason: str
    platform_ids: dict[str, str]
    analysis: PlayerAnalysis
    held_for_later: bool = False  # late-fill position, not yet worth a pick
    pos_rank: int = 0  # rank within position by projected points (full pool)


class RunAlert(BaseModel):
    position: str
    count: int
    window: int


class HiddenGem(BaseModel):
    player: RankedPlayer
    target_pick_min: int
    target_pick_max: int
    upside: str
    risk: str


class OfflineKnobs(BaseModel):
    """Engine knobs the offline fallback has to mirror to stay consistent.

    The extension re-ranks a cached board when the API is unreachable, which
    means it reimplements a slice of the engine's roster rules. Hardcoding these
    on that side made an `EngineParams` change silently desync offline advice
    from live advice with nothing to catch it, so the values ride along with the
    board that was built from them.
    """

    duplicate_qb_te_after_round: int
    duplicate_te_after_round: int
    early_duplicate_qb_te_multiplier: float
    qb_wr_stack_bonus: float
    wr_rb_correlation_penalty: float
    # Bench-depth pricing. The offline path re-ranks against a roster that keeps
    # growing after the last successful poll, so without these it happily serves
    # a sixth running back -- the exact failure the live engine was fixed for.
    # Shipped rather than derived because the cached board carries no league
    # settings to compute startable counts from.
    startable_slots: dict[str, int]
    starter_unavailable_rate: float
    depth_penalty_floor: float


class Recommendation(BaseModel):
    recommended_pick: RankedPlayer
    room_pick: RankedPlayer
    picks_agree: bool
    alternates: list[RankedPlayer]
    board: list[RankedPlayer]  # full ranked available board (extension caches this)
    hidden_gems: list[HiddenGem]
    simulation: NextTurnSimulation
    tier_gone_risk: int  # players in the value pick's tier unlikely to survive
    run_alert: RunAlert | None
    picks_until_next: int | None
    on_clock: bool
    offline_knobs: OfflineKnobs
    # Problems with the inputs that make the advice less trustworthy -- most
    # importantly a roster that could not be identified, which silently reads as
    # "every slot is open". Shown to the user rather than only logged.
    warnings: list[str] = []


@dataclass(frozen=True)
class DraftContext:
    """Draft-time inputs the engine needs beyond the player pool."""

    drafted_ids: frozenset[str]
    my_roster_positions: tuple[str, ...]  # positions of players I already drafted
    current_pick: int
    my_next_pick: int | None
    recent_positions: tuple[str, ...]  # positions of the last N picks, most recent last
    my_remaining_picks: int | None = None  # picks I still own, incl. the current one
    # NFL team abbreviations parallel to my_roster_positions (None when unknown).
    my_roster_nfl_teams: tuple[str | None, ...] = ()
    team_contexts: tuple[TeamDraftContext, ...] = ()
    upcoming_team_ids: tuple[str, ...] = ()
    recent_picks: tuple[DraftPickContext, ...] = ()
    simulation_seed: int | None = None
    on_clock: bool = True


def _unfilled_starters(
    settings: LeagueSettings, my_positions: tuple[str, ...], params: EngineParams
) -> dict[str, float]:
    """Starter slots (direct + flex share) I still need to fill, by position."""
    have = Counter(my_positions)
    need: dict[str, float] = {}
    for pos in params.positions:
        direct = float(settings.roster_slots.get(pos, 0))
        flex = 0.0
        for slot, count in settings.roster_slots.items():
            if slot not in FLEX_ELIGIBILITY or pos not in FLEX_ELIGIBILITY[slot]:
                continue
            # A superflex/OP slot is mostly a QB slot in practice; sharing it
            # out by the RB/WR/TE split hides the league's real QB demand.
            split = (
                params.superflex_split
                if slot in ("SUPER_FLEX", "OP")
                else params.flex_split
            )
            flex += count * split.get(pos, 0.0)
        need[pos] = max(0.0, direct + flex - have[pos])
    return need


def _depth_multiplier(
    pos: str,
    have: dict[str, float],
    startable: dict[str, int],
    params: EngineParams,
) -> float:
    """What a player the lineup cannot start is worth: P(he is needed in a week).

    A bench player only earns his slot when the starters ahead of him are out,
    so his season VOR is scaled by the chance that happens. How many slots he
    backs up is the whole difference between positions: a backup quarterback
    covers one and is needed 22% of weeks, while a fourth running back covers
    three and is needed 53%. Availability itself does not vary by position --
    that was measured rather than assumed, so the same rate drives every one.

    Past the point where no week could call on him (a seventh back at three
    startable slots) this is exactly zero, which is the honest price of a pick
    that cannot enter a lineup under any circumstance.

    Cannot collide with the need boost: a position has an unfilled starter slot
    exactly when it sits below its startable count, so depth is at most zero
    wherever need is at least one.
    """
    slots = startable.get(pos, 0)
    depth = int(have.get(pos, 0.0)) + 1 - slots
    if depth <= 0:
        return 1.0
    rate = params.starter_unavailable_rate
    return sum(
        comb(slots, out) * rate**out * (1.0 - rate) ** (slots - out)
        for out in range(depth, slots + 1)
    )


def _need_multiplier(
    pos: str,
    need: dict[str, float],
    have: dict[str, float],
    startable: dict[str, int],
    settings: LeagueSettings,
    params: EngineParams,
    *,
    current_pick: int,
    total_teams: int,
) -> float:
    current_round = (current_pick - 1) // max(1, total_teams) + 1
    depth = _depth_multiplier(pos, have, startable, params)
    if need.get(pos, 0.0) >= 1.0:
        if pos == "TE" and current_round < params.te_need_boost_after_round:
            return 1.0
        is_single_qb = (
            settings.roster_slots.get("QB", 0) <= 1
            and settings.roster_slots.get("SUPER_FLEX", 0) == 0
        )
        if (
            pos == "QB"
            and is_single_qb
            and current_round < params.qb_need_boost_after_round
        ):
            return 1.0
        # Flat, deliberately. Scaling this by how many starters are missing
        # sounds obviously right and does nothing: swept at 1.0/1.5/2.0/2.5/3.0
        # slots over 54 drafts across a 12- and a 15-team league, finished
        # starters moved 1976.6/1973.1/1973.5/1974.1/1974.1 with an identical
        # worst case. The gaps that decide a pick are VOR-sized, and no
        # multiplier in this range crosses them; the endgame must-fill rule is
        # what actually guarantees the slots get filled.
        return 1.0 + params.need_boost
    # Backup TEs are suppressed via held_for_later (not a soft value haircut), so
    # an elite leftover TE can still win on raw board score if released.
    if (
        pos == "QB"
        and need.get(pos, 0.0) < 1.0
        and current_round < params.duplicate_qb_te_after_round
    ):
        return params.early_duplicate_qb_te_multiplier * depth
    if need.get(pos, 0.0) > 0.0:
        return depth
    return depth * (1.0 - params.surplus_penalty)


def _held_for_later(
    pos: str,
    settings: LeagueSettings,
    ctx: DraftContext,
    have: dict[str, float],
    startable: dict[str, int],
    params: EngineParams,
) -> bool:
    """True while a position should not be surfaced as the current pick."""
    current_round = (ctx.current_pick - 1) // max(1, settings.total_teams) + 1
    # The league's hard cap wins over everything: the platform rejects the pick.
    cap = settings.position_limits.get(pos)
    if cap is not None and have.get(pos, 0.0) >= cap:
        return True
    # Stacked past the point where any combination of absences could start him.
    # Scoring alone will not keep this pick away: the depth discount lands on
    # exactly zero, and zero beats every negative-value player on the kind of
    # late board where this comes up, so the engine reaches for the one player
    # who provably cannot play. Held is the honest answer, not a low score.
    if _depth_multiplier(pos, have, startable, params) <= 0.0:
        return True
    required = settings.roster_slots.get(pos, 0)
    if pos == "QB":
        required += settings.roster_slots.get("SUPER_FLEX", 0)
    # Backup TE: once the starter is filled, prefer RB/WR depth until late.
    # A third TE is never a recommendation target in standard rooms.
    if pos == "TE" and required > 0 and have.get(pos, 0.0) >= required:
        if have.get(pos, 0.0) >= required + 1:
            return True
        return current_round < params.duplicate_te_after_round
    if pos == "QB" and required > 0 and have.get(pos, 0.0) >= required:
        # One backup is a bye-week plan; a third quarterback is a wasted bench
        # slot whatever his VOR says, because only one starts. The round gate
        # applies to the backup only -- the deeper surplus is held forever.
        # (Live failure: round 11, Lamar and Mayfield rostered, engine still
        # surfaced Kyler Murray as the top pick.)
        if have.get(pos, 0.0) >= required + 1:
            return True
        if current_round < params.duplicate_qb_te_after_round:
            return True
    if pos not in params.late_fill_positions:
        return False
    # Never recommend a second K/DEF once that starter slot is filled. Streaming
    # replacements live on waivers; endgame hold-lift is only for unfilled slots.
    if required > 0 and have.get(pos, 0.0) >= required:
        return True
    if ctx.my_remaining_picks is None:
        # No pick budget means this is a pre-draft or mid-draft cached board.
        # Defaulting to "not held" put kickers at the top of the offline
        # fallback in the middle rounds; held is the safe default, and the
        # endgame lift only makes sense when remaining picks are known anyway.
        return True
    unfilled_late = sum(
        max(0.0, settings.roster_slots.get(p, 0) - have.get(p, 0.0))
        for p in params.late_fill_positions
    )
    return ctx.my_remaining_picks > unfilled_late + params.late_fill_buffer


def _release_elite_backup_tight_ends(
    ranked: list[RankedPlayer],
    params: EngineParams,
) -> None:
    """Let ONE held backup TE through when it clearly leads the board.

    Two guards matter here. Only the single best held TE is eligible -- a
    margin measured against the board's best releases the entire TE room the
    moment the board goes negative, which turned a round-11 recommendation list
    into four backup tight ends. And the released TE must itself carry positive
    value: "clearly better than a bad board" is a reason to keep waiting, not a
    reason to spend a pick on a second tight end.
    """
    best_non_held = max(
        (player.score for player in ranked if not player.held_for_later),
        default=float("-inf"),
    )
    if best_non_held == float("-inf"):
        return
    held_tight_ends = [
        player
        for player in ranked
        if player.held_for_later and player.position == "TE"
    ]
    if not held_tight_ends:
        return
    leader = max(held_tight_ends, key=lambda player: player.score)
    if (
        leader.value > 0
        and leader.score > 0
        and leader.score >= best_non_held + params.duplicate_te_exception_margin
    ):
        leader.held_for_later = False


def _reason(
    p: RankedPlayer,
    need: dict[str, float],
    run: RunAlert | None,
    my_next_pick: int | None,
) -> str:
    """Numbers over verdicts (spec §7.2): show projection rank vs market so a
    divergence between the two is visible, never silent."""
    parts = [f"proj {p.position}{p.pos_rank}, tier {p.tier} ({p.tier_remaining} left)"]
    if p.adp is not None:
        parts.append(f"market ADP {p.adp:.0f}")
    # A hard source split is exactly the context a human needs to overrule the
    # pick, so it belongs on the headline, not one click deep in the details.
    # (Live case: ESPN projected Saquon Barkley 326 while Sleeper said 247; the
    # median made him the top available RB with no visible hint of the dispute.)
    spread = p.analysis.projection_high - p.analysis.projection_low
    if spread >= max(25.0, 0.15 * max(1.0, p.analysis.projection_consensus)):
        parts.append(
            f"sources split {p.analysis.projection_low:.0f}-{p.analysis.projection_high:.0f}"
        )
    if need.get(p.position, 0.0) >= 1.0:
        parts.append(f"fills an open {p.position} starter slot")
    if my_next_pick is not None and p.adp is not None:
        parts.append(f"{p.survival * 100:.0f}% to last until pick {my_next_pick}")
    if p.opportunity_cost >= 10.0:
        parts.append(f"waiting at {p.position} costs ~{p.opportunity_cost:.0f} pts")
    if p.reach_penalty >= 3.0:
        parts.append(f"waitability lowers this pick by {p.reach_penalty:.0f} pts")
    if run and run.position == p.position:
        parts.append(f"{run.count} of last {run.window} picks were {run.position}s")
    return "; ".join(parts)


def _tier_board(
    pool: list[PoolPlayer], params: EngineParams
) -> tuple[dict[str, int], dict[tuple[str, int], list[str]], dict[str, int]]:
    """Tier and positional rank for every player, computed over the FULL pool.

    Ranking within the *available* players instead would renumber the board on
    every pick -- every remaining player reads as "tier 1 (1 left)" by the late
    rounds, and replacement levels drift upward as the draft empties.
    """
    tier_of: dict[str, int] = {}
    tier_members: dict[tuple[str, int], list[str]] = {}
    pos_rank_of: dict[str, int] = {}
    for pos in params.positions:
        at_pos = sorted(
            (p for p in pool if p.position == pos), key=lambda p: p.points, reverse=True
        )
        for rank, (player, tier) in enumerate(
            zip(at_pos, assign_tiers([p.points for p in at_pos], params)), start=1
        ):
            tier_of[player.canonical_id] = tier
            tier_members.setdefault((pos, tier), []).append(player.canonical_id)
            pos_rank_of[player.canonical_id] = rank
    return tier_of, tier_members, pos_rank_of


def _run_alert(recent_positions: tuple[str, ...], params: EngineParams) -> "RunAlert | None":
    """A positional run in the last N picks, or None."""
    window = recent_positions[-params.run_window :]
    if not window:
        return None
    position, count = Counter(window).most_common(1)[0]
    if count < params.run_threshold:
        return None
    return RunAlert(position=position, count=count, window=len(window))


def _capped_positions(
    settings: LeagueSettings, have: dict[str, float], params: EngineParams
) -> set[str]:
    """Positions the league will not let this roster add to.

    Held alone is not enough: must-fill promotion and the TE release valve both
    re-admit held rows, and neither may surface a pick the platform rejects.
    """
    return {
        pos
        for pos in params.positions
        if (cap := settings.position_limits.get(pos)) is not None
        and have.get(pos, 0.0) >= cap
    }


def _endgame_state(
    settings: LeagueSettings,
    ctx: DraftContext,
    have: dict[str, float],
    params: EngineParams,
) -> tuple[set[str], bool]:
    """Which starter positions are open, and whether picks have run short.

    A FLEX or SUPER_FLEX opening is a starting slot like any other, so it
    belongs in the pick budget; any player beyond a position's direct
    requirement is already covering one.
    """
    open_direct = {
        pos
        for pos in params.positions
        if settings.roster_slots.get(pos, 0) - have.get(pos, 0.0) >= 1
    }
    open_slots = sum(
        max(0, settings.roster_slots.get(pos, 0) - int(have.get(pos, 0.0)))
        for pos in params.positions
    )
    flex_slots = sum(
        count for slot, count in settings.roster_slots.items() if slot in FLEX_ELIGIBILITY
    )
    surplus = sum(
        max(0, int(have.get(pos, 0.0)) - settings.roster_slots.get(pos, 0))
        for pos in params.positions
    )
    open_slots += max(0, flex_slots - surplus)
    must_fill = (
        ctx.my_remaining_picks is not None
        and len(open_direct) > 0
        and ctx.my_remaining_picks <= open_slots + 1
    )
    return open_direct, must_fill


@dataclass(frozen=True)
class PlayerAdjustments:
    """Per-player terms that do not vary with the roster-need vector."""

    context: float
    correlation: float
    correlation_factors: list[str]
    uncertainty: float


def _player_adjustments(
    player: PoolPlayer, ctx: DraftContext, params: EngineParams
) -> PlayerAdjustments:
    correlation, factors = _correlation_adjustment(
        player, ctx.my_roster_positions, ctx.my_roster_nfl_teams, params
    )
    return PlayerAdjustments(
        context=_context_adjustment(player),
        correlation=correlation,
        correlation_factors=factors,
        uncertainty=_projection_uncertainty(player) * params.projection_uncertainty_weight,
    )


def _projection_uncertainty(player: PoolPlayer) -> float:
    if player.projection_stdev is not None:
        return max(0.0, player.projection_stdev)
    if player.projection_low is not None and player.projection_high is not None:
        return max(0.0, (player.projection_high - player.projection_low) / 2.56)
    return 0.0


def _context_adjustment(player: PoolPlayer) -> float:
    """Small, capped role/team adjustment; projections remain the primary signal."""
    adjustment = 0.0
    status = (player.status or "").lower()
    if any(flag in status for flag in ("injured reserve", " ir", "out", "pup", "reserve")):
        adjustment -= 18.0
    elif "doubtful" in status:
        adjustment -= 10.0
    elif "questionable" in status:
        adjustment -= 4.0

    snap_pct = player.usage.get("snap_pct")
    target_share = player.usage.get("target_share")
    touches = player.usage.get("touches")
    if isinstance(snap_pct, (int, float)):
        adjustment += 1.5 if snap_pct >= 0.75 else -1.5 if snap_pct < 0.45 else 0.0
    if isinstance(target_share, (int, float)) and target_share >= 0.20:
        adjustment += 1.5
    if player.position == "RB" and isinstance(touches, (int, float)) and touches >= 200:
        adjustment += 1.0

    team_epa = player.team_context.get("epa_per_play")
    if isinstance(team_epa, (int, float)):
        adjustment += 1.0 if team_epa >= 0.08 else -1.0 if team_epa < -0.03 else 0.0
    return max(-20.0, min(4.0, adjustment))


def _normalize_nfl_team(team: str | None) -> str | None:
    if team is None:
        return None
    cleaned = team.strip().upper()
    return cleaned or None


def _correlation_adjustment(
    player: PoolPlayer,
    roster_positions: tuple[str, ...],
    roster_nfl_teams: tuple[str | None, ...],
    params: EngineParams,
) -> tuple[float, list[str]]:
    """Same-NFL-team stack bonus / anti-correlation penalty vs the user's roster.

    Returns (adjustment, human-readable factor strings).
    """
    team = _normalize_nfl_team(player.team)
    if team is None or not roster_positions:
        return 0.0, []

    teammates = [
        pos
        for pos, roster_team in zip(roster_positions, roster_nfl_teams, strict=False)
        if _normalize_nfl_team(roster_team) == team
    ]
    if not teammates:
        return 0.0, []

    adjustment = 0.0
    factors: list[str] = []
    teammate_counts = Counter(teammates)

    if player.position == "WR" and teammate_counts.get("QB", 0) > 0:
        adjustment += params.qb_wr_stack_bonus
        factors.append(f"QB-WR stack with rostered {team} QB (+{params.qb_wr_stack_bonus:.1f}).")
    elif player.position == "QB" and teammate_counts.get("WR", 0) > 0:
        stacks = teammate_counts["WR"]
        bonus = params.qb_wr_stack_bonus * min(stacks, 2)
        adjustment += bonus
        factors.append(
            f"QB-WR stack with {stacks} rostered {team} WR"
            f"{'s' if stacks != 1 else ''} (+{bonus:.1f})."
        )

    if player.position == "WR" and teammate_counts.get("RB", 0) > 0:
        adjustment -= params.wr_rb_correlation_penalty
        factors.append(
            f"WR-RB same-team anti-correlation with rostered {team} RB "
            f"(-{params.wr_rb_correlation_penalty:.1f})."
        )
    elif player.position == "RB" and teammate_counts.get("WR", 0) > 0:
        adjustment -= params.wr_rb_correlation_penalty
        factors.append(
            f"WR-RB same-team anti-correlation with rostered {team} WR "
            f"(-{params.wr_rb_correlation_penalty:.1f})."
        )

    return adjustment, factors


def _analysis(
    player: PoolPlayer,
    *,
    need: dict[str, float],
    disappearance: float,
    fallback: float,
    value: float,
    context_adjustment: float,
    correlation_adjustment: float,
    correlation_factors: list[str],
    uncertainty_penalty: float,
    reach_penalty: float,
    next_pick: int | None,
) -> PlayerAnalysis:
    consensus = player.projection_consensus if player.projection_consensus is not None else player.points
    placeholder = (
        player.projection_consensus is None
        or player.projection_low is None
        or player.projection_high is None
    )
    estimated_spread = max(5.0, abs(consensus) * 0.08)
    low = player.projection_low if player.projection_low is not None else consensus - estimated_spread
    high = player.projection_high if player.projection_high is not None else consensus + estimated_spread
    if need.get(player.position, 0.0) >= 1.0:
        roster_fit = f"Fills an open {player.position} starter; value is still compared with other slots."
    else:
        roster_fit = f"Adds {player.position} depth after modeled starter demand is covered."
    if correlation_factors:
        roster_fit = f"{roster_fit} {' '.join(correlation_factors)}"
    if next_pick is None:
        timing = "No next turn is known; timing is based on current value only."
    else:
        timing = (
            f"{disappearance * 100:.0f}% modeled chance to go before pick {next_pick}; "
            f"expected {player.position} fallback value {fallback:.1f}."
        )
    risks = list(player.risk_flags)
    if placeholder:
        risks.append("Projection range is an engine placeholder until source dispersion is available.")
    if not player.usage:
        risks.append("Advanced usage context is not available.")
    if player.status and player.status.lower() not in {"active", "healthy"}:
        risks.append(f"Current status: {player.status}.")
    usage = [
        f"{key.replace('_', ' ')}: {value}"
        for key, value in player.usage.items()
        if value is not None
    ]
    team_context = [
        f"{key.replace('_', ' ')}: {value}"
        for key, value in player.team_context.items()
        if value is not None
    ]
    freshness = "; ".join(
        f"{source}: {timestamp}"
        for source, timestamp in player.freshness.items()
        if timestamp is not None
    )
    return PlayerAnalysis(
        headline=(
            f"{player.position} adjusted value {value:.1f} with "
            f"{disappearance * 100:.0f}% modeled room urgency."
        ),
        projection_summary=(
            f"{consensus:.1f} consensus; {low:.1f}-{high:.1f} modeled range"
            + (" (placeholder)." if placeholder else ".")
        ),
        projection_consensus=round(consensus, 1),
        projection_low=round(low, 1),
        projection_high=round(high, 1),
        projection_is_placeholder=placeholder,
        roster_fit=roster_fit,
        timing=timing,
        model_factors=[
            f"League-adjusted value: {value:.1f}.",
            f"Verified context adjustment: {context_adjustment:+.1f}.",
            f"Same-team correlation adjustment: {correlation_adjustment:+.1f}.",
            *correlation_factors,
            f"Projection uncertainty penalty: -{uncertainty_penalty:.1f}.",
            f"Waitability/reach penalty: -{reach_penalty:.1f}.",
        ],
        usage=usage,
        team_context=team_context,
        status=player.status,
        risks=risks,
        data_freshness=freshness or None,
    )


def _hidden_gems(
    ranked: list[RankedPlayer],
    by_id: dict[str, PoolPlayer],
    current_pick: int,
    params: EngineParams,
) -> list[HiddenGem]:
    immediate_window = current_pick + params.hidden_gem_market_window
    candidates: list[tuple[float, HiddenGem]] = []
    for player in ranked:
        if player.held_for_later:
            continue
        source = by_id[player.canonical_id]
        if source.adp is None or source.adp <= immediate_window:
            continue
        market_value = max(0.0, source.adp - current_pick)
        context_upside = bool(source.usage) or source.projection_high is not None
        upside_spread = max(0.0, player.analysis.projection_high - player.points)
        # Surface the single best stash for this exact draft moment. Extremely
        # distant ADPs are less actionable even when their raw upside is high.
        distance_penalty = 0.08 * max(0.0, market_value - 36.0)
        gem_score = player.value + 0.5 * upside_spread - distance_penalty
        if player.value < params.hidden_gem_min_value and not context_upside:
            continue
        sigma = source.adp_stdev or max(2.0, source.adp * 0.107)
        start = max(current_pick + 1, round(source.adp - sigma))
        end = max(start, round(source.adp + sigma))
        usage_signal = next(
            (f"{key} {value}" for key, value in source.usage.items() if value is not None),
            None,
        )
        upside = (
            f"Role signal: {usage_signal}; projection ceiling {player.analysis.projection_high:.1f}."
            if usage_signal
            else f"Projects above replacement with a {player.analysis.projection_high:.1f} ceiling."
        )
        risk = (
            player.analysis.risks[0]
            if player.analysis.risks
            else "Late market cost reflects uncertain role and weekly floor."
        )
        candidates.append(
            (
                gem_score,
                HiddenGem(
                    player=player,
                    target_pick_min=start,
                    target_pick_max=end,
                    upside=upside,
                    risk=risk,
                ),
            )
        )
    candidates.sort(
        key=lambda item: (-item[0], item[1].target_pick_min, item[1].player.canonical_id)
    )
    return [gem for _, gem in candidates[: params.hidden_gem_count]]


def recommend(
    pool: list[PoolPlayer],
    settings: LeagueSettings,
    ctx: DraftContext,
    params: EngineParams,
) -> Recommendation | None:
    """Rank available players and produce the draft-time recommendation.

    Returns None when nothing in the pool is available (draft over / bad inputs).
    """
    ranks = replacement_ranks(settings, params)
    repl = replacement_points(pool, ranks)

    tier_of, tier_members, pos_rank_of = _tier_board(pool, params)

    need = _unfilled_starters(settings, ctx.my_roster_positions, params)
    have: dict[str, float] = dict(Counter(ctx.my_roster_positions))
    startable = startable_slots(settings, params)
    capped_positions = _capped_positions(settings, have, params)
    run = _run_alert(ctx.recent_positions, params)

    available = [p for p in pool if p.canonical_id not in ctx.drafted_ids]
    if not available:
        return None

    # Context, correlation and uncertainty depend only on the player and the
    # roster, not on the need vector, so they are constant across every
    # adjusted_value call and across the ranking loop. Computing them once here
    # replaces roughly 2N redundant passes -- the counterfactual loop calls
    # adjusted_value for the whole board once per simulated candidate.
    adjustments: dict[str, PlayerAdjustments] = {
        player.canonical_id: _player_adjustments(player, ctx, params)
        for player in available
    }

    def adjusted_value(
        player: PoolPlayer,
        position_need: dict[str, float],
        position_have: dict[str, float],
    ) -> float:
        adjustment = adjustments[player.canonical_id]
        base = vor(player, repl) * params.position_discount.get(player.position, 1.0)
        multiplier = _need_multiplier(
            player.position,
            position_need,
            position_have,
            startable,
            settings,
            params,
            current_pick=ctx.current_pick,
            total_teams=settings.total_teams,
        )
        # The multiplier assumes value is positive: x0.85 as a surplus penalty,
        # x1.25 as a need boost. On the negative VOR a late board is made of,
        # multiplying INVERTS both -- the penalty shrinks a surplus player
        # toward zero (rewarding him) and the boost drags a needed player
        # further down. Dividing when the base is negative preserves the
        # intent at every sign: penalties always demote, boosts always promote.
        # The floor bounds that division, which would otherwise divide by zero
        # against a position stacked past any use for another backup.
        weighted = (
            base * multiplier
            if base >= 0
            else base / max(multiplier, params.depth_penalty_floor)
        )
        return (
            weighted
            - adjustment.uncertainty
            + adjustment.context
            + adjustment.correlation
        )

    values = {p.canonical_id: adjusted_value(p, need, have) for p in available}
    simulation = simulate_next_turn(
        available,
        values,
        current_pick=ctx.current_pick,
        next_pick=ctx.my_next_pick,
        roster_slots=settings.roster_slots,
        params=params,
        teams=ctx.team_contexts,
        upcoming_team_ids=ctx.upcoming_team_ids,
        recent_picks=ctx.recent_picks,
        recent_positions=ctx.recent_positions,
        current_pick_is_user=ctx.on_clock,
        seed=ctx.simulation_seed,
    )

    # Compare complete outcomes.  Each candidate gets its immediate adjusted
    # VOR once, plus the best expected pick next turn.  There is no additive
    # VONA/scarcity term, so the same positional edge cannot be counted twice.
    expected_future: dict[str, float] = {}
    if ctx.my_next_pick is not None:
        simulation_candidates = sorted(
            available,
            key=lambda p: (values[p.canonical_id], -(p.adp or float("inf"))),
            reverse=True,
        )[: params.simulation_candidate_limit]
        for candidate in simulation_candidates:
            post_positions = (*ctx.my_roster_positions, candidate.position)
            post_need = _unfilled_starters(settings, post_positions, params)
            # The hypothetical roster has to move too. Advancing need but not
            # the position counts would let a candidate be evaluated against a
            # next turn where he had not been drafted, so stacking a position
            # would look free for exactly one more pick than it is.
            post_have: dict[str, float] = dict(Counter(post_positions))
            post_values = {
                p.canonical_id: adjusted_value(p, post_need, post_have)
                for p in available
                if p.canonical_id != candidate.canonical_id
            }
            counterfactual = simulate_next_turn(
                available,
                post_values,
                current_pick=ctx.current_pick,
                next_pick=ctx.my_next_pick,
                roster_slots=settings.roster_slots,
                params=params,
                teams=ctx.team_contexts,
                upcoming_team_ids=ctx.upcoming_team_ids,
                recent_picks=ctx.recent_picks,
                recent_positions=ctx.recent_positions,
                current_pick_is_user=ctx.on_clock,
                seed=ctx.simulation_seed,
                excluded_ids=frozenset({candidate.canonical_id}),
            )
            expected_future[candidate.canonical_id] = max(
                (
                    fallback_value
                    for position, fallback_value in counterfactual.expected_fallback_value.items()
                    if not (
                        candidate.position in params.late_fill_positions
                        and position == candidate.position
                    )
                ),
                default=0.0,
            )
        for player in available:
            if player.canonical_id in expected_future:
                continue
            # Avoid double-counting this candidate as both the take-now player
            # and the estimated next-turn fallback for long-tail board rows.
            expected_future[player.canonical_id] = max(
                (
                    fallback_value
                    for position, fallback_value in simulation.expected_fallback_value.items()
                    if simulation.expected_fallback_player.get(position)
                    != player.canonical_id
                    # Taking a K or DEF fills that slot for good, so "take one
                    # now, take another next turn" must never be the plan the
                    # future value is credited for. It also broke the ordering:
                    # the best kicker excluded himself as his own fallback while
                    # the second-best got credited WITH the best one.
                    and not (
                        player.position in params.late_fill_positions
                        and position == player.position
                    )
                ),
                default=0.0,
            )
    else:
        expected_future = {p.canonical_id: 0.0 for p in available}

    ranked: list[RankedPlayer] = []
    for p in available:
        adjustment = adjustments[p.canonical_id]
        p_vor = vor(p, repl) * params.position_discount.get(p.position, 1.0)
        value = values[p.canonical_id]
        fallback = simulation.expected_fallback_value.get(p.position, 0.0)
        opportunity_cost = value - fallback
        # One scarcity number, not two. The simulation is authoritative whenever
        # it actually modeled this player: it knows roster needs, positional runs
        # and observed reaches, which the ADP curve alone cannot see. The
        # analytic curve is the fallback for players outside the simulated pool
        # (deep names the room will not reach in a handful of picks) and for
        # boards built with no next turn. survival and disappearance are kept
        # exact complements so the reason line and the detail card cannot
        # contradict each other.
        if ctx.my_next_pick is None:
            survival, disappearance = 1.0, 0.0
        elif simulation.picks_simulated and p.canonical_id in simulation.disappearance_probability:
            disappearance = simulation.disappearance_probability[p.canonical_id]
            survival = 1.0 - disappearance
        else:
            survival = survival_probability(p.adp, p.adp_stdev, ctx.my_next_pick)
            disappearance = 1.0 - survival
        two_turn_utility = value + params.next_turn_discount * expected_future[p.canonical_id]
        market_gap = max(0.0, (p.adp or ctx.current_pick) - ctx.current_pick)
        # No next pick means waiting is impossible, so "waitability" cannot
        # lower this pick. K/DEF ADPs are clustered placeholder values (~165-170
        # for the whole position), so a reach penalty there is pure noise that
        # once reordered kickers against their own projections.
        if ctx.my_next_pick is None or p.position in params.late_fill_positions:
            reach_penalty = 0.0
        else:
            reach_penalty = min(
                params.max_reach_penalty,
                max(0.0, market_gap - params.reach_grace_picks)
                * params.reach_penalty_per_pick
                * survival,
            )
        tier = tier_of.get(p.canonical_id, 1)
        remaining = sum(
            1
            for cid in tier_members.get((p.position, tier), [])
            if cid not in ctx.drafted_ids
        )
        rp = RankedPlayer(
            canonical_id=p.canonical_id,
            name=p.name,
            position=p.position,
            team=p.team,
            points=round(p.points, 1),
            vor=round(p_vor, 1),
            value=round(value, 1),
            score=round(two_turn_utility - reach_penalty, 1),
            tier=tier,
            tier_remaining=remaining,
            adp=p.adp,
            survival=round(survival, 3),
            disappearance_probability=round(disappearance, 3),
            at_risk=survival < params.at_risk_threshold,
            opportunity_cost=round(opportunity_cost, 1),
            expected_next_turn_value=round(expected_future[p.canonical_id], 1),
            reach_penalty=round(reach_penalty, 1),
            reason="",
            platform_ids=p.platform_ids,
            analysis=_analysis(
                p,
                need=need,
                disappearance=disappearance,
                fallback=fallback,
                value=value,
                context_adjustment=adjustment.context,
                correlation_adjustment=adjustment.correlation,
                correlation_factors=adjustment.correlation_factors,
                uncertainty_penalty=adjustment.uncertainty,
                reach_penalty=reach_penalty,
                next_pick=ctx.my_next_pick,
            ),
            held_for_later=_held_for_later(
                p.position, settings, ctx, have, startable, params
            ),
            pos_rank=pos_rank_of.get(p.canonical_id, 0),
        )
        ranked.append(rp)

    # Backup TEs stay held for roster construction, but an extreme board leader
    # can still surface ("truly the best player available") -- unless a second
    # TE is already rostered or the league cap is reached, where no board lead
    # justifies a pick the roster cannot use.
    te_required = settings.roster_slots.get("TE", 0)
    if (
        te_required > 0
        and have.get("TE", 0.0) < te_required + 1
        and "TE" not in capped_positions
    ):
        _release_elite_backup_tight_ends(ranked, params)

    # Endgame: once remaining picks barely cover unfilled starter slots, filling
    # them outranks stacking bench value at any position.
    open_direct, must_fill = _endgame_state(settings, ctx, have, params)

    # Must-fill promotion has to dominate the held flag -- K and DEF are both
    # held and open at the endgame, and sorting held-first left them at the
    # bottom of the board at the exact moment they became the right pick.
    # Positions at the league cap never surface regardless.
    #
    # Clear the flag rather than merely outranking it: a row that is promoted to
    # the top of the board while still marked held is filtered out of the
    # extension's board table, so the recommended pick vanishes from the list it
    # is supposed to lead.
    if must_fill:
        for row in ranked:
            if row.position in open_direct and row.position not in capped_positions:
                row.held_for_later = False

    ranked.sort(
        key=lambda r: (
            r.position in capped_positions,
            not (must_fill and r.position in open_direct),
            r.held_for_later,
            -r.score,
        )
    )

    recommended_pick = ranked[0]

    # The room-aware answer is driven by modeled disappearance and the value of
    # the same-position cliff. It is intentionally distinct from roster utility.
    # Positive adjusted value is a hard requirement, not a scoring term. The
    # ranking below reads `max(0.0, value)`, which hides how bad a player is
    # while still crediting his opportunity cost in full -- so a second
    # quarterback at value -144 beat the board on "the next quarterback is even
    # worse", and the panel showed him beside the real pick as though he were
    # one. The room pick answers which player worth having is about to go; a
    # player this roster cannot use is not an answer to that.
    room_candidates = [
        r
        for r in ranked
        if r.adp is not None
        and r.value > 0
        and r.position not in capped_positions
        and (must_fill or not r.held_for_later)
    ]
    if simulation.picks_simulated:
        room_pick = max(
            room_candidates,
            key=lambda r: (
                r.disappearance_probability
                * (
                    max(0.0, r.value)
                    + params.room_urgency_weight * max(0.0, r.opportunity_cost)
                ),
                -(r.adp or float("inf")),
                r.canonical_id,
            ),
            default=recommended_pick,
        )
    else:
        room_pick = (
            min(room_candidates, key=lambda r: (r.adp or float("inf"), r.canonical_id))
            if room_candidates
            else recommended_pick
        )

    for rp in {r.canonical_id: r for r in [*ranked[:10], room_pick]}.values():
        rp.reason = _reason(rp, need, run, ctx.my_next_pick)

    survival_of = {r.canonical_id: r.survival for r in ranked}
    tier_gone = sum(
        1
        for cid in tier_members.get((recommended_pick.position, recommended_pick.tier), [])
        if cid not in ctx.drafted_ids
        and survival_of.get(cid, 1.0) < params.at_risk_threshold
    )
    picks_until = (
        None
        if ctx.my_next_pick is None
        else ctx.my_next_pick - ctx.current_pick
    )
    # The board must always carry the held positions (K/DEF): the offline
    # fallback serves from this cache and still needs them at the endgame.
    board = ranked[:200]
    board += [r for r in ranked[200:] if r.position in params.late_fill_positions]
    alternates = [r for r in ranked[1:] if r.canonical_id != room_pick.canonical_id][:2]
    by_id = {p.canonical_id: p for p in available}
    return Recommendation(
        recommended_pick=recommended_pick,
        room_pick=room_pick,
        picks_agree=recommended_pick.canonical_id == room_pick.canonical_id,
        alternates=alternates,
        board=board,
        hidden_gems=_hidden_gems(ranked, by_id, ctx.current_pick, params),
        simulation=simulation,
        tier_gone_risk=tier_gone,
        run_alert=run,
        picks_until_next=picks_until,
        on_clock=ctx.on_clock,
        offline_knobs=OfflineKnobs(
            duplicate_qb_te_after_round=params.duplicate_qb_te_after_round,
            duplicate_te_after_round=params.duplicate_te_after_round,
            early_duplicate_qb_te_multiplier=params.early_duplicate_qb_te_multiplier,
            qb_wr_stack_bonus=params.qb_wr_stack_bonus,
            wr_rb_correlation_penalty=params.wr_rb_correlation_penalty,
            startable_slots=startable,
            starter_unavailable_rate=params.starter_unavailable_rate,
            depth_penalty_floor=params.depth_penalty_floor,
        ),
    )
