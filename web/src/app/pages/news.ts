import { Component, OnDestroy, OnInit, inject, signal } from '@angular/core';

import { Api, type NewsEntry } from '../services/api';

const TAGS = ['injury', 'depth_chart', 'transaction', 'camp', 'general'] as const;

@Component({
  selector: 'app-news',
  template: `
    <div class="controls">
      <label class="field">
        <span>League</span>
        <select [value]="leagueId()" (change)="setLeague($event)">
          <option value="">All leagues</option>
          @for (league of api.leagues(); track league.id) {
            <option [value]="league.id">{{ league.name || league.league_id }}</option>
          }
        </select>
      </label>

      <label class="field">
        <span>Window</span>
        <select [value]="hours()" (change)="setHours($event)">
          <option value="24">Last 24 hours</option>
          <option value="48">Last 48 hours</option>
          <option value="168">Last 7 days</option>
        </select>
      </label>

      <label class="check">
        <input type="checkbox" [checked]="mineOnly()" (change)="toggleMine($event)" />
        My players only
      </label>

      <div class="tagbar">
        @for (tag of tags; track tag) {
          <button type="button" [class.on]="active().has(tag)" (click)="toggleTag(tag)">
            {{ tag.replace('_', ' ') }}
          </button>
        }
      </div>
    </div>

    @if (!api.reachable()) {
      <div class="empty">Could not reach the API. Is it running on port 8000?</div>
    } @else if (visible().length === 0) {
      <div class="empty">No matching news. Ingest runs every 30 minutes.</div>
    } @else {
      <div class="list">
        @for (item of visible(); track item.id) {
          <div class="card news-item" [class.mine]="item.on_my_roster">
            <span class="badge {{ item.tag }}">{{ item.tag.replace('_', ' ') }}</span>
            <div class="news-main">
              @if (item.url) {
                <a class="news-title" [href]="item.url" target="_blank" rel="noreferrer">{{ item.title }}</a>
              } @else {
                <span class="news-title">{{ item.title }}</span>
              }
              <div class="news-meta">
                @if (item.player) {
                  <span class="who">{{ item.player }}</span>
                }
                @for (piece of meta(item); track piece) {
                  <span class="sep">·</span><span>{{ piece }}</span>
                }
              </div>
            </div>
          </div>
        }
      </div>
    }
  `,
})
export class NewsPage implements OnInit, OnDestroy {
  protected readonly api = inject(Api);
  protected readonly tags = TAGS;

  protected readonly leagueId = signal('');
  protected readonly hours = signal('48');
  protected readonly mineOnly = signal(false);
  protected readonly active = signal(new Set<string>());
  protected readonly visible = signal<NewsEntry[]>([]);

  private timer?: ReturnType<typeof setInterval>;

  async ngOnInit(): Promise<void> {
    await this.api.loadLeagues();
    await this.load();
    // Keep the feed current without a manual refresh; ingest runs every 30 min.
    this.timer = setInterval(() => void this.load(), 120_000);
  }

  ngOnDestroy(): void {
    clearInterval(this.timer);
  }

  protected meta(item: NewsEntry): string[] {
    return [
      [item.position, item.team].filter(Boolean).join(' · '),
      item.source,
      timeAgo(item.published_at),
    ].filter((piece): piece is string => Boolean(piece));
  }

  protected setLeague(event: Event): void {
    this.leagueId.set((event.target as HTMLSelectElement).value);
    void this.load();
  }

  protected setHours(event: Event): void {
    this.hours.set((event.target as HTMLSelectElement).value);
    void this.load();
  }

  protected toggleMine(event: Event): void {
    this.mineOnly.set((event.target as HTMLInputElement).checked);
    void this.load();
  }

  protected toggleTag(tag: string): void {
    const next = new Set(this.active());
    if (!next.delete(tag)) next.add(tag);
    this.active.set(next);
    void this.load();
  }

  private async load(): Promise<void> {
    const items = await this.api.news({
      leagueId: this.leagueId(),
      hours: this.hours(),
      mineOnly: this.mineOnly(),
    });
    const selected = this.active();
    this.visible.set(
      selected.size ? (items ?? []).filter((item) => selected.has(item.tag)) : (items ?? []),
    );
  }
}

function timeAgo(iso: string | null): string {
  if (!iso) return '';
  const minutes = Math.round((Date.now() - new Date(iso).getTime()) / 60_000);
  if (minutes < 1) return 'just now';
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  return hours < 24 ? `${hours}h ago` : `${Math.round(hours / 24)}d ago`;
}
