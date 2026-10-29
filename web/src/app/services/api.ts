import { HttpClient } from '@angular/common/http';
import { Injectable, inject, signal } from '@angular/core';
import { firstValueFrom } from 'rxjs';

export interface NewsEntry {
  id: number;
  source: string;
  title: string;
  url: string | null;
  published_at: string | null;
  tag: string;
  player: string | null;
  position: string | null;
  team: string | null;
  on_my_roster: boolean;
}

export interface RegisteredLeague {
  id: number;
  platform: string;
  league_id: string;
  season: string;
  name: string | null;
  my_team_id: string | null;
  active: boolean;
  rostered_players: number;
  unmapped_players: number;
  sync_error?: string | null;
}

export interface JobStatus {
  job: string;
  description: string;
  interval_hours: number;
  last_success: string | null;
  age_hours: number | null;
  due: boolean;
}


export interface PlayerCard {
  canonical_id: string;
  name: string;
  position: string;
  team: string | null;
  points: number;
  adp: number | null;
  bye_week: number | null;
  market_value: number;
  market_premium: number;
  headshot_url: string | null;
  news: string | null;
  /** What this player alone would add to MY starting lineup. */
  solo_gain?: number | null;
  recommended?: boolean;
}

export interface LeagueRosterView {
  team_id: string;
  team_name: string;
  is_mine: boolean;
  players: PlayerCard[];
}

export interface RostersResponse {
  league_id: number;
  my_team_id: string;
  my_needs: TradeNeed[];
  rosters: LeagueRosterView[];
}

export interface WeekImpact {
  week: number;
  before: number;
  after: number;
  slots_before: number;
  slots_after: number;
}

export interface TradeSide {
  team_id: string;
  team_name: string;
  players: PlayerCard[];
  lineup_before: number;
  lineup_after: number;
  lineup_gain: number;
  market_value_sent: number;
}

export interface BuildAroundResponse {
  league_id: number;
  anchor: PlayerCard;
  anchor_is_mine: boolean;
  mode: string;
  packages: TradePackage[];
}

export interface TradeEvaluation {
  league_id: number;
  my_team_name: string;
  give: PlayerCard[];
  receive: PlayerCard[];
  my_gain: number;
  their_gain: number;
  market_edge: number;
  bye_adjusted_gain_per_week: number;
  worst_week_before: WeekImpact | null;
  worst_week_after: WeekImpact | null;
  roster_size_after: number;
  roster_limit: number;
  verdict: string;
  notes: string[];
  my_side: TradeSide;
  their_side: TradeSide;
}

export interface TradeNeed {
  position: string;
  upgrade_value: number;
  owned: number;
}

export interface TradePackage {
  their_team_id: string;
  their_team_name: string;
  give: PlayerCard[];
  receive: PlayerCard[];
  my_gain: number;
  their_gain: number;
  market_edge: number;
  rationale: string[];
}

export interface TradesResponse {
  league_id: number;
  league_name: string | null;
  season: string;
  mode: string;
  my_team_name: string;
  my_needs: TradeNeed[];
  packages: TradePackage[];
  injury_signals: string[];
}

export interface NewsQuery {
  leagueId: string;
  hours: string;
  mineOnly: boolean;
}

export interface Outlook {
  risk: number;
  reward: number;
  bust_probability: number;
  projected_ceiling: number;
  label: string;
  drivers: string[];
}

export interface WaiverTarget {
  canonical_id: number;
  name: string;
  position: string;
  team: string | null;
  headshot_url?: string | null;
  score: number;
  season_ppg: number | null;
  recent_ppg: number | null;
  expected_points_recent: number | null;
  points_over_expected: number | null;
  is_emerging: boolean;
  role_intact: boolean;
  outlook: Outlook | null;
  reasons: string[];
  context: string[];
  /** Prior-season advanced profile. Context only; never part of `score`. */
  advanced: string[];
  advanced_summary: string | null;
}

export interface WaiverResponse {
  league_id: number;
  season: string;
  week: number;
  candidates_considered: number;
  targets: WaiverTarget[];
}

export interface Adjustment {
  label: string;
  delta: number;
}

export interface Starter {
  slot: string;
  canonical_id: number;
  name: string;
  position: string;
  projected: number;
  adjustments: Adjustment[];
  alternative: string | null;
  margin: number | null;
  is_toss_up: boolean;
  outlook: Outlook | null;
  reasons: string[];
  advanced?: string[];
  advanced_summary?: string | null;
  baseline_source?: string;
}

export interface BenchPlayer {
  canonical_id: number;
  name: string;
  position: string;
  projected: number;
  outlook: Outlook | null;
}

export interface LineupResponse {
  league_id: number;
  season: string;
  week: number;
  projected_total: number;
  starters: Starter[];
  bench: BenchPlayer[];
  unfilled: string[];
  unsupported: string[];
  toss_ups: number;
}

/**
 * Talks to the local API. `reachable` is shared by the shell so the header can
 * show connection state without every page tracking it separately.
 */
@Injectable({ providedIn: 'root' })
export class Api {
  private readonly http = inject(HttpClient);

  readonly reachable = signal(true);
  readonly leagues = signal<RegisteredLeague[]>([]);

  private async call<T>(request: Promise<T>): Promise<T | null> {
    try {
      const value = await request;
      this.reachable.set(true);
      return value;
    } catch {
      this.reachable.set(false);
      return null;
    }
  }

