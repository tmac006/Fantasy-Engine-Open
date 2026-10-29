"""Trade valuation and package construction.

The whole engine rests on one idea: a player is worth exactly what he adds to
the best legal lineup a particular roster can field. There is no global "trade
value chart" here, because there is no such thing -- the third running back on
a team already starting two is worth a fraction of what he is worth to a team
starting one. Every number below is therefore computed against a specific
roster, which is also what makes the two sides of a trade comparable: the same
function answers "what do I gain" and "what do they gain".

Two modes are exposed, and they optimise different things:

*Normal* looks for genuine two-sided gains. Both rosters end up fielding more
points than they started with, because each side is converting surplus at a
position it is deep in into help at a position it is thin at. These are the
offers that actually get accepted.

*Fleece* looks for trades where the market's opinion of the players and their
real projected value disagree, and the disagreement runs in our favour. It is
not a trick: the package still has to be one a rational manager would plausibly
accept, which here means it either looks fair by draft-market standards or
answers a need they urgently have. What makes it a fleece is that the value we
receive exceeds the value we give by more than the market's pricing suggests.
"""

from dataclasses import dataclass, replace
from itertools import combinations

from api.engine.lineup import lineup_points
from api.engine.vor import FLEX_ELIGIBILITY, PoolPlayer
from api.schemas import LeagueSettings

# How many players from each side are considered as trade chips. Enumerating
# every subset of two 16-man rosters is combinatorially pointless -- the
# interesting packages are built from each side's most movable assets.
MAX_CHIPS_PER_SIDE = 7
# Package shapes to enumerate, as (players from me, players from them).
PACKAGE_SHAPES = ((1, 1), (2, 1), (1, 2), (2, 2))
# The smallest lineup gain worth proposing a trade over, in season points.
# 1.0 was effectively "any change at all", which is how a +2.4 package -- 0.14
# points a week, indistinguishable from projection noise -- got surfaced as a
# recommendation. A trade carries real risk (a rookie, a knock, a manager who
# now knows what you want), so it has to clear more than rounding error.
MIN_GAIN = 12.0
# The most a counterparty may lose and still plausibly accept. Beyond this
# the offer stops being a shrewd trade and becomes one nobody answers.
MAX_THEIR_LOSS = 20.0
# How much more draft-market value we may receive than we send. Managers judge
# a trade by what the room paid for each name, not by our projections, so an
# offer can clear every lineup test and still be refused on sight. Roughly a
# quarter of a startable player: enough to let a genuine need excuse a small
# imbalance, not enough to dress up "your stud for my bench guy".
MAX_MARKET_IMBALANCE = 40.0
# How many of the other side's assets an anchored search will consider when the
# anchor is ours. Only one side is enumerated, so this can be far wider than
# MAX_CHIPS_PER_SIDE without the combinatorics running away.
ANCHOR_SEARCH_WIDTH = 10
# Injury designations are priced BEFORE players reach this module: the pool
# handed in carries rest-of-season points (`api.engine.restofseason`), which
# already zero the games an IR or OUT player misses and discount his return.
# A flat 20% haircut used to live here instead, on top of full-season
# preseason totals -- which still valued De'Von Achane at 257 points the day
# after he went on IR, so every "normal" offer was built around him.
#
# Driven by the STRUCTURED status, never by news tags. The feed tags
# "Panthers expect Chuba Hubbard back for Week 1" and a kicker roster battle
# as `injury`, and it tagged a Barkley story about joint practices too -- so
# valuing on tags discounted a perfectly healthy player and made trading him
# away look like a gain.
#
# Designations that actually threaten availability. "Questionable" is excluded
# on purpose: 88 of the current pool carry it as routine camp bookkeeping, and
# projections already price it -- the same reason the draft engine stopped
# docking for it.
SERIOUS_INJURY_MARKERS = ("ir", "pup", "out", "doubtful", "dnr", "suspend")


