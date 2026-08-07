// Side panel main loop. Two platform modes:
// - Sleeper: poll our API (which polls Sleeper); offline fallback from cache.
// - ESPN: the draft is only visible inside the page, so the content-script
//   tap streams picks here and ranking runs locally against the cached board
//   (the server only builds the board from the league's settings).

import { fetchPicks, fetchDraftMeta, mergePicks, nextPickForSlot, parseDraftId, remainingPicksForSlot, snakePicksForSlot } from "../adapters/sleeper.ts";
import { isMyPick, roomSizeFromSchedule } from "../lib/roster.ts";
import {
  newAutoDraftState,
  pickForAutoDraft,
  recordAttempt,
  recordDrafted,
  shouldAttempt,
} from "../lib/autodraft.ts";
import {
  API_BASE,
  cacheBoard,
  fetchRecommendations,
  loadCachedBoard,
  postEspnBoard,
  postEspnRecommendations,
} from "../lib/api.ts";
import { computeOffline } from "../lib/board.ts";
import type { PlayerAnalysis, RankedPlayer, Recommendation, SleeperPick } from "../lib/types.ts";

const POLL_MS = 3000;
const MAX_BACKOFF_MS = 12000;
const ESPN_DRAFT_URL = /fantasy\.espn\.com\/football\/draft/;

interface EspnDraftState {
  leagueId: string;
  myTeamId: number | null;
  socketSeen: boolean;
  restSeen?: boolean;
  usingDomFallback: boolean;
  pickedPlayerIds: string[];
  pickedTeamIds?: number[];
  clockPick?: number | null;
  myTurn?: boolean;
  totalRounds?: number | null;
  pickedPlayerNames: string[];
  schedule: Record<string, number>;
  updatedAt: number;
}

interface State {
  mode: "sleeper" | "espn" | null;
  draftId: string | null; // sleeper draft id or "espn:<leagueId>"
  mySlot: number | null;
  picks: SleeperPick[];
  teams: number;
  rounds: number;
  errorStreak: number;
  offline: boolean;
  espnBoardRequested: boolean;
  /** Tab hosting the ESPN draft room, for auto-draft messaging. */
  draftTabId: number | null;
}

const state: State = {
  mode: null,
  draftId: null,
  mySlot: null,
  picks: [],
  teams: 12,
  rounds: 15,
  errorStreak: 0,
  offline: false,
  espnBoardRequested: false,
  draftTabId: null,
};

// Deliberately in-memory only: arming auto-draft must never survive a panel
// reopen, so it cannot carry over into a draft the user did not arm it for.
const autoDraft = newAutoDraftState();

const $ = <T extends HTMLElement>(id: string): T => document.getElementById(id) as T;

// -- config inputs ----------------------------------------------------------

async function restoreConfig(): Promise<void> {
  const stored = await chrome.storage.local.get(["my_slot", "draft_id_override"]);
  if (typeof stored["my_slot"] === "number") {
    state.mySlot = stored["my_slot"];
    $<HTMLInputElement>("my-slot").value = String(state.mySlot);
  }
  if (typeof stored["draft_id_override"] === "string" && stored["draft_id_override"]) {
    state.draftId = stored["draft_id_override"];
    state.mode = "sleeper";
    $<HTMLInputElement>("draft-id").value = state.draftId;
  }
}

function wireAutoDraft(): void {
  const box = $<HTMLInputElement>("auto-draft");
  box.addEventListener("change", () => {
    autoDraft.enabled = box.checked;
    $("auto-draft-state").hidden = !box.checked;
    if (box.checked && state.mode !== "espn") {
      setStatus("idle", "Auto-draft arms on an ESPN draft tab (Sleeper is watch-only)");
    }
  });
}

