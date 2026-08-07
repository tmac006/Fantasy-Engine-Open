"""News parsing, tagging and player attribution.

The attribution rules here came from auditing a real feed pull: the first
version tagged an injury-tracker article to the fourth player mentioned in it.
A wrong attribution is worse than a missing item, so these lock in precision.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from api.ingest.news import PlayerMatcher, classify, parse_feed
from api.models import Player, PlayerIds

RSS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item>
    <title>Tank Dell: Practicing with pads for first time</title>
    <link>https://example.com/a</link>
    <guid>a1</guid>
    <description>&lt;p&gt;Dell &lt;b&gt;returned&lt;/b&gt; Wednesday.&lt;/p&gt;</description>
    <pubDate>Wed, 05 Aug 2026 07:36:00 -0700</pubDate>
  </item>
  <item>
    <title>No link item</title>
    <guid>a2</guid>
  </item>
</channel></rss>"""


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite://")
    for table in (PlayerIds.__table__, Player.__table__):
        table.create(engine)
    s = sessionmaker(bind=engine)()
    roster = [
        (1, "Tank Dell", "WR", 40),
        (2, "Zay Flowers", "WR", 30),
        (3, "Bijan Robinson", "RB", 1),
        (4, "Jahmyr Gibbs", "RB", 2),
        (5, "Jalen McMillan", "WR", 120),
    ]
    for cid, name, pos, rank in roster:
        s.add(PlayerIds(canonical_id=cid, full_name=name, merge_name=name.lower(), position=pos))
        s.add(Player(canonical_id=cid, full_name=name, position=pos, search_rank=rank))
    s.commit()
    return s


# -- parsing ----------------------------------------------------------------


def test_parses_rss_and_strips_html() -> None:
    items = parse_feed("rotowire", RSS)
    assert len(items) == 2
    first = items[0]
    assert first.title.startswith("Tank Dell:")
    assert first.summary == "Dell returned Wednesday."
    assert first.url == "https://example.com/a"
    assert first.published_at is not None
    assert first.external_id == "rotowire:a1"


def test_malformed_feed_yields_nothing_instead_of_raising() -> None:
    assert parse_feed("broken", b"<html>not a feed</html>") == []


# -- tagging ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Tank Dell: Practicing with pads for first time", "injury"),
        ("Zay Flowers, Ravens agree to four-year extension", "transaction"),
        ("Travis Etienne: Ceding third-down work", "depth_chart"),
        ("Eagles open training camp Wednesday", "camp"),
        ("The history of the Super Bowl in Los Angeles", "general"),
    ],
)
def test_classification(text: str, expected: str) -> None:
    assert classify(text) == expected


def test_tags_use_word_boundaries() -> None:
    """Naive substring rules tagged half the feed 'injury' via words like 'about'."""
    assert classify("Scouting the layout of the new stadium") == "general"


# -- attribution ------------------------------------------------------------


def test_player_news_prefix_is_matched(session: Session) -> None:
    assert PlayerMatcher(session).match("Tank Dell: Practicing with pads") == 1


def test_single_subject_in_title_is_matched(session: Session) -> None:
    assert PlayerMatcher(session).match("Zay Flowers, Ravens agree to extension") == 2


def test_roundup_naming_several_players_is_left_unattributed(session: Session) -> None:
    matcher = PlayerMatcher(session)
    assert matcher.match("Bijan Robinson and Jahmyr Gibbs headline Lions-Falcons") is None


def test_possessive_names_still_count_toward_roundup_detection(session: Session) -> None:
    """'Robinson's' normalizes to 'robinsons' and used to vanish, which made a
    two-player roundup look like a clean single match."""
    matcher = PlayerMatcher(session)
    title = "Dan Campbell: Bijan Robinson's extension won't hurt Jahmyr Gibbs' case"
    assert matcher.match(title) is None


def test_body_mentions_do_not_create_attribution(session: Session) -> None:
    """Only the title is evidence of subject; bodies name everyone in passing."""
    matcher = PlayerMatcher(session)
    assert matcher.match("NFL training camp injuries tracker", "Tank Dell is limited") is None


def test_unknown_player_is_not_forced(session: Session) -> None:
    assert PlayerMatcher(session).match("Some Guy: signs with the Jets") is None
