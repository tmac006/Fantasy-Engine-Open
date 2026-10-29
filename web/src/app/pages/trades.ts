import { Component, OnInit, inject, signal } from '@angular/core';

import {
  Api,
  type BuildAroundResponse,
  type LeagueRosterView,
  type PlayerCard,
  type RostersResponse,
  type TradeEvaluation,
  type TradePackage,
  type TradesResponse,
} from '../services/api';

@Component({
  selector: 'app-trades',
  template: `
    <div class="row filters">
      <select [value]="leagueId()" (change)="pick($any($event.target).value)" aria-label="League">
        @for (league of api.leagues(); track league.id) {
          <option [value]="league.id">{{ league.name || league.platform }}</option>
        }
      </select>

      <div class="modes" role="group" aria-label="Trade mode">
        <button type="button" class="mode" [class.on]="mode() === 'normal'"
                (click)="setMode('normal')">Normal</button>
        <button type="button" class="mode fleece" [class.on]="mode() === 'fleece'"
                (click)="setMode('fleece')">Fleece</button>
      </div>

      @if (data(); as d) {
        <span class="pill">{{ d.my_team_name }}</span>
      }
    </div>

    <p class="mode-note">
      @if (mode() === 'normal') {
        Offers both managers gain from — your surplus for their surplus. These are the ones
        that actually get accepted.
      } @else {
        Offers the draft market prices as fair, or that plug a hole they urgently need
        filled, while the projections favour you.
      }
    </p>

    @if (error(); as e) {
      <div class="empty">{{ e }}</div>
    } @else if (loading()) {
      <div class="empty">Working out who wants what…</div>
    } @else if (data(); as d) {
      @if (d.my_needs.length) {
        <div class="needs">
          <span class="needs-label">Your needs</span>
          @for (need of d.my_needs; track need.position) {
            @if (need.upgrade_value > 0.5) {
              <span class="need">
                {{ need.position }}
                <b>+{{ need.upgrade_value.toFixed(0) }}</b>
              </span>
            }
          }
        </div>
      }

      @if (d.injury_signals.length) {
        <details class="signals">
          <summary>{{ d.injury_signals.length }} roster signals from the news feed</summary>
          @for (signal of d.injury_signals; track signal) {
            <div class="note">{{ signal }}</div>
          }
        </details>
      }

      @if (!d.packages.length) {
        <div class="empty">
          No {{ mode() }} trades worth proposing right now.
          @if (mode() === 'normal') {
            Straight after a draft every roster is balanced, so two-sided gains are rare —
            try Fleece.
          }
        </div>
      }


    <!-- Build your own -------------------------------------------------- -->
    <details class="builder" [open]="builderOpen()" (toggle)="onToggle($event)">
      <summary>Build a trade</summary>

      @if (rosterError(); as re) {
        <div class="empty">{{ re }}</div>
      } @else if (rosters(); as rs) {
        <div class="builder-grid">
          <section>
            <h4>You send</h4>
            @for (p of myPlayers(); track p.canonical_id) {
              <div class="pick-row">
                <button type="button" class="pick" [class.on]="isGiving(p)"
                        (click)="toggleGive(p)">
                  <img class="shot sm" [src]="p.headshot_url || placeholder" [alt]="p.name"
                       loading="lazy" (error)="fallback($event)" />
                  <span class="nm">{{ p.name }}</span>
                  <span class="meta">{{ p.position }} · {{ p.points.toFixed(0) }}
                    @if (p.bye_week) { · bye {{ p.bye_week }} }
                  </span>
                </button>
                <button type="button" class="around" title="Build a trade around him"
                        [class.on]="anchorId() === p.canonical_id"
                        (click)="buildAround(p)">⚡</button>
              </div>
            }
          </section>

          <section>
            <div class="them-head">
              <h4>You get</h4>
              <select [value]="theirTeam()" (change)="setTheirTeam($any($event.target).value)"
                      aria-label="Trade partner">
                @for (r of opponents(); track r.team_id) {
                  <option [value]="r.team_id">{{ r.team_name }}</option>
                }
              </select>
            </div>
            @for (p of theirPlayers(); track p.canonical_id) {
              <div class="pick-row">
                <button type="button" class="pick" [class.on]="isGetting(p)"
                        (click)="toggleGet(p)">
                <img class="shot sm" [src]="p.headshot_url || placeholder" [alt]="p.name"
                     loading="lazy" (error)="fallback($event)" />
                <span class="nm">
                  {{ p.name }}
                  <!-- Recommended means he would actually walk into your lineup,
                       not that he has the biggest projection. -->
                  @if (p.recommended) {
                    <span class="rec" title="Would improve your starting lineup">
                      +{{ (p.solo_gain || 0).toFixed(0) }}
                    </span>
                  }
                </span>
                  <span class="meta">{{ p.position }} · {{ p.points.toFixed(0) }}
                    @if (p.bye_week) { · bye {{ p.bye_week }} }
                  </span>
                </button>
                <button type="button" class="around" title="Build a trade around him"
                        [class.on]="anchorId() === p.canonical_id"
                        (click)="buildAround(p)">⚡</button>
              </div>
            }
          </section>
        </div>

        @if (aroundLoading()) {
          <div class="empty">Building trades around {{ anchorName() }}…</div>
        } @else if (around(); as ar) {
          <div class="around-box">
            <div class="around-head">
              Trades built around <b>{{ ar.anchor.name }}</b>
              <span class="muted">{{ ar.anchor_is_mine ? 'you send him' : 'you get him' }}</span>
              <button type="button" class="mode" (click)="clearAround()">Dismiss</button>
            </div>
            @if (!ar.packages.length) {
              <!-- Say WHY, and what to try next. "No packages" alone reads as a
                   broken feature rather than a real answer about this player. -->
              <p class="muted">
                @if (mode() === 'normal') {
                  Nothing here improves both rosters — normal mode needs the other
                  manager to gain too. Try <b>Fleece</b>, which only asks that the
                  offer look fair to them.
                } @else {
                  No offer for him is even plausible: everything that helps your
                  lineup costs them far too much to accept.
                }
              </p>
            }
            @for (pkg of ar.packages; track $index) {
              <button type="button" class="suggestion" (click)="applyPackage(pkg)">
                <span class="sug-team">{{ pkg.their_team_name }}</span>
                <span class="sug-swap">
                  {{ names(pkg.give) }} <span class="arrow-sm">⇄</span> {{ names(pkg.receive) }}
                </span>
                <span class="gain" [class.good]="pkg.my_gain > 0">{{ signed(pkg.my_gain) }}</span>
                <span class="gain them" [class.good]="pkg.their_gain > 0">{{ signed(pkg.their_gain) }}</span>
              </button>
            }
          </div>
        }

        <div class="builder-actions">
          <button type="button" class="mode" (click)="clearBuild()">Clear</button>
          <span class="muted">{{ give().length }} out · {{ get().length }} in</span>
        </div>

        @if (evaluating()) {
          <div class="empty">Valuing…</div>
        } @else if (evalError(); as ee) {
          <div class="empty">{{ ee }}</div>
        } @else if (evaluation(); as ev) {
          <div class="verdict-box" [class.good]="ev.my_gain > 2" [class.bad]="ev.my_gain < -2">
            <div class="verdict-line">
              <b>{{ ev.verdict }}</b>
              <span class="gain" [class.good]="ev.my_gain > 0">you {{ signed(ev.my_gain) }}</span>
              <span class="gain them" [class.good]="ev.their_gain > 0">them {{ signed(ev.their_gain) }}</span>
              <span class="muted">bye-adjusted {{ signed(ev.bye_adjusted_gain_per_week) }}/wk</span>
            </div>
            @if (ev.worst_week_before; as w) {
              <div class="note">
                Worst week ({{ w.week }}): {{ w.before.toFixed(1) }} → {{ w.after.toFixed(1) }},
                {{ w.slots_before }}/{{ ev.roster_limit >= 16 ? 9 : 9 }} slots →
                {{ w.slots_after }}
              </div>
            }
            @for (n of ev.notes; track n) {
              <div class="note">{{ n }}</div>
            }
          </div>
        } @else {
          <p class="muted">Pick at least one player on each side.</p>
        }
      } @else {
        <div class="empty">Loading rosters…</div>
      }
    </details>

      <div class="list">
        @for (pkg of d.packages; track $index) {
          <article class="card trade">
            <div class="trade-head">
              <span class="vs">to</span>
              <span class="team">{{ pkg.their_team_name }}</span>
              <span class="spacer"></span>
              <span class="gain" [class.good]="pkg.my_gain > 0">you {{ signed(pkg.my_gain) }}</span>
              <span class="gain them" [class.good]="pkg.their_gain > 0">them {{ signed(pkg.their_gain) }}</span>
            </div>

            <div class="swap">
              <div class="side">
                <div class="side-label out">You send</div>
                @for (p of pkg.give; track p.canonical_id) {
                  <div class="player">
                    <img class="shot" [src]="p.headshot_url || placeholder" [alt]="p.name"
                         loading="lazy" (error)="fallback($event)" />
                    <div class="who">
                      <div class="nm">{{ p.name }}</div>
                      <div class="meta">
                        {{ p.position }}{{ p.team ? ' · ' + p.team : '' }} ·
                        {{ p.points.toFixed(0) }} pts
                        @if (p.bye_week) { · bye {{ p.bye_week }} }
                      </div>
                    </div>
                  </div>
                }
              </div>

              <div class="arrow" aria-hidden="true">⇄</div>

              <div class="side">
                <div class="side-label in">You get</div>
                @for (p of pkg.receive; track p.canonical_id) {
                  <div class="player">
                    <img class="shot" [src]="p.headshot_url || placeholder" [alt]="p.name"
                         loading="lazy" (error)="fallback($event)" />
                    <div class="who">
                      <div class="nm">{{ p.name }}</div>
                      <div class="meta">
                        {{ p.position }}{{ p.team ? ' · ' + p.team : '' }} ·
                        {{ p.points.toFixed(0) }} pts
                        @if (p.bye_week) { · bye {{ p.bye_week }} }
                      </div>
                      @if (p.news) {
                        <div class="newsline">{{ p.news }}</div>
                      }
                    </div>
                  </div>
                }
              </div>
            </div>

            @if (mode() === 'fleece' && pkg.market_edge > 0) {
              <div class="edge">
                Reads as generous: the draft market prices what you send
                <b>{{ pkg.market_edge.toFixed(0) }}</b> points above what you get back.
              </div>
            }

            <div class="why">
              @for (line of pkg.rationale; track line) {
                <div class="note">{{ line }}</div>
              }
            </div>
          </article>
        }
      </div>
    }
  `,
  styles: [`
    .modes { display: inline-flex; border: 1px solid var(--line); border-radius: 999px; overflow: hidden; }
    .mode { background: transparent; border: 0; padding: 0.35rem 0.9rem; cursor: pointer;
            color: var(--muted); font: inherit; }
    .mode.on { background: var(--accent); color: var(--on-accent, #fff); }
    .mode.fleece.on { background: #b4530a; }
    .mode-note { color: var(--muted); margin: 0.4rem 0 0.9rem; max-width: 62ch; }
    .needs { display: flex; gap: 0.5rem; align-items: center; flex-wrap: wrap; margin-bottom: 0.8rem; }
    .needs-label { color: var(--muted); font-size: 0.85rem; }
    .need { border: 1px solid var(--line); border-radius: 999px; padding: 0.15rem 0.6rem; font-size: 0.85rem; }
    .signals { margin-bottom: 0.9rem; }
    .trade { display: flex; flex-direction: column; gap: 0.7rem; }
    .trade-head { display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap; }
    .vs { color: var(--muted); font-size: 0.85rem; }
    .team { font-weight: 600; }
    .spacer { flex: 1; }
    .gain { font-variant-numeric: tabular-nums; font-size: 0.9rem; color: var(--muted); }
    .gain.good { color: var(--ok, #1a7f37); }
    .swap { display: grid; grid-template-columns: 1fr auto 1fr; gap: 0.8rem; align-items: start; }
    .side { display: flex; flex-direction: column; gap: 0.5rem; min-width: 0; }
    .side-label { font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.04em; color: var(--muted); }
    .arrow { align-self: center; font-size: 1.2rem; color: var(--muted); }
    .player { display: flex; gap: 0.55rem; align-items: center; min-width: 0; }
    /* ESPN headshots are transparent cut-outs with the player centred and a lot
       of empty space above the head, so a short landscape crop shows nothing but
       background. A square crop biased slightly above centre frames the face. */
    .shot { width: 42px; height: 42px; object-fit: cover; object-position: 50% 22%;
            border-radius: 50%; background: var(--line); flex: none; }
    .who { min-width: 0; }
    .nm { font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .meta { font-size: 0.8rem; color: var(--muted); }
    .newsline { font-size: 0.78rem; color: var(--muted); font-style: italic; margin-top: 0.15rem; }
    .edge { font-size: 0.85rem; padding: 0.4rem 0.6rem; border-radius: 6px;
            background: color-mix(in srgb, #b4530a 12%, transparent); }
    .why { display: flex; flex-direction: column; gap: 0.2rem; }
    /* --- trade builder ------------------------------------------------- */
    .pick-row { display: flex; align-items: center; gap: 2px; }
    .pick-row .pick { flex: 1; min-width: 0; }
    .around { border: none; background: none; cursor: pointer; opacity: 0.25;
              font-size: 13px; padding: 2px 5px; border-radius: 6px; line-height: 1; }
    .pick-row:hover .around { opacity: 0.75; }
    .around:hover, .around.on { opacity: 1; background: rgba(47,125,79,0.16); }
    .around-box { margin-top: 10px; padding: 9px 11px; border-radius: 8px;
                  border: 1px solid var(--line); background: var(--bg); }
    .around-head { display: flex; align-items: center; gap: 8px; margin-bottom: 6px;
                   font-size: 13px; }
    .around-head .mode { margin-left: auto; }
    .suggestion { display: flex; align-items: center; gap: 8px; width: 100%; text-align: left;
                  padding: 5px 6px; margin-bottom: 3px; border: 1px solid var(--line);
                  border-radius: 7px; background: none; cursor: pointer; color: inherit; }
    .suggestion:hover { background: rgba(47,125,79,0.08); border-color: #2f7d4f; }
    .sug-team { font-size: 11.5px; color: var(--muted); width: 108px; flex: none;
                overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .sug-swap { flex: 1; min-width: 0; font-size: 12.5px; }
    .arrow-sm { color: var(--muted); margin: 0 3px; }

    .builder { margin: 14px 0 18px; border: 1px solid var(--line); border-radius: 10px;
               padding: 10px 12px; background: var(--card); }
    .builder > summary { cursor: pointer; font-weight: 650; padding: 2px 0; }
    .builder-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; margin-top: 10px; }
    .builder-grid h4 { margin: 0 0 6px; font-size: 13px; color: var(--muted);
                       text-transform: uppercase; letter-spacing: 0.04em; }
    .them-head { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
    .them-head h4 { margin: 0; }
    .them-head select { flex: 1; min-width: 0; }
    .pick { display: flex; align-items: center; gap: 8px; width: 100%; text-align: left;
            padding: 4px 6px; margin-bottom: 3px; border: 1px solid transparent;
            border-radius: 7px; background: none; cursor: pointer; color: inherit; }
    .pick:hover { background: var(--hover, rgba(127,127,127,0.08)); }
    /* Selected has to read at a glance -- this list is long and the whole
       interaction is scanning it repeatedly. */
    .pick.on { border-color: var(--accent, #2f7d4f); background: rgba(47,125,79,0.10); }
    .pick .nm { font-size: 13.5px; flex: 1; min-width: 0; }
    .pick .meta { font-size: 11.5px; color: var(--muted); white-space: nowrap; }
    .shot.sm { width: 36px; height: 26px; object-fit: cover; border-radius: 3px; flex: none; }
    .rec { font-size: 10.5px; font-weight: 700; color: #2f7d4f;
           background: rgba(47,125,79,0.14); border-radius: 4px; padding: 0 4px; margin-left: 4px; }
    .builder-actions { display: flex; align-items: center; gap: 10px; margin: 8px 0 4px; }
    .verdict-box { margin-top: 8px; padding: 9px 11px; border-radius: 8px;
                   border: 1px solid var(--line); background: var(--bg); }
    .verdict-box.good { border-color: #2f7d4f; }
    .verdict-box.bad { border-color: var(--warn, #b4643a); }
    .verdict-line { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap;
                    margin-bottom: 4px; }
    @media (max-width: 620px) {
      .builder-grid { grid-template-columns: 1fr; }
    }
    @media (max-width: 620px) {
      .swap { grid-template-columns: 1fr; }
      .arrow { transform: rotate(90deg); }
    }
  `],
})
export class TradesPage implements OnInit {
  protected readonly api = inject(Api);
  protected readonly leagueId = signal<number | null>(null);
  protected readonly mode = signal<'normal' | 'fleece'>('normal');
  protected readonly data = signal<TradesResponse | null>(null);
  protected readonly error = signal<string | null>(null);
  protected readonly loading = signal(true);

