// ESPN draft WebSocket frame parsing. Protocol verified in the Phase 0
// research (ESPN's own draft.js bundle + a captured live-draft HAR):
// plaintext, space-delimited, e.g. "SELECTED 3 4429795 2 {GUID}".
// The third SELECTED field is a lineup slotId, NOT the overall pick number.

export interface SelectedFrame {
  kind: "selected";
  teamId: number;
  playerId: string;
  slotId: number;
  memberGuid: string | null;
}

export interface SelectingFrame {
  kind: "selecting";
  teamId: number;
  timeMs: number;
}

export interface ClockFrame {
  kind: "clock";
  phase: string;
  timeMs: number;
  teamId: number;
}

export type EspnFrame = SelectedFrame | SelectingFrame | ClockFrame;

export function parseEspnFrame(raw: string): EspnFrame | null {
  // ESPN's client strips a trailing control char before splitting.
  const parts = raw.trim().split(/[\s\t\r]+/);
  switch (parts[0]) {
    case "SELECTED": {
      if (parts.length < 4) return null;
      const teamId = Number(parts[1]);
      const slotId = Number(parts[3]);
      if (!Number.isFinite(teamId) || !Number.isFinite(slotId)) return null;
      return {
        kind: "selected",
        teamId,
        playerId: parts[2],
        slotId,
        memberGuid: parts[4] ?? null,
      };
    }
    case "SELECTING": {
      if (parts.length < 3) return null;
      return { kind: "selecting", teamId: Number(parts[1]), timeMs: Number(parts[2]) };
    }
    case "CLOCK": {
      if (parts.length < 4) return null;
      return {
        kind: "clock",
        phase: parts[1],
        timeMs: Number(parts[2]),
        teamId: Number(parts[3]),
      };
    }
    default:
      return null;
  }
}


/**
 * Is this a real ESPN player id, or one of ESPN's "no pick yet" sentinels?
 *
 * Real players carry positive ids; team defenses carry `-16000 - proTeamId`,
 * so every legitimate D/ST is <= -16001. ESPN fills unmade picks in
 * `mDraftDetail` with `-1` (and occasionally `0`), which is NOT null -- a
 * null-only filter let the placeholder through as if it were a pick, which
 * shifted every later pick index by one and quietly attributed the wrong
 * players to the wrong teams for the rest of the draft.
 */
export function isRealEspnPlayerId(id: number | string | null | undefined): boolean {
  if (id === null || id === undefined || id === "") return false;
  const numeric = Number(id);
  if (!Number.isFinite(numeric)) return false;
  return numeric > 0 || numeric <= -16001;
}