  news(query: NewsQuery): Promise<NewsEntry[] | null> {
    const params: Record<string, string> = { hours: query.hours, limit: '60' };
    if (query.leagueId) params['league_id'] = query.leagueId;
    if (query.mineOnly) params['mine_only'] = 'true';
    return this.call(firstValueFrom(this.http.get<NewsEntry[]>('/news', { params })));
  }

  /** Refreshes the shared league list; the news filter reads from it too. */
  async loadLeagues(): Promise<void> {
    const leagues = await this.call(
      firstValueFrom(this.http.get<RegisteredLeague[]>('/leagues')),
    );
    this.leagues.set(leagues ?? []);
  }

  registerLeague(body: {
    platform: string;
    league_id: string;
    name: string | null;
    my_team_id: string | null;
    season: string;
  }): Promise<RegisteredLeague> {
    return firstValueFrom(this.http.post<RegisteredLeague>('/leagues/register', body));
  }

  ingestStatus(): Promise<{ jobs: JobStatus[] } | null> {
    return this.call(firstValueFrom(this.http.get<{ jobs: JobStatus[] }>('/admin/ingest-status')));
  }

  waivers(leagueId: number, week?: number, season?: string): Promise<WaiverResponse | null> {
    const params: Record<string, string> = { limit: '25' };
    if (week) params['week'] = String(week);
    if (season) params['season'] = season;
    return this.call(
      firstValueFrom(this.http.get<WaiverResponse>(`/weekly/${leagueId}/waivers`, { params })),
    );
  }

  /**
   * Unlike the other calls, a failure here is usually a fixable setup problem
   * (no team id, roster not synced), so the message is surfaced rather than
   * flattened into `reachable`.
   */
  async lineup(
    leagueId: number,
    week?: number,
    season?: string,
  ): Promise<LineupResponse | string> {
    const params: Record<string, string> = {};
    if (week) params['week'] = String(week);
    if (season) params['season'] = season;
    try {
      const value = await firstValueFrom(
        this.http.get<LineupResponse>(`/weekly/${leagueId}/lineup`, { params }),
      );
      this.reachable.set(true);
      return value;
    } catch (error: unknown) {
      const detail = (error as { error?: { detail?: string }; status?: number });
      if (detail.status && detail.status > 0) {
        this.reachable.set(true);
        return detail.error?.detail ?? 'Could not build a lineup.';
      }
      this.reachable.set(false);
      return 'Could not reach the API.';
    }
  }

  /**
   * Trade proposals. A failure here is usually a setup problem the user can
   * fix (league never synced, no team marked as theirs), so the detail is
   * surfaced instead of collapsing into "unreachable".
   */
  async trades(leagueId: number, mode: 'normal' | 'fleece'): Promise<TradesResponse | string> {
    try {
      const value = await firstValueFrom(
        this.http.get<TradesResponse>(`/leagues/${leagueId}/trades`, {
          params: { mode, limit: '12' },
        }),
      );
      this.reachable.set(true);
      return value;
    } catch (error: unknown) {
      const detail = error as { error?: { detail?: string }; status?: number };
      if (detail.status && detail.status > 0) {
        this.reachable.set(true);
        return detail.error?.detail ?? 'Could not build trades.';
      }
      this.reachable.set(false);
      return 'Could not reach the API.';
    }
  }

  /** Every team's roster, opponents' players scored against my lineup. */
  async rosters(leagueId: number): Promise<RostersResponse | string> {
    try {
      const value = await firstValueFrom(
        this.http.get<RostersResponse>(`/leagues/${leagueId}/trades/rosters`),
      );
      this.reachable.set(true);
      return value;
    } catch (error: unknown) {
      const detail = error as { error?: { detail?: string }; status?: number };
      if (detail.status && detail.status > 0) {
        this.reachable.set(true);
        return detail.error?.detail ?? 'Could not load rosters.';
      }
      this.reachable.set(false);
      return 'Could not reach the API.';
    }
  }

  /** Offers built around one specific player. */
  async buildAround(
    leagueId: number,
    canonicalId: string,
    mode: 'normal' | 'fleece',
  ): Promise<BuildAroundResponse | string> {
    try {
      const value = await firstValueFrom(
        this.http.get<BuildAroundResponse>(
          `/leagues/${leagueId}/trades/around/${canonicalId}`,
          { params: { mode, limit: '6' } },
        ),
      );
      this.reachable.set(true);
      return value;
    } catch (error: unknown) {
      const detail = error as { error?: { detail?: string }; status?: number };
      if (detail.status && detail.status > 0) {
        this.reachable.set(true);
        return detail.error?.detail ?? 'Could not build a trade around that player.';
      }
      this.reachable.set(false);
      return 'Could not reach the API.';
    }
  }

  /** Value a hand-built trade. */
  async evaluateTrade(
    leagueId: number,
    give: string[],
    receive: string[],
  ): Promise<TradeEvaluation | string> {
    try {
      const value = await firstValueFrom(
        this.http.post<TradeEvaluation>(`/leagues/${leagueId}/trades/evaluate`, {
          give,
          receive,
        }),
      );
      this.reachable.set(true);
      return value;
    } catch (error: unknown) {
      const detail = error as { error?: { detail?: string }; status?: number };
      if (detail.status && detail.status > 0) {
        this.reachable.set(true);
        return detail.error?.detail ?? 'Could not value that trade.';
      }
      this.reachable.set(false);
      return 'Could not reach the API.';
    }
  }
}