async function maybeAutoDraft(
  rec: Recommendation,
  currentPick: number,
  onClock: boolean,
): Promise<void> {
  if (state.mode !== "espn" || state.draftTabId === null) return;
  if (!shouldAttempt(autoDraft, onClock, currentPick, Date.now())) return;
  const target = pickForAutoDraft(autoDraft, rec.recommended_pick, rec.board);
  if (target === null) {
    setStatus("error", "Auto-draft stopped: no safe pick left -- pick manually");
    return;
  }
  const name = target.name;
  recordAttempt(autoDraft, currentPick, Date.now());
  try {
    const response = (await chrome.tabs.sendMessage(state.draftTabId, {
      type: "auto-draft",
      playerName: name,
    })) as { ok: boolean; detail: string };
    if (response?.ok) {
      recordDrafted(autoDraft, target.position);
      const swapped = target.name !== rec.recommended_pick.name;
      setStatus(
        "live",
        swapped ? `Auto-drafted ${name} (roster cap skipped the top pick)` : `Auto-drafted ${name}`,
      );
    } else {
      setStatus("error", `Auto-draft failed: ${response?.detail ?? "no response"} -- pick manually`);
    }
  } catch {
    setStatus("error", "Auto-draft failed: draft tab unreachable -- pick manually");
  }
}

function wireConfig(): void {
  $<HTMLInputElement>("my-slot").addEventListener("change", (e) => {
    const v = parseInt((e.target as HTMLInputElement).value, 10);
    state.mySlot = Number.isFinite(v) && v >= 1 ? v : null;
    void chrome.storage.local.set({ my_slot: state.mySlot });
  });
  $<HTMLInputElement>("draft-id").addEventListener("change", (e) => {
    const v = (e.target as HTMLInputElement).value.trim();
    state.draftId = v || null;
    state.mode = v ? "sleeper" : null;
    void chrome.storage.local.set({ draft_id_override: v });
  });
}

async function detectDraftFromTab(): Promise<void> {
  const tabs = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  const url = tabs[0]?.url ?? "";
  // Platform detection must beat a Sleeper override saved from another tab.
  // Otherwise an old draft ID makes the ESPN page poll Sleeper and report the
  // API as unreachable even though the local API is healthy.
  if (ESPN_DRAFT_URL.test(url)) {
    const leagueId = new URL(url).searchParams.get("leagueId");
    if (leagueId) {
      state.mode = "espn";
      state.draftTabId = tabs[0]?.id ?? null;
      if (state.draftId !== `espn:${leagueId}`) state.espnBoardRequested = false;
      state.draftId = `espn:${leagueId}`;
      const input = $<HTMLInputElement>("draft-id");
      input.value = "";
      input.placeholder = `espn:${leagueId}`;
      await chrome.storage.local.remove("draft_id_override");
    }
    return;
  }
  if ($<HTMLInputElement>("draft-id").value.trim()) return; // Sleeper manual override
  const sleeperId = parseDraftId(url);
  if (sleeperId) {
    if (sleeperId !== state.draftId) {
      state.mode = "sleeper";
      state.draftId = sleeperId;
      state.picks = [];
      $<HTMLInputElement>("draft-id").placeholder = sleeperId;
    }
    return;
  }
}

// -- rendering --------------------------------------------------------------

function setStatus(kind: "idle" | "live" | "error", text: string): void {
  $("status-dot").className = `dot dot-${kind === "live" ? "live" : kind === "error" ? "error" : "idle"}`;
  // The armed marker rides on every status so "did it arm?" is never a guess.
  $("status-text").textContent = autoDraft.enabled ? `${text} | AUTO armed` : text;
}

function playerMeta(player: RankedPlayer): string {
  return (
    `${player.position}${player.team ? " - " + player.team : ""} | proj ${player.points} | ` +
    `VOR ${player.vor}${player.adp !== null ? " | ADP " + player.adp.toFixed(1) : ""}`
  );
}

function detailRow(label: string, text: string): HTMLDivElement {
  const row = document.createElement("div");
  const strong = document.createElement("strong");
  strong.textContent = `${label}: `;
  row.append(strong, document.createTextNode(text));
  return row;
}

