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
}
