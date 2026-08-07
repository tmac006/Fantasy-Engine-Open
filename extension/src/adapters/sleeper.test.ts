import assert from "node:assert/strict";
import { test } from "node:test";

import { mergePicks, nextPickForSlot, parseDraftId, snakePicksForSlot, snakeSlotForPick } from "./sleeper.ts";
import type { SleeperPick } from "../lib/types.ts";

const pick = (n: number, player: string): SleeperPick => ({
  pick_no: n,
  round: Math.ceil(n / 12),
  draft_slot: 1,
  player_id: player,
});

test("parseDraftId extracts from draft room URLs", () => {
  assert.equal(
    parseDraftId("https://sleeper.com/draft/nfl/000000000000000001?ftue=commish"),
    "000000000000000001",
  );
  assert.equal(parseDraftId("https://sleeper.com/leagues/123/predraft"), null);
});

test("mergePicks never shrinks and keeps first-seen", () => {
  const current = [pick(1, "a"), pick(2, "b")];
  assert.deepEqual(mergePicks(current, [pick(1, "a")]), current); // stale response
  const grown = mergePicks(current, [pick(1, "a"), pick(2, "b"), pick(3, "c")]);
  assert.equal(grown.length, 3);
  assert.equal(mergePicks(current, [pick(2, "OTHER")])[1].player_id, "b");
});

test("snake math mirrors the server", () => {
  assert.equal(snakeSlotForPick(1, 10), 1);
  assert.equal(snakeSlotForPick(10, 10), 10);
  assert.equal(snakeSlotForPick(11, 10), 10);
  assert.equal(snakeSlotForPick(20, 10), 1);
  assert.equal(nextPickForSlot(1, 3, 10, 15), 3);
  assert.equal(nextPickForSlot(4, 3, 10, 15), 18);
  assert.equal(nextPickForSlot(142, 1, 10, 15), null);
});

test("snakePicksForSlot is empty for a slot outside the room", () => {
  // The trap this guards: [] is truthy in JS but falsy in Python, so sending it
  // made the panel skip its fallback while the server silently reverted to
  // team-id guessing -- the stranger-roster bug, reintroduced by a wrong size.
  assert.deepEqual(snakePicksForSlot(7, 2, 16), []);
  assert.deepEqual(snakePicksForSlot(13, 12, 16), []);
});

test("snakePicksForSlot follows the snake", () => {
  assert.deepEqual(snakePicksForSlot(1, 12, 3), [1, 24, 25]);
  assert.deepEqual(snakePicksForSlot(12, 12, 3), [12, 13, 36]);
  assert.deepEqual(snakePicksForSlot(7, 12, 4), [7, 18, 31, 42]);
});

test("room size must not be inferred from picks seen so far", () => {
  // Two picks in, distinct pickers is 2 -- the failure mode. A schedule names
  // every team before the draft starts, which is why sizing comes from there.
  const observedAfterTwoPicks = new Set([5, 9]).size;
  assert.equal(observedAfterTwoPicks, 2);
  assert.deepEqual(snakePicksForSlot(7, observedAfterTwoPicks, 16), []);
  const schedule: Record<string, number> = {};
  for (let pick = 1; pick <= 24; pick++) schedule[String(pick)] = ((pick - 1) % 12) + 1;
  assert.equal(new Set(Object.values(schedule)).size, 12);
  assert.deepEqual(snakePicksForSlot(7, 12, 2).length, 2);
});
