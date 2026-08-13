"""Value over replacement (spec §7.1): the ranking backbone.

Replacement level per position comes from league structure, not raw points:
the player you could still get at rank (teams x effective starters) is worth
zero; everyone is valued by their distance above that player.
"""

from dataclasses import dataclass, field

from api.engine.params import EngineParams
from api.schemas import LeagueSettings

# Roster slots that are position-specific starters (not bench/flex/IDP).
_DIRECT_SLOTS = {"QB", "RB", "WR", "TE", "K", "DEF"}
# Flex-type slots and the positions eligible to fill them.
FLEX_ELIGIBILITY: dict[str, list[str]] = {
    "FLEX": ["RB", "WR", "TE"],
    "WRRB_FLEX": ["WR", "RB"],
    "REC_FLEX": ["WR", "TE"],
    "SUPER_FLEX": ["QB", "RB", "WR", "TE"],
}

type ContextValue = str | int | float | bool | None


@dataclass(frozen=True)
class PoolPlayer:
    """A projected player, already scored under the league's settings."""

    canonical_id: str
    name: str
    position: str
    team: str | None
    points: float
    adp: float | None = None
    adp_stdev: float | None = None
    # Optional richer inputs.  The engine deliberately accepts partial context:
    # existing pool builders can keep constructing the small core record while
    # new ingests progressively populate analysis details.
    projection_consensus: float | None = None
    projection_low: float | None = None
    projection_high: float | None = None
    projection_stdev: float | None = None
    usage: dict[str, ContextValue] = field(default_factory=dict)
    # Advanced-stat card text, already ranked within position. Read-only: these
    # metrics failed the predictive gate and must never enter the score.
    advanced: tuple[str, ...] = ()
    advanced_summary: str | None = None
    advanced_season: str | None = None
    team_context: dict[str, ContextValue] = field(default_factory=dict)
    status: str | None = None
    risk_flags: tuple[str, ...] = ()
    freshness: dict[str, str | None] = field(default_factory=dict)
    # Platform-native IDs ({"sleeper": "9221", "espn": "4429795"}), passed
    # through untouched so clients can match picks offline.
    platform_ids: dict[str, str] = field(default_factory=dict)


def effective_starters(settings: LeagueSettings, params: EngineParams) -> dict[str, float]:
    """Starters per position, with flex slots apportioned by params.flex_split.

    Example: 1 QB, 2 RB, 2 WR, 1 TE, 2 FLEX with default split ->
    {QB: 1, RB: 2.7, WR: 3.0, TE: 1.3, ...}
    """
    starters: dict[str, float] = {p: 0.0 for p in params.positions}
    for slot, count in settings.roster_slots.items():
        if slot in _DIRECT_SLOTS:
            starters[slot] = starters.get(slot, 0.0) + count
        elif slot in FLEX_ELIGIBILITY:
            eligible = [p for p in FLEX_ELIGIBILITY[slot] if p in starters]
            split = params.superflex_split if slot == "SUPER_FLEX" else params.flex_split
            total_share = sum(split.get(p, 0.0) for p in eligible)
            for p in eligible:
                share = split.get(p, 0.0) / total_share if total_share else 1 / len(eligible)
                starters[p] += count * share
    return starters


def startable_slots(settings: LeagueSettings, params: EngineParams) -> dict[str, int]:
    """The most players at one position a legal lineup could ever field at once.

    Deliberately different from `effective_starters`, which splits each flex slot
    across positions to set a league-wide replacement level. The question here is
    about one roster rather than the league: could this player be in my lineup at
    all? So every flex slot he is eligible for counts in full, which is the
    generous reading -- 2 RB + 2 WR + 1 FLEX yields 3 startable at both, though
    only five can start together. Overstating it keeps the depth discount off
    players who might genuinely start.
    """
    out: dict[str, int] = {}
    for pos in params.positions:
        flex = sum(
            count
            for slot, count in settings.roster_slots.items()
            if slot in FLEX_ELIGIBILITY and pos in FLEX_ELIGIBILITY[slot]
        )
        out[pos] = settings.roster_slots.get(pos, 0) + flex
    return out


def replacement_ranks(settings: LeagueSettings, params: EngineParams) -> dict[str, int]:
    """Rank (1-based, within position) of the replacement-level player."""
    starters = effective_starters(settings, params)
    return {
        pos: max(1, round(settings.total_teams * n))
        for pos, n in starters.items()
        if n > 0
    }


def replacement_points(pool: list[PoolPlayer], ranks: dict[str, int]) -> dict[str, float]:
    """Projected points of the replacement-level player at each position.

    Computed over the full pool (drafted included): replacement level is a
    property of the league, not of who happens to be off the board.
    """
    out: dict[str, float] = {}
    for pos, rank in ranks.items():
        at_pos = sorted((p.points for p in pool if p.position == pos), reverse=True)
        if not at_pos:
            out[pos] = 0.0
        else:
            out[pos] = at_pos[min(rank, len(at_pos)) - 1]
    return out


def vor(player: PoolPlayer, repl: dict[str, float]) -> float:
    return player.points - repl.get(player.position, 0.0)
