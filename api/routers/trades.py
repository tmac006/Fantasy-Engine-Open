"""Trade proposals for a registered league.

Two modes share one valuation: *normal* looks for offers both managers gain
from, *fleece* looks for offers the market prices as fair (or that answer a
hole the other manager urgently needs filled) while the projections favour us.

The other side's urgency is read from the news feed rather than guessed: a
manager who just lost a starting back to injury is short at running back today,
whatever his roster looked like on draft night.
"""

from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.db import get_sessionmaker
from api.engine.lineup import fill_lineup
from api.engine.restofseason import RestOfSeason
from api.engine.trades import (
    TeamRoster,
    attach_waiver,
    evaluate_package,
    generate_packages,
    is_seriously_hurt,
    market_premium,
    market_value_curve,
    packages_around,
    roster_points,
    starter_benchmarks,
    team_needs,
)
from api.engine.vor import PoolPlayer
from api.headshots import headshot_url
from api.inseason import rest_of_season_pool
from api.models import League, LeagueRoster, LeagueSettingsRow, NewsItem, PlayerIds
from api.schemas import LeagueSettings
from api.services import build_pool

router = APIRouter(prefix="/leagues", tags=["trades"])

# How far back a news item still says something about a roster's current needs.
NEWS_WINDOW_HOURS = 240


def db_session() -> Iterator[Session]:
    with get_sessionmaker()() as session:
        yield session


class PlayerCard(BaseModel):
    canonical_id: str
    name: str
    position: str
    team: str | None
    points: float
    adp: float | None
    bye_week: int | None
    market_value: float
    market_premium: float
    headshot_url: str | None
    news: str | None = None
    # What this player alone would add to MY starting lineup. The honest
    # "would he help me" number: a 300-point receiver behind two better ones
    # adds nothing, however good he looks in isolation.
    solo_gain: float | None = None
    recommended: bool = False


class NeedOut(BaseModel):
    position: str
    upgrade_value: float
    owned: int


class PackageOut(BaseModel):
    their_team_id: str
    their_team_name: str
    give: list[PlayerCard]
    receive: list[PlayerCard]
    my_gain: float
    their_gain: float
    market_edge: float
    rationale: list[str]


class TradesOut(BaseModel):
    league_id: int
    league_name: str | None
    season: str
    mode: str
    # Player points are rest-of-season from this week through the fantasy
    # final, not preseason full-season totals.
    as_of_week: int
    my_team_name: str
    my_needs: list[NeedOut]
    packages: list[PackageOut]
    # Teams whose recent news suggests an urgent hole, and at which position.
    injury_signals: list[str]


class TradeSideOut(BaseModel):
    """One roster's view of the swap."""

    team_id: str
    team_name: str
    players: list[PlayerCard]
    lineup_before: float
    lineup_after: float
    lineup_gain: float
    market_value_sent: float


class WeekImpact(BaseModel):
    week: int
    before: float
    after: float
    slots_before: int
    slots_after: int


class TradeEvalOut(BaseModel):
    """A manually built trade, valued the same way generated ones are."""

    league_id: int
    my_team_name: str
    give: list[PlayerCard]
    receive: list[PlayerCard]
    my_gain: float
    their_gain: float
    market_edge: float
    # Averaged over the remaining weeks with each side's byes and missed games
    # applied, which flat totals cannot see: a player idle in a week you are
    # already thin contributes nothing that week however good his season looks.
    bye_adjusted_gain_per_week: float
    worst_week_before: WeekImpact | None
    worst_week_after: WeekImpact | None
    roster_size_after: int
    roster_limit: int
    verdict: str
    notes: list[str]
    my_side: TradeSideOut
    their_side: TradeSideOut


class RosterOut(BaseModel):
    team_id: str
    team_name: str
    is_mine: bool
    players: list[PlayerCard]


class RostersOut(BaseModel):
    league_id: int
    my_team_id: str
    my_needs: list[NeedOut]
    rosters: list[RosterOut]


