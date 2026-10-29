"""ESPN projection + ADP ingest.

Two live-verified failure modes are pinned here: the placeholder ADP cluster
(~170 for every player without real draft interest) leaking in as market data,
and the prior season's projection being stored as this season's.
"""

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

import api.ingest.projections as projections_module
from api.ingest.projections import ingest_espn_projections, placeholder_adp_values
from api.models import Adp, PlayerIds

# Points recorded by the stubbed projection upsert: (canonical_id, points).
STORED: list[tuple[int, float]] = []


@pytest.fixture
def session(monkeypatch: pytest.MonkeyPatch) -> Session:
    engine = create_engine("sqlite://")
    # Projection carries a JSONB column sqlite cannot create, so record the
    # upsert instead of performing it.
    STORED.clear()
    monkeypatch.setattr(
        projections_module,
        "_upsert_projection",
        lambda _s, cid, _src, points, _stats: (STORED.append((cid, points)), True)[1],
    )
    for table in (PlayerIds.__table__, Adp.__table__):
        table.create(engine)
    return sessionmaker(bind=engine)()


def test_modal_cluster_is_detected() -> None:
    organic = [float(v) for v in range(1, 140) for _ in range(3)]
    placeholder = [170.2] * 300
    assert placeholder_adp_values(organic + placeholder) == {170}


def test_spread_market_data_has_no_placeholder() -> None:
    organic = [float(v) for v in range(1, 200) for _ in range(4)]
    assert placeholder_adp_values(organic) == set()


def test_small_clusters_are_not_flagged() -> None:
    # A genuine pileup of a few players at one ADP is market data, not a flag.
    assert placeholder_adp_values([50.0] * 10 + [float(v) for v in range(60, 80)]) == set()


def espn_entry(espn_id: int, adp: float) -> dict:
    return {
        "id": espn_id,
        "player": {
            "stats": [
                # Prior season first, exactly as ESPN orders it live.
                {"id": "102025", "seasonId": 2025, "statSourceId": 1, "scoringPeriodId": 0, "appliedTotal": 999.0},
                {"id": "102026", "seasonId": 2026, "statSourceId": 1, "scoringPeriodId": 0, "appliedTotal": 120.0},
            ],
            "ownership": {"averageDraftPosition": adp},
        },
    }


def test_prior_season_projection_is_never_stored_as_this_season(session: Session) -> None:
    """ESPN ships the PRIOR season's full-season projection in the same array,
    under the same (statSourceId=1, scoringPeriodId=0) shape, and usually FIRST.

    Live on 2026-08-23, 91% of the top 300 listed 2025 ahead of 2026, so an
    unfiltered `next()` stored last year's number: James Conner 250.2 against a
    real 2026 projection of 0.0, Quinshon Judkins 49.6 against 226.4, every
    kicker zeroed. Mean error across the sampled board was 61.6 points.
    """
    session.add(
        PlayerIds(
            canonical_id=7, full_name="James Conner", merge_name="james conner",
            position="RB", espn_id="42",
        )
    )
    session.commit()

    ingest_espn_projections(
        session,
        [
            {
                "id": 42,
                "player": {
                    "stats": [
                        {"id": "102025", "seasonId": 2025, "statSourceId": 1, "scoringPeriodId": 0, "appliedTotal": 250.2},
                        {"id": "002025", "seasonId": 2025, "statSourceId": 0, "scoringPeriodId": 0, "appliedTotal": 231.0},
                        {"id": "112025", "seasonId": 2025, "statSourceId": 1, "scoringPeriodId": 14, "appliedTotal": 19.1},
                        {"id": "102026", "seasonId": 2026, "statSourceId": 1, "scoringPeriodId": 0, "appliedTotal": 0.0},
                    ],
                    "ownership": {},
                },
            }
        ],
    )

    assert STORED == [(7, 0.0)], f"must store the 2026 projection, got {STORED}"


def test_player_with_no_current_season_projection_is_skipped(session: Session) -> None:
    session.add(
        PlayerIds(
            canonical_id=8, full_name="Old Timer", merge_name="old timer",
            position="WR", espn_id="43",
        )
    )
    session.commit()

    written, skipped = ingest_espn_projections(
        session,
        [
            {
                "id": 43,
                "player": {
                    "stats": [
                        {"id": "102025", "seasonId": 2025, "statSourceId": 1, "scoringPeriodId": 0, "appliedTotal": 210.0},
                    ],
                    "ownership": {},
                },
            }
        ],
    )
    assert STORED == [], "a player ESPN no longer projects must not inherit last year's number"
    assert (written, skipped) == (0, 1)


