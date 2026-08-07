import { Injectable, signal } from '@angular/core';

export type Theme = 'light' | 'dark';

const STORAGE_KEY = 'fantasy-theme';

/**
 * Resolves the theme to an explicit `data-theme` on <html>, so the stylesheet
 * needs two blocks and no media queries. Follows the OS until the user picks a
 * side, then remembers that choice.
 */
@Injectable({ providedIn: 'root' })
export class ThemeService {
  readonly theme = signal<Theme>('dark');

  init(): void {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved === 'light' || saved === 'dark') {
      this.apply(saved);
      return;
    }
    const prefersDark = window.matchMedia('(prefers-color-scheme: dark)');
    this.set(prefersDark.matches ? 'dark' : 'light');
    prefersDark.addEventListener('change', (event) => {
      if (!localStorage.getItem(STORAGE_KEY)) {
        this.set(event.matches ? 'dark' : 'light');
      }
    });
  }

  toggle(): void {
    this.apply(this.theme() === 'dark' ? 'light' : 'dark');
  }

  /** Applies without recording a preference (used while following the OS). */
  private set(theme: Theme): void {
    this.theme.set(theme);
    document.documentElement.dataset['theme'] = theme;
  }

  private apply(theme: Theme): void {
    this.set(theme);
    localStorage.setItem(STORAGE_KEY, theme);
  }
}