def _valued_pool(
    session: Session, league: League, settings: LeagueSettings
) -> tuple[list[PoolPlayer], dict[str, RestOfSeason], int]:
    """The draft pool re-valued on rest-of-season points.

    Every trade endpoint values through this, so a generated package and a
    hand-built one can never be priced on different clocks. Preseason totals
    are the right number in August and the wrong one for trading future points:
    they kept an IR back at his full 257-point season and built every offer
    around him.
    """
    pool = build_pool(session, settings, season=league.season, platform=league.platform)
    return rest_of_season_pool(session, league, settings, pool)


def _availability(schedules: dict[str, RestOfSeason]) -> dict[str, float]:
    return {
        canonical_id: schedule.availability_factor
        for canonical_id, schedule in schedules.items()
        if schedule.availability_factor < 1.0
    }


def _load_league(session: Session, league_id: int) -> tuple[League, LeagueSettings]:
    league = session.get(League, league_id)
    if league is None:
        raise HTTPException(status_code=404, detail=f"league {league_id} not registered")
    row = session.get(LeagueSettingsRow, league_id)
    if row is None:
        raise HTTPException(
            status_code=409, detail="league has not synced its settings yet"
        )
    return league, LeagueSettings(
        scoring=dict(row.scoring),
        roster_slots=dict(row.roster_slots),
        total_teams=row.total_teams,
    )


def _recent_player_news(session: Session, canonical_ids: set[int]) -> dict[str, str]:
    """Latest injury/depth headline per player, for the ones we care about."""
    if not canonical_ids:
        return {}
    cutoff = datetime.now(UTC) - timedelta(hours=NEWS_WINDOW_HOURS)
    rows = session.scalars(
        select(NewsItem)
        .where(
            NewsItem.canonical_id.in_(canonical_ids),
            NewsItem.tag.in_(("injury", "depth_chart")),
            NewsItem.published_at.is_not(None),
            NewsItem.published_at >= cutoff,
        )
        .order_by(NewsItem.published_at.desc())
    ).all()
    latest: dict[str, str] = {}
    for item in rows:
        key = str(item.canonical_id)
        if key not in latest:
            latest[key] = item.title
    return latest


