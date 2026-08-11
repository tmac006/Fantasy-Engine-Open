// Offline fallback (spec §8: mandatory). When the API is unreachable, serve
// the cached board minus locally-observed picks. Never a spinner mid-clock.

import type { RankedPlayer, Recommendation } from "./types.ts";

export interface OfflineInputs {
  board: RankedPlayer[];
  platform: "sleeper" | "espn";
  draftedPlatformIds: Set<string>;
  myRosterPositions?: string[];
  /** NFL team abbreviations parallel to myRosterPositions. */
  myRosterNflTeams?: Array<string | null | undefined>;
  totalTeams?: number;
  picksUntilNext: number | null;
  myNextPick: number | null;
  /** Picks I still own including the current one; enables the endgame release. */
  myRemainingPicks?: number | null;
  /** Engine knobs from the cached board; falls back to DEFAULT_KNOBS. */
  knobs?: Partial<OfflineKnobs> | null;
}

/**
 * Fallbacks only. The live values ride on the cached board (`offline_knobs`)
 * so that changing EngineParams cannot silently desync offline advice; these
 * apply only to a board cached before the field existed.
 */
export interface OfflineKnobs {
  duplicate_qb_te_after_round: number;
  duplicate_te_after_round: number;
  early_duplicate_qb_te_multiplier: number;
  qb_wr_stack_bonus: number;
  wr_rb_correlation_penalty: number;
  startable_slots: Record<string, number>;
  starter_unavailable_rate: number;
  depth_penalty_floor: number;
}

const DEFAULT_KNOBS: OfflineKnobs = {
  duplicate_qb_te_after_round: 10,
  duplicate_te_after_round: 13,
  early_duplicate_qb_te_multiplier: 0.35,
  qb_wr_stack_bonus: 2.5,
  wr_rb_correlation_penalty: 2.0,
  startable_slots: { QB: 1, RB: 3, WR: 3, TE: 2, K: 1, DEF: 1 },
  starter_unavailable_rate: 0.2214,
  depth_penalty_floor: 0.25,
};

/**
 * Apply a multiplier so it demotes or promotes whatever the sign of the value.
 * Plain multiplication inverts on the negative scores a late board is made of:
 * a x0.35 penalty moves -20 up to -7, rewarding exactly what it should punish.
 */
function scaleSigned(value: number, multiplier: number, floor: number): number {
  return value >= 0 ? value * multiplier : value / Math.max(multiplier, floor);
}

/**
 * What one more player at this position is worth once the lineup is already
 * full there: the chance a week calls on him. Mirrors `_depth_multiplier` on
 * the server, including the rate fitted from 2025 availability.
 */
function depthMultiplier(knobs: OfflineKnobs, position: string, have: number): number {
  const slots = knobs.startable_slots[position] ?? 0;
  const depth = have + 1 - slots;
  if (depth <= 0) return 1;
  const rate = knobs.starter_unavailable_rate;
  let total = 0;
  for (let out = depth; out <= slots; out += 1) {
    let ways = 1;
    for (let step = 0; step < out; step += 1) ways = (ways * (slots - step)) / (step + 1);
    total += ways * rate ** out * (1 - rate) ** (slots - out);
  }
  return total;
}

function offlineCorrelationAdjustment(
  knobs: OfflineKnobs,
  player: RankedPlayer,
  rosterPositions: string[],
  rosterNflTeams: Array<string | null | undefined>,
): number {
  const team = player.team?.trim().toUpperCase();
  if (!team) return 0;
  const teammates = rosterPositions.filter((position, index) => {
    const rosterTeam = rosterNflTeams[index]?.trim().toUpperCase();
    return rosterTeam === team;
  });
  if (teammates.length === 0) return 0;

  let adjustment = 0;
  if (player.position === "WR" && teammates.includes("QB")) adjustment += knobs.qb_wr_stack_bonus;
  if (player.position === "QB" && teammates.includes("WR")) {
    adjustment += knobs.qb_wr_stack_bonus * Math.min(
      teammates.filter((position) => position === "WR").length,
      2,
    );
  }
  if (
    (player.position === "WR" && teammates.includes("RB"))
    || (player.position === "RB" && teammates.includes("WR"))
  ) {
    adjustment -= knobs.wr_rb_correlation_penalty;
  }
  return adjustment;
}