  // A transparent 1x1 keeps the layout from jumping when ESPN has no portrait
  // for a player (rookies and practice-squad call-ups routinely have none).
  protected readonly placeholder =
    'data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" width="44" height="32"/>';

  async ngOnInit(): Promise<void> {
    await this.api.loadLeagues();
    const first = this.api.leagues()[0];
    if (first) this.leagueId.set(first.id);
    await this.load();
  }

  protected signed(value: number): string {
    return `${value >= 0 ? '+' : ''}${value.toFixed(1)}`;
  }

  protected fallback(event: Event): void {
    (event.target as HTMLImageElement).src = this.placeholder;
  }


  // -- trade builder --------------------------------------------------------
  protected readonly builderOpen = signal(false);
  protected readonly rosters = signal<RostersResponse | null>(null);
  protected readonly rosterError = signal<string | null>(null);
  protected readonly theirTeam = signal<string>('');
  protected readonly give = signal<PlayerCard[]>([]);
  protected readonly get = signal<PlayerCard[]>([]);
  protected readonly evaluation = signal<TradeEvaluation | null>(null);
  protected readonly evalError = signal<string | null>(null);
  protected readonly evaluating = signal(false);

  protected myPlayers(): PlayerCard[] {
    return this.rosters()?.rosters.find((r) => r.is_mine)?.players ?? [];
  }