@router.get("/{league_id}/trades", response_model=TradesOut)
def league_trades(
    league_id: int,
    session: Annotated[Session, Depends(db_session)],
    mode: Annotated[Literal["normal", "fleece"], Query()] = "normal",
    limit: Annotated[int, Query(ge=1, le=30)] = 12,
) -> TradesOut:
    league, settings = _load_league(session, league_id)
    pool, schedules, week = _valued_pool(session, league, settings)
    availability = _availability(schedules)
    by_id = {player.canonical_id: player for player in pool}

    roster_rows = session.scalars(
        select(LeagueRoster).where(LeagueRoster.league_id == league_id)
    ).all()
    if not roster_rows:
        raise HTTPException(
            status_code=409,
            detail="no rosters stored for this league yet -- run a league sync first",
        )

    rosters: list[TeamRoster] = []
    for row in roster_rows:
        players = tuple(
            by_id[str(pid)] for pid in (row.player_ids or []) if str(pid) in by_id
        )
        rosters.append(
            TeamRoster(
                team_id=row.platform_roster_id,
                name=row.owner or f"Team {row.platform_roster_id}",
                players=players,
                is_mine=bool(row.is_mine),
            )
        )
    rosters = attach_waiver(rosters, pool, settings)
    mine = next((r for r in rosters if r.is_mine), None)
    if mine is None:
        raise HTTPException(
            status_code=409,
            detail="this league has no team marked as yours -- set my_team_id and re-sync",
        )
    others = [r for r in rosters if not r.is_mine and r.players]
    # An undrafted league has roster rows but no players in them. Returning an
    # empty package list there reads as "nothing worth proposing", which is a
    # very different statement from "this league has not drafted yet".
    if not mine.players:
        raise HTTPException(
            status_code=409,
            detail="your roster in this league is empty -- it has not drafted yet",
        )
    if not others:
        raise HTTPException(
            status_code=409,
            detail="no opposing rosters have players yet -- this league has not drafted",
        )

    all_ids = {int(p.canonical_id) for r in rosters for p in r.players if p.canonical_id.isdigit()}
    news = _recent_player_news(session, all_ids)

    # A manager with a freshly injured starter is short at that position now,
    # whatever his draft looked like. That urgency is what a fleece trades on.
    needy: dict[str, list[str]] = {}
    signals: list[str] = []
    for roster in others:
        hurt = [p for p in roster.players if p.canonical_id in news]
        if not hurt:
            continue
        needy[roster.team_id] = sorted({p.position for p in hurt})
        for player in hurt[:2]:
            signals.append(f"{roster.name}: {player.name} ({player.position}) — {news[player.canonical_id]}")

    curve = market_value_curve(pool, availability)
    benchmarks = starter_benchmarks(pool, settings)

    def card(player: PoolPlayer) -> PlayerCard:
        ids_row = session.get(PlayerIds, int(player.canonical_id)) if player.canonical_id.isdigit() else None
        return PlayerCard(
            canonical_id=player.canonical_id,
            name=player.name,
            position=player.position,
            team=player.team,
            points=round(player.points, 1),
            adp=player.adp,
            bye_week=player.bye_week,
            market_value=round(curve.get(player.canonical_id, player.points), 1),
            market_premium=round(market_premium(player, curve), 1),
            headshot_url=headshot_url(
                ids_row.espn_id if ids_row else None, player.position, player.team
            ),
            news=news.get(player.canonical_id),
        )

    packages = generate_packages(
        mine, others, settings, pool, mode=mode, injury_notes=news,
        needy_teams=needy, limit=limit, availability=availability,
    )
    return TradesOut(
        league_id=league_id,
        league_name=league.name,
        season=league.season,
        mode=mode,
        as_of_week=week,
        my_team_name=mine.name,
        my_needs=[
            NeedOut(
                position=need.position,
                upgrade_value=round(need.upgrade_value, 1),
                owned=need.quality_owned,
            )
            for need in team_needs(mine, settings, benchmarks)
        ],
        packages=[
            PackageOut(
                their_team_id=package.their_team_id,
                their_team_name=package.their_team_name,
                give=[card(p) for p in package.give],
                receive=[card(p) for p in package.receive],
                my_gain=round(package.my_gain, 1),
                their_gain=round(package.their_gain, 1),
                market_edge=round(package.market_edge, 1),
                rationale=list(package.rationale),
            )
            for package in packages
        ],
        injury_signals=signals[:8],
    )


def _context(
    session: Session, league_id: int
) -> tuple[
    League,
    LeagueSettings,
    list[PoolPlayer],
    list[TeamRoster],
    TeamRoster,
    dict[str, float],
    Callable[..., PlayerCard],
    dict[str, RestOfSeason],
]:
    """Everything both manual-trade endpoints need, assembled once.

    Deliberately mirrors what `league_trades` builds so a hand-made trade is
    valued by exactly the same machinery as a generated one -- if the two drifted,
    the tool would recommend a package and then grade it differently.
    """
    league, settings = _load_league(session, league_id)
    pool, schedules, _week = _valued_pool(session, league, settings)
    by_id = {player.canonical_id: player for player in pool}

    roster_rows = session.scalars(
        select(LeagueRoster).where(LeagueRoster.league_id == league_id)
    ).all()
    if not roster_rows:
        raise HTTPException(
            status_code=409,
            detail="no rosters stored for this league yet -- run a league sync first",
        )
    rosters = [
        TeamRoster(
            team_id=row.platform_roster_id,
            name=row.owner or f"Team {row.platform_roster_id}",
            players=tuple(by_id[str(pid)] for pid in (row.player_ids or []) if str(pid) in by_id),
            is_mine=bool(row.is_mine),
        )
        for row in roster_rows
    ]
    rosters = attach_waiver(rosters, pool, settings)
    mine = next((r for r in rosters if r.is_mine), None)
    if mine is None or not mine.players:
        raise HTTPException(
            status_code=409,
            detail="your roster in this league is empty -- it has not drafted yet",
        )
    all_ids = {int(p.canonical_id) for r in rosters for p in r.players if p.canonical_id.isdigit()}
    news = _recent_player_news(session, all_ids)
    curve = market_value_curve(pool, _availability(schedules))

    espn_ids = {
        str(row.canonical_id): row.espn_id
        for row in session.scalars(select(PlayerIds).where(PlayerIds.canonical_id.in_(all_ids)))
    }
    base = roster_points(mine, list(mine.players), settings)

    def card(player: PoolPlayer, *, with_gain: bool = False) -> PlayerCard:
        gain = None
        if with_gain:
            gain = round(
                roster_points(mine, [*mine.players, player], settings) - base, 1
            )
        return PlayerCard(
            canonical_id=player.canonical_id,
            name=player.name,
            position=player.position,
            team=player.team,
            points=round(player.points, 1),
            adp=player.adp,
            bye_week=player.bye_week,
            market_value=round(curve.get(player.canonical_id, player.points), 1),
            market_premium=round(market_premium(player, curve), 1),
            headshot_url=headshot_url(
                espn_ids.get(player.canonical_id), player.position, player.team
            ),
            news=news.get(player.canonical_id),
            solo_gain=gain,
            # "Recommended" means he would actually walk into my lineup, not
            # that he is famous. Anything that adds nothing is not a target.
            recommended=bool(with_gain and gain is not None and gain > 0.0),
        )

    return league, settings, pool, rosters, mine, curve, card, schedules