@dataclass(frozen=True)
class TeamRoster:
    """One league team's players, in engine terms."""

    team_id: str
    name: str
    players: tuple[PoolPlayer, ...]
    is_mine: bool = False
    # The best unrostered players, which this team could add for nothing.
    # Every lineup is valued with these available, so an "upgrade" is measured
    # against the waiver wire rather than against the team's own backup.
    waiver: tuple[PoolPlayer, ...] = ()


@dataclass(frozen=True)
class PositionNeed:
    position: str
    # Points the lineup would gain from a meaningfully better starter here.
    upgrade_value: float
    starters_required: int
    quality_owned: int


@dataclass(frozen=True)
class TradePackage:
    """One concrete offer, valued from both sides."""

    their_team_id: str
    their_team_name: str
    mode: str
    give: tuple[PoolPlayer, ...]
    receive: tuple[PoolPlayer, ...]
    my_gain: float
    their_gain: float
    # Positive when the draft market prices what we send ABOVE what we receive,
    # which is what makes a lopsided trade look palatable to the other side.
    market_edge: float
    rationale: tuple[str, ...] = ()

    @property
    def total_gain(self) -> float:
        return self.my_gain + self.their_gain


def market_value_curve(
    pool: list[PoolPlayer], availability: dict[str, float] | None = None
) -> dict[str, float]:
    """What the draft market implicitly pays for each player, in points.

    Computed WITHIN each position, which matters more than it sounds. Done
    globally, a kicker taken at pick 86 is priced against whatever the 86th-best
    skill player scores -- so the league's best kicker reads as a 211-point
    asset instead of a 144-point one, and every package containing him looks
    like a heist. Positions score on different scales; the market knows that and
    so must this.

    Comparing a player's market price with his projection separates reputation
    from production, which is the entire basis of the fleece mode: the names the
    room overpays for are the ones to send.

    `availability` (expected share of his healthy rest-of-season points) shades
    the price of injured players. ADP is August's opinion; no manager pays it
    for a back on IR, and unshaded, sending him would read as a generous offer.
    """
    availability = availability or {}
    curve: dict[str, float] = {}
    by_position: dict[str, list[PoolPlayer]] = {}
    for player in pool:
        by_position.setdefault(player.position, []).append(player)
    for players in by_position.values():
        ranked_points = sorted((p.points for p in players), reverse=True)
        with_adp = sorted(
            (p for p in players if p.adp is not None),
            key=lambda p: (p.adp if p.adp is not None else 0.0),
        )
        for market_rank, player in enumerate(with_adp):
            index = min(market_rank, len(ranked_points) - 1)
            curve[player.canonical_id] = ranked_points[index] * availability.get(
                player.canonical_id, 1.0
            )
    return curve


def market_value(player: PoolPlayer, curve: dict[str, float]) -> float:
    """What the room would pay for this player, in points.

    Falls back to his projection when he has no ADP: an undrafted waiver add
    carries no market opinion, and pretending it is zero would make every
    package containing him look like a steal.
    """
    return curve.get(player.canonical_id, player.points)


def market_premium(player: PoolPlayer, curve: dict[str, float]) -> float:
    """How much the market overpays for this player, in projected points.

    Positive means reputation exceeds production -- a sell-high asset. Players
    with no ADP (undrafted, waiver adds) carry no market opinion at all, so they
    are neutral rather than assumed cheap.
    """
    market = curve.get(player.canonical_id)
    if market is None:
        return 0.0
    return market - player.points


def attach_waiver(
    rosters: list[TeamRoster], pool: list[PoolPlayer], settings: LeagueSettings
) -> list[TeamRoster]:
    """Give every roster the same free-agent fallback.

    Without it a QB or kicker "upgrade" was priced against the team's own
    backup: Maye over Shough read as +28 and Aubrey over Jason Myers as +31,
    so Maye + a kicker came back as fair value for CeeDee Lamb -- when a
    streamer off waivers would have closed most of either gap for free.
    Only enough players to fill every slot are kept, since the best legal
    lineup never reaches further down the wire than that.
    """
    rostered = {p.canonical_id for r in rosters for p in r.players}
    flex = sum(
        count for slot, count in settings.roster_slots.items() if slot in FLEX_ELIGIBILITY
    )
    keep: list[PoolPlayer] = []
    for position in ("QB", "RB", "WR", "TE", "K", "DEF"):
        depth = settings.roster_slots.get(position, 0) + flex
        available = sorted(
            (
                p
                for p in pool
                if p.position == position and p.canonical_id not in rostered and p.points > 0
            ),
            key=lambda p: -p.points,
        )
        keep.extend(available[:depth])
    waiver = tuple(keep)
    return [replace(r, waiver=waiver) for r in rosters]


