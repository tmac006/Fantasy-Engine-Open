/**
 * Session-storage contract between the ESPN bridge content script and the
 * panel, keyed per draft-room TAB.
 *
 * One unkeyed slot was a live hazard: two ESPN draft tabs (a mock plus the
 * real room -- same league id! -- or two leagues drafting the same evening)
 * clobbered each other's state every 500ms, and armed auto-draft could act on
 * the other room's myTurn. The tab id is the only identifier that separates
 * two rooms of the SAME league, so it is the storage key; the league id check
 * is the second lock for stale state left behind by a tab that navigated.
 */

export interface BridgeDraftState {
  leagueId: string;
  myTurn?: boolean;
  updatedAt: number;
}

/**
 * How stale bridge state may be before the panel refuses to act on it. The
 * bridge heartbeats every HEARTBEAT_MS even when nothing changed, so passing
 * this threshold means the bridge is dead or frozen -- and a frozen bridge
 * keeps myTurn=true forever, which armed auto-draft must never trust.
 *
 * The threshold has to clear Chrome's intensive timer throttling, not just the
 * heartbeat. Once a tab has been hidden for 5 minutes Chrome caps repeating
 * timers at roughly ONE wake per minute, so a perfectly healthy bridge in a
 * background draft tab reports an age near 60s. At 15s the panel called that
 * dead: it dropped the live badge and, worse, silently refused to auto-draft
 * during exactly the long waits seat 11 has (21 picks between 14 and 35).
 * 90s clears the throttled cadence with margin while still catching a bridge
 * that is genuinely gone, which shows up as minutes of silence, not seconds.
 */
export const BRIDGE_FRESH_MS = 90000;
export const HEARTBEAT_MS = 10000;

export function draftStateKey(tabId: number): string {
  return `espn_draft_state_tab${tabId}`;
}

export function leagueSettingsKey(tabId: number): string {
  return `espn_league_settings_tab${tabId}`;
}

/** Accept bridge state only when it describes the league this panel serves. */
export function acceptBridgeState<T extends BridgeDraftState>(
  draft: T | undefined,
  expectedLeagueId: string,
): { draft: T | undefined; mismatch: boolean } {
  if (draft === undefined) return { draft: undefined, mismatch: false };
  if (draft.leagueId !== expectedLeagueId) return { draft: undefined, mismatch: true };
  return { draft, mismatch: false };
}

export function bridgeFresh(draft: BridgeDraftState | undefined, now: number): boolean {
  return draft !== undefined && now - draft.updatedAt < BRIDGE_FRESH_MS;
}
