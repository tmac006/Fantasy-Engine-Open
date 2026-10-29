import assert from "node:assert/strict";
import { test } from "node:test";

import {
  BRIDGE_FRESH_MS,
  HEARTBEAT_MS,
  acceptBridgeState,
  bridgeFresh,
  draftStateKey,
  leagueSettingsKey,
} from "./bridge-state.ts";

const draft = (leagueId: string, updatedAt: number) => ({
  leagueId,
  myTurn: true,
  updatedAt,
});

test("storage keys are distinct per tab", () => {
  assert.notEqual(draftStateKey(1), draftStateKey(2));
  assert.notEqual(leagueSettingsKey(1), leagueSettingsKey(2));
  assert.notEqual(draftStateKey(1), leagueSettingsKey(1));
});

test("matching league state is accepted", () => {
  const result = acceptBridgeState(draft("123", 1000), "123");
  assert.equal(result.mismatch, false);
  assert.equal(result.draft?.leagueId, "123");
});

test("mismatched league state is refused and flagged", () => {
  // A tab that navigated between leagues leaves the other league's state
  // behind for a beat; acting on it means advising (or auto-drafting!) off
  // the wrong room.
  const result = acceptBridgeState(draft("999", 1000), "123");
  assert.equal(result.mismatch, true);
  assert.equal(result.draft, undefined);
});

test("absent state is not a mismatch", () => {
  const result = acceptBridgeState(undefined, "123");
  assert.equal(result.mismatch, false);
  assert.equal(result.draft, undefined);
});

test("fresh state within the window, stale beyond it", () => {
  const now = 100_000;
  assert.equal(bridgeFresh(draft("1", now - BRIDGE_FRESH_MS + 1), now), true);
  assert.equal(bridgeFresh(draft("1", now - BRIDGE_FRESH_MS), now), false);
  assert.equal(bridgeFresh(undefined, now), false);
});

test("heartbeat cadence keeps a healthy bridge inside the freshness window", () => {
  // If a heartbeat ever exceeded the freshness window, a healthy-but-quiet
  // room would read as frozen and auto-draft would be gated off on the
  // user's real turn.
  assert.ok(HEARTBEAT_MS < BRIDGE_FRESH_MS);
});