@router.get("/{league_id}/trades/rosters", response_model=RostersOut)
def league_rosters(
    league_id: int,
    session: Annotated[Session, Depends(db_session)],
) -> RostersOut:
    """Every team's roster, with opponents' players scored against MY lineup.

    This is the browsing view behind the trade builder: pick a team, see who on
    it would genuinely improve your starting eleven rather than who merely has
    the biggest projection.
    """
    _league, settings, pool, rosters, mine, _curve, card, _schedules = _context(
        session, league_id
    )
    benchmarks = starter_benchmarks(pool, settings)
    return RostersOut(
        league_id=league_id,
        my_team_id=mine.team_id,
        my_needs=[
            NeedOut(
                position=need.position,
                upgrade_value=round(need.upgrade_value, 1),
                owned=need.quality_owned,
            )
            for need in team_needs(mine, settings, benchmarks)
        ],
        rosters=[
            RosterOut(
                team_id=roster.team_id,
                team_name=roster.name,
                is_mine=roster.is_mine,
                players=sorted(
                    (card(p, with_gain=not roster.is_mine) for p in roster.players),
                    key=lambda c: -c.points,
                ),
            )
            for roster in rosters
            if roster.players
        ],
    )


class TradeRequest(BaseModel):
    """Canonical ids for a hand-built offer."""

    give: list[str]
    receive: list[str]


def _weekly_curve(
    players: list[PoolPlayer],
    roster_slots: dict[str, int],
    schedules: dict[str, RestOfSeason],
) -> list[tuple[int, float, int]]:
    """(week, points, slots filled) for each remaining week.

    Season totals price a player as though he plays every week. He does not,
    and a roster can look strong on paper while being unable to field a legal
    lineup in the one week half of it is idle -- or the four an IR starter
    sits out. Each week is filled from that week's expected points, so byes and
    missed games both empty the slot.
    """
    weeks = sorted({week for schedule in schedules.values() for week in schedule.weekly})
    curve: list[tuple[int, float, int]] = []
    for week in weeks:
        available = []
        for player in players:
            schedule = schedules.get(player.canonical_id)
            points = schedule.weekly.get(week, 0.0) if schedule else 0.0
            if points > 0.0:
                available.append(replace(player, points=points))
        starters, _ = fill_lineup(available, roster_slots)
        curve.append((week, sum(p.points for p in starters), len(starters)))
    return curve


