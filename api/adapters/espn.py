"""ESPN settings translation (Phase 4).

ESPN's live draft is only observable inside the draft room page (see the
Phase 0 findings), so the extension taps it client-side. The server's ESPN
job is translating a raw mSettings payload - which the extension fetches with
the user's own cookies - into normalized LeagueSettings for board building.

Stat IDs and lineup slot IDs verified against cwendt94/espn-api
(espn_api/football/constant.py) and a live leaguedefaults mSettings payload.
"""

from typing import Any

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from api.config import get_settings
from api.schemas import LeagueSettings

# ESPN statId -> Sleeper-style scoring key, for stats our projections carry.
# Bonus/branch stat IDs that Sleeper never projects are intentionally omitted:
# score_stats only counts overlapping keys.
STAT_ID_TO_KEY: dict[int, str] = {
    3: "pass_yd",
    4: "pass_td",
    19: "pass_2pt",
    20: "pass_int",
    24: "rush_yd",
    25: "rush_td",
    26: "rush_2pt",
    41: "rec",
    42: "rec_yd",
    43: "rec_td",
    44: "rec_2pt",
    53: "rec",  # ESPN uses 53 (not 41) in scoring items for PPR
    64: "pass_sack",
    72: "fum_lost",
    17: "bonus_pass_yd_300",
    18: "bonus_pass_yd_400",
    37: "bonus_rush_yd_100",
    38: "bonus_rush_yd_200",
    56: "bonus_rec_yd_100",
    57: "bonus_rec_yd_200",
}

# ESPN expresses yardage scoring two different ways and a league uses one or the
# other. The statIds above carry a rate per single yard ("0.04 per passing
# yard"). These carry whole points per block of yards instead ("Every 25 passing
# yards = 1"), under statIds the per-yard map never mentions, so the rate is
# points divided by the block size.
#
# Missing this form is not a cosmetic gap. A league scoring yards this way came
# through as scoring NO yardage at all, which silently rescored every projection
# and reordered the board -- receivers and quarterbacks worst, since yardage is
# most of what they do. Found in a live 16-team league, whose settings page
# reads PY25/RY10/REY10 while the payload carries statIds 8/28/48 at 1.0 each.
#
# The rate is a linear approximation: ESPN truncates per game, so 274 passing
# yards scores 10 rather than 10.96. That runs about 1% high on a season total
# and lands almost uniformly across players, which is far smaller than the
# alternative of dropping the category on the floor.
STAT_ID_YARDS_PER_BLOCK: dict[int, tuple[str, int]] = {
    8: ("pass_yd", 25),
    28: ("rush_yd", 10),
    48: ("rec_yd", 10),
}

# ESPN proTeamId -> our team abbreviation. Verified live against
# site.api.espn.com/apis/site/v2/sports/football/nfl/teams (32 teams) on
# 2026-08-05; the only divergence from our codes is Washington (ESPN "WSH").
# Team defenses have playerId == -16000 - proTeamId (verified against all 32
# kona D/ST rows), so this map is what lets a drafted D/ST resolve to a player.
ESPN_PRO_TEAM_ABBREV: dict[int, str] = {
    1: "ATL", 2: "BUF", 3: "CHI", 4: "CIN", 5: "CLE", 6: "DAL", 7: "DEN",
    8: "DET", 9: "GB", 10: "TEN", 11: "IND", 12: "KC", 13: "LV", 14: "LAR",
    15: "MIA", 16: "MIN", 17: "NE", 18: "NO", 19: "NYG", 20: "NYJ", 21: "PHI",
    22: "ARI", 23: "PIT", 24: "LAC", 25: "SF", 26: "SEA", 27: "TB", 28: "WAS",
    29: "CAR", 30: "JAX", 33: "BAL", 34: "HOU",
}

# ESPN positionLimits is keyed by *position* id, a different namespace from the
# lineup slot ids below (K is position 5 but slot 17). Verified against a real
# mSettings payload; -1 means unlimited.
POSITION_ID_TO_NAME: dict[int, str] = {1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 16: "DEF"}

