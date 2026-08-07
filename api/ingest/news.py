"""Player news aggregation (spec 7.4).

Aggregate, identify the player, tag by type, dedupe. No sentiment scoring and
no breakout detection — a clean, relevant, unpadded feed.

Feeds were verified live on 2026-08-05; four commonly-cited ones (FantasyPros,
NFL.com, Footballguys, DraftSharks) return 404 and are deliberately absent.
Re-run `verify_feeds()` before trusting this list again — feeds rot.

Idempotent. Run: `uv run python -m api.ingest.news`
"""

import html
import logging
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.ingest.players import make_merge_name
from api.models import NewsItem, Player, PlayerIds

log = logging.getLogger("ingest.news")

FEEDS: dict[str, str] = {
    # Player-centric; headlines arrive as "Player Name: what happened".
    "rotowire": "https://www.rotowire.com/rss/news.php?sport=NFL",
    "pft": "https://profootballtalk.nbcsports.com/feed/",
    "espn": "https://www.espn.com/espn/rss/nfl/news",
    "cbs": "https://www.cbssports.com/rss/headlines/nfl/",
    "yahoo": "https://sports.yahoo.com/nfl/rss.xml",
    "sportingnews": "https://www.sportingnews.com/us/rss/nfl",
}

_ATOM = "{http://www.w3.org/2005/Atom}"
_TAG_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    # Stems, because the patterns anchor on a word boundary at the START only:
    # "practic" catches practice/practicing/practiced, "practice" does not.
    ("injury", ("injur", "hurt", "questionable", "doubtful", "out for", "pup",
                "concussion", "acl", "hamstring", "ankle", "knee", "surger",
                "practic", "limited", "dnp", "sidelin", "activated off")),
    ("transaction", ("sign", "trade", "waiv", "releas", "claim", "activat",
                     "cut ", "acquir", "extension", "contract")),
    ("depth_chart", ("depth chart", "starter", "starting", "backup", "first team",
                     "committee", "workhorse", "snap", "cedin", "third-down",
                     "reps", "role", "rotation", "touches", "carries")),
    ("camp", ("camp", "preseason", "ota", "minicamp", "holdout", "padded")),
)
# Names too generic to match on their own inside prose.
_MIN_NAME_WORDS = 2
# Possessives survive punctuation stripping as a trailing "s" ("Robinson's" ->
# "robinsons"), which silently hides a name and can turn a roundup into a
# false single match. Strip the marker before normalizing.
_POSSESSIVE = re.compile(r"[\u2019']s\b|[\u2019']\B")


@dataclass(frozen=True)
class FeedItem:
    source: str
    external_id: str
    url: str | None
    title: str
    summary: str | None
    published_at: datetime | None


_TAG_PATTERNS = tuple(
    (tag, re.compile(r"\b(?:" + "|".join(needles) + r")", re.IGNORECASE))
    for tag, needles in _TAG_RULES
)


def classify(text: str) -> str:
    """Word-boundary matching; naive substrings tagged half the feed as injuries."""
    for tag, pattern in _TAG_PATTERNS:
        if pattern.search(text):
            return tag
    return "general"


