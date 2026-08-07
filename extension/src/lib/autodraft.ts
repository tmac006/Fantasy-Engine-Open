/**
 * Auto-draft decision logic, kept pure so it can be tested without a DOM.
 *
 * The DOM click itself lives in espn-bridge; everything that decides WHETHER
 * to click lives here. The rules are deliberately conservative: off by
 * default, never persisted (arming cannot survive into a real draft by
 * accident), at most two attempts per pick, and a settle delay so a pick is
 * never fired into a room whose state we saw for a single poll tick.
 */

export interface AutoDraftState {
  enabled: boolean;
  /** pick number -> attempts already made for that pick */
  attempts: Map<number, number>;
  /** ms timestamp of the last attempt, any pick */
  lastAttemptAt: number;
  /** Positions this toggle has actually drafted, counted client-side. */
  drafted: Map<string, number>;
}

/**
 * Ceilings the auto-drafter enforces on its own, independent of anything the
 * server believes about the roster.
 *
 * The server infers which picks are yours from ids and schedules, and when that
 * inference is wrong it hands the engine a stranger's roster -- which is how a
 * bot test ended with three quarterbacks. What the toggle itself clicked is not
 * an inference, so it is the one count that cannot be wrong, and it is enough to
 * make that failure impossible rather than merely unlikely.
 */
export const AUTO_POSITION_CEILING: Record<string, number> = {
  QB: 2,
  TE: 2,
  K: 1,
  DEF: 1,
};

export const MAX_ATTEMPTS_PER_PICK = 2;
export const ATTEMPT_COOLDOWN_MS = 4000;

export function newAutoDraftState(): AutoDraftState {
  return { enabled: false, attempts: new Map(), lastAttemptAt: 0, drafted: new Map() };
}

export function recordDrafted(state: AutoDraftState, position: string): void {
  const key = position.toUpperCase();
  state.drafted.set(key, (state.drafted.get(key) ?? 0) + 1);
}

/** True when taking this position would exceed what the toggle may draft. */
export function wouldExceedCeiling(state: AutoDraftState, position: string): boolean {
  const key = position.toUpperCase();
  const ceiling = AUTO_POSITION_CEILING[key];
  return ceiling !== undefined && (state.drafted.get(key) ?? 0) >= ceiling;
}

/**
 * The player to actually click: the top recommendation unless it would breach a
 * ceiling, in which case the best board row that would not. Returns null when
 * nothing is safe, which is a "stop and let the human pick" signal.
 */
export function pickForAutoDraft<T extends { name: string; position: string; held_for_later?: boolean }>(
  state: AutoDraftState,
  recommended: T,
  board: readonly T[],
): T | null {
  if (!wouldExceedCeiling(state, recommended.position)) return recommended;
  return (
    board.find((row) => !row.held_for_later && !wouldExceedCeiling(state, row.position)) ?? null
  );
}

export function shouldAttempt(
  state: AutoDraftState,
  onClock: boolean,
  pickNo: number,
  now: number,
): boolean {
  if (!state.enabled || !onClock) return false;
  if ((state.attempts.get(pickNo) ?? 0) >= MAX_ATTEMPTS_PER_PICK) return false;
  if (now - state.lastAttemptAt < ATTEMPT_COOLDOWN_MS) return false;
  return true;
}

export function recordAttempt(state: AutoDraftState, pickNo: number, now: number): void {
  state.attempts.set(pickNo, (state.attempts.get(pickNo) ?? 0) + 1);
  state.lastAttemptAt = now;
}

/**
 * Match our player name against ESPN's row text. ESPN appends injury tags
 * ("Mike Evans Q") and uses different punctuation ("AJ" vs "A.J.",
 * "Ja'Marr"), so compare on lowercase alphanumerics only and accept a
 * containment match against the row.
 */
export function normalizeName(name: string): string {
  return name.toLowerCase().replace(/[^a-z0-9]/g, "");
}

export function rowMatchesPlayer(rowText: string, playerName: string): boolean {
  const target = normalizeName(playerName);
  return target.length > 0 && normalizeName(rowText).includes(target);
}
