"""Surfacing rules under board exhaustion and edge seats.

Four related regressions from the 2026-08 audit: held rows leaking into the
room pick during must-fill, camp injury designations double-taxing August
drafts, edge-seat survival math inventing pressure when zero opponents pick,
and alternates/headline bypassing the held/capped filters.
"""

from api.engine.params import EngineParams
from api.engine.recommend import DraftContext, _context_adjustment, recommend
from api.engine.vor import PoolPlayer
from api.schemas import LeagueSettings

PARAMS = EngineParams()


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


ENDGAME_SETTINGS = LeagueSettings(
    scoring={"rec": 1.0},
    roster_slots={"QB": 1, "RB": 2, "WR": 2, "TE": 1, "K": 1, "DEF": 1, "BN": 5},
    total_teams=12,
)


# -- room pick must not surface held rows (audit #3) -------------------------


def test_room_pick_never_a_second_kicker_during_must_fill() -> None:
    """K rostered, DEF open, two picks left, kicker run in the room.

    Must-fill promotion clears the held flag on rows it legitimately promotes
    (DEF here); a second kicker stays held. The old `must_fill or` disjunct
    admitted exactly those still-held rows to the room-pick pool, and a
    kicker run gave them the urgency to win it.
    """
    pool = (
        linear_pool("K", 15, 160.0, 3.0, adp_start=140.0)
        + linear_pool("DEF", 15, 100.0, 3.0, adp_start=155.0)
        + linear_pool("RB", 30, 80.0, 2.0, adp_start=100.0)
    )
    context = DraftContext(
        drafted_ids=frozenset(),
        my_roster_positions=(
            "RB", "RB", "WR", "WR", "QB", "TE", "RB", "WR", "RB", "WR", "RB", "WR", "K",
        ),
        current_pick=157,
        my_next_pick=160,
        recent_positions=("K", "K", "K", "K"),
        my_remaining_picks=2,
    )
    rec = recommend(pool, ENDGAME_SETTINGS, context, PARAMS)
    assert rec is not None
    assert rec.room_pick.position != "K"
    assert not rec.room_pick.held_for_later


# -- no preseason injury dock (audit #4) -------------------------------------


def player_with_status(status: str | None) -> PoolPlayer:
    return PoolPlayer(
        canonical_id="p1", name="P", position="RB", team="KC",
        points=200.0, status=status,
    )


def test_camp_injury_designations_are_not_docked() -> None:
    """Projection sources already price known injuries; docking status again
    double-counts (Kittle lost a TE tier break to a camp PUP tag)."""
    healthy = _context_adjustment(player_with_status("Active"))
    for status in ("PUP", "Questionable", "Out", "Injured Reserve", "Doubtful"):
        assert _context_adjustment(player_with_status(status)) == healthy, status


def test_status_still_reaches_the_risks_list() -> None:
    pool = [player_with_status("PUP")] + linear_pool("RB", 20, 150.0, 3.0)
    context = DraftContext(
        drafted_ids=frozenset(), my_roster_positions=(), current_pick=1,
        my_next_pick=13, recent_positions=(),
    )
    rec = recommend(pool, ENDGAME_SETTINGS, context, PARAMS)
    assert rec is not None
    row = next(r for r in rec.board if r.canonical_id == "p1")
    assert any("PUP" in risk for risk in row.analysis.risks)


# -- edge-seat survival (audit #6) -------------------------------------------


def test_back_to_back_picks_mean_certain_survival() -> None:
    """At the snake turn (pick 24 -> 25) zero opponents pick between my turns:
    nobody can take anyone, so survival is 1.0 for the whole board and the
    room pick is the recommendation, not min-ADP chalk."""
    pool = linear_pool("RB", 30, 280.0, 5.0, adp_start=1.0, adp_step=2.0) + linear_pool(
        "WR", 30, 270.0, 4.0, adp_start=2.0, adp_step=2.0
    )
    drafted = [p.canonical_id for p in pool if p.adp is not None and p.adp < 24]
    context = DraftContext(
        drafted_ids=frozenset(drafted),
        my_roster_positions=("RB", "WR"),
        current_pick=24,
        my_next_pick=25,
        recent_positions=(),
        my_remaining_picks=13,
    )
    rec = recommend(pool, ENDGAME_SETTINGS, context, PARAMS)
    assert rec is not None
    assert rec.simulation.picks_simulated == 0
    for row in rec.board:
        assert row.survival == 1.0, row.canonical_id
        assert row.disappearance_probability == 0.0
        assert not row.at_risk
    assert rec.room_pick.canonical_id == rec.recommended_pick.canonical_id
    assert rec.picks_agree


# -- alternates and headline obey the surfacing filter (audit #7) ------------


def test_alternates_never_contain_held_rows() -> None:
    """Nearly-exhausted board: everything left is surplus RBs (held via zero
    depth) plus held second kickers. Alternates must not list them."""
    pool = (
        linear_pool("K", 5, 160.0, 3.0, adp_start=140.0)
        + linear_pool("RB", 10, 80.0, 2.0, adp_start=100.0)
        + linear_pool("DEF", 5, 100.0, 3.0, adp_start=155.0)
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
    rec = recommend(pool, ENDGAME_SETTINGS, context, PARAMS)
    assert rec is not None
    for alt in rec.alternates:
        assert not alt.held_for_later, alt.canonical_id


def test_recommended_pick_is_never_marked_held() -> None:
    """When only held rows remain, whatever tops the board is the
    recommendation; it must not carry the flag the extension filters on."""
    pool = linear_pool("K", 5, 160.0, 3.0, adp_start=140.0)
    context = DraftContext(
        drafted_ids=frozenset(),
        my_roster_positions=("K",),
        current_pick=157,
        my_next_pick=None,
        recent_positions=(),
        my_remaining_picks=None,
    )
    rec = recommend(pool, ENDGAME_SETTINGS, context, PARAMS)
    assert rec is not None
    assert not rec.recommended_pick.held_for_later
    board_row = next(
        r for r in rec.board if r.canonical_id == rec.recommended_pick.canonical_id
    )
    assert not board_row.held_for_later