  protected opponents(): LeagueRosterView[] {
    return this.rosters()?.rosters.filter((r) => !r.is_mine) ?? [];
  }

  protected theirPlayers(): PlayerCard[] {
    const id = this.theirTeam();
    return this.opponents().find((r) => r.team_id === id)?.players ?? [];
  }

  protected isGiving(p: PlayerCard): boolean {
    return this.give().some((x) => x.canonical_id === p.canonical_id);
  }

  protected isGetting(p: PlayerCard): boolean {
    return this.get().some((x) => x.canonical_id === p.canonical_id);
  }

  protected toggleGive(p: PlayerCard): void {
    this.give.update((cur) =>
      cur.some((x) => x.canonical_id === p.canonical_id)
        ? cur.filter((x) => x.canonical_id !== p.canonical_id)
        : [...cur, p],
    );
    void this.evaluate();
  }

  protected toggleGet(p: PlayerCard): void {
    this.get.update((cur) =>
      cur.some((x) => x.canonical_id === p.canonical_id)
        ? cur.filter((x) => x.canonical_id !== p.canonical_id)
        : [...cur, p],
    );
    void this.evaluate();
  }

  protected clearBuild(): void {
    this.give.set([]);
    this.get.set([]);
    this.evaluation.set(null);
    this.evalError.set(null);
  }

