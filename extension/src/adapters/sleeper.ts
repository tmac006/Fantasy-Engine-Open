// Sleeper draft reads. Phase 0 findings baked in: responses are CDN-cached
// (~15s, stale-while-revalidate) and NON-monotonic across polls, so every
// live read is cache-busted and picks are merged monotonically, never replaced.

import type { SleeperPick } from "../lib/types.ts";

const API_BASE = "https://api.sleeper.app";

// sleeper.com/draft/nfl/<draft_id>[?...]
const DRAFT_URL_RE = /sleeper\.com\/draft\/nfl\/(\d+)/;

export function parseDraftId(url: string): string | null {
  const match = DRAFT_URL_RE.exec(url);
  return match ? match[1] : null;
}

export function mergePicks(existing: SleeperPick[], incoming: SleeperPick[]): SleeperPick[] {
  const byNo = new Map<number, SleeperPick>();
  for (const p of existing) byNo.set(p.pick_no, p);
  for (const p of incoming) if (!byNo.has(p.pick_no)) byNo.set(p.pick_no, p);
  return [...byNo.values()].sort((a, b) => a.pick_no - b.pick_no);
}

export async function fetchPicks(draftId: string): Promise<SleeperPick[]> {
  const resp = await fetch(`${API_BASE}/v1/draft/${draftId}/picks?_cb=${Date.now()}`);
  if (!resp.ok) throw new Error(`sleeper picks ${resp.status}`);
  return (await resp.json()) as SleeperPick[];
}

export interface SleeperDraftMeta {
  status: string;
  settings: { teams: number; rounds: number };
}

export async function fetchDraftMeta(draftId: string): Promise<SleeperDraftMeta> {
  const resp = await fetch(`${API_BASE}/v1/draft/${draftId}?_cb=${Date.now()}`);
  if (!resp.ok) throw new Error(`sleeper draft ${resp.status}`);
  return (await resp.json()) as SleeperDraftMeta;
}

// 1-based slot on the clock for a 1-based overall pick (snake).
export function snakeSlotForPick(pickNo: number, teams: number): number {
  const roundIdx = Math.floor((pickNo - 1) / teams);
  const pos = (pickNo - 1) % teams;
  return roundIdx % 2 === 0 ? pos + 1 : teams - pos;
}

export function nextPickForSlot(
  currentPick: number,
  slot: number,
  teams: number,
  rounds: number,
): number | null {
  for (let pick = currentPick; pick <= teams * rounds; pick++) {
    if (snakeSlotForPick(pick, teams) === slot) return pick;
  }
  return null;
}

/** Every overall pick number belonging to `slot`, in order. */
export function snakePicksForSlot(slot: number, teams: number, rounds: number): number[] {
  const picks: number[] = [];
  for (let pick = 1; pick <= teams * rounds; pick++) {
    if (snakeSlotForPick(pick, teams) === slot) picks.push(pick);
  }
  return picks;
}

/** Picks this slot still owns from currentPick to the end, inclusive. */
export function remainingPicksForSlot(
  currentPick: number,
  slot: number,
  teams: number,
  rounds: number,
): number {
  let count = 0;
  for (let pick = currentPick; pick <= teams * rounds; pick++) {
    if (snakeSlotForPick(pick, teams) === slot) count += 1;
  }
  return count;
}
