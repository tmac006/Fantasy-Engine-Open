// Isolated-world content script on ESPN draft room pages. Receives tapped
// WebSocket frames from the MAIN-world tap, maintains draft state in
// chrome.storage.session, fetches league settings and the pick schedule with
// the user's own cookies, and runs the DOM-scrape fallback when no socket
// frames arrive (spec 8: selectors are constants, isolated, 5-minute fix).

import { isRealEspnPlayerId, parseEspnFrame } from "./adapters/espn-frames.ts";
import { rowMatchesPlayer } from "./lib/autodraft.ts";

const TAP_MESSAGE_TYPE = "fantasy-draft-tap";
const API_READS = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl";

// DOM fallback selectors (verified 2026 in Haud/wyncast#29). If ESPN breaks
// them, fix here and only here.
const SEL_PICK_FEED_ITEM = "li.pick-message__container";
const SEL_PLAYER_NAME = ".playerinfo__playername";

const DOM_FALLBACK_AFTER_MS = 20000;

export interface EspnDraftState {
  leagueId: string;
  seasonId: string;
  myTeamId: number | null;
  socketSeen: boolean;
  restSeen: boolean;
  usingDomFallback: boolean;
  // ESPN player IDs in observed order (socket path).
  pickedPlayerIds: string[];
  // Team IDs aligned with pickedPlayerIds, for opponent roster modeling.
  pickedTeamIds: number[];
  // Player display names (DOM fallback path; IDs unavailable in the DOM).
  pickedPlayerNames: string[];
  onClockTeamId: number | null;
  // The pick number ESPN's own header says is on the clock. Scraped by text
  // match so the panel can detect when it has missed picks (extension loaded
  // mid-draft, tab refreshed, socket gap) instead of advising off a stale board.
  clockPick: number | null;
  // ESPN's own "You are on the clock!" banner. The only turn signal that is
  // true in a practice room, where league team ids do not match seats.
  myTurn: boolean;
  /** Total rounds, from ESPN's own "RND 6 OF 16" header. */
  totalRounds: number | null;
  // overallPickNumber -> teamId, from the pre-allocated REST schedule.
  schedule: Record<string, number>;
  updatedAt: number;
}

const params = new URLSearchParams(window.location.search);
const state: EspnDraftState = {
  leagueId: params.get("leagueId") ?? "",
  seasonId: params.get("seasonId") ?? String(new Date().getFullYear()),
  myTeamId: params.get("teamId") ? Number(params.get("teamId")) : null,
  socketSeen: false,
  restSeen: false,
  usingDomFallback: false,
  pickedPlayerIds: [],
  pickedTeamIds: [],
  pickedPlayerNames: [],
  onClockTeamId: null,
  clockPick: null,
  myTurn: false,
  totalRounds: null,
  schedule: {},
  updatedAt: 0,
};

let dirty = false;
let flushTimer: number | null = null;
let draftDetailTimer: number | null = null;
let contextValid = true;
let domObserver: MutationObserver | null = null;

function markDirty(): void {
  dirty = true;
}

function stopOrphanedBridge(): void {
  contextValid = false;
  dirty = false;
  if (flushTimer !== null) window.clearInterval(flushTimer);
  flushTimer = null;
  if (draftDetailTimer !== null) window.clearInterval(draftDetailTimer);
  draftDetailTimer = null;
}

function hasExtensionContext(): boolean {
  try {
    return contextValid && Boolean(chrome.runtime?.id);
  } catch {
    return false;
  }
}

function setSession(value: Record<string, unknown>): Promise<void> {
  if (!hasExtensionContext()) {
    stopOrphanedBridge();
    return Promise.resolve();
  }
  return new Promise((resolve) => {
    try {
      // The callback form consumes runtime.lastError instead of letting Chrome
      // report it as an uncaught promise rejection during extension reloads.
      chrome.storage.session.set(value, () => {
        try {
          const lastError = chrome.runtime.lastError;
          if (lastError?.message?.includes("Extension context invalidated")) {
            stopOrphanedBridge();
          } else if (lastError) {
            console.warn(
              "draft-assistant: session storage write failed",
              lastError.message ?? "unknown Chrome runtime error",
            );
          }
        } catch {
          stopOrphanedBridge();
        }
        resolve();
      });
    } catch (error) {
      if (String(error).includes("Extension context invalidated")) {
        stopOrphanedBridge();
      } else {
        console.warn("draft-assistant: session storage write failed", error);
      }
      resolve();
    }
  });
}

flushTimer = window.setInterval(() => {
  if (!dirty) return;
  dirty = false;
  state.updatedAt = Date.now();
  void setSession({ espn_draft_state: state });
}, 500);

// -- socket path ------------------------------------------------------------

