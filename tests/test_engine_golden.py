"""A behaviour lock for the recommendation pipeline.

`recommend()` is a long pipeline that invites tidying, and tidying it is exactly
when a subtle scoring change slips through: the positional tests all still pass
because they assert *rules*, not numbers. This asserts the numbers.

Any diff here means the refactor changed behaviour. That is either a bug or an
intentional model change -- and if it is intentional, updating this file should
be a deliberate, reviewed line in the diff rather than a silent drift.
"""

from api.engine.params import EngineParams
from api.engine.recommend import DraftContext, recommend
from api.engine.vor import PoolPlayer
from api.schemas import LeagueSettings

PARAMS = EngineParams(simulation_seed=42)

SETTINGS = LeagueSettings(
    scoring={"rec": 1.0},
    roster_slots={"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "K": 1, "DEF": 1, "BN": 7},
    position_limits={"QB": 4, "RB": 8, "WR": 8, "TE": 3, "K": 3, "DEF": 3},
    total_teams=12,
)


def pool() -> list[PoolPlayer]:
    """Deterministic board with interleaved ADPs across positions."""
    players: list[PoolPlayer] = []
    cid = 1
    shape = (
        ("QB", 24, 380.0, 5.5, 20.0, 7.0),
        ("RB", 60, 340.0, 3.4, 1.0, 3.0),
        ("WR", 60, 330.0, 3.1, 2.0, 3.0),
        ("TE", 20, 250.0, 6.0, 18.0, 9.0),
        ("K", 14, 150.0, 4.0, 165.0, 1.0),
        ("DEF", 14, 130.0, 4.0, 158.0, 1.0),
    )
    for position, count, top, drop, first, gap in shape:
        for index in range(count):
            players.append(
                PoolPlayer(
                    canonical_id=str(cid),
                    name=f"{position}{index + 1}",
                    position=position,
                    team=None,
                    points=top - index * drop,
                    adp=first + index * gap,
                    platform_ids={},
                )
            )
            cid += 1
    return players


def drafted(players: list[PoolPlayer], count: int) -> frozenset[str]:
    ordered = sorted((p for p in players if p.adp is not None), key=lambda p: (p.adp, p.canonical_id))
    return frozenset(p.canonical_id for p in ordered[:count])


# (current pick, roster so far, picks remaining) -> (recommended, room pick)
SCENARIOS = (
    ((1, (), 16), ("RB1", "RB1")),
    ((28, ("RB", "RB"), 14), ("WR12", "WR12")),
    (
        # Deliberate change when bench depth started being priced by how many
        # lineup slots a backup covers: this roster holds four receivers where
        # three can start and three backs where three can start, so the next
        # back is the first bench body at a 3-slot position (needed 53% of
        # weeks) while the next receiver is the second (13%). Was ("WR46",
        # "RB46") under the flat surplus penalty, which charged both the same.
        (121, ("QB", "RB", "RB", "WR", "WR", "TE", "WR", "RB", "WR", "QB"), 6),
        ("RB46", "WR46"),
    ),
    (
        # Endgame with K and DEF open. K6 is the best kicker still on the board
        # (K1-K5 are gone), so this doubles as a check that the endgame takes
        # the best available rather than an arbitrary one.
        (
            169,
            ("QB", "RB", "RB", "WR", "WR", "TE", "WR", "RB", "WR", "RB", "WR", "RB", "WR", "RB"),
            2,
        ),
        ("K6", "TE18"),
    ),
)


def test_pipeline_output_is_stable() -> None:
    players = pool()
    for (pick, roster, remaining), expected in SCENARIOS:
        ctx = DraftContext(
            drafted_ids=drafted(players, pick - 1),
            my_roster_positions=roster,
            current_pick=pick,
            my_next_pick=pick + 11,
            recent_positions=(),
            my_remaining_picks=remaining,
            simulation_seed=42,
        )
        result = recommend(players, SETTINGS, ctx, PARAMS)
        assert result is not None, f"pick {pick}: no recommendation"
        actual = (result.recommended_pick.name, result.room_pick.name)
        assert actual == expected, f"pick {pick}: {actual} != {expected}"


def test_scores_are_stable_to_three_decimals() -> None:
    """Names alone can hide a scoring drift that reorders the rest of the board."""
    players = pool()
    ctx = DraftContext(
        drafted_ids=drafted(players, 27),
        my_roster_positions=("RB", "RB"),
        current_pick=28,
        my_next_pick=39,
        recent_positions=(),
        my_remaining_picks=14,
        simulation_seed=42,
    )
    result = recommend(players, SETTINGS, ctx, PARAMS)
    assert result is not None
    top = [(row.name, round(row.score, 3), round(row.value, 3)) for row in result.board[:8]]
    assert top == EXPECTED_TOP_AT_PICK_28


EXPECTED_TOP_AT_PICK_28: list[tuple[str, float, float]] = [
    ("WR12", 121.1, 70.7),
    ("WR13", 117.0, 67.0),
    ("WR14", 112.6, 63.2),
    ("TE3", 110.1, 60.0),
    ("RB12", 108.1, 57.8),
    ("WR15", 106.5, 59.5),
    ("RB13", 104.3, 54.4),
    ("WR16", 103.3, 55.8),
]