  protected setTheirTeam(id: string): void {
    this.theirTeam.set(id);
    // A trade is with one manager, so switching partners cannot keep the
    // players already picked from the previous one.
    this.get.set([]);
    this.evaluation.set(null);
    void this.evaluate();
  }

  protected onToggle(event: Event): void {
    const open = (event.target as HTMLDetailsElement).open;
    this.builderOpen.set(open);
    if (open && this.rosters() === null) void this.loadRosters();
  }

  private async loadRosters(): Promise<void> {
    const id = this.leagueId();
    if (id === null) return;
    this.rosterError.set(null);
    const result = await this.api.rosters(id);
    if (typeof result === 'string') {
      this.rosterError.set(result);
      this.rosters.set(null);
      return;
    }
    this.rosters.set(result);
    const first = result.rosters.find((r) => !r.is_mine);
    if (first) this.theirTeam.set(first.team_id);
  }

  private async evaluate(): Promise<void> {
    const id = this.leagueId();
    const give = this.give();
    const get = this.get();
    if (id === null || !give.length || !get.length) {
      this.evaluation.set(null);
      this.evalError.set(null);
      return;
    }
    this.evaluating.set(true);
    this.evalError.set(null);
    const result = await this.api.evaluateTrade(
      id,
      give.map((p) => p.canonical_id),
      get.map((p) => p.canonical_id),
    );
    if (typeof result === 'string') {
      this.evalError.set(result);
      this.evaluation.set(null);
    } else {
      this.evaluation.set(result);
    }
    this.evaluating.set(false);
  }