window.addEventListener("message", (ev: MessageEvent) => {
  if (ev.source !== window || ev.data?.type !== TAP_MESSAGE_TYPE) return;
  const payload = ev.data.payload as { event: string; data?: string };
  if (payload.event === "open") {
    // A live socket only reports picks made from now on. Dropping the DOM
    // fallback the moment it connects throws away the only view of the picks
    // that happened BEFORE we attached -- the drafted set holes out and the
    // panel starts recommending players who are already gone. The fallback is
    // retired in syncDraftDetail, once a source with history has delivered.
    state.socketSeen = true;
    markDirty();
    return;
  }
  if (payload.event === "close" || payload.event === "error") {
    state.socketSeen = false;
    enableDomFallback();
    markDirty();
    return;
  }
  if (payload.event !== "frame" || typeof payload.data !== "string") return;
  const frame = parseEspnFrame(payload.data);
  if (!frame) return;
  if (frame.kind === "selected") {
    if (isRealEspnPlayerId(frame.playerId) && !state.pickedPlayerIds.includes(frame.playerId)) {
      state.pickedPlayerIds.push(frame.playerId);
      state.pickedTeamIds.push(frame.teamId);
    }
  } else if (frame.kind === "selecting") {
    state.onClockTeamId = frame.teamId;
  }
  markDirty();
});

// -- cookie-authenticated REST fetches (page context sends espn_s2/SWID) ----

async function fetchLeagueJson(view: string): Promise<unknown> {
  const url =
    `${API_READS}/seasons/${state.seasonId}/segments/0/leagues/${state.leagueId}` +
    `?view=${view}&_cb=${Date.now()}`;
  const resp = await fetch(url, { credentials: "include", cache: "no-store" });
  if (!resp.ok) throw new Error(`espn ${view} ${resp.status}`);
  return resp.json();
}

async function loadSettingsAndSchedule(): Promise<void> {
  try {
    const settings = await fetchLeagueJson("mSettings");
    await setSession({ espn_league_settings: settings });
  } catch (err) {
    console.warn("draft-assistant: mSettings fetch failed", err);
  }
  await syncDraftDetail();
}

interface EspnDraftDetailPick {
  overallPickNumber: number;
  teamId: number;
  playerId?: number | string | null;
  player?: { id?: number | string | null };
}

async function syncDraftDetail(): Promise<void> {
  if (!contextValid) return;
  try {
    const detail = (await fetchLeagueJson("mDraftDetail")) as {
      draftDetail?: { picks?: EspnDraftDetailPick[] };
    };
    const picks = [...(detail.draftDetail?.picks ?? [])].sort(
      (a, b) => a.overallPickNumber - b.overallPickNumber,
    );
    for (const pick of picks) {
      state.schedule[String(pick.overallPickNumber)] = pick.teamId;
    }
    const selected = picks.flatMap((pick) => {
      const playerId = pick.playerId ?? pick.player?.id;
      return isRealEspnPlayerId(playerId)
        ? [{ playerId: String(playerId), teamId: pick.teamId }]
        : [];
    });
    // mDraftDetail describes the whole draft, so it is authoritative in both
    // directions. The old growth-only guard meant an undo or a room reset could
    // never shrink our list, leaving phantom picks in the drafted set for the
    // rest of the session. A zero-length response is still ignored: that is a
    // failed or pre-draft fetch, not an emptied draft.
    if (selected.length > 0) {
      state.pickedPlayerIds = selected.map((pick) => pick.playerId);
      state.pickedTeamIds = selected.map((pick) => pick.teamId);
      state.restSeen = true;
      // Now that a source WITH history has delivered, the scraper is redundant.
      state.usingDomFallback = false;
      if (domObserver !== null) {
        domObserver.disconnect();
        domObserver = null;
      }
    }
    markDirty();
  } catch (err) {
    console.warn("draft-assistant: mDraftDetail fetch failed", err);
  }
}

// -- DOM fallback -----------------------------------------------------------

function scrapeTotalRounds(): void {
  const match = document.body?.innerText.match(/RND\s*\d+\s*OF\s*(\d+)/i);
  if (!match) return;
  const rounds = Number(match[1]);
  if (Number.isFinite(rounds) && rounds > 0 && rounds !== state.totalRounds) {
    state.totalRounds = rounds;
    markDirty();
  }
}

