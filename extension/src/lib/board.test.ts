import assert from "node:assert/strict";
import { test } from "node:test";

import { computeOffline } from "./board.ts";
import type { RankedPlayer } from "./types.ts";

const player = (
  id: string,
  sleeperId: string,
  position: string,
  tier: number,
  value: number,
  adp: number,
  team: string | null = null,
): RankedPlayer => ({
  canonical_id: id,
  name: `Player ${id}`,
  position,
  team,
  points: value + 100,
  vor: value,
  value,
  score: value,
  tier,
  tier_remaining: 0,
  adp,
  survival: 1,
  disappearance_probability: 0,
  opportunity_cost: 0,
  expected_next_turn_value: 0,
  reach_penalty: 0,
  at_risk: false,
  held_for_later: false,
  pos_rank: 1,
  reason: "",
  platform_ids: { sleeper: sleeperId },
  analysis: {
    headline: "",
    projection_summary: "",
    projection_consensus: 0,
    projection_low: 0,
    projection_high: 0,
    projection_is_placeholder: true,
    roster_fit: "",
    timing: "",
    model_factors: [],
    usage: [],
    team_context: [],
    status: null,
    risks: [],
    data_freshness: null,
  },
});

const board = [
  player("1", "s1", "RB", 1, 90, 1),
  player("2", "s2", "RB", 1, 85, 2),
  player("3", "s3", "WR", 1, 80, 3),
  player("4", "s4", "RB", 2, 60, 9),
  player("5", "s5", "WR", 2, 55, 30),
];

test("offline filters drafted players by sleeper id", () => {
  const rec = computeOffline({
    board,
    platform: "sleeper",
    draftedPlatformIds: new Set(["s1"]),
    picksUntilNext: 5,
    myNextPick: 8,
  });
  if (!rec) throw new Error("expected a recommendation");
  assert.equal(rec.recommended_pick.canonical_id, "2");
  assert.ok(!rec.board.some((p) => p.canonical_id === "1"));
});

test("offline recounts tier_remaining among available players", () => {
  const rec = computeOffline({
    board,
    platform: "sleeper",
    draftedPlatformIds: new Set(["s1"]),
    picksUntilNext: 5,
    myNextPick: 8,
  });
  if (!rec) throw new Error("expected a recommendation");
  assert.equal(rec.recommended_pick.tier, 1);
  assert.equal(rec.recommended_pick.tier_remaining, 1); // s1 gone; only s2 left in RB tier 1
});

test("offline recomputes at_risk from adp vs my next pick", () => {
  const rec = computeOffline({
    board,
    platform: "sleeper",
    draftedPlatformIds: new Set(),
    picksUntilNext: 7,
    myNextPick: 8,
  });
  if (!rec) throw new Error("expected a recommendation");
  const p1 = rec.board.find((p) => p.canonical_id === "1");
  const p4 = rec.board.find((p) => p.canonical_id === "4");
  assert.equal(p1?.at_risk, true); // adp 1 < my next pick 8
  assert.equal(p4?.at_risk, false); // adp 9 survives to pick 8
});

test("offline returns null when board exhausted", () => {
  const rec = computeOffline({
    board,
    platform: "sleeper",
    draftedPlatformIds: new Set(["s1", "s2", "s3", "s4", "s5"]),
    picksUntilNext: null,
    myNextPick: null,
  });
  assert.equal(rec, null);
});

test("offline suppresses an early backup tight end", () => {
  const rec = computeOffline({
    board: [
      player("te", "ste", "TE", 1, 100, 55),
      player("wr", "swr", "WR", 2, 80, 48),
    ],
    platform: "sleeper",
    draftedPlatformIds: new Set(),
    myRosterPositions: ["TE"],
    totalTeams: 12,
    picksUntilNext: 3,
    myNextPick: 50,
  });
  if (!rec) throw new Error("expected a recommendation");
  assert.equal(rec.recommended_pick.canonical_id, "wr");
  assert.equal(rec.board.find((candidate) => candidate.canonical_id === "te")?.held_for_later, true);
});

