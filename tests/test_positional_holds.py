"""Positional sanity in the draft engine.

Each test here encodes a rule a human drafter would state flatly: never a third
quarterback as the headline pick, never a second defense, the kicker you finally
take is the best one, and value math must not invert when the late board goes
negative. All were violated in live ESPN drafts before being fixed.
"""

from api.engine.params import EngineParams
from api.engine.recommend import (
    DraftContext,
    _depth_multiplier,
    _held_for_later,
    recommend,
)
from api.engine.vor import PoolPlayer, startable_slots
from api.schemas import LeagueSettings

PARAMS = EngineParams()

SETTINGS = LeagueSettings(
    scoring={"rec": 1.0},
    roster_slots={"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "K": 1, "DEF": 1, "BN": 7},
    total_teams=12,
)


def player(
    cid: int, name: str, pos: str, points: float, adp: float | None = None
) -> PoolPlayer:
    return PoolPlayer(
        canonical_id=str(cid),
        name=name,
        position=pos,
        team=None,
        points=points,
        adp=adp,
        adp_stdev=None,
        platform_ids={},
    )


def late_pool() -> list[PoolPlayer]:
    """A realistic board: positions interleave in ADP the way real drafts run.

    ADPs must interleave -- give each position a sequential block and "draft the
    top 120" swallows entire positions, making every hold test pass vacuously.
    (The same fixture mistake once hid a real bug in the survival tests.)
    """
    pool: list[PoolPlayer] = []
    cid = 1
    # (position, count, top points, first ADP, ADP gap): tuned so pick 121 has
    # roughly QB13+, RB27+, WR27+, TE9+ remaining -- a normal round-11 board.
    shape = (
        ("QB", 22, 380.0, 20.0, 9.0),
        ("RB", 55, 300.0, 2.0, 4.5),
        ("WR", 60, 290.0, 3.0, 4.5),
        ("TE", 20, 230.0, 15.0, 12.0),
    )
    for pos, count, top, first, gap in shape:
        for i in range(count):
            pool.append(
                player(cid, f"{pos}{i + 1}", pos, top - i * 5.0, adp=first + i * gap)
            )
            cid += 1
    for i in range(12):
        pool.append(player(cid, f"K{i + 1}", "K", 120.0 - i * 4.0, adp=165.0 + i))
        cid += 1
    for i in range(12):
        pool.append(player(cid, f"DEF{i + 1}", "DEF", 110.0 - i * 4.0, adp=160.0 + i))
        cid += 1
    return pool


def drafted_top(pool: list[PoolPlayer], n: int, keep: set[str] = frozenset()) -> frozenset[str]:
    byadp = sorted((p for p in pool if p.adp is not None), key=lambda p: p.adp)
    out: list[str] = []
    for p in byadp:
        if p.name in keep:
            continue
        out.append(p.canonical_id)
        if len(out) >= n:
            break
    return frozenset(out)


class TestThirdQuarterback:
    def test_never_the_headline_pick(self) -> None:
        """Round 11, two QBs rostered: a third QB is a wasted bench slot."""
        pool = late_pool()
        ctx = DraftContext(
            drafted_ids=drafted_top(pool, 120),
            my_roster_positions=("QB", "RB", "RB", "WR", "WR", "TE", "WR", "RB", "WR", "QB"),
            current_pick=121,
            my_next_pick=144,
            recent_positions=(),
            my_remaining_picks=6,
        )
        rec = recommend(pool, SETTINGS, ctx, PARAMS)
        assert rec is not None
        assert rec.recommended_pick.position != "QB"
        assert rec.room_pick.position != "QB"
        assert all(alt.position != "QB" for alt in rec.alternates)
        # The outcome above can pass by accident of fixture calibration; the
        # mechanism cannot: with two QBs rostered every remaining QB is held,
        # in any round, whatever the board looks like.
        qb_rows = [r for r in rec.board if r.position == "QB"]
        assert qb_rows, "fixture must leave QBs on the board"
        assert all(r.held_for_later for r in qb_rows)

    def test_second_qb_is_still_held_in_the_middle_rounds(self) -> None:
        """Round 11 is no longer late enough.

        The hold moved from round 10 to 14 on measurement: skipping the backup
        entirely cost 0.1 finished-lineup points out of 1985 while cutting the
        share of drafts that spent a pick on one from 44% to 11%.
        """
        pool = late_pool()
        ctx = DraftContext(
            drafted_ids=drafted_top(pool, 120),
            my_roster_positions=("QB", "RB", "RB", "WR", "WR", "TE", "WR", "RB", "WR", "RB"),
            current_pick=121,
            my_next_pick=144,
            recent_positions=(),
            my_remaining_picks=6,
        )
        rec = recommend(pool, SETTINGS, ctx, PARAMS)
        assert rec is not None
        qb_rows = [r for r in rec.board if r.position == "QB"]
        assert qb_rows, "fixture must leave QBs on the board"
        assert all(r.held_for_later for r in qb_rows)

    def test_second_qb_is_allowed_once_the_hold_lifts(self) -> None:
        """The rule itself, at the boundary.

        Asserted through `_held_for_later` rather than a board outcome: the
        fixture drafts its quarterbacks out before round 14, so a board-level
        check there passes vacuously whatever the rule says.
        """
        have = {"QB": 1.0, "RB": 4.0, "WR": 4.0, "TE": 1.0}
        startable = startable_slots(SETTINGS, PARAMS)

        def held_at(round_no: int) -> bool:
            ctx = DraftContext(
                drafted_ids=frozenset(),
                my_roster_positions=("QB",),
                current_pick=(round_no - 1) * SETTINGS.total_teams + 1,
                my_next_pick=None,
                recent_positions=(),
                my_remaining_picks=4,
            )
            return _held_for_later("QB", SETTINGS, ctx, have, startable, PARAMS)

        assert held_at(PARAMS.duplicate_qb_te_after_round - 1)
        assert not held_at(PARAMS.duplicate_qb_te_after_round)
        assert PARAMS.duplicate_qb_te_after_round == 14


class TestSecondDefense:
    def test_defense_held_once_rostered(self) -> None:
        pool = late_pool()
        ctx = DraftContext(
            drafted_ids=drafted_top(pool, 150),
            my_roster_positions=(
                "QB", "RB", "RB", "WR", "WR", "TE", "WR", "RB", "WR", "RB", "WR", "RB", "DEF",
            ),
            current_pick=151,
            my_next_pick=168,
            recent_positions=(),
            my_remaining_picks=3,
        )
        rec = recommend(pool, SETTINGS, ctx, PARAMS)
        assert rec is not None
        assert rec.recommended_pick.position != "DEF"
        for row in rec.board:
            if row.position == "DEF":
                assert row.held_for_later, "second defense must never surface"


class TestKickerEndgame:
    def test_best_kicker_by_projection_is_the_pick(self) -> None:
        """When the endgame finally opens K, take the best one, not a random one."""
        pool = late_pool()
        ctx = DraftContext(
            drafted_ids=drafted_top(pool, 168, keep={"K1", "K2", "K3", "DEF1", "DEF2"}),
            my_roster_positions=(
                "QB", "RB", "RB", "WR", "WR", "TE", "WR", "RB", "WR", "RB", "WR", "RB", "WR", "RB",
            ),
            current_pick=169,
            my_next_pick=180,
            recent_positions=(),
            my_remaining_picks=2,
        )
        rec = recommend(pool, SETTINGS, ctx, PARAMS)
        assert rec is not None
        assert rec.recommended_pick.position in ("K", "DEF")
        if rec.recommended_pick.position == "K":
            best = max(
                (r for r in rec.board if r.position == "K"), key=lambda r: r.points
            )
            assert rec.recommended_pick.points == best.points


class TestKickerDisagreement:
    def test_source_disagreement_does_not_reorder_kickers(self) -> None:
        """A 15-point source disagreement on the better kicker must not hand the
        pick to a worse consensus, via the uncertainty penalty or anything else.
        (Adversarial-review case: K discount halves projection gaps, so noise
        terms of a couple of points can flip an ordering.)"""
        pool = late_pool()
        contested = PoolPlayer(
            canonical_id="900",
            name="Contested K",
            position="K",
            team=None,
            points=112.0,
            adp=166.0,
            adp_stdev=None,
            projection_low=95.0,
            projection_high=125.0,
            projection_stdev=15.0,
            platform_ids={},
        )
        agreed = PoolPlayer(
            canonical_id="901",
            name="Agreed K",
            position="K",
            team=None,
            points=104.0,
            adp=167.0,
            adp_stdev=None,
            projection_stdev=0.0,
            platform_ids={},
        )
        pool = [p for p in pool if p.position != "K"] + [contested, agreed]
        ctx = DraftContext(
            drafted_ids=drafted_top(pool, 168, keep={"Contested K", "Agreed K", "DEF1"}),
            my_roster_positions=(
                "QB", "RB", "RB", "WR", "WR", "TE", "WR", "RB", "WR", "RB", "WR", "RB", "WR", "DEF",
            ),
            current_pick=169,
            my_next_pick=None,
            recent_positions=(),
            my_remaining_picks=1,
        )
        rec = recommend(pool, SETTINGS, ctx, PARAMS)
        assert rec is not None
        assert rec.recommended_pick.position == "K"
        assert rec.recommended_pick.name == "Contested K", (
            "the 8-point-better kicker lost to noise terms"
        )


class TestPositionCaps:
    def test_capped_position_never_surfaces_anywhere(self) -> None:
        """League cap reached: not the pick, not the room pick, not an alternate,
        and never released by the TE valve."""
        capped = SETTINGS.model_copy(update={"position_limits": {"TE": 1}})
        pool = late_pool()
        ctx = DraftContext(
            drafted_ids=drafted_top(pool, 120),
            my_roster_positions=("QB", "RB", "RB", "WR", "WR", "TE", "WR", "RB", "WR", "RB"),
            current_pick=121,
            my_next_pick=144,
            recent_positions=(),
            my_remaining_picks=6,
        )
        rec = recommend(pool, capped, ctx, PARAMS)
        assert rec is not None
        assert rec.recommended_pick.position != "TE"
        assert rec.room_pick.position != "TE"
        assert all(alt.position != "TE" for alt in rec.alternates)
        assert all(r.held_for_later for r in rec.board if r.position == "TE")


class TestNegativeVorMultipliers:
    def test_surplus_penalty_still_penalizes_below_replacement(self) -> None:
        """A surplus-position player below replacement must rank worse than an
        equally-bad player at a position with remaining need, not better.

        Multiplying a negative VOR by a penalty factor < 1 shrinks it toward
        zero, which silently turns the penalty into a reward.
        """
        pool = late_pool()
        ctx = DraftContext(
            drafted_ids=drafted_top(pool, 120),
            my_roster_positions=("QB", "RB", "RB", "WR", "WR", "TE", "WR", "RB", "WR", "QB"),
            current_pick=121,
            my_next_pick=144,
            recent_positions=(),
            my_remaining_picks=6,
        )
        rec = recommend(pool, SETTINGS, ctx, PARAMS)
        assert rec is not None
        # QB17 and RB41 sit at comparable raw VOR deficits; the roster has two
        # QBs and two open FLEX-eligible bench needs, so the RB must not lose
        # the value comparison because a penalty flattered the QB's negative VOR.
        qb_surplus = [r for r in rec.board if r.position == "QB" and r.vor < 0]
        rb_needed = [r for r in rec.board if r.position == "RB" and r.vor < 0]
        assert qb_surplus and rb_needed
        # Compare closest pair by raw VOR.
        qb = max(qb_surplus, key=lambda r: r.vor)
        rb = min(rb_needed, key=lambda r: abs(r.vor - qb.vor))
        if abs(qb.vor - rb.vor) < 3.0:
            assert rb.value >= qb.value, (
                f"needed {rb.name} (vor {rb.vor}) valued below surplus "
                f"{qb.name} (vor {qb.vor})"
            )


class TestMustFillSlots:
    """Endgame promotion must count every open starting slot, and must leave
    promoted rows visible on the board rather than merely outranking the flag."""

    def _endgame_ctx(self, pool: list[PoolPlayer], remaining: int) -> DraftContext:
        return DraftContext(
            drafted_ids=drafted_top(pool, 150, keep={"K1", "K2", "DEF1", "DEF2"}),
            my_roster_positions=(
                "QB", "RB", "RB", "WR", "WR", "TE", "RB", "WR", "RB", "WR", "RB", "WR", "RB",
            ),
            current_pick=151,
            my_next_pick=168,
            recent_positions=(),
            my_remaining_picks=remaining,
        )

    def test_promoted_rows_are_not_left_marked_held(self) -> None:
        """A promoted row still flagged held is filtered out of the board table,
        so the recommended pick vanishes from the list it is supposed to lead."""
        pool = late_pool()
        rec = recommend(pool, SETTINGS, self._endgame_ctx(pool, 2), PARAMS)
        assert rec is not None
        top = rec.recommended_pick
        assert top.position in ("K", "DEF")
        assert not top.held_for_later
        assert any(row.canonical_id == top.canonical_id for row in rec.board)
        visible = [row for row in rec.board if not row.held_for_later]
        assert visible and visible[0].canonical_id == top.canonical_id

    def test_flex_opening_counts_toward_the_pick_budget(self) -> None:
        """A FLEX slot has to be filled to field a lineup, so it is a slot the
        endgame must budget a pick for."""
        pool = late_pool()
        flex_open = SETTINGS.model_copy(
            update={"roster_slots": {**SETTINGS.roster_slots, "FLEX": 2}}
        )
        ctx = DraftContext(
            drafted_ids=drafted_top(pool, 150, keep={"K1", "DEF1"}),
            # Direct starters full, both FLEX slots and K/DEF still open.
            my_roster_positions=("QB", "RB", "RB", "WR", "WR", "TE"),
            current_pick=151,
            my_next_pick=163,
            recent_positions=(),
            my_remaining_picks=4,
        )
        rec = recommend(pool, flex_open, ctx, PARAMS)
        assert rec is not None
        # With four picks left and four openings (2 FLEX + K + DEF), filling a
        # required slot outranks bench value.
        assert rec.recommended_pick.position in ("K", "DEF", "RB", "WR", "TE")


class TestBenchDepth:
    """A pick the lineup can never start is worth what a lineup gap costs.

    The live failure: a room let running backs slide, so every remaining back
    outscored every receiver on raw VOR, and the engine took backs at rounds 4,
    6, 8 and 9 while the second receiver slot sat empty. VOR is measured against
    the league's starting line and has no idea only three backs can ever be in
    a lineup, so a seventh was priced exactly like a fourth.
    """

    def test_multiplier_falls_as_a_position_stacks(self) -> None:
        startable = {"QB": 1, "RB": 3, "WR": 3, "TE": 2}
        rates = [
            _depth_multiplier("RB", {"RB": float(have)}, startable, PARAMS)
            for have in range(7)
        ]
        # The first three all start, so nothing is discounted.
        assert rates[0] == rates[1] == rates[2] == 1.0
        # Then: needed 53% of weeks, 13%, 1%, and never.
        assert round(rates[3], 3) == 0.528
        assert round(rates[4], 3) == 0.125
        assert round(rates[5], 3) == 0.011
        assert rates[6] == 0.0
        assert rates == sorted(rates, reverse=True)

    def test_backup_quarterback_is_worth_less_than_a_fourth_back(self) -> None:
        """Not because quarterbacks get hurt less -- measured, they do not.

        2025 miss rates were flat across positions (QB .223, RB .216, WR .222,
        TE .226). What separates them is how many slots the backup covers: one
        for a quarterback against three for a back.
        """
        startable = {"QB": 1, "RB": 3}
        backup_qb = _depth_multiplier("QB", {"QB": 1.0}, startable, PARAMS)
        fourth_rb = _depth_multiplier("RB", {"RB": 3.0}, startable, PARAMS)
        assert backup_qb < fourth_rb
        assert round(backup_qb, 4) == round(PARAMS.starter_unavailable_rate, 4)

    def test_startable_counts_include_every_flex_slot(self) -> None:
        slots = startable_slots(SETTINGS, PARAMS)
        assert slots["RB"] == 3  # 2 direct + FLEX
        assert slots["WR"] == 3
        assert slots["TE"] == 2
        assert slots["QB"] == 1

    def test_open_starter_slot_beats_a_sixth_back(self) -> None:
        """The exact live roster: five backs, one receiver, round 9."""
        pool = late_pool()
        ctx = DraftContext(
            drafted_ids=drafted_top(pool, 103),
            my_roster_positions=("QB", "RB", "RB", "WR", "TE", "RB", "RB", "RB"),
            current_pick=104,
            my_next_pick=113,
            recent_positions=(),
            my_remaining_picks=8,
        )
        rec = recommend(pool, SETTINGS, ctx, PARAMS)
        assert rec is not None
        assert rec.recommended_pick.position == "WR", (
            f"WR2 is empty and a sixth back cannot start; "
            f"got {rec.recommended_pick.position} {rec.recommended_pick.name}"
        )

    def test_a_fourth_back_is_not_banned_outright(self) -> None:
        """The discount is graded, not a blanket rule against depth.

        With every starting slot filled, the best back on the board is a fair
        pick: he backs up three slots and plays in over half of all weeks.
        """
        pool = late_pool()
        ctx = DraftContext(
            drafted_ids=drafted_top(pool, 103),
            my_roster_positions=("QB", "RB", "RB", "WR", "WR", "TE", "RB"),
            current_pick=104,
            my_next_pick=113,
            recent_positions=(),
            my_remaining_picks=9,
        )
        rec = recommend(pool, SETTINGS, ctx, PARAMS)
        assert rec is not None
        backs = [r for r in rec.board if r.position == "RB"]
        assert backs, "fixture must leave backs on the board"
        assert not any(r.held_for_later for r in backs)


class TestRoomPickIsAlsoARecommendation:
    """The room pick sits beside the real pick in the panel, so it must obey
    roster construction too.

    Live failure: with a quarterback already rostered, the room pick surfaced a
    second one at adjusted value -144. The ranking read `max(0.0, value)`, which
    erased how bad he was while still crediting his opportunity cost in full, so
    "the next quarterback is even worse" carried a player the roster could not
    use to the top of the board.
    """

    def _context(self, roster: tuple[str, ...], pick: int, remaining: int) -> DraftContext:
        return DraftContext(
            drafted_ids=drafted_top(late_pool(), pick - 1),
            my_roster_positions=roster,
            current_pick=pick,
            my_next_pick=pick + 11,
            recent_positions=(),
            my_remaining_picks=remaining,
        )

    def test_the_room_pick_is_always_worth_having(self) -> None:
        """Whatever else it is, it must not be a player of negative value."""
        pool = late_pool()
        for roster, pick, remaining in (
            (("QB", "RB", "RB"), 49, 13),
            (("QB", "RB", "RB", "WR", "WR", "TE", "RB", "WR"), 109, 8),
            (("QB", "RB", "RB", "WR", "WR", "TE", "RB", "WR", "WR", "RB", "TE"), 133, 5),
        ):
            rec = recommend(pool, SETTINGS, self._context(roster, pick, remaining), PARAMS)
            assert rec is not None
            # Either a genuinely wanted player, or it falls back to the
            # recommended pick when nothing on the board qualifies.
            assert (
                rec.room_pick.value > 0
                or rec.room_pick.canonical_id == rec.recommended_pick.canonical_id
            ), f"pick {pick}: room pick {rec.room_pick.name} value {rec.room_pick.value}"
