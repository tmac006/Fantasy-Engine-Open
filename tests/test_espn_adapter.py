"""ESPN settings translation over a recorded leaguedefaults mSettings payload."""

import json
from pathlib import Path

import pytest

from api.adapters.espn import translate_espn_settings
from api.eval.scoring_check import our_total
from api.schemas import LeagueSettings

FIXTURE = Path(__file__).parent / "fixtures" / "espn_leaguedefault_msettings.json"


def test_translates_default_ppr_league() -> None:
    payload = json.loads(FIXTURE.read_text())
    s = translate_espn_settings(payload)
    assert s.total_teams == 10
    # ESPN PPR default league: 1 point per reception.
    assert s.scoring["rec"] == 1.0
    assert s.scoring["pass_td"] == 4.0
    assert s.scoring["rush_td"] == 6.0
    assert s.scoring["pass_int"] == -2.0
    assert s.scoring["fum_lost"] == -2.0
    assert abs(s.scoring["pass_yd"] - 0.04) < 1e-9
    assert abs(s.scoring["rush_yd"] - 0.1) < 1e-9
    # Roster: QB1 RB2 WR2 TE1 FLEX1 DST1 K1 BN7.
    assert s.roster_slots["QB"] == 1
    assert s.roster_slots["RB"] == 2
    assert s.roster_slots["WR"] == 2
    assert s.roster_slots["FLEX"] == 1
    assert s.roster_slots["DEF"] == 1
    assert s.roster_slots["K"] == 1
    assert s.roster_slots["BN"] == 7


def test_translator_tolerates_unknown_ids() -> None:
    payload = {
        "settings": {
            "size": 12,
            "scoringSettings": {"scoringItems": [
                {"statId": 9999, "points": 5.0},
                {"statId": 53, "points": 0.5},
            ]},
            "rosterSettings": {"lineupSlotCounts": {"0": 1, "99": 3, "20": 5}},
        }
    }
    s = translate_espn_settings(payload)
    assert s.scoring == {"rec": 0.5}
    assert s.roster_slots == {"QB": 1, "BN": 5}
    assert s.total_teams == 12


class TestBlockedYardageScoring:
    """ESPN has two ways to score yardage; a league uses one or the other.

    Per-yard items carry the rate directly (statId 3 at 0.04). Bucketed items
    carry whole points per block of yards under entirely different statIds, and
    a real 16-team league used those: its settings page reads "Every 25 passing
    yards = 1" while the payload carries statId 8 at 1.0.

    Handling only the per-yard form made that league read as scoring NO yardage,
    which rescored every projection and reordered the board. The live draft path
    translates the same payload, so it was wrong there too.
    """

    def _payload(self, items: list[dict[str, float]]) -> dict:
        return {
            "settings": {
                "scoringSettings": {"scoringItems": items},
                "rosterSettings": {
                    "lineupSlotCounts": {"0": 1, "2": 2, "4": 2, "6": 1, "23": 1, "20": 7},
                    "positionLimits": {},
                },
                "size": 16,
            }
        }

    def test_blocked_yardage_becomes_a_per_yard_rate(self) -> None:
        settings = translate_espn_settings(
            self._payload([
                {"statId": 8, "points": 1.0},   # every 25 passing yards
                {"statId": 28, "points": 1.0},  # every 10 rushing yards
                {"statId": 48, "points": 1.0},  # every 10 receiving yards
                {"statId": 53, "points": 1.0},
            ])
        )
        assert settings.scoring["pass_yd"] == 0.04
        assert settings.scoring["rush_yd"] == 0.1
        assert settings.scoring["rec_yd"] == 0.1
        assert settings.scoring["rec"] == 1.0

    def test_per_yard_form_is_untouched(self) -> None:
        settings = translate_espn_settings(
            self._payload([
                {"statId": 3, "points": 0.04},
                {"statId": 24, "points": 0.1},
                {"statId": 42, "points": 0.1},
            ])
        )
        assert settings.scoring["pass_yd"] == 0.04
        assert settings.scoring["rush_yd"] == 0.1
        assert settings.scoring["rec_yd"] == 0.1

    def test_a_half_point_block_scales_too(self) -> None:
        """The divisor is the block size, not a hardcoded rate."""
        settings = translate_espn_settings(self._payload([{"statId": 8, "points": 2.0}]))
        assert settings.scoring["pass_yd"] == 0.08


class TestScoringSelfCheck:
    """Scoring ESPN's own raw projections must reproduce ESPN's own total.

    This is the systematic version of the bucketed-yardage bug: rather than
    trusting a hand-maintained statId map, score the platform's numbers and
    compare against the platform's answer. A gap is an unmapped statId.
    """

    SETTINGS = LeagueSettings(
        scoring={
            "pass_yd": 0.04, "pass_td": 4.0, "pass_int": -2.0,
            "rush_yd": 0.1, "rush_td": 6.0,
            "rec": 1.0, "rec_yd": 0.1, "rec_td": 6.0,
        },
        roster_slots={"QB": 1, "RB": 2, "WR": 2, "TE": 1, "BN": 7},
        total_teams=12,
    )

    def test_reads_yardage_from_the_per_yard_stat_ids(self) -> None:
        """Raw stats always carry real yards; only the *scoring* form varies.

        A league scoring in blocks stores both (statId 24 = 1374 yards and
        statId 28 = 137 blocks), so reading the per-yard id and applying the
        translated rate works for either form without special-casing.
        """
        raw = {24: 1000.0, 28: 100.0, 42: 500.0, 48: 50.0, 25: 10.0, 53: 60.0}
        assert our_total(raw, self.SETTINGS) == pytest.approx(
            1000 * 0.1 + 500 * 0.1 + 10 * 6.0 + 60 * 1.0
        )

    def test_a_stat_the_league_does_not_score_contributes_nothing(self) -> None:
        no_ppr = self.SETTINGS.model_copy(
            update={"scoring": {k: v for k, v in self.SETTINGS.scoring.items() if k != "rec"}}
        )
        raw = {42: 500.0, 53: 60.0}
        assert our_total(raw, no_ppr) == pytest.approx(50.0)

    def test_duplicate_reception_stat_ids_are_not_counted_twice(self) -> None:
        """ESPN carries receptions under both 41 and 53; our map names both."""
        raw = {41: 60.0, 53: 60.0}
        assert our_total(raw, self.SETTINGS) == pytest.approx(60.0)

    def test_a_missing_stat_is_absent_rather_than_zero(self) -> None:
        assert our_total({}, self.SETTINGS) == 0.0
