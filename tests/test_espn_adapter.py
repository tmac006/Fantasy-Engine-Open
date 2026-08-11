"""ESPN settings translation over a recorded leaguedefaults mSettings payload."""

import json
from pathlib import Path

from api.adapters.espn import translate_espn_settings

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