function renderAnalysis(targetId: string, analysis: PlayerAnalysis): void {
  const target = $(targetId);
  const rows: HTMLElement[] = [
    detailRow("Outlook", analysis.headline),
    detailRow("Projection", analysis.projection_summary),
    detailRow("Roster fit", analysis.roster_fit),
    detailRow("Timing", analysis.timing),
  ];
  const bullets = [
    ...analysis.model_factors.map((text) => `Model: ${text}`),
    ...analysis.usage.map((text) => `Usage: ${text}`),
    ...analysis.team_context.map((text) => `Team: ${text}`),
    ...analysis.risks.map((text) => `Risk: ${text}`),
  ];
  if (bullets.length) {
    const list = document.createElement("ul");
    list.className = "detail-list";
    list.replaceChildren(...bullets.map((text) => {
      const li = document.createElement("li");
      li.textContent = text;
      return li;
    }));
    rows.push(list);
  }
  if (analysis.status) rows.push(detailRow("Status", analysis.status));
  if (analysis.data_freshness) rows.push(detailRow("Updated", analysis.data_freshness));
  target.replaceChildren(...rows);
}

function renderPick(prefix: "recommended" | "room", player: RankedPlayer): void {
  $(`${prefix}-name`).textContent = player.name;
  $(`${prefix}-meta`).textContent = playerMeta(player);
  $(`${prefix}-reason`).textContent =
    player.reason || `tier ${player.tier} ${player.position}, ${player.tier_remaining} left`;
  renderAnalysis(`${prefix}-details`, player.analysis ?? {
    headline: player.reason,
    projection_summary: `${player.points} projected points; ${player.vor} above replacement`,
    roster_fit: "Cached board does not include current roster detail.",
    timing: player.adp === null ? "No market ADP available." : `Market ADP ${player.adp.toFixed(1)}.`,
    model_factors: [],
    usage: [],
    team_context: [],
    status: null,
    risks: [],
    data_freshness: null,
  });
}

function render(rec: Recommendation): void {
  $("content").hidden = false;

  // A roster the server could not identify makes every position call suspect,
  // so it is shown above the picks rather than buried in a server log.
  const warnings = rec.warnings ?? [];
  const banner = $("warnings");
  banner.hidden = warnings.length === 0;
  banner.replaceChildren(
    ...warnings.map((text) => {
      const div = document.createElement("div");
      div.className = "warning-line";
      div.textContent = text;
      return div;
    }),
  );

  const top = rec.recommended_pick;
  renderPick("recommended", top);
  renderPick("room", rec.room_pick);
  $("agreement-line").textContent = rec.picks_agree
    ? "Both models agree: this is the strongest pick and the tier the room is most likely to erase."
    : "The room-aware option protects against what opponents are taking; the recommendation maximizes your roster.";

  const alts = $("alt-list");
  alts.replaceChildren(
    ...rec.alternates.map((p) => {
      const li = document.createElement("li");
      li.innerHTML = `<strong>${p.name}</strong> <span class="meta">${p.position} | tier ${p.tier} | value ${p.value}</span>`;
      return li;
    }),
  );

  $("tier-line").textContent =
    `Tier ${top.tier} ${top.position}: ${top.tier_remaining} left` +
    (rec.tier_gone_risk > 0 ? `; ${rec.tier_gone_risk} likely gone before your next pick` : "");
  $("picks-line").textContent =
    rec.on_clock
      ? "You're on the clock"
      : rec.picks_until_next !== null
        ? `${rec.picks_until_next} picks until your turn`
        : state.mode === "espn"
          ? "Turn tracking arrives once the draft order posts"
          : "Set your draft slot for turn tracking";
  const run = $("run-line");
  run.hidden = rec.run_alert === null;
  if (rec.run_alert) {
    run.textContent = `${rec.run_alert.position} run: ${rec.run_alert.count} of last ${rec.run_alert.window} picks`;
  }

  const gem = rec.hidden_gems[0];
  $("hidden-gems").hidden = gem === undefined;
  if (gem) {
    const card = document.createElement("article");
    card.className = "gem";
    const title = document.createElement("div");
    title.className = "gem-title";
    title.textContent = `${gem.player.name} (${gem.player.position})`;
    const meta = document.createElement("div");
    meta.className = "gem-meta";
    meta.textContent = `Target picks ${gem.target_pick_min}-${gem.target_pick_max}: ${gem.upside}`;
    const risk = document.createElement("div");
    risk.className = "gem-risk";
    risk.textContent = `Risk: ${gem.risk}`;
    card.replaceChildren(title, meta, risk);
    $("gem-list").replaceChildren(card);
  } else {
    $("gem-list").replaceChildren();
  }

  const rows = rec.board.filter((p) => !p.held_for_later).slice(0, 15).map((p) => {
    const tr = document.createElement("tr");
    if (p.at_risk) tr.classList.add("risk");
    tr.innerHTML =
      `<td>${p.name}</td><td>${p.position}</td>` +
      `<td class="num">${p.value.toFixed(1)}</td><td class="num">T${p.tier}</td>`;
    return tr;
  });
  $("board").replaceChildren(...rows);

  $("mode-badge").hidden = !state.offline;
  $("updated").textContent = `updated ${new Date().toLocaleTimeString()}`;
}

