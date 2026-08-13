import { Component, OnInit, inject, signal } from '@angular/core';
import { ActivatedRoute } from '@angular/router';

import { MetersComponent } from '../components/meters';
import { Api, type WaiverTarget } from '../services/api';

@Component({
  selector: 'app-waivers',
  imports: [MetersComponent],
  template: `
    <div class="row filters">
      <select [value]="leagueId()" (change)="pick($any($event.target).value)" aria-label="League">
        @for (league of api.leagues(); track league.id) {
          <option [value]="league.id">{{ league.name || league.platform }}</option>
        }
      </select>
      @if (week()) {
        <span class="pill">{{ season() ? season() + ' ' : '' }}Week {{ week() }}</span>
      }
      @if (considered()) {
        <span class="muted">{{ considered() }} free agents considered</span>
      }
    </div>

    @if (!api.reachable()) {
      <div class="empty">Could not reach the API.</div>
    } @else if (loading()) {
      <div class="empty">Loading…</div>
    } @else if (targets().length === 0) {
      <div class="empty">
        No waiver targets yet. Usage data starts once games are played.
      </div>
    } @else {
      <div class="list">
        @for (target of targets(); track target.canonical_id; let i = $index) {
          <div class="card">
            <div class="card-title">
              <span class="rank">{{ i + 1 }}</span>
              {{ target.name }}
              <span class="pos">{{ target.position }}</span>
              @if (target.team) {
                <span class="muted">{{ target.team }}</span>
              }
              <span class="score">{{ target.score.toFixed(1) }}</span>
            </div>
            <div class="card-sub">
              @for (reason of target.reasons; track reason; let first = $first) {
                @if (!first) {
                  <span class="sep">·</span>
                }
                <span>{{ reason }}</span>
              }
            </div>
            @if (target.outlook; as o) {
              <app-meters [outlook]="o" [showDrivers]="false" />
            }
            @if (target.context.length) {
              <div class="context">
                @for (note of target.context; track note) {
                  <div class="note">{{ note }}</div>
                }
              </div>
            }
            @if (target.advanced?.length) {
              <details class="advanced">
                <summary>
                  {{ target.advanced_summary || 'Advanced stats' }}
                </summary>
                @for (line of target.advanced; track line) {
                  <div class="note">{{ line }}</div>
                }
                <div class="note muted">
                  Prior season, shown for context. Not part of the score.
                </div>
              </details>
            }
          </div>
        }
      </div>
      <p class="muted" style="margin-top:14px">
        Ranked by points per game with the role still intact. Role changes and
        regression risk are shown as context because both scored worse than the
        plain rate when tested against last season. Advanced stats are shown the
        same way: they did not predict next season beyond a player's own scoring
        when tested out of sample, so they inform you rather than the ranking.
      </p>
    }
  `,
})
export class WaiversPage implements OnInit {
  protected readonly api = inject(Api);
  private readonly route = inject(ActivatedRoute);

  protected readonly targets = signal<WaiverTarget[]>([]);
  protected readonly leagueId = signal('');
  protected readonly week = signal<number | null>(null);
  protected readonly season = signal('');
  protected readonly considered = signal(0);
  protected readonly loading = signal(false);

  /**
   * `?season=2025&week=9` reviews a past week. No visible control: it is for
   * checking the model against a season that already happened, not a routine
   * part of setting a lineup.
   */
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
    const response = await this.api.waivers(
      Number(id),
      Number(this.query('week')) || undefined,
      this.query('season') || undefined,
    );
    this.season.set(this.query('season'));
    this.targets.set(response?.targets ?? []);
    this.week.set(response?.week ?? null);
    this.considered.set(response?.candidates_considered ?? 0);
    this.loading.set(false);
  }
}