test("offline keeps a third tight end held late", () => {
  const rec = computeOffline({
    board: [
      player("te3", "ste3", "TE", 1, 100, 145),
      player("wr", "swr", "WR", 2, 70, 140),
    ],
    platform: "sleeper",
    draftedPlatformIds: new Set(),
    myRosterPositions: ["TE", "TE"],
    totalTeams: 12,
    picksUntilNext: 2,
    myNextPick: 155,
  });
  if (!rec) throw new Error("expected a recommendation");
  assert.equal(rec.recommended_pick.canonical_id, "wr");
  assert.equal(rec.board.find((candidate) => candidate.canonical_id === "te3")?.held_for_later, true);
});

test("offline suppresses a second kicker once one is rostered", () => {
  const rec = computeOffline({
    board: [
      player("k2", "sk2", "K", 1, 95, 140),
      player("wr", "swr", "WR", 2, 70, 120),
    ],
    platform: "sleeper",
    draftedPlatformIds: new Set(),
    myRosterPositions: ["K"],
    totalTeams: 12,
    picksUntilNext: 1,
    myNextPick: 160,
  });
  if (!rec) throw new Error("expected a recommendation");
  assert.equal(rec.recommended_pick.canonical_id, "wr");
  assert.equal(rec.board.find((candidate) => candidate.canonical_id === "k2")?.held_for_later, true);
});

test("offline applies QB-WR stack bonus and WR-RB same-team penalty", () => {
  const rec = computeOffline({
    board: [
      player("stack", "s1", "WR", 1, 80, 40, "KC"),
      player("anti", "s2", "WR", 1, 80, 41, "SF"),
      player("neutral", "s3", "WR", 1, 80, 42, "DAL"),
    ],
    platform: "sleeper",
    draftedPlatformIds: new Set(),
    myRosterPositions: ["QB", "RB"],
    myRosterNflTeams: ["KC", "SF"],
    picksUntilNext: 4,
    myNextPick: 40,
  });
  if (!rec) throw new Error("expected a recommendation");
  const byId = Object.fromEntries(rec.board.map((candidate) => [candidate.canonical_id, candidate]));
  assert.equal(byId.stack.score, 82.5);
  assert.equal(byId.anti.score, 78);
  assert.equal(byId.neutral.score, 80);
  assert.equal(rec.recommended_pick.canonical_id, "stack");
});

test("offline releases held K/DEF at the endgame", () => {
  // The server ships cached K/DEF rows held so they never surface mid-draft;
  // with two picks left and both slots open the offline path must free them.
  const heldKicker = { ...player("k1", "sk1", "K", 1, 95, 165), held_for_later: true };
  const heldDefense = { ...player("d1", "sd1", "DEF", 1, 90, 160), held_for_later: true };
  const rec = computeOffline({
    board: [heldKicker, heldDefense, player("wr", "swr", "WR", 3, 40, 150)],
    platform: "sleeper",
    draftedPlatformIds: new Set(),
    myRosterPositions: ["QB", "RB", "RB", "WR", "WR", "TE", "WR", "RB"],
    totalTeams: 12,
    picksUntilNext: 8,
    myNextPick: 170,
    myRemainingPicks: 2,
  });
  if (!rec) throw new Error("expected a recommendation");
  assert.equal(rec.recommended_pick.position, "K");
  const board = Object.fromEntries(rec.board.map((c) => [c.canonical_id, c.held_for_later]));
  assert.equal(board.k1, false);
  assert.equal(board.d1, false);
});

test("offline keeps K/DEF held while picks remain plentiful", () => {
  const heldKicker = { ...player("k1", "sk1", "K", 1, 95, 165), held_for_later: true };
  const rec = computeOffline({
    board: [heldKicker, player("wr", "swr", "WR", 3, 40, 150)],
    platform: "sleeper",
    draftedPlatformIds: new Set(),
    myRosterPositions: ["QB", "RB", "RB", "WR"],
    totalTeams: 12,
    picksUntilNext: 8,
    myNextPick: 100,
    myRemainingPicks: 8,
  });
  if (!rec) throw new Error("expected a recommendation");
  assert.equal(rec.recommended_pick.position, "WR");
  assert.equal(
    rec.board.find((c) => c.canonical_id === "k1")?.held_for_later,
    true,
  );
});

