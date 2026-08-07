"""News feed endpoints (spec 7.4): relevant, deduped, unpadded."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.db import get_sessionmaker
from api.models import League, LeagueRoster, NewsItem, Player, PlayerIds

router = APIRouter(prefix="/news", tags=["news"])


def db_session() -> Iterator[Session]:
    with get_sessionmaker()() as session:
        yield session


class NewsEntry(BaseModel):
    id: int
    source: str
    title: str
    url: str | None
    published_at: datetime | None
    tag: str
    player: str | None
    position: str | None
    team: str | None
    on_my_roster: bool


def _my_rostered_ids(session: Session, league_id: int | None) -> set[int]:
    query = select(LeagueRoster).where(LeagueRoster.is_mine.is_(True))
    if league_id is not None:
        query = query.where(LeagueRoster.league_id == league_id)
    ids: set[int] = set()
    for roster in session.scalars(query):
        ids.update(int(pid) for pid in (roster.player_ids or []) if str(pid).isdigit())
    return ids


@router.get("")
def list_news(
    session: Annotated[Session, Depends(db_session)],
    league_id: int | None = None,
    hours: Annotated[int, Query(ge=1, le=336)] = 48,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    mine_only: bool = False,
    top_available: Annotated[int, Query(ge=0, le=500)] = 200,
) -> list[NewsEntry]:
    """Recent player news, filtered to players who could matter to you.

    Relevance is your rostered players plus the top `top_available` by search
    rank, per spec 7.4. Items with no identified player are excluded: a feed
    padded with league-wide articles is the thing this is meant to avoid.
    """
    if league_id is not None and session.get(League, league_id) is None:
        raise HTTPException(status_code=404, detail=f"league {league_id} is not registered")

    mine = _my_rostered_ids(session, league_id)
    if mine_only and not mine:
        return []

    since = datetime.now(UTC) - timedelta(hours=hours)
    rows = session.execute(
        select(NewsItem, PlayerIds.full_name, Player.position, Player.team, Player.search_rank)
        .join(PlayerIds, PlayerIds.canonical_id == NewsItem.canonical_id)
        .join(Player, Player.canonical_id == NewsItem.canonical_id)
        .where(NewsItem.canonical_id.is_not(None), NewsItem.published_at >= since)
        .order_by(NewsItem.published_at.desc())
    ).all()

    seen: set[tuple[int, str]] = set()
    entries: list[NewsEntry] = []
    for item, name, position, team, rank in rows:
        on_roster = item.canonical_id in mine
        if mine_only and not on_roster:
            continue
        relevant = on_roster or (rank is not None and rank <= top_available)
        if not relevant:
            continue
        # Dedupe by player + tag: the same story arrives from several feeds.
        key = (item.canonical_id or 0, item.tag)
        if key in seen:
            continue
        seen.add(key)
        entries.append(
            NewsEntry(
                id=item.id,
                source=item.source,
                title=item.title,
                url=item.url,
                published_at=item.published_at,
                tag=item.tag,
                player=name,
                position=position,
                team=team,
                on_my_roster=on_roster,
            )
        )
        if len(entries) >= limit:
            break
    return entries
