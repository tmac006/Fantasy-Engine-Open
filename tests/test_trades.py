"""Trade valuation and package construction.

The rules encoded here are the ones that were wrong in the first working
version, each of which produced offers that were useless in a specific way:
packages of two benches worth +0.0 to everybody, a kicker priced as a
211-point asset because his ADP was compared against skill-position scoring,
and "fleece" offers that cost the other side 177 points and would never be
answered.
"""

from dataclasses import replace

import pytest

from api.engine.lineup import fill_lineup, lineup_points
from api.engine.trades import (
    MAX_THEIR_LOSS,
    MIN_GAIN,
    TeamRoster,
    generate_packages,
    market_premium,
    market_value_curve,
    starter_benchmarks,
    team_needs,
)
from api.engine.vor import PoolPlayer
from api.schemas import LeagueSettings

SETTINGS = LeagueSettings(
    scoring={"rec": 1.0},
    roster_slots={"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "K": 1, "DEF": 1, "BN": 7},
    total_teams=12,
)


def player(pid: str, pos: str, points: float, adp: float | None = None, team: str = "KC") -> PoolPlayer:
    return PoolPlayer(
        canonical_id=pid, name=f"{pos} {pid}", position=pos, team=team,
        points=points, adp=adp,
    )


def roster(team_id: str, spec: list[tuple[str, float]], mine: bool = False) -> TeamRoster:
    players = tuple(
        player(f"{team_id}-{i}", pos, pts, adp=float(i + 1) * 5)
        for i, (pos, pts) in enumerate(spec)
    )
    return TeamRoster(team_id=team_id, name=f"Team {team_id}", players=players, is_mine=mine)


BALANCED = [
    ("QB", 300.0), ("RB", 260.0), ("RB", 230.0), ("WR", 250.0), ("WR", 220.0),
    ("TE", 180.0), ("K", 140.0), ("DEF", 110.0),
]


# -- market pricing ----------------------------------------------------------


def test_market_value_is_priced_within_position() -> None:
    """A kicker's ADP must be priced against kickers.

    Priced against the whole pool, the best kicker (drafted ~pick 86) was valued
    at whatever the 86th-best skill player scores -- 211 points against a real
    projection of 144 -- so every package containing him looked like a heist.
    """
    pool = [player(f"wr{i}", "WR", 300.0 - i * 10, adp=float(i + 1)) for i in range(20)]
    pool += [player(f"k{i}", "K", 150.0 - i * 5, adp=100.0 + i) for i in range(5)]
    curve = market_value_curve(pool)
    best_kicker = next(p for p in pool if p.canonical_id == "k0")
    assert curve[best_kicker.canonical_id] == pytest.approx(150.0)
    assert abs(market_premium(best_kicker, curve)) < 1.0


def test_market_premium_flags_reputation_above_production() -> None:
    # Drafted first, produces like the fourth-best: the market overrates him.
    pool = [
        player("hype", "WR", 200.0, adp=1.0),
        player("a", "WR", 260.0, adp=2.0),
        player("b", "WR", 240.0, adp=3.0),
        player("c", "WR", 220.0, adp=4.0),
    ]
    curve = market_value_curve(pool)
    assert market_premium(pool[0], curve) > 50.0


def test_player_without_adp_carries_no_market_opinion() -> None:
    pool = [player("a", "WR", 200.0, adp=1.0), player("waiver", "WR", 150.0, adp=None)]
    curve = market_value_curve(pool)
    assert market_premium(pool[1], curve) == 0.0


# -- needs -------------------------------------------------------------------


def test_needs_rank_the_thinnest_starting_slot_first() -> None:
    thin_at_wr = roster("x", [
        ("QB", 300.0), ("RB", 260.0), ("RB", 250.0), ("RB", 240.0),
        ("WR", 120.0), ("WR", 110.0), ("TE", 180.0), ("K", 140.0), ("DEF", 110.0),
    ], mine=True)
    pool = [
        *thin_at_wr.players,
        *[player(f"wr{i}", "WR", 260.0 - i * 6) for i in range(30)],
        *[player(f"rb{i}", "RB", 265.0 - i * 6) for i in range(30)],
        *[player(f"qb{i}", "QB", 310.0 - i * 6) for i in range(14)],
        *[player(f"te{i}", "TE", 200.0 - i * 6) for i in range(14)],
    ]
    needs = team_needs(thin_at_wr, SETTINGS, starter_benchmarks(pool, SETTINGS))
    assert needs[0].position == "WR"


# -- packages ----------------------------------------------------------------


def test_normal_mode_requires_both_sides_to_gain() -> None:
    """Ranked only by what each side could spare, both rosters offered their
    bench and every package was worth exactly +0.0 to both -- so normal mode
    returned nothing at all.

    The surplus here is deliberately large. A mild imbalance produces gains of
    a point or two, which is projection noise rather than a reason to trade,
    and MIN_GAIN now rejects it.
    """
    mine, others, pool = _around_setup()
    packages = generate_packages(mine, others, SETTINGS, pool, mode="normal")
    assert packages, "a roster deep at RB and one deep at WR must find a win-win"
    for package in packages:
        assert package.my_gain >= MIN_GAIN
        assert package.their_gain >= MIN_GAIN


def test_normal_mode_never_asks_them_to_overpay_on_market_value() -> None:
    """Week 8 2026: Henderson + Bryce Young for Jeremiyah Love + Luther Burden.
    Both lineups gained, but they would have handed over 20 more points of
    draft-market value -- an offer no manager answers."""
    from api.engine.trades import is_acceptable_offer

    qb = player("q", "QB", 170.0, adp=137.0)
    assert not is_acceptable_offer("normal", (qb,), 55.1, 24.3, -20.2, [], set())
    assert is_acceptable_offer("normal", (qb,), 55.1, 24.3, 0.0, [], set())
    mine, others, pool = _around_setup()
    for package in generate_packages(mine, others, SETTINGS, pool, mode="normal"):
        assert package.market_edge >= 0.0


def test_fleece_never_proposes_transparent_theft() -> None:
    """The "answers a need" branch once bypassed every sanity check and offered
    one bench receiver for two studs, costing the other side 177 points."""
    mine = roster("me", [*BALANCED, ("WR", 80.0), ("WR", 75.0)], mine=True)
    them = roster("them", [*BALANCED, ("WR", 280.0), ("RB", 275.0)])
    pool = [*mine.players, *them.players]
    for package in generate_packages(mine, [them], SETTINGS, pool, mode="fleece"):
        assert package.their_gain >= -MAX_THEIR_LOSS


def test_every_package_improves_my_lineup() -> None:
    mine = roster("me", [*BALANCED, ("RB", 245.0), ("RB", 240.0)], mine=True)
    them = roster("them", [*BALANCED, ("WR", 250.0), ("WR", 248.0)])
    pool = [*mine.players, *them.players]
    for mode in ("normal", "fleece"):
        for package in generate_packages(mine, [them], SETTINGS, pool, mode=mode):
            before = lineup_points(list(mine.players), SETTINGS.roster_slots)
            kept = [p for p in mine.players if p not in package.give]
            after = lineup_points([*kept, *package.receive], SETTINGS.roster_slots)
            assert after - before == pytest.approx(package.my_gain)
            assert package.my_gain > 0.0


def test_offers_are_spread_across_opponents() -> None:
    mine = roster("me", [*BALANCED, ("RB", 245.0), ("RB", 240.0)], mine=True)
    others = [roster(f"t{i}", [*BALANCED, ("WR", 250.0 - i), ("WR", 248.0 - i)]) for i in range(5)]
    pool = [*mine.players, *[p for o in others for p in o.players]]
    packages = generate_packages(mine, others, SETTINGS, pool, mode="normal", limit=12)
    counts: dict[str, int] = {}
    for package in packages:
        counts[package.their_team_id] = counts.get(package.their_team_id, 0) + 1
    assert all(count <= 2 for count in counts.values())


# -- manual trade builder ---------------------------------------------------


def _schedules(players: list[PoolPlayer], week: int = 1, statuses: dict[str, str] | None = None):
    from api.engine.restofseason import rest_of_season

    return {
        p.canonical_id: rest_of_season(
            preseason_total=p.points, current_week=week, bye_week=p.bye_week,
            injury_status=(statuses or {}).get(p.canonical_id),
        )
        for p in players
    }


def test_weekly_curve_drops_a_player_only_in_his_bye_week() -> None:
    """Season totals price a player as though he plays every week. The builder's
    bye-adjusted number exists because he does not."""
    from api.routers.trades import _weekly_curve

    slots = {"QB": 1, "RB": 1, "BN": 2}
    roster = [
        PoolPlayer(canonical_id="q", name="QB", position="QB", team="NE", points=340.0, bye_week=11),
        PoolPlayer(canonical_id="r", name="RB", position="RB", team="MIA", points=170.0, bye_week=6),
    ]
    weeks = {w: (pts, filled) for w, pts, filled in _weekly_curve(roster, slots, _schedules(roster))}
    assert weeks[1][1] == 2, "both start in a normal week"
    assert weeks[11][1] == 1, "the QB is idle in week 11"
    assert weeks[6][1] == 1, "the RB is idle in week 6"
    assert weeks[11][0] < weeks[1][0]


def test_weekly_curve_covers_only_the_weeks_left() -> None:
    from api.routers.trades import _weekly_curve

    roster = [PoolPlayer(canonical_id="a", name="A", position="RB", team="NE", points=200.0, bye_week=11)]
    weeks = _weekly_curve(roster, {"RB": 1}, _schedules(roster, week=4))
    assert [w for w, _, _ in weeks] == list(range(4, 18))


def test_weekly_curve_empties_the_slot_while_he_is_on_ir() -> None:
    from api.routers.trades import _weekly_curve

    roster = [PoolPlayer(canonical_id="a", name="A", position="RB", team="MIA", points=257.0)]
    weeks = _weekly_curve(roster, {"RB": 1}, _schedules(roster, week=4, statuses={"a": "INJURY_RESERVE"}))
    filled = {w: n for w, _, n in weeks}
    assert [filled[w] for w in (4, 5, 6, 7)] == [0, 0, 0, 0]
    assert filled[8] == 1


def test_a_player_with_no_bye_is_never_idle() -> None:
    from api.routers.trades import _weekly_curve

    roster = [PoolPlayer(canonical_id="a", name="A", position="RB", team=None, points=200.0)]
    weeks = _weekly_curve(roster, {"RB": 1}, _schedules(roster))
    assert all(filled == 1 for _, _, filled in weeks)


# -- build around one player -------------------------------------------------


def _around_setup() -> tuple[TeamRoster, list[TeamRoster], list[PoolPlayer]]:
    """Rosters with genuinely UNUSABLE surplus, so a two-sided trade exists.

    Two identical rosters cannot produce one, and neither can mild imbalance: a
    FLEX makes a spare running back and a spare receiver interchangeable, so a
    swap of equals is a wash by construction. Only depth past what any lineup
    can field is tradeable, which is the same reason the engine values players
    by lineup impact rather than by projection.
    """
    mine = roster(
        "me",
        [
            ("QB", 300.0), ("TE", 180.0), ("K", 140.0), ("DEF", 110.0),
            ("RB", 300.0), ("RB", 290.0), ("RB", 280.0), ("RB", 270.0), ("RB", 260.0),
            ("WR", 100.0), ("WR", 90.0),
        ],
        mine=True,
    )
    them = roster(
        "them",
        [
            ("QB", 295.0), ("TE", 175.0), ("K", 135.0), ("DEF", 105.0),
            ("WR", 300.0), ("WR", 290.0), ("WR", 280.0), ("WR", 270.0), ("WR", 260.0),
            ("RB", 100.0), ("RB", 90.0),
        ],
    )
    return mine, [them], [*mine.players, *them.players]


def _find_anchor(
    players: tuple[PoolPlayer, ...], position: str, rank: int = 0
) -> PoolPlayer:
    """The nth-best player at a position. `rank=0` is the best, which is
    deliberately NOT what a realistic acquisition target looks like -- a team's
    best player is normally unaffordable, and the engine says so by returning
    nothing."""
    at_position = sorted(
        (p for p in players if p.position == position), key=lambda p: -p.points
    )
    return at_position[rank]


def test_every_package_contains_the_anchor() -> None:
    """The whole point: the player is fixed and only the other side is searched."""
    from api.engine.trades import packages_around

    mine, others, pool = _around_setup()
    anchor = _find_anchor(mine.players, "RB", rank=3)  # my surplus back
    packages = packages_around(anchor, mine, others, SETTINGS, pool, limit=6)
    assert packages, "unusable surplus on both sides must yield at least one offer"
    for pkg in packages:
        assert any(p.canonical_id == anchor.canonical_id for p in pkg.give)
        assert all(p.canonical_id != anchor.canonical_id for p in pkg.receive)


def test_an_opponents_player_is_anchored_on_the_receiving_side() -> None:
    from api.engine.trades import packages_around

    mine, others, pool = _around_setup()
    anchor = _find_anchor(others[0].players, "WR", rank=2)  # their surplus, not their best
    packages = packages_around(anchor, mine, others, SETTINGS, pool, limit=6)
    assert packages
    for pkg in packages:
        assert any(p.canonical_id == anchor.canonical_id for p in pkg.receive)
        assert all(p.canonical_id != anchor.canonical_id for p in pkg.give)


def test_anchoring_never_relaxes_who_would_accept() -> None:
    """The regression this exists for: fixing a player made the search skip the
    plausibility rule, so it served one bench receiver for two studs -- ranked
    top precisely because it was the most lopsided in our favour."""
    from api.engine.trades import MAX_MARKET_IMBALANCE, MIN_GAIN, packages_around

    mine, others, pool = _around_setup()
    for anchor in (
        _find_anchor(mine.players, "RB", rank=3),
        _find_anchor(others[0].players, "WR", rank=2),
    ):
        for package in packages_around(anchor, mine, others, SETTINGS, pool, limit=8):
            assert package.my_gain >= MIN_GAIN
            assert package.their_gain >= MIN_GAIN, "normal mode needs both sides to gain"
            assert package.market_edge >= -MAX_MARKET_IMBALANCE


def test_an_unaffordable_anchor_returns_nothing_rather_than_nonsense() -> None:
    """Better to say no package works than to invent one nobody would answer."""
    from api.engine.trades import packages_around

    mine, others, pool = _around_setup()
    untouchable = player("them-star", "WR", 800.0, adp=1.0)
    loaded = TeamRoster(
        team_id="them",
        name="Team them",
        players=(*others[0].players, untouchable),
    )
    assert packages_around(untouchable, mine, [loaded], SETTINGS, [*pool, untouchable]) == []


def test_smaller_packages_win_ties() -> None:
    """Bench players cost zero lineup points, so among equally acceptable offers
    the search must not bundle free bodies in."""
    from api.engine.trades import packages_around

    mine, others, pool = _around_setup()
    anchor = _find_anchor(mine.players, "RB", rank=3)
    packages = packages_around(anchor, mine, others, SETTINGS, pool, limit=8)
    best = packages[0]
    same_gain = [p for p in packages if round(p.my_gain, 1) == round(best.my_gain, 1)]
    assert len(best.give) == min(len(p.give) for p in same_gain)


def test_an_unrostered_anchor_yields_nothing_rather_than_guessing() -> None:
    from api.engine.trades import packages_around

    mine, others, pool = _around_setup()
    stranger = player("free-agent", "WR", 200.0)
    assert packages_around(stranger, mine, others, SETTINGS, pool) == []


def test_packages_are_deduplicated() -> None:
    from api.engine.trades import packages_around

    mine, others, pool = _around_setup()
    anchor = mine.players[1]
    packages = packages_around(anchor, mine, others, SETTINGS, pool, limit=10)
    keys = [
        (
            p.their_team_id,
            tuple(sorted(x.canonical_id for x in p.give)),
            tuple(sorted(x.canonical_id for x in p.receive)),
        )
        for p in packages
    ]
    assert len(keys) == len(set(keys))


def test_acquiring_considers_starters_not_just_spare_parts() -> None:
    """Naming a target usually means accepting that a starter goes back.

    The chip heuristics rank by who each side can spare, which is right when
    hunting for any trade and wrong once the user has picked a player. While
    the anchored search reused them it could not even see the obvious swap of
    one good receiver for another -- both sides' best asset at the position is
    never a "chip".
    """
    from api.engine.trades import packages_around

    mine, others, pool = _around_setup()
    target = _find_anchor(others[0].players, "WR", rank=2)
    packages = packages_around(
        target, mine, others, SETTINGS, pool, mode="fleece", limit=10
    )
    assert packages
    offered = {p.canonical_id for pkg in packages for p in pkg.give}
    starters, _ = fill_lineup(list(mine.players), SETTINGS.roster_slots)
    assert offered & {p.canonical_id for p in starters}, (
        "a package for a named target must be allowed to send a starter"
    )


def test_a_seriously_hurt_player_is_worth_less_in_a_package() -> None:
    """The exact complaint: an offer read "+2.4 for you" with an injury note
    printed underneath it. The note was surfaced and never counted. Availability
    is now priced upstream, in his rest-of-season points, so the package must
    see the lower number."""
    from api.engine.restofseason import rest_of_season
    from api.engine.trades import evaluate_package, market_value_curve

    mine, others, pool = _around_setup()
    them = others[0]
    curve = market_value_curve(pool)
    give = (_find_anchor(mine.players, "RB", rank=3),)
    healthy = _find_anchor(them.players, "WR", rank=2)
    ros = rest_of_season(preseason_total=healthy.points, current_week=1, injury_status="INJURY_RESERVE")
    hurt = replace(healthy, points=ros.points, status="injury: IR")

    healthy_gain, _, _ = evaluate_package(mine, them, give, (healthy,), SETTINGS, curve)
    hurt_roster = TeamRoster(
        team_id=them.team_id,
        name=them.name,
        players=tuple(hurt if p.canonical_id == healthy.canonical_id else p for p in them.players),
    )
    hurt_gain, _, _ = evaluate_package(mine, hurt_roster, give, (hurt,), SETTINGS, curve)
    assert hurt_gain < healthy_gain


def test_an_ir_star_does_not_anchor_the_normal_offers() -> None:
    """2026-09-29: every normal offer was built around De'Von Achane, on IR since
    the day before and still valued at his 257-point preseason total. Valued on
    rest-of-season points he is worth only the games he can still play, and the
    offers move to the healthy backs."""
    from api.engine.restofseason import rest_of_season

    mine = roster("me", [
        ("QB", 300.0), ("RB", 150.0), ("RB", 110.0), ("WR", 250.0), ("WR", 240.0),
        ("WR", 235.0), ("WR", 225.0), ("TE", 150.0), ("K", 130.0), ("DEF", 100.0),
    ], mine=True)
    others = [
        roster(t, [
            ("QB", 290.0), ("RB", 257.0 if t == "a" else 215.0), ("RB", 240.0),
            ("RB", 230.0), ("RB", 220.0), ("WR", 120.0), ("WR", 100.0), ("TE", 140.0),
            ("K", 130.0), ("DEF", 100.0),
        ])
        for t in ("a", "b", "c")
    ]
    achane = "a-1"
    everyone = [p for r in (mine, *others) for p in r.players]

    def involves_achane(packages) -> bool:
        return any(
            achane in {p.canonical_id for p in (*pkg.give, *pkg.receive)} for pkg in packages
        )

    preseason = generate_packages(mine, others, SETTINGS, everyone, mode="normal")
    assert achane in {p.canonical_id for p in preseason[0].receive}, (
        "precondition: on preseason totals the IR back is the top target"
    )

    schedules = {
        p.canonical_id: rest_of_season(
            preseason_total=p.points, current_week=4,
            injury_status="INJURY_RESERVE" if p.canonical_id == achane else None,
        )
        for p in everyone
    }

    def revalue(r: TeamRoster) -> TeamRoster:
        return replace(r, players=tuple(
            replace(p, points=schedules[p.canonical_id].points) for p in r.players
        ))

    mine_ros, others_ros = revalue(mine), [revalue(o) for o in others]
    pool_ros = [p for r in (mine_ros, *others_ros) for p in r.players]
    availability = {k: v.availability_factor for k, v in schedules.items()}
    ros = generate_packages(
        mine_ros, others_ros, SETTINGS, pool_ros, mode="normal", availability=availability
    )
    assert ros
    assert not involves_achane(ros)
    assert next(p for p in pool_ros if p.canonical_id == achane).points < 0.5 * 257.0


def test_market_price_is_shaded_for_an_injured_player() -> None:
    """ADP is August's opinion. Nobody pays it for a back on IR, and unshaded,
    sending him would read as a generous offer."""
    pool = [player("a", "RB", 200.0, adp=5.0), player("b", "RB", 150.0, adp=20.0)]
    assert market_value_curve(pool)["a"] == 200.0
    assert market_value_curve(pool, {"a": 0.4})["a"] == pytest.approx(80.0)
    assert market_value_curve(pool, {"a": 0.4})["b"] == 150.0


def test_routine_camp_designations_are_not_discounted() -> None:
    """88 of the current pool are "Questionable" as ordinary camp bookkeeping,
    and projections already price it -- the same reason the draft engine
    stopped docking for it."""
    from api.engine.trades import is_seriously_hurt

    base = player("x", "WR", 200.0)
    assert not is_seriously_hurt(base)
    assert not is_seriously_hurt(replace(base, status="Active"))
    assert not is_seriously_hurt(replace(base, status="Active; injury: Questionable"))
    assert is_seriously_hurt(replace(base, status="Active; injury: PUP"))
    assert is_seriously_hurt(replace(base, status="Inactive; injury: IR"))
    assert is_seriously_hurt(replace(base, status="Active; injury: Doubtful"))


def test_noise_sized_gains_are_not_proposed() -> None:
    """A package worth a couple of points a season is projection noise, not a
    reason to hand another manager one of your starters."""
    from api.engine.trades import MIN_GAIN, packages_around

    mine, others, pool = _around_setup()
    for mode in ("normal", "fleece"):
        for anchor in (_find_anchor(mine.players, "RB", rank=3),):
            for pkg in packages_around(anchor, mine, others, SETTINGS, pool, mode=mode):
                assert pkg.my_gain >= MIN_GAIN


def test_a_qb_or_kicker_upgrade_is_priced_against_the_waiver_wire() -> None:
    """Week 8 2026: Maye + Aubrey came back as fair value for CeeDee Lamb,
    because each was priced against the other team's own backup (Shough,
    Jason Myers) rather than the streamer they could add for free."""
    from api.engine.trades import attach_waiver, evaluate_package

    mine = roster("me", [*BALANCED, ("QB", 290.0), ("K", 150.0)], mine=True)
    them = roster(
        "them",
        [("QB", 200.0), ("RB", 260.0), ("RB", 230.0), ("WR", 330.0), ("WR", 220.0),
         ("WR", 200.0), ("WR", 180.0), ("TE", 180.0), ("K", 50.0), ("DEF", 110.0)],
    )
    free_qb = player("fa-qb", "QB", 245.0, adp=150.0)
    free_k = player("fa-k", "K", 135.0, adp=160.0)
    pool = [*mine.players, *them.players, free_qb, free_k]
    give = (mine.players[-2], mine.players[-1])  # backup QB + spare K
    receive = (them.players[3],)  # their WR1
    curve = {}

    _, blind_gain, _ = evaluate_package(mine, them, give, receive, SETTINGS, curve)
    mine_w, them_w = attach_waiver([mine, them], pool, SETTINGS)
    _, honest_gain, _ = evaluate_package(mine_w, them_w, give, receive, SETTINGS, curve)

    assert blind_gain > 0, "against his own backups the QB+K pair looks like a win"
    assert honest_gain < -MAX_THEIR_LOSS, "against the wire it costs him his WR1 for little"


def test_waiver_never_includes_a_rostered_player() -> None:
    from api.engine.trades import attach_waiver

    mine = roster("me", BALANCED, mine=True)
    them = roster("them", BALANCED)
    free = player("fa", "WR", 90.0)
    pool = [*mine.players, *them.players, free]
    for team in attach_waiver([mine, them], pool, SETTINGS):
        assert [p.canonical_id for p in team.waiver] == ["fa"]
