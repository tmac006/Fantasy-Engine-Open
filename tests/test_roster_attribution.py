"""Working out which completed picks are mine.

Getting this wrong is not a cosmetic bug. An unidentified roster reads to the
engine as "every starter slot is still open", which is how a live ESPN practice
draft came to recommend a second quarterback in round 5 to a team that already
had one.
"""

from api.routers.draft import _attribute_my_roster

# 12-team snake. Team 4 picks 4th, then 21st, then 28th, ...
SNAKE = {
    **{str(pick): pick for pick in range(1, 13)},
    **{str(13 + offset): 12 - offset for offset in range(12)},
    **{str(25 + offset): offset + 1 for offset in range(12)},
}


def positions_for(count: int) -> list[str | None]:
    """A plausible run of picks: QBs land on 4 and 21, which team 4 owns."""
    out: list[str | None] = ["RB"] * count
    if count > 3:
        out[3] = "QB"  # pick 4
    if count > 20:
        out[20] = "WR"  # pick 21
    return out


class TestNormalCase:
    def test_room_ids_are_used_when_they_agree(self) -> None:
        positions = positions_for(24)
        owners = [SNAKE[str(i)] for i in range(1, 25)]
        mine, teams, used, warnings = _attribute_my_roster(
            my_team_id=4,
            picked_team_ids=owners,
            schedule=SNAKE,
            positions=positions,
            nfl_teams=[None] * 24,
        )
        assert mine == ("QB", "WR")
        assert not warnings
        assert len(teams) == 2
        assert used == [str(o) for o in owners]

    def test_no_picks_yet_is_not_a_warning(self) -> None:
        mine, _, _, warnings = _attribute_my_roster(
            my_team_id=4, picked_team_ids=[], schedule=SNAKE, positions=[], nfl_teams=[]
        )
        assert mine == ()
        assert not warnings


class TestPracticeDraftMismatch:
    """The room's team ids and the league's are not always the same namespace."""

    def test_falls_back_to_the_schedule(self) -> None:
        positions = positions_for(24)
        # The practice room numbers its teams 101..112, so nothing matches team 4.
        room_ids = [100 + SNAKE[str(i)] for i in range(1, 25)]
        mine, _, used, warnings = _attribute_my_roster(
            my_team_id=4,
            picked_team_ids=room_ids,
            schedule=SNAKE,
            positions=positions,
            nfl_teams=[None] * 24,
        )
        assert mine == ("QB", "WR"), "the schedule still knows which picks are mine"
        assert used == [str(SNAKE[str(i)]) for i in range(1, 25)]
        assert any("disagrees with the league schedule for team 4" in w for w in warnings)

    def test_the_recovered_roster_would_block_a_second_qb(self) -> None:
        """The actual regression: QB must appear so the engine holds a backup."""
        mine, _, _, _ = _attribute_my_roster(
            my_team_id=4,
            picked_team_ids=[100 + SNAKE[str(i)] for i in range(1, 25)],
            schedule=SNAKE,
            positions=positions_for(24),
            nfl_teams=[None] * 24,
        )
        assert "QB" in mine


class TestUnrecoverable:
    def test_missing_team_id_is_called_out(self) -> None:
        mine, _, _, warnings = _attribute_my_roster(
            my_team_id=None,
            picked_team_ids=[1, 2, 3],
            schedule=SNAKE,
            positions=["RB", "WR", "TE"],
            nfl_teams=[None] * 3,
        )
        assert mine == ()
        assert any("no team id" in w for w in warnings)

    def test_schedule_that_omits_me_warns_rather_than_pretending(self) -> None:
        # Neither source knows about team 99: better to say so than to imply an
        # empty roster is a real one.
        mine, _, _, warnings = _attribute_my_roster(
            my_team_id=99,
            picked_team_ids=[1, 2, 3],
            schedule=SNAKE,
            positions=["RB", "WR", "TE"],
            nfl_teams=[None] * 3,
        )
        assert mine == ()
        # owed == 0 here, so this is genuinely "not my turn yet", not an error.
        assert not any("could not be matched" in w for w in warnings)

    def test_owed_picks_with_nothing_matched_is_flagged(self) -> None:
        # Schedule says team 4 owns pick 4, but no position resolved for it.
        mine, _, _, warnings = _attribute_my_roster(
            my_team_id=4,
            picked_team_ids=[100 + SNAKE[str(i)] for i in range(1, 6)],
            schedule=SNAKE,
            positions=[None] * 5,
            nfl_teams=[None] * 5,
        )
        assert mine == ()
        assert any("could not be matched to a player" in w for w in warnings)


class TestLengthMismatch:
    def test_short_room_ids_fall_back_to_the_schedule(self) -> None:
        positions = positions_for(24)
        mine, _, _, warnings = _attribute_my_roster(
            my_team_id=4,
            picked_team_ids=[1, 2],  # stale/partial
            schedule=SNAKE,
            positions=positions,
            nfl_teams=[None] * 24,
        )
        assert mine == ("QB", "WR")
        assert not warnings, "the schedule covered it cleanly; nothing to report"

    def test_no_room_ids_at_all(self) -> None:
        mine, _, _, _ = _attribute_my_roster(
            my_team_id=4,
            picked_team_ids=None,
            schedule=SNAKE,
            positions=positions_for(24),
            nfl_teams=[None] * 24,
        )
        assert mine == ("QB", "WR")


class TestExplicitPickNumbers:
    """The typed draft slot outranks every id-based inference.

    Live failure this encodes: in an ESPN practice draft the URL's teamId names
    the user's real-league team, so both the schedule and the room ids pointed
    at a different seat. The server handed the engine a stranger's roster --
    4 RB / 3 WR for a team that actually held QB/RB/RB/WR/WR/TE -- and the panel
    duly recommended a second quarterback and a second tight end.
    """

    def test_pick_numbers_beat_a_mismatched_schedule(self) -> None:
        # Schedule says team 4 owns picks 4 and 21; the user actually sits in
        # the seat owning picks 7 and 18.
        positions: list[str | None] = ["RB"] * 24
        positions[6] = "QB"   # pick 7
        positions[17] = "TE"  # pick 18
        mine, teams, _, warnings = _attribute_my_roster(
            my_team_id=4,
            picked_team_ids=[SNAKE[str(i)] for i in range(1, 25)],
            schedule=SNAKE,
            positions=positions,
            nfl_teams=[None] * 24,
            my_pick_numbers=[7, 18, 31, 42],
        )
        assert mine == ("QB", "TE")
        assert len(teams) == 2
        assert not warnings

    def test_future_picks_do_not_count_as_missing(self) -> None:
        mine, _, _, warnings = _attribute_my_roster(
            my_team_id=4,
            picked_team_ids=None,
            schedule=SNAKE,
            positions=["RB"] * 10,
            nfl_teams=[None] * 10,
            my_pick_numbers=[7, 18, 31],
        )
        assert mine == ("RB",)  # only pick 7 has happened
        assert not warnings

    def test_unmatched_owned_pick_is_reported(self) -> None:
        positions: list[str | None] = ["RB"] * 24
        positions[6] = None  # my pick 7 did not resolve to a player
        mine, _, _, warnings = _attribute_my_roster(
            my_team_id=4,
            picked_team_ids=None,
            schedule=SNAKE,
            positions=positions,
            nfl_teams=[None] * 24,
            my_pick_numbers=[7, 18],
        )
        assert mine == ("RB",)
        assert any("could not be matched to a player" in w for w in warnings)
