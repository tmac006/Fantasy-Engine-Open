/**
 * Which observed picks are mine, on the extension side.
 *
 * The server answers this too (`_attribute_my_roster`), and deliberately keeps
 * its schedule/room fallback for rooms where no slot is typed. What must not
 * differ is the panel's own answer: the live path and the offline path each had
 * their own copy, so an API blip could change whose roster the advice was for.
 * One function, used by both.
 */

export interface RoomSize {
  teams: number;
  rounds: number;
}

/**
 * Room dimensions from the pre-allocated schedule, which names every team
 * before a single pick is made. Counting distinct teams among *observed* picks
 * looks equivalent and is not: two picks in it reports a two-team league, and
 * every snake calculation for the early rounds comes out wrong.
 */
export function roomSizeFromSchedule(
  schedule: Record<string, number> | undefined,
  totalRounds: number | null | undefined,
  fallback: RoomSize,
): RoomSize {
  const rounds = totalRounds ?? fallback.rounds;
  const distinctTeams = new Set(Object.values(schedule ?? {})).size;
  const scheduledPicks = Object.keys(schedule ?? {}).length;
  const teams =
    distinctTeams >= 2
      ? distinctTeams
      : scheduledPicks > 0 && rounds > 0
        ? Math.max(2, Math.round(scheduledPicks / rounds))
        : fallback.teams;
  return { teams, rounds };
}

/**
 * The picks a roster entry at each index belongs to me.
 *
 * `ownedPicks` (from the typed slot) wins when present. Otherwise fall back to
 * team ids, which is the only option in a room where the user never told us
 * their seat.
 */
export function isMyPick(
  index: number,
  ownedPicks: Set<number> | null,
  ownerTeamId: number | undefined,
  myTeamId: number | null | undefined,
): boolean {
  if (ownedPicks !== null) return ownedPicks.has(index + 1);
  if (myTeamId === null || myTeamId === undefined) return false;
  return ownerTeamId === myTeamId;
}
