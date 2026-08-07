// Local API client + board cache in chrome.storage.

import type { Recommendation } from "./types.ts";

export const API_BASE = "http://localhost:8000";

export async function fetchRecommendations(
  draftId: string,
  mySlot: number | null,
): Promise<Recommendation> {
  const slot = mySlot !== null ? `?my_slot=${mySlot}` : "";
  const resp = await fetch(
    `${API_BASE}/draft/sleeper/${draftId}/recommendations${slot}`,
    { signal: AbortSignal.timeout(5000) },
  );
  if (!resp.ok) throw new Error(`api ${resp.status}`);
  return (await resp.json()) as Recommendation;
}

export async function postEspnBoard(leagueJson: unknown): Promise<Recommendation> {
  const resp = await fetch(`${API_BASE}/draft/espn/board`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(leagueJson),
    signal: AbortSignal.timeout(10000),
  });
  if (!resp.ok) throw new Error(`api espn board ${resp.status}`);
  return (await resp.json()) as Recommendation;
}

export interface EspnLiveDraftRequest {
  league_json: unknown;
  picked_player_ids: string[];
  picked_team_ids?: number[];
  schedule: Record<string, number>;
  my_team_id: number | null;
  /** Overall picks this user owns, from the typed draft slot. Authoritative. */
  my_pick_numbers?: number[] | null;
}

export async function postEspnRecommendations(
  request: EspnLiveDraftRequest,
): Promise<Recommendation> {
  const resp = await fetch(`${API_BASE}/draft/espn/recommendations`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(request),
    signal: AbortSignal.timeout(10000),
  });
  if (!resp.ok) throw new Error(`api espn recommendations ${resp.status}`);
  return (await resp.json()) as Recommendation;
}

const CACHE_KEY = "cached_board";

export interface CachedBoard {
  draftId: string;
  fetchedAt: number;
  recommendation: Recommendation;
}

export async function cacheBoard(draftId: string, rec: Recommendation): Promise<void> {
  const entry: CachedBoard = { draftId, fetchedAt: Date.now(), recommendation: rec };
  await chrome.storage.local.set({ [CACHE_KEY]: entry });
}

export async function loadCachedBoard(draftId: string): Promise<CachedBoard | null> {
  const data = await chrome.storage.local.get(CACHE_KEY);
  const entry = data[CACHE_KEY] as CachedBoard | undefined;
  if (!entry || entry.draftId !== draftId) return null;
  // A cache written before the dual-pick shape has no recommended_pick. It is
  // one poll old at worst, so discard it rather than reconstruct it.
  if (!entry.recommendation?.recommended_pick) return null;
  return entry;
}
