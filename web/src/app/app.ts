import { Component, inject } from '@angular/core';
import { RouterLink, RouterLinkActive, RouterOutlet } from '@angular/router';

import { Api } from './services/api';
import { ThemeService } from './services/theme';

@Component({
  selector: 'app-root',
  imports: [RouterOutlet, RouterLink, RouterLinkActive],
  template: `
    <header>
      <h1 class="brand">Fantasy</h1>
      <nav aria-label="Sections">
        <a class="tab" routerLink="/news" routerLinkActive="active" ariaCurrentWhenActive="page">News</a>
        <a class="tab" routerLink="/lineup" routerLinkActive="active" ariaCurrentWhenActive="page">Start/Sit</a>
        <a class="tab" routerLink="/waivers" routerLinkActive="active" ariaCurrentWhenActive="page">Waivers</a>
        <a class="tab" routerLink="/leagues" routerLinkActive="active" ariaCurrentWhenActive="page">Leagues</a>
        <a class="tab" routerLink="/data" routerLinkActive="active" ariaCurrentWhenActive="page">Data</a>
      </nav>
      <div class="header-right">
        <span class="pill" [class.bad]="!api.reachable()">
          {{ api.reachable() ? 'connected' : 'API unreachable' }}
        </span>
        <button class="icon-button" type="button" (click)="theme.toggle()"
                title="Toggle light and dark" aria-label="Toggle light and dark">
          <svg class="icon-sun" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true">
            <circle cx="12" cy="12" r="4.2" />
            <path d="M12 2.5v2M12 19.5v2M2.5 12h2M19.5 12h2M5.2 5.2l1.4 1.4M17.4 17.4l1.4 1.4M18.8 5.2l-1.4 1.4M6.6 17.4l-1.4 1.4" />
          </svg>
          <svg class="icon-moon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <path d="M20 14.2A8.2 8.2 0 1 1 9.8 4a6.6 6.6 0 0 0 10.2 10.2z" />
          </svg>
        </button>
      </div>
    </header>

    <main>
      <div class="view"><router-outlet /></div>
    </main>
  `,
})
export class App {
  protected readonly api = inject(Api);
  protected readonly theme = inject(ThemeService);

  constructor() {
    this.theme.init();
  }
}
