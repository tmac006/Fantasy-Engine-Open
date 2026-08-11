import assert from "node:assert/strict";
import { test } from "node:test";

import { isMyPick, roomSizeFromSchedule } from "./roster.ts";

const FALLBACK = { teams: 12, rounds: 15 };

test("room size comes from the schedule, not from picks seen", () => {
  const schedule: Record<string, number> = {};
  for (let pick = 1; pick <= 192; pick++) schedule[String(pick)] = ((pick - 1) % 12) + 1;
  assert.deepEqual(roomSizeFromSchedule(schedule, 16, FALLBACK), { teams: 12, rounds: 16 });
});

test("room size falls back to picks/rounds when team ids are unusable", () => {
  const schedule: Record<string, number> = {};
  for (let pick = 1; pick <= 160; pick++) schedule[String(pick)] = 1;
  assert.deepEqual(roomSizeFromSchedule(schedule, 16, FALLBACK), { teams: 10, rounds: 16 });
});

test("room size falls back entirely with no schedule", () => {
  assert.deepEqual(roomSizeFromSchedule({}, null, FALLBACK), FALLBACK);
  assert.deepEqual(roomSizeFromSchedule(undefined, null, FALLBACK), FALLBACK);
});

test("typed slot beats team ids for ownership", () => {
  const owned = new Set([7, 18, 31]);
  assert.equal(isMyPick(6, owned, 99, 1), true); // pick 7, wrong team id, still mine
  assert.equal(isMyPick(7, owned, 1, 1), false); // pick 8, my team id, not my slot
});

test("team ids are used only when no slot was typed", () => {
  assert.equal(isMyPick(3, null, 4, 4), true);
  assert.equal(isMyPick(3, null, 5, 4), false);
  assert.equal(isMyPick(3, null, 4, null), false);
  assert.equal(isMyPick(3, null, undefined, 4), false);
});

test("room size counts distinct team ids, not the largest one", () => {
  // A real 16-team ESPN league: ids run to 21 and skip five values. Sizing the
  // room off the maximum id would report 21 teams and break every snake turn.
  const teamIds = [1, 2, 4, 5, 6, 7, 8, 9, 10, 11, 13, 14, 15, 16, 20, 21];
  const schedule: Record<string, number> = {};
  let pick = 1;
  for (let round = 0; round < 16; round++) {
    const order = round % 2 === 0 ? teamIds : [...teamIds].reverse();
    for (const id of order) schedule[String(pick++)] = id;
  }
  const size = roomSizeFromSchedule(schedule, 16, { teams: 12, rounds: 16 });
  assert.equal(size.teams, 16);
  assert.equal(size.rounds, 16);
});

test("a team id outside the room-size range still owns its picks", () => {
  const owned = new Set([16, 17]);
  assert.equal(isMyPick(15, owned, 21, 21), true);
  assert.equal(isMyPick(15, null, 21, 21), true);
  assert.equal(isMyPick(15, null, 20, 21), false);
});