def _parse_date(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = parsedate_to_datetime(raw.strip())
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(raw.strip())
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def parse_feed(source: str, content: bytes) -> list[FeedItem]:
    """Parse RSS or Atom into a common shape; malformed feeds yield nothing."""
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        log.warning("news: %s returned unparseable XML", source)
        return []

    items: list[FeedItem] = []
    for node in root.findall(".//item") or root.findall(f".//{_ATOM}entry"):
        # Feeds double-escape entities, so XML decoding alone leaves &#39; visible.
        title = html.unescape(
            node.findtext("title") or node.findtext(f"{_ATOM}title") or ""
        ).strip()
        if not title:
            continue
        # Some feeds pad <link> with newlines and indentation.
        link = (node.findtext("link") or "").strip()
        if not link:
            link_node = node.find(f"{_ATOM}link")
            link = (link_node.get("href", "") if link_node is not None else "").strip()
        guid = node.findtext("guid") or node.findtext(f"{_ATOM}id") or link or title
        summary = (
            node.findtext("description")
            or node.findtext(f"{_ATOM}summary")
            or None
        )
        if summary:
            summary = re.sub(r"<[^>]+>", " ", html.unescape(summary))
            summary = re.sub(r"\s+", " ", summary).strip() or None
        published = _parse_date(
            node.findtext("pubDate")
            or node.findtext(f"{_ATOM}updated")
            or node.findtext(f"{_ATOM}published")
        )
        items.append(
            FeedItem(
                source=source,
                external_id=f"{source}:{guid}"[:512],
                url=link or None,
                title=title[:512],
                summary=summary,
                published_at=published,
            )
        )
    return items


class PlayerMatcher:
    """Find which fantasy player a headline is about.

    Two passes: the "Player Name: headline" convention that player-news feeds
    use, then a scan for known names inside the text. Ambiguous names that map
    to more than one active player are skipped rather than guessed — a wrong
    attribution is worse than a missing item.
    """

    def __init__(self, session: Session) -> None:
        rows = session.execute(
            select(PlayerIds.canonical_id, PlayerIds.full_name, Player.search_rank)
            .join(Player, Player.canonical_id == PlayerIds.canonical_id)
            .where(Player.position.in_(("QB", "RB", "WR", "TE", "K")))
        ).all()
        index: dict[str, set[int]] = {}
        self._rank: dict[int, int] = {}
        for canonical_id, full_name, rank in rows:
            key = make_merge_name(full_name or "")
            if len(key.split()) < _MIN_NAME_WORDS:
                continue
            index.setdefault(key, set()).add(canonical_id)
            self._rank[canonical_id] = rank if rank is not None else 10_000
        self._index = index
        self._max_words = max((len(k.split()) for k in index), default=2)

    def _resolve(self, key: str) -> int | None:
        candidates = self._index.get(key)
        if not candidates:
            return None
        if len(candidates) == 1:
            return next(iter(candidates))
        # Same name, multiple players: prefer the fantasy-relevant one, but only
        # when it is clearly more relevant than the alternatives.
        ranked = sorted(candidates, key=lambda cid: self._rank.get(cid, 10_000))
        best, runner_up = ranked[0], ranked[1]
        if self._rank.get(best, 10_000) * 3 < self._rank.get(runner_up, 10_000):
            return best
        return None

    def match(self, title: str, summary: str | None = None) -> int | None:
        """The article's subject, or None.

        Deliberately conservative. Two hard rules learned from auditing a live
        pull: only the TITLE is evidence of subject (bodies name everyone who
        got a passing mention), and a title naming several players is a roundup
        whose subject is nobody in particular. Attributing an injury tracker to
        the fourth player listed in it is worse than showing no player at all.
        """
        cleaned = _POSSESSIVE.sub("", title)
        # "Player Name: what happened" — the player-news convention, unambiguous.
        if ":" in cleaned:
            found = self._resolve(make_merge_name(cleaned.split(":", 1)[0]))
            if found is not None:
                return found

        words = make_merge_name(cleaned).split()
        hits: list[int] = []
        for size in range(min(self._max_words, len(words)), _MIN_NAME_WORDS - 1, -1):
            for start in range(len(words) - size + 1):
                found = self._resolve(" ".join(words[start : start + size]))
                if found is not None and found not in hits:
                    hits.append(found)
        if len(hits) != 1:
            return None  # nobody, or a roundup naming several players
        return hits[0]


def store_items(session: Session, items: list[FeedItem]) -> dict[str, int]:
    matcher = PlayerMatcher(session)
    known = {
        row
        for row in session.scalars(
            select(NewsItem.external_id).where(
                NewsItem.external_id.in_([i.external_id for i in items])
            )
        )
    }
    written = skipped = unmatched = 0
    for item in items:
        if item.external_id in known:
            skipped += 1
            continue
        canonical = matcher.match(item.title, item.summary)
        if canonical is None:
            unmatched += 1
        session.add(
            NewsItem(
                source=item.source,
                external_id=item.external_id,
                url=item.url,
                title=item.title,
                summary=item.summary,
                published_at=item.published_at,
                canonical_id=canonical,
                tag=classify(f"{item.title} {item.summary or ''}"),
            )
        )
        known.add(item.external_id)
        written += 1
    session.commit()
    log.info(
        "news: written=%d duplicate=%d without a player=%d", written, skipped, unmatched
    )
    return {"rows_in": len(items), "rows_written": written, "rows_skipped": skipped}


def verify_feeds() -> dict[str, str]:
    """Report which configured feeds still work. Feeds rot; check before trusting."""
    status: dict[str, str] = {}
    with httpx.Client(timeout=20.0, follow_redirects=True, headers={"User-Agent": "fantasy-tool"}) as client:
        for source, url in FEEDS.items():
            try:
                resp = client.get(url)
                status[source] = (
                    f"ok ({len(parse_feed(source, resp.content))} items)"
                    if resp.status_code == 200
                    else f"HTTP {resp.status_code}"
                )
            except httpx.HTTPError as exc:
                status[source] = f"error: {type(exc).__name__}"
    return status


def run(session: Session) -> dict[str, int]:
    items: list[FeedItem] = []
    with httpx.Client(timeout=20.0, follow_redirects=True, headers={"User-Agent": "fantasy-tool"}) as client:
        for source, url in FEEDS.items():
            try:
                resp = client.get(url)
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                # One dead feed must not take down the rest.
                log.warning("news: %s unavailable (%s)", source, type(exc).__name__)
                continue
            items.extend(parse_feed(source, resp.content))
    return store_items(session, items)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s %(message)s", stream=sys.stderr
    )
    from api.db import get_sessionmaker

    with get_sessionmaker()() as session:
        run(session)