// -- sleeper mode -----------------------------------------------------------

async function sleeperTick(draftId: string): Promise<void> {
  try {
    const rec = await fetchRecommendations(draftId, state.mySlot);
    state.offline = false;
    state.errorStreak = 0;
    await cacheBoard(draftId, rec);
    setStatus("live", `Live | Sleeper draft ${draftId}`);
    render(rec);
    return;
  } catch {
    state.errorStreak += 1;
  }

  try {
    const [meta, picks] = await Promise.all([fetchDraftMeta(draftId), fetchPicks(draftId)]);
    state.teams = meta.settings.teams;
    state.rounds = meta.settings.rounds;
    state.picks = mergePicks(state.picks, picks);
  } catch {
    // Even Sleeper unreachable: render pure cache below.
  }

  const cached = await loadCachedBoard(draftId);
  if (!cached) {
    setStatus("error", `API unreachable (${API_BASE}) and no cached board yet`);
    return;
  }
  const currentPick = state.picks.length + 1;
  const myNext =
    state.mySlot !== null ? nextPickForSlot(currentPick, state.mySlot, state.teams, state.rounds) : null;
  const myPicks = state.picks.filter(
    (pick) => state.mySlot !== null && pick.draft_slot === state.mySlot,
  );
  const offlineRec = computeOffline({
    board: cached.recommendation.board,
    platform: "sleeper",
    draftedPlatformIds: new Set(state.picks.map((p) => p.player_id)),
    myRosterPositions: myPicks
      .map((pick) => pick.metadata?.position)
      .filter((position): position is string => typeof position === "string"),
    myRosterNflTeams: myPicks
      .filter((pick) => typeof pick.metadata?.position === "string")
      .map((pick) => pick.metadata?.team ?? null),
    totalTeams: state.teams,
    picksUntilNext: myNext !== null ? myNext - currentPick : null,
    myNextPick: myNext,
    myRemainingPicks:
      state.mySlot !== null
        ? remainingPicksForSlot(currentPick, state.mySlot, state.teams, state.rounds)
        : null,
    knobs: cached.recommendation.offline_knobs,
  });
  state.offline = true;
  if (offlineRec) {
    setStatus("error", "Offline mode: cached board minus observed picks");
    render(offlineRec);
  } else {
    setStatus("error", "Offline: cached board exhausted");
  }
}

// -- espn mode --------------------------------------------------------------

const normalizeName = (n: string): string =>
  n.toLowerCase().replace(/[^a-z0-9 ]/g, "").replace(/\s+/g, " ").trim();