def roster_points(
    roster: TeamRoster, players: list[PoolPlayer], settings: LeagueSettings
) -> float:
    """Best legal lineup from these players plus the free agents he could add."""
    return lineup_points([*players, *roster.waiver], settings.roster_slots)


def starter_benchmarks(pool: list[PoolPlayer], settings: LeagueSettings) -> dict[str, float]:
    """A solidly-above-average starter's projection at each position.

    Needs must be measured against a fixed, league-wide yardstick. Probing with
    the team's OWN best player at the position (the obvious first attempt)
    inverts the metric: a roster starting two bad receivers probes with a bad
    receiver and reports almost no need, while a roster stacked at running back
    probes with a stud and reports a huge one. It ranked RB as the top need for
    a team starting RB1/RB2/RB3 and 120-point receivers.
    """
    benchmarks: dict[str, float] = {}
    for position in ("QB", "RB", "WR", "TE", "K", "DEF"):
        required = settings.roster_slots.get(position, 0)
        if required <= 0:
            continue
        ranked = sorted(
            (p.points for p in pool if p.position == position), reverse=True
        )
        if not ranked:
            continue
        # Halfway down the league's starters at that position: better than a
        # replacement, short of a stud, which is what a trade realistically buys.
        index = min(len(ranked) - 1, max(0, settings.total_teams * required // 2))
        benchmarks[position] = ranked[index]
    return benchmarks


def team_needs(
    roster: TeamRoster, settings: LeagueSettings, benchmarks: dict[str, float]
) -> list[PositionNeed]:
    """Where this roster's starting lineup is weakest, strongest need first.

    Measured by dropping a league-typical starter at each position onto the
    roster and seeing how much the best legal lineup improves. A team already
    starting two good backs gains nothing from another; a team starting one
    gains a lot. That difference is the need, and it is what the other side of
    a trade is actually buying.
    """
    base = roster_points(roster, list(roster.players), settings)
    needs: list[PositionNeed] = []
    for position, benchmark in benchmarks.items():
        probe = PoolPlayer(
            canonical_id=f"__probe_{position}",
            name=f"probe {position}",
            position=position,
            team=None,
            points=benchmark,
        )
        upgraded = roster_points(roster, [*roster.players, probe], settings)
        needs.append(
            PositionNeed(
                position=position,
                upgrade_value=upgraded - base,
                starters_required=settings.roster_slots.get(position, 0),
                quality_owned=sum(1 for p in roster.players if p.position == position),
            )
        )
    needs.sort(key=lambda n: -n.upgrade_value)
    return needs


def _swap(players: tuple[PoolPlayer, ...], out: tuple[PoolPlayer, ...], into: tuple[PoolPlayer, ...]) -> list[PoolPlayer]:
    removed = {p.canonical_id for p in out}
    return [p for p in players if p.canonical_id not in removed] + list(into)


def _cost_to_move(roster: TeamRoster, settings: LeagueSettings, player: PoolPlayer, base: float) -> float:
    """Lineup points this roster loses by giving the player up."""
    without = roster_points(
        roster, [p for p in roster.players if p.canonical_id != player.canonical_id], settings
    )
    return base - without


def _blend(primary: list[PoolPlayer], secondary: list[PoolPlayer]) -> list[PoolPlayer]:
    """Interleave two rankings, keeping each player once.

    Taking the top N of a single ranking is what starved normal mode: ranked by
    "what can I spare", a roster offers only its bench, and a bench never enters
    the other team's lineup, so every package was worth exactly +0.0 to them.
    Real win-win trades live where one side's surplus STARTER meets the other's
    hole, so both rankings have to be represented.
    """
    out: list[PoolPlayer] = []
    seen: set[str] = set()
    for a, b in zip(primary, secondary, strict=False):
        for player in (a, b):
            if player.canonical_id not in seen:
                seen.add(player.canonical_id)
                out.append(player)
    for player in [*primary, *secondary]:
        if player.canonical_id not in seen:
            seen.add(player.canonical_id)
            out.append(player)
    return out[:MAX_CHIPS_PER_SIDE]


def is_acceptable_offer(
    mode: str,
    give: tuple[PoolPlayer, ...],
    my_gain: float,
    their_gain: float,
    edge: float,
    their_needs: list[PositionNeed],
    urgent: set[str],
) -> bool:
    """Would a real manager on the other side even consider this?

    Shared by both searches on purpose. When the anchored search had its own
    (absent) version of this rule it happily proposed a bench receiver for two
    studs -- offers nobody would answer, ranked top because they were best for
    us. A trade the other manager refuses is not a trade, so plausibility is
    part of the engine rather than a presentation concern.
    """
    if my_gain < MIN_GAIN:
        return False
    # Applies in BOTH modes. A package can clear every lineup test and still be
    # refused on sight because the room paid far more for what we are asking
    # than for what we are offering. Bundling a spare bench player is the
    # normal way to close that gap, and the search will do exactly that rather
    # than proposing the bare version that gets ignored.
    if edge < -MAX_MARKET_IMBALANCE:
        return False
    if mode == "normal":
        # Both sides must actually gain, or it is a message that gets ignored.
        # And they must not pay more in market value than they receive: lineup
        # points alone price their bench at ~zero, so this once asked a manager
        # for his 49th- and 81st-pick youngsters in exchange for a QB upgrade
        # worth 1.4 a week. Leaning on the room's pricing is fleece mode's job.
        return their_gain >= MIN_GAIN and edge >= 0.0
    # A fleece still has to LOOK fair or answer a hole they urgently need
    # filled -- managers judge by market value and need, not our projections.
    answers_need = any(p.position in urgent for p in give) or any(
        p.position in {n.position for n in their_needs[:2]} for p in give
    )
    if edge < 0.0 and not answers_need:
        return False
    # ...and it must never be transparent theft.
    return their_gain >= -MAX_THEIR_LOSS


def _send_chips(
    mine: TeamRoster,
    theirs: TeamRoster,
    settings: LeagueSettings,
    curve: dict[str, float],
    mode: str,
) -> list[PoolPlayer]:
    """What we might send: what they most want, blended with what we can spare.

    Fleece mode ranks instead by reputation we can convert -- players the market
    prices above their projection, discounted by what losing them really costs.
    """
    my_base = roster_points(mine, list(mine.players), settings)
    their_base = roster_points(theirs, list(theirs.players), settings)

    def cost_to_me(player: PoolPlayer) -> float:
        return _cost_to_move(mine, settings, player, my_base)

    def gain_to_them(player: PoolPlayer) -> float:
        return roster_points(theirs, [*theirs.players, player], settings) - their_base

    if mode == "fleece":
        return sorted(
            mine.players,
            key=lambda p: market_premium(p, curve) - cost_to_me(p),
            reverse=True,
        )[:MAX_CHIPS_PER_SIDE]
    wanted = sorted(mine.players, key=gain_to_them, reverse=True)
    sparable = sorted(mine.players, key=cost_to_me)
    return _blend(wanted, sparable)


def _target_chips(
    theirs: TeamRoster, mine: TeamRoster, settings: LeagueSettings
) -> list[PoolPlayer]:
    """What is worth asking them for: what we most want, blended with what they
    can most afford to lose. Same reasoning as the sending side."""
    their_base = roster_points(theirs, list(theirs.players), settings)
    my_base = roster_points(mine, list(mine.players), settings)

    def gain_to_me(player: PoolPlayer) -> float:
        return roster_points(mine, [*mine.players, player], settings) - my_base

    def cost_to_them(player: PoolPlayer) -> float:
        return _cost_to_move(theirs, settings, player, their_base)

    wanted = sorted(theirs.players, key=gain_to_me, reverse=True)
    sparable = sorted(theirs.players, key=cost_to_them)
    return _blend(wanted, sparable)


def is_seriously_hurt(player: PoolPlayer) -> bool:
    """Whether his availability is genuinely in doubt, per the structured status."""
    status = (player.status or "").lower()
    return any(marker in status for marker in SERIOUS_INJURY_MARKERS)


def evaluate_package(
    mine: TeamRoster,
    theirs: TeamRoster,
    give: tuple[PoolPlayer, ...],
    receive: tuple[PoolPlayer, ...],
    settings: LeagueSettings,
    curve: dict[str, float],
) -> tuple[float, float, float]:
    """(my gain, their gain, market edge) for one concrete swap."""

    my_before = roster_points(mine, list(mine.players), settings)
    my_after = roster_points(mine, _swap(mine.players, give, receive), settings)
    their_before = roster_points(theirs, list(theirs.players), settings)
    their_after = roster_points(theirs, _swap(theirs.players, receive, give), settings)
    # Total market value on each side, NOT the sum of premiums. Summing
    # premiums made a one-for-two heist read as fair, because each individual
    # player's over/under-valuation is small even when the packages are wildly
    # different sizes. What decides whether an offer is plausible is what the
    # room thinks each side is worth in total.
    edge = sum(market_value(p, curve) for p in give) - sum(
        market_value(p, curve) for p in receive
    )
    return my_after - my_before, their_after - their_before, edge


def _rationale(
    mode: str,
    give: tuple[PoolPlayer, ...],
    receive: tuple[PoolPlayer, ...],
    my_gain: float,
    their_gain: float,
    edge: float,
    their_needs: list[PositionNeed],
    injury_notes: dict[str, str],
) -> tuple[str, ...]:
    lines: list[str] = []
    lines.append(
        f"Your lineup gains {my_gain:+.1f} projected points; theirs gains {their_gain:+.1f}."
    )
    need_positions = {n.position for n in their_needs[:2]}
    filled = [p.position for p in give if p.position in need_positions]
    if filled:
        lines.append(
            f"They are thin at {', '.join(sorted(set(filled)))}, which is what you are sending."
        )
    for player in receive:
        note = injury_notes.get(player.canonical_id)
        if note:
            # Headlines usually lead with the player's own name, so prefixing it
            # again produced "Josh Jacobs: Josh Jacobs: set for limited work".
            headline = note if note.startswith(player.name) else f"{player.name}: {note}"
            if is_seriously_hurt(player):
                headline += (
                    " -- valued only for the games he is expected to play, so the "
                    "gain above already assumes the missed weeks."
                )
            lines.append(headline)
    for player in give:
        note = injury_notes.get(player.canonical_id)
        if note:
            lines.append(f"Note on a player you would be sending -- {note}")
    if mode == "fleece" and edge > 0:
        lines.append(
            f"The draft market prices what you send {edge:.0f} points above what you get back, "
            "so the offer reads as generous while the projections favour you."
        )
    byes_in = {p.bye_week for p in receive if p.bye_week is not None}
    byes_out = {p.bye_week for p in give if p.bye_week is not None}
    if byes_in and byes_in == byes_out:
        lines.append("Bye weeks are unchanged by this swap.")
    return tuple(lines)


def generate_packages(
    mine: TeamRoster,
    others: list[TeamRoster],
    settings: LeagueSettings,
    pool: list[PoolPlayer],
    mode: str = "normal",
    injury_notes: dict[str, str] | None = None,
    needy_teams: dict[str, list[str]] | None = None,
    limit: int = 12,
    availability: dict[str, float] | None = None,
) -> list[TradePackage]:
    """Rank plausible offers against every other team in the league.

    Normal mode requires both sides to gain, because a trade nobody benefits
    from is not an offer, it is a message that gets ignored. Fleece mode relaxes
    the other side's gain to "not obviously terrible for them" and instead
    maximises the gap between what we get and what the market thinks we gave.
    """
    injury_notes = injury_notes or {}
    needy_teams = needy_teams or {}
    curve = market_value_curve(pool, availability)
    benchmarks = starter_benchmarks(pool, settings)
    packages: list[TradePackage] = []

    for other in others:
        their_needs = team_needs(other, settings, benchmarks)
        my_chips = _send_chips(mine, other, settings, curve, mode)
        their_chips = _target_chips(other, mine, settings)
        urgent = set(needy_teams.get(other.team_id, ()))
        for take_mine, take_theirs in PACKAGE_SHAPES:
            for give in combinations(my_chips, take_mine):
                for receive in combinations(their_chips, take_theirs):
                    my_gain, their_gain, edge = evaluate_package(
                        mine, other, give, receive, settings, curve
                    )
                    if not is_acceptable_offer(
                        mode, give, my_gain, their_gain, edge, their_needs, urgent
                    ):
                        continue
                    packages.append(
                        TradePackage(
                            their_team_id=other.team_id,
                            their_team_name=other.name,
                            mode=mode,
                            give=give,
                            receive=receive,
                            my_gain=my_gain,
                            their_gain=their_gain,
                            market_edge=edge,
                            rationale=_rationale(
                                mode, give, receive, my_gain, their_gain, edge,
                                their_needs, injury_notes,
                            ),
                        )
                    )

    if mode == "normal":
        packages.sort(key=lambda p: (-(p.my_gain + 0.25 * p.their_gain), -p.their_gain))
    else:
        packages.sort(key=lambda p: (-(p.my_gain + 0.5 * p.market_edge), -p.my_gain))

    # One offer per opposing team keeps the list readable and stops a single
    # lopsided roster from filling every slot with variations of one idea.
    seen: dict[str, int] = {}
    top: list[TradePackage] = []
    for package in packages:
        count = seen.get(package.their_team_id, 0)
        if count >= 2:
            continue
        seen[package.their_team_id] = count + 1
        top.append(package)
        if len(top) >= limit:
            break
    return top


def packages_around(
    anchor: PoolPlayer,
    mine: TeamRoster,
    others: list[TeamRoster],
    settings: LeagueSettings,
    pool: list[PoolPlayer],
    mode: str = "normal",
    limit: int = 6,
    injury_notes: dict[str, str] | None = None,
    needy_teams: dict[str, list[str]] | None = None,
    availability: dict[str, float] | None = None,
) -> list[TradePackage]:
    """Offers built around one specific player the user picked.

    Different question from `generate_packages`, which asks "what trades are
    worth making at all". Here the player is fixed and the search runs over the
    other side only: the user has already decided who he wants, or who he is
    willing to lose, and needs to know what it would take.

    Fixing the player does NOT relax who would accept it. An earlier version
    skipped the plausibility rule so that it always had something to show, and
    the result was offers like one bench receiver for two studs -- ranked top
    precisely because they were the most lopsided in our favour. Every package
    here clears exactly the same bar as a generated one; when nothing does, the
    honest answer is an empty list.
    """
    injury_notes = injury_notes or {}
    needy_teams = needy_teams or {}
    curve = market_value_curve(pool, availability)
    benchmarks = starter_benchmarks(pool, settings)
    mine_ids = {p.canonical_id for p in mine.players}
    anchor_is_mine = anchor.canonical_id in mine_ids
    packages: list[TradePackage] = []

    for other in others:
        their_ids = {p.canonical_id for p in other.players}
        if not anchor_is_mine and anchor.canonical_id not in their_ids:
            continue
        their_needs = team_needs(other, settings, benchmarks)
        urgent = set(needy_teams.get(other.team_id, ()))
        # Anchored search deliberately widens the side being searched. The
        # chip heuristics rank by "who can we spare" / "who can they spare",
        # which is right when hunting for any trade at all but wrong once the
        # user has named a target: asking for a specific player usually means
        # accepting that a starter goes the other way. Restricted to chips, the
        # search could not even see A.J. Brown for Nico Collins -- a swap both
        # sides price as fair -- because Brown is too useful to be a "chip".
        # Only one side is enumerated here, so the wider list stays cheap.
        if anchor_is_mine:
            my_chips = [
                p for p in _send_chips(mine, other, settings, curve, mode)
                if p.canonical_id != anchor.canonical_id
            ]
            their_chips = [
                p for p in _target_chips(other, mine, settings)
                if p.canonical_id != anchor.canonical_id
            ][:ANCHOR_SEARCH_WIDTH]
        else:
            my_chips = [
                p for p in mine.players if p.canonical_id != anchor.canonical_id
            ]
            their_chips = [
                p for p in _target_chips(other, mine, settings)
                if p.canonical_id != anchor.canonical_id
            ]

        shapes: tuple[tuple[int, int], ...]
        if anchor_is_mine:
            # He is going out; search what this team could send back. Sending a
            # second player of my own has to stay possible: a stud alone is
            # often not enough to pry loose what I want.
            shapes = ((0, 1), (0, 2), (1, 1), (1, 2))
        else:
            # He is coming in; search what I could pay. Up to three out, since
            # acquiring an elite player usually costs more than one asset.
            # Same (extras, searched) ordering as the branch above: extras are
            # the players riding along WITH the anchor, the searched side is
            # what I send.
            shapes = ((0, 1), (0, 2), (0, 3), (1, 1), (1, 2))

        for a_count, b_count in shapes:
            extras_source = my_chips if anchor_is_mine else their_chips
            search_source = their_chips if anchor_is_mine else my_chips
            for extras in combinations(extras_source, a_count):
                for chosen in combinations(search_source, b_count):
                    if anchor_is_mine:
                        give = (anchor, *extras)
                        receive = chosen
                    else:
                        give = chosen
                        receive = (anchor, *extras)
                    if not give or not receive:
                        continue
                    my_gain, their_gain, edge = evaluate_package(
                        mine, other, give, receive, settings, curve
                    )
                    if not is_acceptable_offer(
                        mode, give, my_gain, their_gain, edge, their_needs, urgent
                    ):
                        continue
                    packages.append(
                        _anchored_package(
                            mine, other, give, receive, settings, curve, mode,
                            their_needs, injury_notes,
                        )
                    )

    # Everything here already clears the acceptance bar, so rank by value to us
    # and then prefer the SMALLEST package: a bench player the lineup never
    # starts costs zero projected points, so without this the search bundles
    # free bodies in purely to even up market value -- real depth given away
    # for optics.
    packages.sort(
        key=lambda p: (round(p.my_gain, 1), -len(p.give), -abs(p.market_edge)),
        reverse=True,
    )
    seen: set[tuple[str, ...]] = set()
    unique: list[TradePackage] = []
    for package in packages:
        key = (
            package.their_team_id,
            *sorted(p.canonical_id for p in package.give),
            *sorted(p.canonical_id for p in package.receive),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(package)
        if len(unique) >= limit:
            break
    return unique


def _anchored_package(
    mine: TeamRoster,
    other: TeamRoster,
    give: tuple[PoolPlayer, ...],
    receive: tuple[PoolPlayer, ...],
    settings: LeagueSettings,
    curve: dict[str, float],
    mode: str,
    their_needs: list[PositionNeed],
    injury_notes: dict[str, str],
) -> TradePackage:
    my_gain, their_gain, edge = evaluate_package(
        mine, other, give, receive, settings, curve
    )
    return TradePackage(
        their_team_id=other.team_id,
        their_team_name=other.name,
        mode=mode,
        give=give,
        receive=receive,
        my_gain=my_gain,
        their_gain=their_gain,
        market_edge=edge,
        rationale=_rationale(
            mode, give, receive, my_gain, their_gain, edge, their_needs, injury_notes
        ),
    )