test("offline honours engine knobs shipped on the board", () => {
  // The point of shipping knobs: a server-side change to when a backup QB may
  // surface must move the offline fallback too, with no code change here.
  const qb = player("qb2", "sqb2", "QB", 2, 60, 30);
  const wr = player("wr", "swr", "WR", 3, 40, 31);
  const inputs = {
    board: [qb, wr],
    platform: "sleeper" as const,
    draftedPlatformIds: new Set<string>(),
    myRosterPositions: ["QB"],
    totalTeams: 12,
    // Round 3: default knobs hold a backup QB until round 10.
    picksUntilNext: 1,
    myNextPick: 30,
  };
  const held = computeOffline(inputs);
  assert.equal(held?.board.find((c) => c.canonical_id === "qb2")?.held_for_later, true);

  // Same board, server says backups open at round 2 -> no longer held.
  const released = computeOffline({
    ...inputs,
    knobs: { duplicate_qb_te_after_round: 2 },
  });
  assert.equal(released?.board.find((c) => c.canonical_id === "qb2")?.held_for_later, false);
});

test("offline stops climbing a position the lineup cannot start", () => {
  // The live failure, offline: five backs rostered and the second receiver slot
  // empty. The cached board still ranks the back higher, because it was scored
  // against a roster several picks staler than the one being drafted.
  const rb = player("rb6", "srb6", "RB", 7, 40, 110);
  const wr = player("wr2", "swr2", "WR", 9, 12, 115);
  const result = computeOffline({
    board: [rb, wr],
    platform: "sleeper" as const,
    draftedPlatformIds: new Set<string>(),
    myRosterPositions: ["QB", "RB", "RB", "WR", "TE", "RB", "RB", "RB"],
    totalTeams: 12,
    picksUntilNext: 0,
    myNextPick: 104,
    myRemainingPicks: 8,
  });
  assert.equal(result?.recommended_pick.canonical_id, "wr2");
  // A sixth back at three startable slots needs all three out at once.
  assert.ok((result?.board.find((c) => c.canonical_id === "rb6")?.score ?? 0) < 1);
});

test("offline keeps a fourth back available rather than banning depth", () => {
  const rb = player("rb4", "srb4", "RB", 7, 40, 110);
  const result = computeOffline({
    board: [rb, player("wr4", "swr4", "WR", 9, 12, 115)],
    platform: "sleeper" as const,
    draftedPlatformIds: new Set<string>(),
    myRosterPositions: ["QB", "RB", "RB", "WR", "WR", "TE", "RB"],
    totalTeams: 12,
    picksUntilNext: 0,
    myNextPick: 104,
    myRemainingPicks: 9,
  });
  const row = result?.board.find((c) => c.canonical_id === "rb4");
  assert.equal(row?.held_for_later, false);
  assert.equal(result?.recommended_pick.canonical_id, "rb4");
});

test("offline penalties demote a sub-replacement player instead of promoting him", () => {
  // Multiplying a negative score by a penalty inverts it: x0.35 moves -20 up to
  // -7 and rewards exactly what it should punish.
  const belowReplacement = player("qb2", "sqb2", "QB", 5, -20, 60);
  const result = computeOffline({
    board: [belowReplacement, player("wr", "swr", "WR", 5, -25, 61)],
    platform: "sleeper" as const,
    draftedPlatformIds: new Set<string>(),
    myRosterPositions: ["QB"],
    totalTeams: 12,
    picksUntilNext: 1,
    myNextPick: 30,
  });
  const row = result?.board.find((c) => c.canonical_id === "qb2");
  assert.ok((row?.score ?? 0) < -20, `expected demotion, got ${row?.score}`);
});