  // -- build around one player ---------------------------------------------
  protected readonly around = signal<BuildAroundResponse | null>(null);
  protected readonly aroundLoading = signal(false);
  protected readonly anchorId = signal<string | null>(null);

  protected anchorName(): string {
    return this.around()?.anchor.name ?? 'him';
  }

  protected names(players: PlayerCard[]): string {
    return players.map((p) => p.name).join(' + ');
  }

  protected clearAround(): void {
    this.around.set(null);
    this.anchorId.set(null);
  }

  protected async buildAround(player: PlayerCard): Promise<void> {
    const id = this.leagueId();
    if (id === null) return;
    // Clicking the same player again closes the panel rather than rebuilding.
    if (this.anchorId() === player.canonical_id) {
      this.clearAround();
      return;
    }
    this.anchorId.set(player.canonical_id);
    this.aroundLoading.set(true);
    this.around.set(null);
    const result = await this.api.buildAround(id, player.canonical_id, this.mode());
    this.aroundLoading.set(false);
    if (typeof result === 'string') {
      this.evalError.set(result);
      this.anchorId.set(null);
      return;
    }
    this.around.set(result);
  }

  /** Load a suggested package into the builder so it can be edited. */
  protected applyPackage(pkg: TradePackage): void {
    this.theirTeam.set(pkg.their_team_id);
    this.give.set([...pkg.give]);
    this.get.set([...pkg.receive]);
    void this.evaluate();
  }

  protected async pick(value: string): Promise<void> {
    this.leagueId.set(Number(value));
    this.rosters.set(null);
    this.clearBuild();
    this.clearAround();
    await this.load();
    if (this.builderOpen()) await this.loadRosters();
  }

  protected async setMode(mode: 'normal' | 'fleece'): Promise<void> {
    if (this.mode() === mode) return;
    this.mode.set(mode);
    await this.load();
  }

  private async load(): Promise<void> {
    const id = this.leagueId();
    if (id === null) {
      this.loading.set(false);
      this.error.set('No leagues registered.');
      return;
    }
    this.loading.set(true);
    this.error.set(null);
    const result = await this.api.trades(id, this.mode());
    if (typeof result === 'string') {
      this.error.set(result);
      this.data.set(null);
    } else {
      this.data.set(result);
    }
    this.loading.set(false);
  }
}