@router.post("/{league_id}/trades/evaluate", response_model=TradeEvalOut)
def evaluate_trade(
    league_id: int,
    session: Annotated[Session, Depends(db_session)],
    request: Annotated[TradeRequest, Body()],
) -> TradeEvalOut:
    """Value a trade the user built by hand.

    Reported on three axes because they routinely disagree, and a trade that
    looks good on one can be bad on another: lineup gain (what actually scores),
    market edge (whether the other manager will entertain it), and the bye-week
    picture (whether the gain survives contact with the schedule).
    """
    _league, settings, _pool, rosters, mine, curve, card, schedules = _context(
        session, league_id
    )
    all_ids = {int(p.canonical_id) for r in rosters for p in r.players if p.canonical_id.isdigit()}
    injury_notes = _recent_player_news(session, all_ids)
    owned = {p.canonical_id: r for r in rosters for p in r.players}
    mine_by_id = {p.canonical_id: p for p in mine.players}

    give = tuple(mine_by_id[i] for i in request.give if i in mine_by_id)
    if len(give) != len(request.give):
        missing = [i for i in request.give if i not in mine_by_id]
        raise HTTPException(
            status_code=422,
            detail=f"not on your roster: {', '.join(missing)}",
        )

    their_rosters = {owned[i].team_id for i in request.receive if i in owned}
    if len(their_rosters) > 1:
        raise HTTPException(
            status_code=422,
            detail="a trade is with one manager -- those players are on different teams",
        )
    if not their_rosters:
        raise HTTPException(status_code=422, detail="none of those players are rostered")
    # Pop ONCE. Inside the generator's condition this re-evaluates per
    # candidate roster and drains the set on the first non-match.
    their_team_id = their_rosters.pop()
    theirs = next(r for r in rosters if r.team_id == their_team_id)
    theirs_by_id = {p.canonical_id: p for p in theirs.players}
    receive = tuple(theirs_by_id[i] for i in request.receive if i in theirs_by_id)
    if not give or not receive:
        raise HTTPException(status_code=422, detail="both sides need at least one player")

    my_gain, their_gain, edge = evaluate_package(
        mine, theirs, give, receive, settings, curve
    )

    before = list(mine.players)
    after = [p for p in mine.players if p not in give] + list(receive)
    weeks_before = _weekly_curve(before, settings.roster_slots, schedules)
    weeks_after = _weekly_curve(after, settings.roster_slots, schedules)
    weeks_left = max(1, len(weeks_before))
    bye_gain = (
        sum(a[1] for a in weeks_after) - sum(b[1] for b in weeks_before)
    ) / weeks_left

    def impact(worst: tuple[int, float, int] | None) -> WeekImpact | None:
        # None once the fantasy season is over: there is no week left to be short in.
        if worst is None:
            return None
        before_week = next(w for w in weeks_before if w[0] == worst[0])
        after_week = next(w for w in weeks_after if w[0] == worst[0])
        return WeekImpact(
            week=worst[0],
            before=round(before_week[1], 1),
            after=round(after_week[1], 1),
            slots_before=before_week[2],
            slots_after=after_week[2],
        )

    worst_before = min(weeks_before, key=lambda w: w[1], default=None)
    worst_after = min(weeks_after, key=lambda w: w[1], default=None)

    limit = sum(settings.roster_slots.values())
    size_after = len(after)

    notes: list[str] = []
    if size_after > limit:
        notes.append(
            f"you would be at {size_after} players against a {limit} limit -- "
            f"{size_after - limit} would have to be dropped"
        )
    if my_gain > 0 and bye_gain * weeks_left > my_gain * 1.25:
        notes.append("worth more than the flat projection says: it eases a bye-week pileup")
    if my_gain > 0 and bye_gain <= 0:
        notes.append("the season-total gain does not survive the bye schedule")
    if edge < -20:
        notes.append(
            f"you receive {abs(edge):.0f} more points of draft-market value than you send, "
            "so expect resistance"
        )
    elif edge > 20:
        notes.append(f"you send {edge:.0f} more points of market value than you receive")
    if their_gain < 0:
        notes.append("it costs them lineup points, so they need another reason to say yes")
    hurt_incoming = [p.name for p in receive if is_seriously_hurt(p)]
    if hurt_incoming:
        notes.append(
            f"you would be taking on an availability question: {', '.join(hurt_incoming)} "
            "(valued above only for the games he is expected to play)"
        )
    watch = [p.name for p in receive if p.canonical_id in injury_notes and not is_seriously_hurt(p)]
    if watch:
        notes.append(f"recent news worth reading first on: {', '.join(watch)}")
    surplus = [p for p in receive if p.position in {q.position for q in give}]
    if len(receive) > len(give) and surplus:
        notes.append(
            "more bodies than you send at the same position -- the extra one mostly rides "
            "the bench, which is why the lineup gain is smaller than the point totals"
        )

    if my_gain >= 12 and their_gain >= 0:
        verdict = "good for both"
    elif my_gain >= 12:
        verdict = "good for you"
    elif my_gain > 2:
        verdict = "marginal gain"
    elif my_gain >= -2:
        verdict = "roughly neutral"
    else:
        verdict = "loses you points"

    def side(roster: TeamRoster, players: tuple[PoolPlayer, ...], swap_in: tuple[PoolPlayer, ...]) -> TradeSideOut:
        start = roster_points(roster, list(roster.players), settings)
        end = roster_points(
            roster, [p for p in roster.players if p not in players] + list(swap_in), settings
        )
        return TradeSideOut(
            team_id=roster.team_id,
            team_name=roster.name,
            players=[card(p) for p in players],
            lineup_before=round(start, 1),
            lineup_after=round(end, 1),
            lineup_gain=round(end - start, 1),
            market_value_sent=round(sum(curve.get(p.canonical_id, p.points) for p in players), 1),
        )

    return TradeEvalOut(
        league_id=league_id,
        my_team_name=mine.name,
        give=[card(p) for p in give],
        receive=[card(p, with_gain=True) for p in receive],
        my_gain=round(my_gain, 1),
        their_gain=round(their_gain, 1),
        market_edge=round(edge, 1),
        bye_adjusted_gain_per_week=round(bye_gain, 2),
        worst_week_before=impact(worst_before),
        worst_week_after=impact(worst_after),
        roster_size_after=size_after,
        roster_limit=limit,
        verdict=verdict,
        notes=notes,
        my_side=side(mine, give, receive),
        their_side=side(theirs, receive, give),
    )


