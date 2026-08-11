"""Engine acceptance tests (spec Phase 2): replacement level must shift correctly
between standard / half-PPR / full-PPR and between 10- and 12-team leagues."""

from api.engine.params import EngineParams
from api.engine.recommend import DraftContext, recommend
from api.engine.scoring import score_stats
from api.engine.tiers import assign_tiers
from api.engine.vor import (
    PoolPlayer,
    effective_starters,
    replacement_points,
    replacement_ranks,
)
from api.schemas import LeagueSettings

PARAMS = EngineParams()


def league(teams: int, rec: float) -> LeagueSettings:
    return LeagueSettings(
        scoring={"rec": rec, "rec_yd": 0.1, "rush_yd": 0.1, "rec_td": 6, "rush_td": 6},
        roster_slots={"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 2, "BN": 5},
        total_teams=teams,
    )


def linear_pool(
    position: str,
    count: int,
    top: float,
    step: float,
    adp_start: float = 1.0,
    adp_step: float = 1.0,
) -> list[PoolPlayer]:
    return [
        PoolPlayer(
            canonical_id=f"{position}{i}", name=f"{position} {i}", position=position,
            team=None, points=top - i * step, adp=adp_start + i * adp_step,
        )
        for i in range(count)
    ]


def deep_filler() -> list[PoolPlayer]:
    """Background board whose ADPs run past the pick under test, so the room
    still has realistic alternatives to spend its simulated picks on."""
    return linear_pool(
        "RB", 40, 200.0, 3.0, adp_start=20.0, adp_step=2.0
    ) + linear_pool("WR", 40, 200.0, 3.0, adp_start=21.0, adp_step=2.0)


def drafted_before(pool: list[PoolPlayer], pick: int) -> list[str]:
    """Ids of players the room would already have taken by `pick`.

    Fixtures that leave every early-ADP player on the board are not draft
    boards; the simulated room correctly spends its picks on those overdue
    bargains instead of the player under test.
    """
    return [p.canonical_id for p in pool if p.adp is not None and p.adp < pick]


# -- scoring ----------------------------------------------------------------


def test_scoring_formats_shift_points() -> None:
    stats = {"rec": 80.0, "rec_yd": 1000.0, "rec_td": 8.0}
    scoring = {"rec_yd": 0.1, "rec_td": 6.0}
    std = score_stats(stats, {**scoring, "rec": 0.0})
    half = score_stats(stats, {**scoring, "rec": 0.5})
    full = score_stats(stats, {**scoring, "rec": 1.0})
    assert std == 148.0
    assert half == std + 40.0  # 80 receptions x 0.5
    assert full == std + 80.0


def test_te_premium_applies_to_te_receptions_only() -> None:
    stats = {"rec": 80.0, "rec_yd": 900.0, "rec_td": 7.0}
    scoring = {"rec": 1.0, "rec_yd": 0.1, "rec_td": 6.0, "bonus_rec_te": 0.5}
    receiver = score_stats(stats, scoring, position="WR")
    tight_end = score_stats(stats, scoring, position="TE")
    assert tight_end == receiver + 40.0


# -- replacement level ------------------------------------------------------


def test_replacement_rank_shifts_with_team_count() -> None:
    r10 = replacement_ranks(league(10, 1.0), PARAMS)
    r12 = replacement_ranks(league(12, 1.0), PARAMS)
    # More teams -> replacement level sits deeper at every position.
    for pos in ("QB", "RB", "WR", "TE"):
        assert r12[pos] > r10[pos], pos
    # 12 teams, 2 RB + 0.40 x 2 FLEX = 2.8 starters -> rank 34
    assert r12["RB"] == round(12 * (2 + 2 * PARAMS.flex_split["RB"]))


def test_flex_share_deepens_replacement() -> None:
    starters = effective_starters(league(12, 1.0), PARAMS)
    assert starters["RB"] == 2 + 2 * PARAMS.flex_split["RB"]
    assert starters["WR"] == 2 + 2 * PARAMS.flex_split["WR"]
    assert starters["TE"] == 1 + 2 * PARAMS.flex_split["TE"]
    assert starters["QB"] == 1  # QB not FLEX-eligible in this league


def test_superflex_deepens_qb_replacement_demand() -> None:
    settings = league(12, 1.0).model_copy(
        update={
            "roster_slots": {
                "QB": 1,
                "RB": 2,
                "WR": 2,
                "TE": 1,
                "SUPER_FLEX": 1,
                "BN": 5,
            }
        }
    )
    starters = effective_starters(settings, PARAMS)
    assert starters["QB"] == 1 + PARAMS.superflex_split["QB"]
    assert replacement_ranks(settings, PARAMS)["QB"] == 22


def test_replacement_points_shift_with_team_count() -> None:
    pool = linear_pool("RB", 60, 300.0, 4.0)
    p10 = replacement_points(pool, replacement_ranks(league(10, 1.0), PARAMS))
    p12 = replacement_points(pool, replacement_ranks(league(12, 1.0), PARAMS))
    # Deeper replacement rank -> lower replacement points -> everyone's VOR rises.
    assert p12["RB"] < p10["RB"]


def test_ppr_shifts_replacement_between_positions() -> None:
    """The full acceptance case: WRs gain on RBs as rec scoring rises."""
    rb_stats = {"rush_yd": 1200.0, "rush_td": 9.0, "rec": 25.0, "rec_yd": 190.0}
    wr_stats = {"rec": 95.0, "rec_yd": 1150.0, "rec_td": 7.0}
    for rec_value, expect_wr_ahead in ((0.0, False), (1.0, True)):
        scoring = {"rec": rec_value, "rec_yd": 0.1, "rush_yd": 0.1, "rec_td": 6.0, "rush_td": 6.0}
        rb = score_stats(rb_stats, scoring)
        wr = score_stats(wr_stats, scoring)
        assert (wr > rb) is expect_wr_ahead, f"rec={rec_value}"


# -- tiers ------------------------------------------------------------------


def test_tier_breaks_on_gaps_not_bucket_size() -> None:
    points = [300.0, 298.0, 297.0, 280.0, 279.0, 278.0, 250.0]
    tiers = assign_tiers(points, PARAMS)
    assert tiers == [1, 1, 1, 2, 2, 2, 3]


def test_tiers_empty_and_single() -> None:
    assert assign_tiers([], PARAMS) == []
    assert assign_tiers([100.0], PARAMS) == [1]


# -- recommendation ---------------------------------------------------------


def full_pool() -> list[PoolPlayer]:
    """A 120-player board with globally unique ADPs (1-120), interleaved the way
    a real draft runs: RB/WR heavy early, QB/TE spread through."""
    return (
        linear_pool("RB", 40, 280.0, 5.0, adp_start=1.0, adp_step=3.0)
        + linear_pool("WR", 40, 270.0, 4.0, adp_start=2.0, adp_step=3.0)
        + linear_pool("QB", 20, 350.0, 6.0, adp_start=3.0, adp_step=6.0)
        + linear_pool("TE", 20, 200.0, 7.0, adp_start=6.0, adp_step=6.0)
    )


def ctx(drafted: list[str], mine: tuple[str, ...], pick: int, next_pick: int | None) -> DraftContext:
    return DraftContext(
        drafted_ids=frozenset(drafted),
        my_roster_positions=mine,
        current_pick=pick,
        my_next_pick=next_pick,
        recent_positions=(),
    )


def test_recommend_returns_top_and_alternates() -> None:
    rec = recommend(full_pool(), league(12, 1.0), ctx([], (), 1, 12), PARAMS)
    assert rec is not None
    assert rec.recommended_pick.value >= rec.alternates[0].value >= rec.alternates[1].value
    assert rec.recommended_pick.reason
    assert rec.picks_until_next == 11


def test_recommend_excludes_drafted() -> None:
    pool = full_pool()
    best_qb = "QB0"
    rec = recommend(pool, league(12, 1.0), ctx([best_qb], (), 2, 12), PARAMS)
    assert rec is not None
    assert best_qb not in [p.canonical_id for p in rec.board]


def test_roster_need_reorders_candidates() -> None:
    """With RB starters already filled, an equal-VOR WR should outrank the RB."""
    settings = league(12, 1.0)
    pool = full_pool()
    no_need = recommend(pool, settings, ctx([], (), 1, 24), PARAMS)
    rb_heavy = recommend(
        pool, settings, ctx([], ("RB", "RB", "RB", "RB"), 1, 24), PARAMS
    )
    assert no_need is not None and rb_heavy is not None
    rb_value_before = next(p.value for p in no_need.board if p.canonical_id == "RB0")
    rb_value_after = next(p.value for p in rb_heavy.board if p.canonical_id == "RB0")
    assert rb_value_after < rb_value_before  # surplus penalty applied


def test_positional_run_flagged() -> None:
    context = DraftContext(
        drafted_ids=frozenset(),
        my_roster_positions=(),
        current_pick=20,
        my_next_pick=30,
        recent_positions=("RB", "RB", "WR", "RB", "RB", "QB", "RB", "RB"),
    )
    rec = recommend(full_pool(), league(12, 1.0), context, PARAMS)
    assert rec is not None
    assert rec.run_alert is not None
    assert rec.run_alert.position == "RB"
    assert rec.run_alert.count == 6


def test_no_kicker_recommended_early() -> None:
    """Regression: a kicker with inflated VOR must not top the board mid-draft."""
    settings = LeagueSettings(
        scoring={"rec": 1.0, "rec_yd": 0.1, "rush_yd": 0.1, "rec_td": 6, "rush_td": 6},
        roster_slots={"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 2, "K": 1, "DEF": 1, "BN": 5},
        total_teams=12,
    )
    # Kicker pool with a big raw VOR spread (the bug scenario).
    pool = full_pool() + linear_pool("K", 15, 160.0, 10.0)
    context = DraftContext(
        drafted_ids=frozenset(),
        my_roster_positions=("RB", "RB", "WR"),
        current_pick=37,
        my_next_pick=45,
        recent_positions=(),
        my_remaining_picks=12,  # round 4 of 15
    )
    rec = recommend(pool, settings, context, PARAMS)
    assert rec is not None
    assert rec.recommended_pick.position != "K"
    assert all(p.position != "K" for p in rec.alternates)
    # Kickers exist but sink to the bottom of the board, marked held.
    kickers = [p for p in rec.board if p.position == "K"]
    assert kickers and all(p.held_for_later for p in kickers)
    non_held = [p for p in rec.board if not p.held_for_later]
    assert rec.board.index(kickers[0]) > rec.board.index(non_held[-1])


def test_kicker_recommended_at_endgame() -> None:
    """With 2 picks left and K+DEF unfilled, the hold lifts."""
    settings = LeagueSettings(
        scoring={"rec": 1.0},
        roster_slots={"QB": 1, "RB": 2, "WR": 2, "TE": 1, "K": 1, "DEF": 1, "BN": 5},
        total_teams=12,
    )
    pool = linear_pool("K", 15, 160.0, 3.0) + linear_pool("RB", 30, 150.0, 2.0)
    context = DraftContext(
        drafted_ids=frozenset(),
        my_roster_positions=("RB", "RB", "WR", "WR", "QB", "TE", "RB", "WR", "RB", "WR", "RB", "WR", "RB"),
        current_pick=157,
        my_next_pick=160,
        recent_positions=(),
        my_remaining_picks=2,
    )
    rec = recommend(pool, settings, context, PARAMS)
    assert rec is not None
    # Assert the hold this test is about. The roster carries six backs against
    # two startable slots in a league with no FLEX, so those rows are held on
    # their own account -- correctly, since no absence could start a seventh.
    assert not any(p.held_for_later for p in rec.board if p.position == "K")
    assert rec.recommended_pick.position == "K"  # open starter slot + need boost wins now


def test_second_kicker_stays_held_in_endgame() -> None:
    """Having K filled must keep backup kickers suppressed even late."""
    settings = LeagueSettings(
        scoring={"rec": 1.0},
        roster_slots={"QB": 1, "RB": 2, "WR": 2, "TE": 1, "K": 1, "DEF": 1, "BN": 5},
        total_teams=12,
    )
    pool = (
        linear_pool("K", 15, 160.0, 3.0)
        + linear_pool("DEF", 15, 150.0, 3.0)
        + linear_pool("RB", 30, 140.0, 2.0)
    )
    context = DraftContext(
        drafted_ids=frozenset(),
        my_roster_positions=(
            "RB", "RB", "WR", "WR", "QB", "TE", "RB", "WR", "RB", "WR", "RB", "WR", "K",
        ),
        current_pick=157,
        my_next_pick=160,
        recent_positions=(),
        my_remaining_picks=2,
    )
    rec = recommend(pool, settings, context, PARAMS)
    assert rec is not None
    kickers = [p for p in rec.board if p.position == "K"]
    assert kickers and all(p.held_for_later for p in kickers)
    assert rec.recommended_pick.position != "K"
    assert rec.room_pick.position != "K"


def test_position_discount_halves_streaming_positions() -> None:
    settings = LeagueSettings(
        scoring={"rec": 1.0},
        roster_slots={"QB": 1, "RB": 2, "WR": 2, "TE": 1, "K": 1, "BN": 5},
        total_teams=12,
    )
    pool = linear_pool("RB", 40, 280.0, 5.0) + linear_pool("K", 15, 160.0, 10.0)
    rec = recommend(
        pool, settings,
        DraftContext(
            drafted_ids=frozenset(), my_roster_positions=(), current_pick=1,
            my_next_pick=13, recent_positions=(), my_remaining_picks=None,
        ),
        PARAMS,
    )
    assert rec is not None
    # K replacement rank is 12 -> raw VOR of K0 is (160 - 160+10*11) = 110.
    k0 = next(p for p in rec.board if p.canonical_id == "K0")
    assert k0.vor == 110.0 * PARAMS.position_discount["K"]


def test_scarcity_prefers_player_who_wont_survive() -> None:
    """Regression (the D'Andre Swift case): with near-equal value, take the
    player the market says will be gone; the survivor waits a round."""
    settings = league(12, 1.0)
    # "survivor" is worth marginally more but lasts (adp 65 vs my pick 51);
    # "riser" (adp 47) will not be there.
    pool = [
        PoolPlayer(canonical_id="survivor", name="Survivor", position="RB", team=None,
                   points=250.0, adp=65.0),
        PoolPlayer(canonical_id="riser", name="Riser", position="WR", team=None,
                   points=249.0, adp=47.0),
    ] + deep_filler()
    context = DraftContext(
        drafted_ids=frozenset(drafted_before(pool, 46)),
        my_roster_positions=("RB", "WR", "RB"),
        current_pick=46,
        my_next_pick=51,
        recent_positions=(),
        my_remaining_picks=12,
    )
    rec = recommend(pool, settings, context, PARAMS)
    assert rec is not None
    assert rec.recommended_pick.canonical_id == "riser"
    # Survival drives it. Assert the relationship rather than a magic constant:
    # the number now comes from the room simulation, which also weighs the
    # competing board, so its absolute level legitimately differs from the bare
    # ADP curve.
    riser = next(p for p in rec.board if p.canonical_id == "riser")
    survivor = next(p for p in rec.board if p.canonical_id == "survivor")
    assert riser.survival < 0.5  # adp 47, my pick 51: more likely gone than not
    assert survivor.survival > 0.9  # adp 65, safely past my pick
    assert riser.survival < survivor.survival
    assert riser.survival < survivor.survival


def test_big_value_gap_beats_scarcity_when_both_are_scarce() -> None:
    """Value wins only when waiting cannot recover it.

    An earlier version of this test asserted the stud should win even at ADP 65
    with the next turn at pick 51 — but there the two-turn optimum really is to
    take the scarce player now and the stud next turn, because a 98%-survival
    stud is still there. The engine was right and the test was wrong. The real
    principle: when BOTH players will be gone, take the more valuable one.
    """
    settings = league(12, 1.0)
    pool = [
        PoolPlayer(canonical_id="stud", name="Stud", position="RB", team=None,
                   points=280.0, adp=47.0),
        PoolPlayer(canonical_id="risky", name="Risky", position="WR", team=None,
                   points=230.0, adp=46.0),
    ] + deep_filler()
    rec = recommend(pool, settings, DraftContext(
        drafted_ids=frozenset(drafted_before(pool, 46)),
        my_roster_positions=("RB", "WR", "RB"),
        current_pick=46, my_next_pick=51, recent_positions=(), my_remaining_picks=12,
    ), PARAMS)
    assert rec is not None
    assert rec.recommended_pick.canonical_id == "stud"


def test_scarce_first_when_the_stud_will_survive() -> None:
    """The converse, and the whole point of two-turn reasoning: if the better
    player demonstrably lasts, bank the one who will not and take him next."""
    settings = league(12, 1.0)
    pool = [
        PoolPlayer(canonical_id="stud", name="Stud", position="RB", team=None,
                   points=280.0, adp=65.0),  # ~98% to survive to pick 51
        PoolPlayer(canonical_id="fleeting", name="Fleeting", position="WR", team=None,
                   points=230.0, adp=47.0),
    ] + deep_filler()
    rec = recommend(pool, settings, DraftContext(
        drafted_ids=frozenset(drafted_before(pool, 46)),
        my_roster_positions=("RB", "WR", "RB"),
        current_pick=46, my_next_pick=51, recent_positions=(), my_remaining_picks=12,
    ), PARAMS)
    assert rec is not None
    stud = next(r for r in rec.board if r.canonical_id == "stud")
    assert stud.survival > 0.9
    assert rec.recommended_pick.canonical_id == "fleeting"


def test_room_pick_is_live_urgency_and_can_differ() -> None:
    """The two answers are independent: roster utility versus room urgency."""
    settings = league(12, 1.0)
    pool = [
        # Our projections love him; the market does not (late ADP).
        PoolPlayer(canonical_id="ourguy", name="Our Guy", position="WR", team=None,
                   points=300.0, adp=80.0),
        # The room's next pick; we think he is ordinary.
        PoolPlayer(canonical_id="chalk", name="Chalk", position="RB", team=None,
                   points=215.0, adp=12.0),
    ] + linear_pool("RB", 40, 200.0, 3.0) + linear_pool("WR", 40, 195.0, 3.0)
    rec = recommend(pool, settings, DraftContext(
        drafted_ids=frozenset(), my_roster_positions=(), current_pick=12,
        my_next_pick=20, recent_positions=(), my_remaining_picks=14,
    ), PARAMS)
    assert rec is not None
    assert rec.recommended_pick.canonical_id == "ourguy"
    assert rec.room_pick.reason
    assert rec.room_pick.disappearance_probability > 0
    # Alternates never duplicate the room-aware pick.
    assert all(a.canonical_id != rec.room_pick.canonical_id for a in rec.alternates)


def test_picks_agree_when_value_matches_chalk() -> None:
    rec = recommend(full_pool(), league(12, 1.0), ctx([], (), 1, 12), PARAMS)
    assert rec is not None
    if rec.recommended_pick.canonical_id == rec.room_pick.canonical_id:
        assert rec.picks_agree


def test_reason_shows_position_rank_and_market() -> None:
    rec = recommend(full_pool(), league(12, 1.0), ctx([], (), 1, 12), PARAMS)
    assert rec is not None
    assert "proj " in rec.recommended_pick.reason
    assert "market ADP" in rec.recommended_pick.reason
    assert rec.recommended_pick.pos_rank >= 1


def test_survival_flags_players_gone_by_next_turn() -> None:
    rec = recommend(full_pool(), league(12, 1.0), ctx([], (), 1, 24), PARAMS)
    assert rec is not None
    # Top players have ADP well before pick 24: unlikely to survive.
    assert rec.recommended_pick.at_risk
    assert rec.recommended_pick.survival < PARAMS.at_risk_threshold
    assert rec.tier_gone_risk >= 1


def test_te_replacement_not_inflated_by_flex() -> None:
    """Regression: a generous TE flex share pushed TE replacement to TE16,
    which drafted elite TEs a full round early."""
    settings = league(12, 1.0)  # 2 FLEX
    starters = effective_starters(settings, PARAMS)
    assert starters["TE"] <= 1.2
    assert replacement_ranks(settings, PARAMS)["TE"] <= 13


def test_scarcity_is_one_coherent_number() -> None:
    """survival and disappearance describe the same event; a card that shows
    both must never show two different answers."""
    pool = full_pool()
    rec = recommend(pool, league(12, 1.0), ctx([], (), 25, 32), PARAMS)
    assert rec is not None
    for player in rec.board[:40]:
        assert abs((player.survival + player.disappearance_probability) - 1.0) < 1e-6, (
            f"{player.name}: survival {player.survival} + "
            f"disappearance {player.disappearance_probability} != 1"
        )
    # And the simulation, not the raw ADP curve, is what drives it.
    simulated = [p for p in rec.board if p.canonical_id in rec.simulation.disappearance_probability]
    assert simulated, "expected the simulation to cover the top of the board"
    for player in simulated[:10]:
        assert (
            abs(
                player.disappearance_probability
                - rec.simulation.disappearance_probability[player.canonical_id]
            )
            < 1e-3
        )
