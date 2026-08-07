import { Component, OnInit, inject, signal } from '@angular/core';

import { Api, type JobStatus } from '../services/api';

@Component({
  selector: 'app-status',
  template: `
    @if (!api.reachable()) {
      <div class="empty">Could not reach the API.</div>
    } @else {
      <div class="list">
        @for (job of jobs(); track job.job) {
          <div class="card status-row" [class.stale]="job.due">
            <span class="status-dot"></span>
            <span class="status-name">{{ job.job }}</span>
            <span class="status-desc">
              {{ job.description }} · every {{ job.interval_hours }}h · updated {{ age(job) }}
            </span>
          </div>
        }
      </div>
      <p class="muted" style="margin-top:14px">
        Ingest runs on a schedule and catches up on startup, so these stay
        current without you doing anything.
      </p>
    }
  `,
})
export class StatusPage implements OnInit {
  protected readonly api = inject(Api);
  protected readonly jobs = signal<JobStatus[]>([]);

  async ngOnInit(): Promise<void> {
    const status = await this.api.ingestStatus();
    this.jobs.set(status?.jobs ?? []);
  }

  protected age(job: JobStatus): string {
    if (job.age_hours === null) return 'never';
    return job.age_hours < 1
      ? `${Math.round(job.age_hours * 60)}m ago`
      : `${job.age_hours.toFixed(1)}h ago`;
  }
}