function scrapeMyTurn(): void {
  const text = document.body?.innerText ?? "";
  // "You are on the clock!" when it is your pick; "You're on the clock in: N
  // Picks" when it is not. Require the absence of the countdown form.
  const onClock = /you(?:'re| are) on the clock!/i.test(text) && !/on the clock in:/i.test(text);
  if (onClock !== state.myTurn) {
    state.myTurn = onClock;
    markDirty();
  }
}

function scrapeClockPick(): void {
  // Text match, not a CSS selector: ESPN's class names churn, but the header
  // literally reads "ON THE CLOCK: PICK 27" in every draft room variant seen.
  const match = document.body?.innerText.match(/ON THE CLOCK:?\s*PICK\s*(\d+)/i);
  if (!match) return;
  const pick = Number(match[1]);
  if (Number.isFinite(pick) && pick !== state.clockPick) {
    state.clockPick = pick;
    markDirty();
  }
}

function scrapePickFeed(): void {
  const names: string[] = [];
  for (const item of document.querySelectorAll(SEL_PICK_FEED_ITEM)) {
    const name = item.querySelector(SEL_PLAYER_NAME)?.textContent?.trim();
    if (name) names.push(name);
  }
  if (names.length > state.pickedPlayerNames.length) {
    state.pickedPlayerNames = names;
    markDirty();
  }
}

function enableDomFallback(): void {
  if (domObserver !== null) return;
  if (!document.body) {
    window.addEventListener("DOMContentLoaded", enableDomFallback, { once: true });
    return;
  }
  state.usingDomFallback = true;
  markDirty();
  domObserver = new MutationObserver(() => scrapePickFeed());
  domObserver.observe(document.body, { childList: true, subtree: true });
  scrapePickFeed();
}

function armDomFallback(): void {
  setTimeout(() => {
    if (state.socketSeen) return;
    enableDomFallback();
  }, DOM_FALLBACK_AFTER_MS);
}

if (state.leagueId) {
  void loadSettingsAndSchedule();
  draftDetailTimer = window.setInterval(() => {
    void syncDraftDetail();
    scrapeClockPick();
    scrapeMyTurn();
    scrapeTotalRounds();
  }, 2000);
  armDomFallback();
  markDirty();
}

// -- auto-draft executor ----------------------------------------------------
// The panel decides WHETHER to draft (armed toggle, on-clock, attempt caps);
// this end only executes: filter the player list to the requested name and
// click ESPN's own DRAFT button, so the pick flows through ESPN's client
// exactly as a human click would. Best-effort DOM automation -- every failure
// is reported back and surfaced, never swallowed.

const SEARCH_SETTLE_MS = 500;
const CONFIRM_SETTLE_MS = 400;

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function findSearchBox(): HTMLInputElement | null {
  for (const input of document.querySelectorAll<HTMLInputElement>("input[type=text], input:not([type])")) {
    if (/player/i.test(input.placeholder ?? "")) return input;
  }
  return null;
}

function setReactInputValue(input: HTMLInputElement, value: string): void {
  // Assigning .value directly does not notify React-controlled inputs; go
  // through the native setter and then emit the input event.
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value")?.set;
  setter?.call(input, value);
  input.dispatchEvent(new Event("input", { bubbles: true }));
}

function draftButtons(root: ParentNode): HTMLElement[] {
  // ESPN renders the pick control differently across room variants; match any
  // clickable whose visible label is exactly DRAFT rather than assuming a tag.
  return [...root.querySelectorAll<HTMLElement>("button, a, [role=button]")].filter(
    (element) => (element.textContent ?? "").trim().toUpperCase() === "DRAFT",
  );
}

async function performAutoDraft(playerName: string): Promise<{ ok: boolean; detail: string }> {
  const search = findSearchBox();
  if (search) {
    setReactInputValue(search, playerName);
    await sleep(SEARCH_SETTLE_MS);
  }
  try {
    // Each DRAFT button sits inside a player row; match the row's text.
    let clicked: string | null = null;
    for (const button of draftButtons(document)) {
      const row = button.closest("tr, li, [role=row]") ?? button.parentElement?.parentElement;
      if (row && rowMatchesPlayer(row.textContent ?? "", playerName)) {
        button.click();
        clicked = (row.textContent ?? "").slice(0, 60);
        break;
      }
    }
    if (clicked === null) {
      return {
        ok: false,
        detail: search
          ? `no DRAFT row matched "${playerName}"`
          : "player search box not found",
      };
    }
    // Some room variants interpose a confirmation dialog; click through it if
    // one appeared. Harmless when absent.
    await sleep(CONFIRM_SETTLE_MS);
    for (const dialog of document.querySelectorAll("[role=dialog], [class*=modal i]")) {
      const confirm = draftButtons(dialog)[0];
      if (confirm) {
        confirm.click();
        break;
      }
    }
    return { ok: true, detail: `clicked DRAFT for ${playerName}` };
  } finally {
    if (search) setReactInputValue(search, "");
  }
}

chrome.runtime.onMessage.addListener(
  (message: unknown, _sender, sendResponse: (response: { ok: boolean; detail: string }) => void) => {
    const request = message as { type?: string; playerName?: string };
    if (request?.type !== "auto-draft" || typeof request.playerName !== "string") {
      return undefined;
    }
    void performAutoDraft(request.playerName).then(sendResponse);
    return true; // keep the channel open for the async response
  },
);
