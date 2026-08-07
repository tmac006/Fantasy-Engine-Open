import { Component, OnInit, inject, signal } from '@angular/core';
import { ActivatedRoute } from '@angular/router';

import { MetersComponent } from '../components/meters';
import { Api, type LineupResponse } from '../services/api';

@Component({
  selector: 'app-lineup',
  imports: [MetersComponent],
  template: `
    <div class="row filters">
      <select [value]="leagueId()" (change)="pick($any($event.target).value)" aria-label="League">
        @for (league of api.leagues(); track league.id) {
          <option [value]="league.id">{{ league.name || league.platform }}</option>
        }
      </select>
      @if (lineup(); as data) {
        <span class="pill">Week {{ data.week }}</span>
        <span class="pill">{{ data.projected_total.toFixed(1) }} projected</span>
        @if (data.toss_ups) {
          <span class="pill warn-pill">{{ data.toss_ups }} close call{{ data.toss_ups > 1 ? 's' : '' }}</span>
        }
      }
    </div>

    @if (loading()) {
      <div class="empty">Loading…</div>
    } @else if (error()) {
      <div class="empty">{{ error() }}</div>
    } @else if (lineup(); as data) {
      <div class="list">
        @for (starter of data.starters; track starter.canonical_id) {
          <div class="card" [class.toss-up]="starter.is_toss_up">
            <div class="card-title">
              <span class="slot">{{ starter.slot }}</span>
              {{ starter.name }}
              <!-- Only worth showing when the slot does not already say it. -->
              @if (starter.slot !== starter.position) {
                <span class="pos">{{ starter.position }}</span>
              }
              <span class="score">{{ starter.projected.toFixed(1) }}</span>
            </div>
            <div class="card-sub">
              @for (reason of starter.reasons; track reason; let first = $first) {
                @if (!first) {
                  <span class="sep">·</span>
                }
                <span>{{ reason }}</span>
              }
            </div>
            @if (starter.outlook; as o) {
              <app-meters [outlook]="o" [showDrivers]="starter.is_toss_up" />
            }
          </div>
        }
      </div>

      @if (data.unfilled.length) {
        <div class="warn" style="margin-top:12px">
          No eligible player for: {{ data.unfilled.join(', ') }}
        </div>
      }
      @if (data.unsupported.length) {
        <p class="muted" style="margin-top:10px">
          {{ data.unsupported.join(', ') }} not ranked: the weekly data has no
          kicking or team-defense stats, so set those slots by matchup.
        </p>
      }

      @if (data.bench.length) {
        <h2 class="section">Bench</h2>
        <div class="list">
          @for (player of data.bench; track player.canonical_id) {
            <div class="card bench-row">
              <span>{{ player.name }}</span>
              <span class="pos">{{ player.position }}</span>
              <span class="score muted">{{ player.projected.toFixed(1) }}</span>
            </div>
          }
        </div>
      }
    }
  `,
})
export class LineupPage implements OnInit {
  protected readonly api = inject(Api);
  private readonly route = inject(ActivatedRoute);

  protected readonly lineup = signal<LineupResponse | null>(null);
  protected readonly error = signal('');
  protected readonly leagueId = signal('');
  protected readonly loading = signal(false);

  /** `?season=2025&week=9` replays a past week; see the waivers page. */
  private query(name: string): string {
    return this.route.snapshot.queryParamMap.get(name) ?? '';
  }

  async ngOnInit(): Promise<void> {
    if (this.api.leagues().length === 0) await this.api.loadLeagues();
    const first = this.api.leagues()[0];
    if (first) await this.pick(String(first.id));
  }

  protected async pick(id: string): Promise<void> {
    this.leagueId.set(id);
    this.loading.set(true);
    this.error.set('');
    const response = await this.api.lineup(
      Number(id),
      Number(this.query('week')) || undefined,
      this.query('season') || undefined,
    );
    if (typeof response === 'string') {
      this.lineup.set(null);
      this.error.set(response);
    } else {
      this.lineup.set(response);
    }
    this.loading.set(false);
  }
}