export function computeOffline(inputs: OfflineInputs): Recommendation | null {
  const knobs: OfflineKnobs = { ...DEFAULT_KNOBS, ...(inputs.knobs ?? {}) };
  const available = inputs.board.filter((p) => {
    const pid = p.platform_ids[inputs.platform];
    return pid === undefined || !inputs.draftedPlatformIds.has(pid);
  });
  if (available.length === 0) return null;

  // Recount tiers among who's left; cached tier_remaining is stale by now.
  const tierCounts = new Map<string, number>();
  for (const p of available) {
    const key = `${p.position}:${p.tier}`;
    tierCounts.set(key, (tierCounts.get(key) ?? 0) + 1);
  }
  const rosterCounts = new Map<string, number>();
  const rosterPositions = inputs.myRosterPositions ?? [];
  const rosterNflTeams = inputs.myRosterNflTeams ?? [];
  for (const position of rosterPositions) {
    rosterCounts.set(position, (rosterCounts.get(position) ?? 0) + 1);
  }
  const currentPick =
    inputs.myNextPick !== null && inputs.picksUntilNext !== null
      ? inputs.myNextPick - inputs.picksUntilNext
      : null;
  const currentRound =
    currentPick === null
      ? null
      : Math.floor((currentPick - 1) / Math.max(1, inputs.totalTeams ?? 12)) + 1;
  const withCounts = available.map((p) => {
    const teCount = rosterCounts.get("TE") ?? 0;
    const earlyDuplicateQb =
      currentRound !== null
      && currentRound < knobs.duplicate_qb_te_after_round
      && p.position === "QB"
      && (rosterCounts.get("QB") ?? 0) >= 1;
    // Backup TE stays off-board until late; a third TE stays off entirely.
    const backupTe =
      p.position === "TE"
      && (
        teCount >= 2
        || (teCount >= 1
          && (currentRound === null || currentRound < knobs.duplicate_te_after_round))
      );
    // Once K/DEF is rostered, keep backups off the offline recommendation surface.
    const filledLateStreamer =
      (p.position === "K" || p.position === "DEF")
      && (rosterCounts.get(p.position) ?? 0) >= 1;
    // Endgame release: the server now ships K/DEF held on the cached board
    // (they must not surface mid-draft), so the offline path has to run its
    // own release once remaining picks barely cover the open K/DEF slots --
    // otherwise an offline round 15 recommends bench RBs forever.
    const openLate =
      ((rosterCounts.get("K") ?? 0) >= 1 ? 0 : 1)
      + ((rosterCounts.get("DEF") ?? 0) >= 1 ? 0 : 1);
    const lateRelease =
      (p.position === "K" || p.position === "DEF")
      && !filledLateStreamer
      && inputs.myRemainingPicks != null
      && openLate > 0
      && inputs.myRemainingPicks <= openLate + 2;
    const correlation = offlineCorrelationAdjustment(knobs, p, rosterPositions, rosterNflTeams);
    // A player stacked past what the lineup can start is worth only the chance
    // a week calls on him. Without this the offline board keeps climbing the
    // same position: every remaining back outranks the receiver filling an
    // empty starting slot, because the cached score was ranked against a roster
    // several picks staler than the one being drafted.
    const depth = depthMultiplier(knobs, p.position, rosterCounts.get(p.position) ?? 0);
    const combined = (earlyDuplicateQb ? knobs.early_duplicate_qb_te_multiplier : 1) * depth;
    const scoreBase = scaleSigned(p.score, combined, knobs.depth_penalty_floor);
    const valueBase = scaleSigned(p.value, combined, knobs.depth_penalty_floor);
    return {
    ...p,
    score: scoreBase + correlation,
    value: valueBase + correlation,
    held_for_later:
      (p.held_for_later && !lateRelease)
      || earlyDuplicateQb
      || backupTe
      || filledLateStreamer
      // Stacked so deep that no combination of absences could start him.
      || depth === 0,
    tier_remaining: tierCounts.get(`${p.position}:${p.tier}`) ?? 0,
    at_risk:
      inputs.myNextPick !== null && p.adp !== null ? p.adp < inputs.myNextPick : p.at_risk,
    };
  });
  withCounts.sort(
    (a, b) =>
      Number(a.held_for_later) - Number(b.held_for_later)
      || b.score - a.score,
  );

  const top = withCounts[0];
  const roomPick = withCounts.find((p) => p.at_risk && !p.held_for_later) ?? top;
  const tierGone = withCounts.filter(
    (p) => p.position === top.position && p.tier === top.tier && p.at_risk,
  ).length;
  return {
    // The offline board is built from a cached ranking, so it has no server
    // input to complain about; the status line already says it is offline.
    warnings: [],
    // Echo what this recompute actually used, so a board re-cached offline
    // carries the same knobs forward instead of silently reverting.
    offline_knobs: knobs,
    recommended_pick: top,
    room_pick: roomPick,
    picks_agree: top.canonical_id === roomPick.canonical_id,
    alternates: withCounts
      .filter(
        (p) =>
          p.canonical_id !== top.canonical_id
          && p.canonical_id !== roomPick.canonical_id,
      )
      .slice(0, 2),
    board: withCounts,
    hidden_gems: [],
    // Offline means no server, so no room simulation ran. Report that honestly
    // with zero trials rather than implying modeled numbers we do not have;
    // at_risk above falls back to the plain ADP comparison.
    simulation: {
      trials: 0,
      seed: 0,
      picks_simulated: 0,
      disappearance_probability: {},
      expected_fallback_value: {},
      expected_fallback_player: {},
    },
    tier_gone_risk: tierGone,
    run_alert: null, // needs position lookups the offline cache doesn't carry
    picks_until_next: inputs.picksUntilNext,
    on_clock: inputs.picksUntilNext === 0,
  };
}
