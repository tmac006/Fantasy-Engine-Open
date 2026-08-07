import { Component, OnInit, inject, signal } from '@angular/core';

import { Api, type RegisteredLeague } from '../services/api';

@Component({
  selector: 'app-leagues',
  template: `
    @if (api.leagues().length === 0) {
      <div class="empty">No leagues registered yet.</div>
    } @else {
      <div class="list">
        @for (league of api.leagues(); track league.id) {
          <div class="card">
            <div class="card-title">{{ league.name || league.platform + ' ' + league.league_id }}</div>
            <div class="card-sub">
              @for (piece of details(league); track piece; let first = $first) {
                @if (!first) {
                  <span class="sep">·</span>
                }
                <span>{{ piece }}</span>
              }
            </div>
            @if (league.sync_error) {
              <div class="warn">Sync failed: {{ league.sync_error }}</div>
            }
          </div>
        }
      </div>
    }

    <form class="card register" (submit)="add($event)">
      <div class="card-title">Add a league</div>
      <div class="row">
        <select [value]="platform()" (change)="platform.set($any($event.target).value)" aria-label="Platform">
          <option value="sleeper">Sleeper</option>
          <option value="espn">ESPN</option>
        </select>
        <input type="text" placeholder="League ID" [value]="leagueId()"
               (input)="leagueId.set($any($event.target).value)" />
        <input type="text" placeholder="Name (optional)" [value]="name()"
               (input)="name.set($any($event.target).value)" />
        <input type="text" placeholder="My team ID" [value]="teamId()"
               (input)="teamId.set($any($event.target).value)" />
        <button type="submit" class="btn primary">Add</button>
      </div>
      @if (message()) {
        <div class="muted">{{ message() }}</div>
      }
    </form>
  `,
})
export class LeaguesPage implements OnInit {
  protected readonly api = inject(Api);

  protected readonly platform = signal('sleeper');
  protected readonly leagueId = signal('');
  protected readonly name = signal('');
  protected readonly teamId = signal('');
  protected readonly message = signal('');

  ngOnInit(): void {
    void this.api.loadLeagues();
  }

  protected details(league: RegisteredLeague): string[] {
    const parts = [
      league.platform === 'espn' ? 'ESPN' : 'Sleeper',
      `Season ${league.season}`,
      league.my_team_id ? `Team ${league.my_team_id}` : 'My team not set',
      `${league.rostered_players} rostered`,
    ];
    if (league.unmapped_players) parts.push(`${league.unmapped_players} unmapped`);
    return parts;
  }

  protected async add(event: Event): Promise<void> {
    event.preventDefault();
    this.message.set('Registering…');
    try {
      const league = await this.api.registerLeague({
        platform: this.platform(),
        league_id: this.leagueId().trim(),
        // Blank lets the platform's own league name win during sync.
        name: this.name().trim() || null,
        my_team_id: this.teamId().trim() || null,
        season: '2026',
      });
      this.message.set(
        league.sync_error
          ? `Registered, but sync failed: ${league.sync_error}`
          : `Registered ${league.name || league.league_id}`,
      );
      this.leagueId.set('');
      this.name.set('');
      this.teamId.set('');
      await this.api.loadLeagues();
    } catch (error) {
      this.message.set(`Failed: ${String(error)}`);
    }
  }
}