class BuildAroundOut(BaseModel):
    """Offers constructed around one player the user picked."""

    league_id: int
    anchor: PlayerCard
    anchor_is_mine: bool
    mode: str
    packages: list[PackageOut]


@router.get("/{league_id}/trades/around/{canonical_id}", response_model=BuildAroundOut)
def build_around(
    league_id: int,
    canonical_id: str,
    session: Annotated[Session, Depends(db_session)],
    mode: Annotated[Literal["normal", "fleece"], Query()] = "normal",
    limit: Annotated[int, Query(ge=1, le=12)] = 6,
) -> BuildAroundOut:
    """What would it take to trade for (or away) this specific player.

    The player is fixed and only the other side of the deal is searched, which
    is the question a manager actually asks once he has decided who he wants.
    """
    _league, settings, pool, rosters, mine, _curve, card, schedules = _context(
        session, league_id
    )
    everyone = {p.canonical_id: p for r in rosters for p in r.players}
    anchor = everyone.get(canonical_id)
    if anchor is None:
        raise HTTPException(
            status_code=404, detail="that player is not on any roster in this league"
        )
    anchor_is_mine = any(p.canonical_id == canonical_id for p in mine.players)
    others = [r for r in rosters if not r.is_mine and r.players]

    # Fleece mode leans on who is short-handed RIGHT NOW, so the same news
    # signal the generated packages use has to reach this search too.
    all_ids = {int(p.canonical_id) for r in rosters for p in r.players if p.canonical_id.isdigit()}
    news = _recent_player_news(session, all_ids)
    needy: dict[str, list[str]] = {}
    for other in others:
        hurt = sorted({p.position for p in other.players if p.canonical_id in news})
        if hurt:
            needy[other.team_id] = hurt

    packages = packages_around(
        anchor, mine, others, settings, pool, mode=mode, limit=limit,
        injury_notes=news, needy_teams=needy, availability=_availability(schedules),
    )
    return BuildAroundOut(
        league_id=league_id,
        anchor=card(anchor, with_gain=not anchor_is_mine),
        anchor_is_mine=anchor_is_mine,
        mode=mode,
        packages=[
            PackageOut(
                their_team_id=p.their_team_id,
                their_team_name=p.their_team_name,
                give=[card(x) for x in p.give],
                receive=[card(x) for x in p.receive],
                my_gain=round(p.my_gain, 1),
                their_gain=round(p.their_gain, 1),
                market_edge=round(p.market_edge, 1),
                rationale=list(p.rationale),
            )
            for p in packages
        ],
    )
