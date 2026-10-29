import type { Routes } from '@angular/router';

import { LeaguesPage } from './pages/leagues';
import { LineupPage } from './pages/lineup';
import { NewsPage } from './pages/news';
import { StatusPage } from './pages/status';
import { TradesPage } from './pages/trades';
import { WaiversPage } from './pages/waivers';

export const routes: Routes = [
  { path: 'news', component: NewsPage, title: 'News' },
  { path: 'lineup', component: LineupPage, title: 'Start/Sit' },
  { path: 'waivers', component: WaiversPage, title: 'Waivers' },
  { path: 'trades', component: TradesPage, title: 'Trades' },
  { path: 'leagues', component: LeaguesPage, title: 'Leagues' },
  { path: 'data', component: StatusPage, title: 'Data' },
  { path: '**', redirectTo: 'news' },
];
