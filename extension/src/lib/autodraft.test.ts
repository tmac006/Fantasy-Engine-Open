import assert from "node:assert/strict";
import { test } from "node:test";

import {
  ATTEMPT_COOLDOWN_MS,
  MAX_ATTEMPTS_PER_PICK,
  newAutoDraftState,
  pickForAutoDraft,
  recordAttempt,
  recordDrafted,
  rowMatchesPlayer,
  shouldAttempt,
  wouldExceedCeiling,
} from "./autodraft.ts";

test("disabled or off-clock never fires", () => {
  const state = newAutoDraftState();
  assert.equal(shouldAttempt(state, true, 12, 100_000), false);
  state.enabled = true;
  assert.equal(shouldAttempt(state, false, 12, 100_000), false);
  assert.equal(shouldAttempt(state, true, 12, 100_000), true);
});

test("caps attempts per pick and respects the cooldown", () => {
  const state = newAutoDraftState();
  state.enabled = true;
  let now = 100_000;
  for (let attempt = 0; attempt < MAX_ATTEMPTS_PER_PICK; attempt++) {
    assert.equal(shouldAttempt(state, true, 30, now), true);
    recordAttempt(state, 30, now);
    // Immediately after an attempt the cooldown blocks a refire.
    assert.equal(shouldAttempt(state, true, 30, now + 1000), false);
    now += ATTEMPT_COOLDOWN_MS + 1;
  }
  // Cap reached: never again for this pick, even after the cooldown.
  assert.equal(shouldAttempt(state, true, 30, now + 60_000), false);
  // A different pick starts fresh.
  assert.equal(shouldAttempt(state, true, 31, now + 60_000), true);
});

test("fresh state starts disarmed", () => {
  assert.equal(newAutoDraftState().enabled, false);
});

test("row matching survives ESPN's punctuation and injury tags", () => {
  assert.equal(rowMatchesPlayer("Ja'Marr Chase CIN WR DRAFT", "Ja'Marr Chase"), true);
  assert.equal(rowMatchesPlayer("AJ Brown Q PHI WR 198.5 DRAFT", "A.J. Brown"), true);
  assert.equal(rowMatchesPlayer("Marvin Harrison Jr. ARI WR", "Marvin Harrison Jr."), true);
  assert.equal(rowMatchesPlayer("Mike Evans O SF WR", "Mike Williams"), false);
  assert.equal(rowMatchesPlayer("anything", ""), false);
});

test("position ceilings stop a third QB even if the server says otherwise", () => {
  const state = newAutoDraftState();
  state.enabled = true;
  const qb1 = { name: "QB One", position: "QB" };
  const qb2 = { name: "QB Two", position: "QB" };
  const qb3 = { name: "QB Three", position: "QB" };
  const rb = { name: "RB One", position: "RB" };

  // Two quarterbacks are allowed.
  assert.equal(pickForAutoDraft(state, qb1, [qb1, rb])?.name, "QB One");
  recordDrafted(state, "QB");
  assert.equal(pickForAutoDraft(state, qb2, [qb2, rb])?.name, "QB Two");
  recordDrafted(state, "QB");
  // The third is refused and the best safe board row is taken instead.
  assert.equal(pickForAutoDraft(state, qb3, [qb3, rb])?.name, "RB One");
});

test("ceilings cover the streamer positions too", () => {
  const state = newAutoDraftState();
  const k = { name: "Kicker", position: "K" };
  const wr = { name: "Receiver", position: "WR" };
  recordDrafted(state, "K");
  assert.equal(wouldExceedCeiling(state, "K"), true);
  assert.equal(pickForAutoDraft(state, k, [k, wr])?.name, "Receiver");
});

test("held board rows are never used as the fallback", () => {
  const state = newAutoDraftState();
  recordDrafted(state, "QB");
  recordDrafted(state, "QB");
  const qb = { name: "QB Three", position: "QB" };
  const heldK = { name: "Kicker", position: "K", held_for_later: true };
  const wr = { name: "Receiver", position: "WR", held_for_later: false };
  assert.equal(pickForAutoDraft(state, qb, [heldK, wr])?.name, "Receiver");
});

test("returns null when nothing on the board is safe", () => {
  const state = newAutoDraftState();
  recordDrafted(state, "QB");
  recordDrafted(state, "QB");
  const qb = { name: "QB Three", position: "QB" };
  assert.equal(pickForAutoDraft(state, qb, [qb]), null);
});
