// API types are derived from the OpenAPI schema (src/lib/api-types.ts, produced
// by `npm run types`), so the extension cannot drift from the models it
// consumes. `npm run types:check` fails the build when they disagree.
//
// Only types the API does not own — platform payloads we read directly — are
// declared by hand below.

import type { components } from "./api-types.ts";

type Schemas = components["schemas"];

export type RankedPlayer = Schemas["RankedPlayer"];
export type PlayerAnalysis = Schemas["PlayerAnalysis"];
export type HiddenGem = Schemas["HiddenGem"];
export type RunAlert = Schemas["RunAlert"];
export type Recommendation = Schemas["Recommendation"];
export type NextTurnSimulation = Schemas["NextTurnSimulation"];

/** Sleeper's own draft pick payload; read straight from their API, not ours. */
export interface SleeperPick {
  pick_no: number;
  round: number;
  draft_slot: number;
  player_id: string;
  metadata?: {
    first_name?: string;
    last_name?: string;
    position?: string;
    team?: string;
  };
}
