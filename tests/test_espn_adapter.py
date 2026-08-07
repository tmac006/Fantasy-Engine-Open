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
