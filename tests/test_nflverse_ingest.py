"""Focused tests for dependency-light nflverse context shaping."""

import gzip
from datetime import UTC, datetime

import httpx
import respx

from api.ingest.nflverse import (
    _upsert_player_context,
    aggregate_player_stats,
    aggregate_team_stats,
    build_schedule_context,
    build_sleeper_status,
    decode_csv,
    fetch_optional,
    latest_roster_context,
)
from api.models import PlayerContext


def test_decode_csv_accepts_gzip_and_missing_values() -> None:
    content = gzip.compress(b"player_id,targets,optional\n00-1,8,\n")
    assert decode_csv(content) == [
        {"player_id": "00-1", "targets": "8", "optional": ""}
    ]
    assert decode_csv(b"team,week\nKC,1\n") == [{"team": "KC", "week": "1"}]
    assert decode_csv(b"\x1f\x8bnot-gzip") == []


@respx.mock
def test_missing_feed_is_optional() -> None:
    url = "https://example.test/missing.csv.gz"
    respx.get(url).respond(status_code=404)
    with httpx.Client() as client:
        assert fetch_optional(client, url) is None


def test_player_stats_are_compact_and_ignore_missing_fields() -> None:
    rows = [
        {
            "player_id": "00-1",
            "season": "2025",
            "season_type": "REG",
            "game_id": "g1",
            "week": "1",
            "team": "MIN",
            "position": "WR",
            "targets": "10",
            "receptions": "7",
            "receiving_yards": "100",
            "receiving_tds": "1",
            "receiving_epa": "5",
            "target_share": "0.25",
            "air_yards_share": "0.30",
            "fantasy_points_ppr": "23",
        },
        {
            "player_id": "00-1",
            "season": "2025",
            "season_type": "REG",
            "game_id": "g2",
            "week": "2",
            "team": "MIN",
            "position": "WR",
            "targets": "5",
            "receptions": "3",
            "receiving_yards": "50",
            "receiving_tds": "",
            "receiving_epa": "1",
            "target_share": "0.15",
            "air_yards_share": "",
            "fantasy_points_ppr": "8",
        },
        {
            "player_id": "00-1",
            "season": "2025",
            "season_type": "POST",
            "game_id": "g3",
            "targets": "99",
        },
    ]

    data = aggregate_player_stats(rows, 2025)["00-1"]
    assert data["games"] == 2
    assert data["last_week"] == 2
    assert data["volume"] == {
        "targets": 15,
        "receptions": 10,
        "touches": 10,
    }
    assert data["opportunity"]["target_share"] == 0.2
    assert data["opportunity"]["air_yards_share"] == 0.3
    assert data["efficiency"]["yards_per_target"] == 10.0
    assert data["production"]["fantasy_points_ppr"] == 31.0


def test_latest_roster_row_wins_without_requiring_every_field() -> None:
    rows = [
        {"season": "2026", "gsis_id": "00-1", "week": "0", "status": "ACT"},
        {
            "season": "2026",
            "gsis_id": "00-1",
            "week": "1",
            "status": "RES",
            "depth_chart_position": "WR",
        },
        {"season": "2025", "gsis_id": "00-1", "week": "18", "status": "OLD"},
    ]
    roster = latest_roster_context(rows, 2026)["00-1"]["roster"]
    assert roster["status"] == "RES"
    assert roster["week"] == 1
    assert roster["depth_chart_position"] == "WR"


def test_team_environment_and_schedule_are_season_scoped() -> None:
    stats = [
        {
            "season": "2025",
            "season_type": "REG",
            "game_id": "g1",
            "team": "KC",
            "attempts": "40",
            "carries": "20",
            "passing_yards": "300",
            "rushing_yards": "120",
            "passing_epa": "12",
            "rushing_epa": "0",
            "receiving_epa": "0",
            "passing_tds": "3",
            "rushing_tds": "1",
        }
    ]
    offense = aggregate_team_stats(stats, 2025)["KC"]["offense"]
    assert offense["plays_per_game"] == 60.0
    assert offense["pass_rate"] == 0.667
    assert offense["yards_per_play"] == 7.0

    games = [
        {
            "season": "2026",
            "game_type": "REG",
            "week": "1",
            "away_team": "KC",
            "home_team": "DEN",
            "total_line": "",
        },
        {
            "season": "2025",
            "game_type": "REG",
            "week": "1",
            "away_team": "KC",
            "home_team": "LV",
        },
    ]
    schedule = build_schedule_context(games, 2026)
    assert schedule["KC"]["schedule"] == [
        {"week": 1, "opponent": "DEN", "home": False}
    ]


def test_nflverse_team_aliases_match_platform_abbreviations() -> None:
    stats = [
        {
            "season": "2025",
            "season_type": "REG",
            "game_id": "g1",
            "team": "LA",
            "attempts": "30",
            "carries": "20",
        }
    ]
    assert "LAR" in aggregate_team_stats(stats, 2025)
    schedule = build_schedule_context(
        [
            {
                "season": "2026",
                "game_type": "REG",
                "week": "1",
                "away_team": "LA",
                "home_team": "SF",
            }
        ],
        2026,
    )
    assert schedule["LAR"]["schedule"][0]["opponent"] == "SF"


def test_sleeper_status_keeps_only_draft_relevant_fields() -> None:
    payload = {
        "123": {
            "team": "KC",
            "status": "Active",
            "injury_status": "Questionable",
            "practice_participation": None,
            "depth_chart_order": 1,
            "college": "Example University",
        },
        "bad": "not a player",
    }
    assert build_sleeper_status(payload) == {
        "123": {
            "team": "KC",
            "status": "Active",
            "injury_status": "Questionable",
            "depth_chart_order": 1,
        }
    }


def test_upsert_refreshes_existing_source_key() -> None:
    existing = PlayerContext(
        canonical_id=7,
        season="2026",
        source="nflverse",
        data={"old": True},
    )

    class ExistingSession:
        def scalar(self, statement: object) -> PlayerContext:
            return existing

        def add(self, value: object) -> None:
            raise AssertionError("an existing source key must not insert")

    fetched_at = datetime(2026, 8, 4, tzinfo=UTC)
    _upsert_player_context(
        ExistingSession(),  # type: ignore[arg-type]
        canonical_id=7,
        season=2026,
        source="nflverse",
        data={"games": 17},
        fetched_at=fetched_at,
        source_updated_at=None,
    )
    assert existing.data == {"games": 17}
    assert existing.fetched_at == fetched_at