# ESPN lineupSlotCounts slot id -> our roster slot name.
SLOT_ID_TO_NAME: dict[int, str] = {
    0: "QB",
    2: "RB",
    3: "WRRB_FLEX",
    4: "WR",
    5: "REC_FLEX",
    6: "TE",
    7: "SUPER_FLEX",  # ESPN "OP"
    16: "DEF",
    17: "K",
    20: "BN",
    23: "FLEX",
}


def translate_espn_settings(payload: dict[str, Any]) -> LeagueSettings:
    """Raw ESPN league JSON (any response carrying `settings`) -> LeagueSettings."""
    settings = payload.get("settings") or payload
    scoring_items = settings["scoringSettings"]["scoringItems"]
    scoring: dict[str, float] = {}
    for item in scoring_items:
        stat_id = item.get("statId", -1)
        points = item.get("points")
        if points is None:
            continue
        if (blocked := STAT_ID_YARDS_PER_BLOCK.get(stat_id)) is not None:
            key, yards_per_block = blocked
            value = float(points) / yards_per_block
        else:
            mapped = STAT_ID_TO_KEY.get(stat_id)
            if mapped is None:
                continue
            key, value = mapped, float(points)
        # Duplicate rec statIds (41/53): last nonzero wins.
        if key not in scoring or value != 0.0:
            scoring[key] = value

    slots: dict[str, int] = {}
    for slot_id, count in settings["rosterSettings"]["lineupSlotCounts"].items():
        name = SLOT_ID_TO_NAME.get(int(slot_id))
        if name and count:
            slots[name] = int(count)

    # Hard per-position roster caps (QB 4, K 3, ... in a default league). The
    # league enforces these at pick time, so advice that ignores them can
    # recommend a pick the room would reject.
    limits: dict[str, int] = {}
    for position_id, cap in (settings["rosterSettings"].get("positionLimits") or {}).items():
        name = POSITION_ID_TO_NAME.get(int(position_id))
        if name and int(cap) > 0:
            limits[name] = int(cap)

    return LeagueSettings(
        scoring=scoring,
        roster_slots=slots,
        position_limits=limits,
        total_teams=int(settings["size"]),
    )


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code >= 500


class EspnReadClient:
    """Server-side ESPN reads for in-season jobs.

    Draft-time ESPN access happens in the browser, where the user's own session
    is already present. Scheduled jobs have no browser, so private leagues need
    the espn_s2/SWID cookies from settings. Without them this still works for
    public leagues and raises a clear error for private ones.
    """

    def __init__(self, client: httpx.Client | None = None) -> None:
        settings = get_settings()
        self._base = settings.espn_base_url
        cookies = {}
        if settings.espn_s2 and settings.espn_swid:
            cookies = {"espn_s2": settings.espn_s2, "SWID": settings.espn_swid}
        self.has_credentials = bool(cookies)
        self._client = client or httpx.Client(timeout=20.0, cookies=cookies)

    @retry(
        retry=retry_if_exception(_is_retryable),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, max=4),
        reraise=True,
    )
    def league_view(self, league_id: str, season: str, view: str) -> dict[str, Any]:
        resp = self._client.get(
            f"{self._base}/seasons/{season}/segments/0/leagues/{league_id}",
            params={"view": view},
        )
        if resp.status_code in (401, 403):
            hint = (
                "set ESPN_S2 and ESPN_SWID in .env"
                if not self.has_credentials
                else "stored ESPN cookies may have expired"
            )
            raise PermissionError(f"ESPN league {league_id} is private: {hint}")
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        return data

    def get_league_settings(self, league_id: str, season: str) -> LeagueSettings:
        return translate_espn_settings(self.league_view(league_id, season, "mSettings"))

    def get_rosters(self, league_id: str, season: str) -> dict[str, list[str]]:
        """teamId -> ESPN player ids currently rostered (starters and bench)."""
        payload = self.league_view(league_id, season, "mRoster")
        out: dict[str, list[str]] = {}
        for team in payload.get("teams") or []:
            entries = (team.get("roster") or {}).get("entries") or []
            out[str(team.get("id"))] = [
                str(entry["playerId"]) for entry in entries if entry.get("playerId") is not None
            ]
        return out