def test_placeholder_rows_are_not_stored_and_stale_ones_are_cleared(session: Session) -> None:
    for i in range(40):
        session.add(
            PlayerIds(
                canonical_id=i + 1,
                full_name=f"Player {i}",
                merge_name=f"player {i}",
                position="WR",
                espn_id=str(1000 + i),
            )
        )
    # A previous ingest stored the placeholder for player 1 as real market data.
    session.add(Adp(canonical_id=1, source="espn", format="ppr", teams=None, value=170.2))
    session.commit()

    payload = [espn_entry(1000 + i, 170.2) for i in range(30)]  # placeholder cluster
    payload += [espn_entry(1000 + 30 + i, 10.0 + 7 * i) for i in range(10)]  # real market
    ingest_espn_projections(session, payload)

    stored = session.scalars(select(Adp).where(Adp.source == "espn")).all()
    values = {row.canonical_id: row.value for row in stored}
    assert 1 not in values, "stale placeholder row must be cleared on re-ingest"
    assert all(round(v) != 170 for v in values.values())
    assert values[31] == 10.0, "real market values must still be stored"
    assert len(values) == 10


# Weekly rows recorded by the stubbed upsert: (canonical_id, week, points).
STORED_WEEKLY: list[tuple[int, int | None, float]] = []


@pytest.fixture
def weekly_session(monkeypatch: pytest.MonkeyPatch) -> Session:
    engine = create_engine("sqlite://")
    STORED_WEEKLY.clear()
    monkeypatch.setattr(
        projections_module,
        "_upsert_projection",
        lambda _s, cid, _src, points, _stats, week=None: (
            STORED_WEEKLY.append((cid, week, points)),
            True,
        )[1],
    )
    monkeypatch.setattr(projections_module, "_purge_weekly", lambda *_a, **_k: 0)
    for table in (PlayerIds.__table__, Adp.__table__):
        table.create(engine)
    session = sessionmaker(bind=engine)()
    session.add_all(
        [
            PlayerIds(
                canonical_id=7, full_name="James Conner", merge_name="james conner",
                position="RB", espn_id="42", sleeper_id="4137",
            ),
            PlayerIds(
                canonical_id=8, full_name="Bench Guy", merge_name="bench guy",
                position="WR", espn_id="43", sleeper_id="9999",
            ),
        ]
    )
    session.commit()
    return session


def test_espn_weekly_picks_this_season_and_this_week(weekly_session: Session) -> None:
    """The kona payload carries every week of BOTH seasons under the same
    statSourceId/scoringPeriodId shape; only (season, week) may match."""
    written, skipped = projections_module.ingest_espn_weekly_projections(
        weekly_session,
        [
            {
                "id": 42,
                "player": {
                    "stats": [
                        {"seasonId": 2025, "statSourceId": 1, "scoringPeriodId": 2, "appliedTotal": 18.1},
                        {"seasonId": 2026, "statSourceId": 0, "scoringPeriodId": 2, "appliedTotal": 33.6},
                        {"seasonId": 2026, "statSourceId": 1, "scoringPeriodId": 0, "appliedTotal": 372.2},
                        {"seasonId": 2026, "statSourceId": 1, "scoringPeriodId": 3, "appliedTotal": 23.7},
                        {"seasonId": 2026, "statSourceId": 1, "scoringPeriodId": 2, "appliedTotal": 21.4},
                    ]
                },
            },
            {"id": 43, "player": {"stats": [
                {"seasonId": 2026, "statSourceId": 1, "scoringPeriodId": 0, "appliedTotal": 90.0},
            ]}},
        ],
        week=2,
    )
    assert STORED_WEEKLY == [(7, 2, 21.4)]
    assert (written, skipped) == (1, 1), "a player with no week-2 line is skipped, not zeroed"


def test_sleeper_weekly_skips_unprojected_rows(weekly_session: Session) -> None:
    written, skipped = projections_module.ingest_sleeper_weekly_projections(
        weekly_session,
        [
            {"player_id": "4137", "week": 2, "stats": {"pts_ppr": 14.6, "rush_att": 15.0, "adp_ppr": 40}},
            {"player_id": "9999", "week": 2, "stats": {"gp": 1.0}},
            {"player_id": "1", "week": 2, "stats": {"pts_ppr": 9.0}},
        ],
        week=2,
    )
    assert STORED_WEEKLY == [(7, 2, 14.6)]
    assert (written, skipped) == (1, 2)


def test_upcoming_weeks_is_current_and_next(monkeypatch: pytest.MonkeyPatch) -> None:
    from api import inseason

    monkeypatch.setattr(inseason, "current_week", lambda _s, _season: 2)
    assert projections_module.upcoming_weeks(None) == [2, 3]
    monkeypatch.setattr(inseason, "current_week", lambda _s, _season: 18)
    assert projections_module.upcoming_weeks(None) == [18]
