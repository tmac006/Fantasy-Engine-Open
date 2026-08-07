import assert from "node:assert/strict";
import { test } from "node:test";

import { isRealEspnPlayerId, parseEspnFrame } from "./espn-frames.ts";

// Frame shapes from the Phase 0 protocol capture (TheFranchise HAR + ESPN bundle).

test("parses SELECTED with member guid", () => {
  const f = parseEspnFrame("SELECTED 2 4362628 4 {00000000-0000-4000-8000-000000000001}");
  assert.deepEqual(f, {
    kind: "selected",
    teamId: 2,
    playerId: "4362628",
    slotId: 4,
    memberGuid: "{00000000-0000-4000-8000-000000000001}",
  });
});

test("parses SELECTED without member guid (autopick)", () => {
  const f = parseEspnFrame("SELECTED 10 4426515 4");
  assert.equal(f?.kind, "selected");
  assert.equal((f as { memberGuid: string | null }).memberGuid, null);
});

test("parses SELECTING and CLOCK", () => {
  assert.deepEqual(parseEspnFrame("SELECTING 2 30000"), {
    kind: "selecting",
    teamId: 2,
    timeMs: 30000,
  });
  const clock = parseEspnFrame("CLOCK 1 28000 5 -1 0");
  assert.equal(clock?.kind, "clock");
  assert.equal((clock as { teamId: number }).teamId, 5);
});

test("ignores chatter and malformed frames", () => {
  assert.equal(parseEspnFrame("PING 1756607417674"), null);
  assert.equal(parseEspnFrame("JOINED 3 {guid}"), null);
  assert.equal(parseEspnFrame("SELECTED"), null);
  assert.equal(parseEspnFrame("SELECTED x y"), null);
  assert.equal(parseEspnFrame(""), null);
});

test("tolerates trailing whitespace/control residue", () => {
  const f = parseEspnFrame("SELECTED 1 4430807 2 \r");
  assert.equal(f?.kind, "selected");
  assert.equal((f as { playerId: string }).playerId, "4430807");
});

test("clock pick regex matches ESPN header text", () => {
  // Mirrors scrapeClockPick's pattern; keep the two in sync.
  const pattern = /ON THE CLOCK:?\s*PICK\s*(\d+)/i;
  assert.equal("ON THE CLOCK: PICK 27".match(pattern)?.[1], "27");
  assert.equal("on the clock: pick 108".match(pattern)?.[1], "108");
  assert.equal("YOU ARE ON THE CLOCK PICK 4".match(pattern)?.[1], "4");
  // Must not match the upcoming-pick cells ("PICK 28" without the clock text).
  assert.equal("PICK 28 AUTO Team 7".match(pattern), null);
});

test("ESPN pick sentinels are not real player ids", () => {
  // Real players and team defenses.
  assert.equal(isRealEspnPlayerId(3918298), true);
  assert.equal(isRealEspnPlayerId("-16033"), true); // Ravens D/ST
  assert.equal(isRealEspnPlayerId(-16001), true);
  // Sentinels for a pick that has not happened. Letting -1 through shifted
  // every later pick index by one and misattributed rosters all draft long.
  assert.equal(isRealEspnPlayerId(-1), false);
  assert.equal(isRealEspnPlayerId("-1"), false);
  assert.equal(isRealEspnPlayerId(0), false);
  assert.equal(isRealEspnPlayerId(null), false);
  assert.equal(isRealEspnPlayerId(undefined), false);
  assert.equal(isRealEspnPlayerId(""), false);
  assert.equal(isRealEspnPlayerId("abc"), false);
});
