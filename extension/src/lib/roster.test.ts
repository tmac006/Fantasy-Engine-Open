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