async function espnTick(leagueId: string): Promise<void> {
  const session = await chrome.storage.session.get(["espn_draft_state", "espn_league_settings"]);
  const draft = session["espn_draft_state"] as EspnDraftState | undefined;
  const leagueJson = session["espn_league_settings"];
  const cacheKey = `espn:${leagueId}`;

  let cached = await loadCachedBoard(cacheKey);
  if (!cached && !state.espnBoardRequested) {
    if (!leagueJson) {
      setStatus("idle", "Waiting for league settings from the draft page…");
      return;
    }
    state.espnBoardRequested = true;
    try {
      const rec = await postEspnBoard(leagueJson);
      await cacheBoard(cacheKey, rec);
      cached = await loadCachedBoard(cacheKey);
    } catch {
      state.espnBoardRequested = false; // API down; retry next tick
      setStatus("error", `Board build failed; is the API up at ${API_BASE}?`);
      return;
    }
  }
  if (!cached) return;
  const board = cached.recommendation.board;

  // Drafted set: socket path gives ESPN player IDs; DOM fallback gives names.
  const draftedIds = new Set(draft?.pickedPlayerIds ?? []);
  if (draft?.usingDomFallback && draft.pickedPlayerNames.length > 0) {
    const byName = new Map(board.map((p) => [normalizeName(p.name), p.platform_ids["espn"]]));
    for (const name of draft.pickedPlayerNames) {
      const espnId = byName.get(normalizeName(name));
      if (espnId) draftedIds.add(espnId);
    }
  }

  const currentPick = draftedIds.size + 1;

  // The typed draft slot beats every id-based inference: in a practice room the
  // URL's teamId is the user's REAL-league team, so matching it against the
  // real league's schedule attributes a stranger's roster to them (observed
  // live: roster came back as 4 RB / 3 WR for a QB/RB/RB/WR/WR/TE team, which
  // is why the panel kept offering a second QB and a second TE).
  // Room size must come from a source that is COMPLETE from the first poll.
  // Counting distinct teams among observed picks looked room-specific but grows
  // as the draft goes: two picks in, it says the league has two teams, and the
  // snake math for every early round is nonsense. The pre-allocated schedule
  // names every team before a single pick is made.
  const { teams: espnTeams, rounds: espnRounds } = roomSizeFromSchedule(
    draft?.schedule,
    draft?.totalRounds,
    { teams: state.teams, rounds: state.rounds },
  );

  // A slot outside the room has no picks, and an empty list is a trap: it is
  // truthy in JS (so the panel skips its own fallback) and falsy in Python (so
  // the server silently reverts to team-id guessing). Send null and say so.
  const slotPicks =
    state.mySlot !== null ? snakePicksForSlot(state.mySlot, espnTeams, espnRounds) : [];
  const myPickNumbers = slotPicks.length > 0 ? slotPicks : null;
  const slotWarning =
    state.mySlot !== null && slotPicks.length === 0
      ? `draft slot ${state.mySlot} is outside this ${espnTeams}-team room -- `
        + "set it to your seat number or roster advice will be wrong"
      : null;

  let myNext: number | null = null;
  let remainingMine = 0;
  if (myPickNumbers) {
    remainingMine = myPickNumbers.filter((pick) => pick >= currentPick).length;
    myNext = myPickNumbers.find((pick) => pick >= currentPick) ?? null;
  } else if (draft?.myTeamId != null) {
    for (const [pickStr, teamId] of Object.entries(draft.schedule)) {
      const pick = Number(pickStr);
      if (teamId === draft.myTeamId && pick >= currentPick) {
        remainingMine += 1;
        if (myNext === null || pick < myNext) myNext = pick;
      }
    }
  }

  // ESPN's header is the ground truth for how far the draft has advanced;
  // if we have seen materially fewer picks than that, every number below is
  // built on a stale board and the user must be told, not advised.
  const expectedPick = draft?.clockPick ?? null;
  const desyncWarning =
    expectedPick !== null && expectedPick - (draftedIds.size + 1) >= 3
      ? `only ${draftedIds.size} of ${expectedPick - 1} picks observed -- `
        + "refresh the ESPN draft page to resync"
      : null;

  if (leagueJson) {
    try {
      const live = await postEspnRecommendations({
        league_json: leagueJson,
        picked_player_ids: [...draftedIds],
        picked_team_ids: draft?.pickedTeamIds,
        schedule: draft?.schedule ?? {},
        my_team_id: draft?.myTeamId ?? null,
        my_pick_numbers: myPickNumbers,
      });
      const inputWarnings = [desyncWarning, slotWarning].filter(
        (warning): warning is string => warning !== null,
      );
      if (inputWarnings.length) live.warnings = [...inputWarnings, ...(live.warnings ?? [])];
      await cacheBoard(cacheKey, live);
      state.offline = false;
      const fresh = draft && Date.now() - draft.updatedAt < 15000;
      if (desyncWarning) {
        // Every number below is computed from a board we know is behind, so it
        // must not wear the same "live" badge as a healthy poll.
        setStatus("error", `ESPN | STALE BOARD | ${draftedIds.size} picks seen`);
      } else if (draft?.restSeen && fresh) {
        setStatus("live", `ESPN | live REST | ${draftedIds.size} picks seen`);
      } else if (draft?.usingDomFallback) {
        setStatus("live", `ESPN | DOM fallback | ${draftedIds.size} picks seen`);
      } else if (draft?.socketSeen && fresh) {
        setStatus("live", `ESPN | live model | ${draftedIds.size} picks seen`);
      } else {
        setStatus("idle", `ESPN | live model waiting (${draftedIds.size} picks seen)`);
      }
      render(live);
      // A desynced board means on_clock itself is computed from stale state;
      // advising off it is bad enough, clicking DRAFT off it is worse.
      // ESPN's own banner is authoritative for whose turn it is; the
      // server's on_clock is only a fallback when the slot is unset.
      const onClock = draft?.myTurn ?? live.on_clock;
      if (!desyncWarning) void maybeAutoDraft(live, currentPick, onClock);
      return;
    } catch {
      // The cached board remains a safe clock-time fallback.
    }
  }

  const metaByEspnId = new Map(
    board.flatMap((player) => {
      const espnId = player.platform_ids["espn"];
      return espnId ? [[espnId, { position: player.position, team: player.team }] as const] : [];
    }),
  );
  // Same ownership rule as the live path. Keying this off myTeamId while the
  // server keys off the slot made offline and online disagree about whose
  // roster it was, so an API blip silently changed the advice.
  const ownedPicks = myPickNumbers !== null ? new Set(myPickNumbers) : null;
  const myRoster = (draft?.pickedPlayerIds ?? []).flatMap((playerId, index) => {
    const meta = metaByEspnId.get(playerId);
    const owner = draft?.pickedTeamIds?.[index] ?? draft?.schedule[String(index + 1)];
    return meta && isMyPick(index, ownedPicks, owner, draft?.myTeamId) ? [meta] : [];
  });
  const rec = computeOffline({
    board,
    platform: "espn",
    draftedPlatformIds: draftedIds,
    myRosterPositions: myRoster.map((entry) => entry.position),
    myRosterNflTeams: myRoster.map((entry) => entry.team),
    totalTeams: espnTeams,
    picksUntilNext: myNext !== null ? myNext - currentPick : null,
    myNextPick: myNext,
    // remainingMine counts picks >= currentPick, matching my_remaining_picks.
    myRemainingPicks:
      myPickNumbers !== null || draft?.myTeamId != null ? remainingMine : null,
    knobs: cached.recommendation.offline_knobs,
  });
  // Reaching here means the live call failed or settings are missing: this is
  // the cached-board heuristic, not a live answer, and the status must say so.
  const offlineWarnings = [
    "API unreachable -- showing a cached board, not live recommendations",
    desyncWarning,
    slotWarning,
  ].filter((warning): warning is string => warning !== null);
  if (rec) rec.warnings = [...offlineWarnings, ...(rec.warnings ?? [])];
  state.offline = true;
  if (!rec) {
    setStatus("error", "Board exhausted");
    return;
  }
  const fresh = draft && Date.now() - draft.updatedAt < 15000;
  if (draft?.restSeen && fresh) {
    setStatus("live", `ESPN | live REST | ${draftedIds.size} picks seen`);
  } else if (draft?.usingDomFallback) {
    setStatus("live", `ESPN | DOM fallback | ${draftedIds.size} picks seen`);
  } else if (draft?.socketSeen && fresh) {
    setStatus("live", `ESPN | live socket | ${draftedIds.size} picks seen`);
  } else {
    setStatus("idle", `ESPN | waiting for draft activity (${draftedIds.size} picks seen)`);
  }
  render(rec);
}

// -- loop -------------------------------------------------------------------

async function tick(): Promise<void> {
  await detectDraftFromTab();
  if (!state.draftId || !state.mode) {
    setStatus("idle", "Open a Sleeper or ESPN draft room (or enter a draft ID)");
    return;
  }
  if (state.mode === "espn") {
    await espnTick(state.draftId.replace(/^espn:/, ""));
  } else {
    await sleeperTick(state.draftId);
  }
}

function loop(): void {
  const delay = Math.min(POLL_MS * Math.max(1, state.errorStreak), MAX_BACKOFF_MS);
  void tick().finally(() => setTimeout(loop, delay));
}

async function main(): Promise<void> {
  await restoreConfig();
  wireConfig();
  wireAutoDraft();
  loop();
}

void main();
