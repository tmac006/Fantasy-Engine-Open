"""Focused deterministic coverage for the dynamic draft engine."""

from api.engine.params import EngineParams
from api.engine.recommend import DraftContext, recommend
from api.engine.simulation import TeamDraftContext, simulate_next_turn
from api.engine.vor import PoolPlayer
from api.schemas import LeagueSettings


def league() -> LeagueSettings:
    return LeagueSettings(
        scoring={"rec": 1.0},
        roster_slots={"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "BN": 6},
        total_teams=12,
    )


def position_pool(position: str, count: int, top: float, step: float) -> list[PoolPlayer]:
    return [
        PoolPlayer(
            canonical_id=f"{position}{index}",
            name=f"{position} {index}",
            position=position,
            team=None,
            points=top - index * step,
            adp=float(index + 1),
        )
        for index in range(count)
    ]


def base_pool() -> list[PoolPlayer]:
    return (
        position_pool("RB", 40, 275.0, 4.0)
        + position_pool("WR", 40, 270.0, 3.8)
        + position_pool("QB", 20, 330.0, 6.0)
        + position_pool("TE", 20, 205.0, 3.0)
    )


def test_seeded_simulation_is_deterministic_and_uses_roster_need() -> None:
    players = [
        PoolPlayer("rb", "RB", "RB", None, 100.0, adp=11.0),
        PoolPlayer("wr", "WR", "WR", None, 100.0, adp=11.0),
    ]
    kwargs = {
        "current_pick": 10,
        "next_pick": 12,
        "roster_slots": {"RB": 1, "WR": 1, "BN": 3},
        "params": EngineParams(simulation_trials=400, simulation_seed=91),
        "teams": (TeamDraftContext("team-a", roster_positions=("RB",)),),
        "upcoming_team_ids": ("team-a",),
    }
    first = simulate_next_turn(players, {"rb": 10.0, "wr": 10.0}, **kwargs)
    second = simulate_next_turn(players, {"rb": 10.0, "wr": 10.0}, **kwargs)

    assert first == second
    assert first.disappearance_probability["wr"] > first.disappearance_probability["rb"]


def test_recent_room_run_changes_opponent_probabilities() -> None:
    players = [
        PoolPlayer("rb", "RB", "RB", None, 100.0, adp=21.0),
        PoolPlayer("wr", "WR", "WR", None, 100.0, adp=21.0),
    ]
    params = EngineParams(simulation_trials=500, simulation_seed=17)
    common = {
        "available": players,
        "values": {"rb": 10.0, "wr": 10.0},
        "current_pick": 20,
        "next_pick": 22,
        "roster_slots": {"RB": 1, "WR": 1, "BN": 3},
        "params": params,
    }
    wr_run = simulate_next_turn(**common, recent_positions=("WR",) * 8)
    rb_run = simulate_next_turn(**common, recent_positions=("RB",) * 8)

    assert (
        wr_run.disappearance_probability["wr"]
        > rb_run.disappearance_probability["wr"]
    )


def test_simulation_includes_current_opponent_pick_but_skips_user_pick() -> None:
    players = position_pool("WR", 8, 200.0, 2.0)
    common = {
        "available": players,
        "values": {player.canonical_id: player.points for player in players},
        "current_pick": 10,
        "next_pick": 13,
        "roster_slots": {"WR": 2, "BN": 4},
        "params": EngineParams(simulation_trials=8),
    }
    on_clock = simulate_next_turn(**common, current_pick_is_user=True)
    waiting = simulate_next_turn(**common, current_pick_is_user=False)
    assert on_clock.picks_simulated == 2
    assert waiting.picks_simulated == 3


def test_simulation_bounds_real_world_player_pool() -> None:
    players = position_pool("WR", 500, 300.0, 0.2)
    result = simulate_next_turn(
        players,
        {player.canonical_id: player.points for player in players},
        current_pick=25,
        next_pick=36,
        roster_slots={"WR": 2, "BN": 6},
        params=EngineParams(simulation_trials=2, simulation_pool_limit=80),
    )
    assert len(result.disappearance_probability) <= 80


def test_response_serializes_only_new_pick_names_and_two_turn_utility() -> None:
    params = EngineParams(simulation_trials=24, simulation_candidate_limit=8)
    result = recommend(
        base_pool(),
        league(),
        DraftContext(frozenset(), (), 1, 24, ()),
        params,
    )
    assert result is not None
    payload = result.model_dump()

    assert "recommended_pick" in payload and "room_pick" in payload
    # The aliases are gone from the model entirely, not merely from the dump.
    assert "value_pick" not in payload and "market_pick" not in payload and "top" not in payload
    assert not hasattr(result, "value_pick") and not hasattr(result, "top")
    assert result.recommended_pick is result.recommended_pick
    assert result.room_pick is result.room_pick
    pick = result.recommended_pick
    assert abs(
        pick.score
        - (
            pick.value
            + params.next_turn_discount * pick.expected_next_turn_value
            - pick.reach_penalty
        )
    ) <= 0.2


def test_te_waits_when_flat_but_elite_cliff_can_win() -> None:
    params = EngineParams(simulation_trials=24, simulation_candidate_limit=8)
    flat = base_pool()
    flat_result = recommend(flat, league(), DraftContext(frozenset(), (), 1, 24, ()), params)
    assert flat_result is not None
    assert flat_result.recommended_pick.position != "TE"

    elite = [p for p in flat if p.position != "TE"] + [
        PoolPlayer("elite-te", "Elite TE", "TE", None, 330.0, adp=3.0),
        *position_pool("TE", 19, 190.0, 2.0),
    ]
    elite_result = recommend(elite, league(), DraftContext(frozenset(), (), 1, 24, ()), params)
    assert elite_result is not None
    assert elite_result.recommended_pick.canonical_id == "elite-te"


def test_likely_survivor_is_penalized_as_an_early_reach() -> None:
    near = PoolPlayer("near", "Near", "WR", None, 245.0, adp=42.0)
    later = PoolPlayer("later", "Later", "WR", None, 245.0, adp=70.0)
    result = recommend(
        [near, later, *base_pool()],
        league(),
        DraftContext(frozenset(), ("RB", "RB", "WR"), 40, 51, ()),
        EngineParams(simulation_trials=16, simulation_candidate_limit=6),
    )
    assert result is not None
    by_id = {player.canonical_id: player for player in result.board}
    assert by_id["near"].reach_penalty == 0.0
    assert by_id["later"].reach_penalty >= 4.5
    assert by_id["near"].score > by_id["later"].score


def test_open_qb_slot_is_not_boosted_early_in_single_qb() -> None:
    result = recommend(
        base_pool(),
        league(),
        DraftContext(frozenset(), ("RB", "RB", "WR"), 40, 51, ()),
        EngineParams(simulation_trials=8, simulation_candidate_limit=4),
    )
    assert result is not None
    quarterback = next(player for player in result.board if player.position == "QB")
    assert quarterback.value == quarterback.vor


def test_backup_tight_end_is_held_until_late_rounds() -> None:
    # Round 11 (pick 122 in 12-team) is still before duplicate_te_after_round=13.
    result = recommend(
        base_pool(),
        league(),
        DraftContext(frozenset(), ("RB", "WR", "TE", "RB", "WR"), 122, 131, ()),
        EngineParams(simulation_trials=8, simulation_candidate_limit=4),
    )
    assert result is not None
    tight_ends = [player for player in result.board if player.position == "TE"]
    assert tight_ends and all(player.held_for_later for player in tight_ends)
    assert result.recommended_pick.position != "TE"
    assert result.room_pick.position != "TE"


def test_third_tight_end_stays_held_even_late() -> None:
    result = recommend(
        base_pool(),
        league(),
        DraftContext(
            frozenset(),
            ("RB", "WR", "TE", "RB", "WR", "TE", "QB"),
            150,
            159,
            (),
        ),
        EngineParams(simulation_trials=8, simulation_candidate_limit=4),
    )
    assert result is not None
    tight_ends = [player for player in result.board if player.position == "TE"]
    assert tight_ends and all(player.held_for_later for player in tight_ends)
    assert result.recommended_pick.position != "TE"


def test_elite_backup_tight_end_can_win_when_clearly_best() -> None:
    params = EngineParams(
        simulation_trials=8,
        simulation_candidate_limit=4,
        duplicate_te_exception_margin=5.0,
    )
    # Sparse leftover board: only a truly dominant TE should clear the hold.
    elite = PoolPlayer("elite-te", "Elite TE", "TE", None, 280.0, adp=55.0)
    pool = [
        elite,
        *position_pool("RB", 8, 190.0, 2.0),
        *position_pool("WR", 8, 185.0, 2.0),
        *position_pool("QB", 4, 220.0, 4.0),
        *position_pool("TE", 6, 170.0, 3.0),
    ]
    result = recommend(
        pool,
        league(),
        DraftContext(frozenset(), ("RB", "WR", "TE"), 50, 71, ()),
        params,
    )
    assert result is not None
    ranked = next(player for player in result.board if player.canonical_id == "elite-te")
    assert not ranked.held_for_later
    assert result.recommended_pick.canonical_id == "elite-te"


def test_second_single_qb_is_held_out_of_early_recommendations() -> None:
    result = recommend(
        base_pool(),
        league(),
        DraftContext(frozenset(), ("RB", "WR", "QB"), 28, 45, ()),
        EngineParams(simulation_trials=8, simulation_candidate_limit=4),
    )
    assert result is not None
    quarterbacks = [player for player in result.board if player.position == "QB"]
    assert quarterbacks and all(player.held_for_later for player in quarterbacks)
    assert result.recommended_pick.position != "QB"
    assert result.room_pick.position != "QB"


def test_qb_wr_stack_gets_bonus_and_wr_rb_same_team_is_penalized() -> None:
    params = EngineParams(simulation_trials=8, simulation_candidate_limit=4)
    stacked_wr = PoolPlayer("stack-wr", "Stack WR", "WR", "KC", 240.0, adp=50.0)
    anti_wr = PoolPlayer("anti-wr", "Anti WR", "WR", "SF", 240.0, adp=50.0)
    neutral_wr = PoolPlayer("neutral-wr", "Neutral WR", "WR", "DAL", 240.0, adp=50.0)
    result = recommend(
        [stacked_wr, anti_wr, neutral_wr, *base_pool()],
        league(),
        DraftContext(
            frozenset(),
            ("QB", "RB"),
            36,
            49,
            (),
            my_roster_nfl_teams=("KC", "SF"),
        ),
        params,
    )
    assert result is not None
    by_id = {player.canonical_id: player for player in result.board}
    assert by_id["stack-wr"].value == round(
        by_id["neutral-wr"].value + params.qb_wr_stack_bonus,
        1,
    )
    assert by_id["anti-wr"].value == round(
        by_id["neutral-wr"].value - params.wr_rb_correlation_penalty,
        1,
    )
    assert any("QB-WR stack" in factor for factor in by_id["stack-wr"].analysis.model_factors)
    assert any(
        "WR-RB same-team anti-correlation" in factor
        for factor in by_id["anti-wr"].analysis.model_factors
    )


def test_analysis_and_hidden_gems_preserve_optional_context() -> None:
    sleeper = PoolPlayer(
        "sleeper",
        "Sleeper",
        "WR",
        "SEA",
        245.0,
        adp=105.0,
        projection_consensus=245.0,
        projection_low=190.0,
        projection_high=285.0,
        usage={"target_share": 0.22, "route_rate": 0.86},
        team_context={"pass_rate": 0.59},
        status="active",
        risk_flags=("First season in a full-time role.",),
        freshness={"usage": "2026-08-01T00:00:00Z"},
    )
    result = recommend(
        [sleeper, *base_pool()],
        league(),
        DraftContext(frozenset(), ("RB", "WR"), 36, 49, ()),
        EngineParams(simulation_trials=24, simulation_candidate_limit=8),
    )
    assert result is not None
    ranked = next(player for player in result.board if player.canonical_id == "sleeper")

    assert ranked.analysis.projection_consensus == 245.0
    assert not ranked.analysis.projection_is_placeholder
    assert "target share: 0.22" in ranked.analysis.usage
    assert "pass rate: 0.59" in ranked.analysis.team_context
    assert ranked.analysis.data_freshness == "usage: 2026-08-01T00:00:00Z"
    assert len(result.hidden_gems) == 1
    gem = result.hidden_gems[0]
    assert gem.player.canonical_id == "sleeper"
    assert gem.target_pick_min < gem.target_pick_max
    assert gem.upside and gem.risk


def test_verified_injury_and_usage_context_adjust_value() -> None:
    healthy = PoolPlayer(
        "healthy",
        "Healthy",
        "WR",
        "KC",
        240.0,
        adp=40.0,
        usage={"snap_pct": 0.9, "target_share": 0.24},
        team_context={"epa_per_play": 0.12},
        status="Active",
    )
    injured = PoolPlayer(
        "injured",
        "Injured",
        "WR",
        "KC",
        240.0,
        adp=40.0,
        status="Active; injury: Out",
        risk_flags=("Sleeper injury designation: Out.",),
    )
    result = recommend(
        [healthy, injured, *base_pool()],
        league(),
        DraftContext(frozenset(), ("RB",), 30, 43, ()),
        EngineParams(simulation_trials=16, simulation_candidate_limit=6),
    )
    assert result is not None
    by_id = {player.canonical_id: player for player in result.board}
    assert by_id["healthy"].value > by_id["injured"].value
    assert any("context adjustment" in factor.lower() for factor in by_id["healthy"].analysis.model_factors)


def test_simulated_disappearance_tracks_analytic_survival() -> None:
    """The simulation and the ADP model estimate the SAME event, so they must
    agree. A too-wide market decay makes the simulated room draft near-randomly,
    which silently understates who is about to leave the board.
    """
    from api.engine.survival import estimate_stdev, survival_probability

    params = EngineParams()
    current_pick, next_pick = 46, 51
    # A realistic near-term board: consensus names spaced ~1 pick apart, plus a
    # long tail the room is unlikely to reach for.
    pool = [
        PoolPlayer(
            canonical_id=f"p{i}",
            name=f"Player {i}",
            position=["RB", "WR", "TE", "QB"][i % 4],
            team=None,
            points=200.0 - i,
            adp=float(current_pick + i),
        )
        for i in range(60)
    ]
    values = {p.canonical_id: 100.0 - i for i, p in enumerate(pool)}
    sim = simulate_next_turn(
        pool,
        values,
        current_pick=current_pick,
        next_pick=next_pick,
        roster_slots={"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 2, "BN": 5},
        params=params,
        trials=400,
    )

    def conditional_gone(player: PoolPlayer) -> float:
        """P(gone by next_pick | still available now)."""
        here = survival_probability(player.adp, player.adp_stdev, current_pick)
        later = survival_probability(player.adp, player.adp_stdev, next_pick)
        return 1.0 - later / here if here > 1e-9 else 1.0

    # Check the names actually in play — the ones the room is choosing among.
    errors = [
        abs(sim.disappearance_probability[p.canonical_id] - conditional_gone(p))
        for p in pool[:8]
    ]
    assert max(errors) < 0.30, f"simulation diverges from ADP model: {errors}"
    assert sum(errors) / len(errors) < 0.18

    # The player on the clock must be far likelier to vanish than a late one.
    assert sim.disappearance_probability["p0"] > 0.4
    assert sim.disappearance_probability["p0"] > 3 * sim.disappearance_probability["p40"]
    assert estimate_stdev(50.0) > 0  # guards the shared spread model
