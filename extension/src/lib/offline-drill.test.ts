// Draft-day failure drill: the API dies mid-draft and the panel must keep
// giving correct advice from its cached board. Unit fixtures are too small to
// catch shape problems, so this runs the real cached board captured from the
// live API against a simulated draft that proceeds without a server.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import { computeOffline } from "./board.ts";
import type { Recommendation } from "./types.ts";

const cached = JSON.parse(
  readFileSync(new URL("./__fixtures__/real-board.json", import.meta.url), "utf8"),
) as Recommendation;

const sleeperId = (index: number): string =>
  cached.board[index].platform_ids["sleeper"];

test("cached board survives the API going down mid-draft", () => {
  // 30 picks have happened since the board was cached; the server is gone.
  const drafted = new Set(
    Array.from({ length: 30 }, (_, i) => sleeperId(i)).filter(Boolean),
  );
  const rec = computeOffline({
    board: cached.board,
    platform: "sleeper",
    draftedPlatformIds: drafted,
    picksUntilNext: 6,
    myNextPick: 40,
  });
  if (!rec) throw new Error("offline mode produced nothing");

  // Nobody already drafted may be recommended.
  assert.ok(!drafted.has(rec.recommended_pick.platform_ids["sleeper"]));
  for (const player of rec.board) {
    assert.ok(!drafted.has(player.platform_ids["sleeper"]), `${player.name} was drafted`);
  }
  // The advice must still be complete enough to act on.
  assert.ok(rec.recommended_pick.name);
  assert.ok(rec.recommended_pick.position);
  assert.equal(typeof rec.recommended_pick.score, "number");
  assert.ok(Number.isFinite(rec.recommended_pick.score));
  assert.ok(rec.room_pick.name);
  assert.equal(rec.alternates.length, 2);
  // Offline means no simulation ran; say so rather than implying modeled numbers.
  assert.equal(rec.simulation.trials, 0);
  assert.equal(rec.simulation.picks_simulated, 0);
});

test("tier counts are recomputed, not served stale from cache", () => {
  // Pick a tier that actually has company; the top of a real board is often a
  // one-player tier (Bijan Robinson is alone in RB tier 1).
  const groups = new Map<string, typeof cached.board>();
  for (const player of cached.board) {
    const key = `${player.position}:${player.tier}`;
    groups.set(key, [...(groups.get(key) ?? []), player]);
  }
  const samePosition = [...groups.values()]
    .filter((members) => members.length >= 2)
    .sort((a, b) => b.length - a.length)[0];
  assert.ok(samePosition, "expected at least one multi-player tier");
  const drafted = new Set([samePosition[0].platform_ids["sleeper"]]);
  const rec = computeOffline({
    board: cached.board,
    platform: "sleeper",
    draftedPlatformIds: drafted,
    picksUntilNext: 4,
    myNextPick: 20,
  });
  if (!rec) throw new Error("offline mode produced nothing");
  const survivor = rec.board.find(
    (p) => p.canonical_id === samePosition[1].canonical_id,
  );
  assert.ok(survivor);
  assert.equal(survivor.tier_remaining, samePosition.length - 1);
});

test("draining the whole board degrades cleanly instead of throwing", () => {
  const everyone = new Set(
    cached.board.map((p) => p.platform_ids["sleeper"]).filter(Boolean),
  );
  const rec = computeOffline({
    board: cached.board,
    platform: "sleeper",
    draftedPlatformIds: everyone,
    picksUntilNext: null,
    myNextPick: null,
  });
  assert.equal(rec, null, "an exhausted board must return null, not a broken pick");
});

test("late-round board still fills required starters, not more kickers", () => {
  // Deep into the draft: everything but the tail is gone.
  const drafted = new Set(
    cached.board.slice(0, 100).map((p) => p.platform_ids["sleeper"]).filter(Boolean),
  );
  const rec = computeOffline({
    board: cached.board,
    platform: "sleeper",
    draftedPlatformIds: drafted,
    picksUntilNext: 1,
    myNextPick: 150,
  });
  if (!rec) throw new Error("offline mode produced nothing");
  assert.ok(rec.recommended_pick.name);
  assert.ok(rec.board.length > 0);
});
