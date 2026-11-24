import { Component } from '@angular/core';
import { RouterLink, RouterLinkActive, RouterOutlet } from '@angular/router';
import { Icon } from './icon';

/**
 * Reports: how hard the agents are working. Live activity is counted as it happens
 * (app/activity.py, the last 7 days at most); Usage history is the ledger, one row per
 * finished run, kept for good.
 */
@Component({
  selector: 'hq-reports-layout',
  imports: [RouterOutlet, RouterLink, RouterLinkActive, Icon],
  template: `
    <div class="page-wrap">
      <header class="crumbs board-crumbs">
        <hq-icon name="activity" [size]="18" /><strong>Reports</strong>
        <nav class="rp-tabs">
          <a routerLink="/reports/live" routerLinkActive="on">Live activity</a>
          <a routerLink="/reports/history" routerLinkActive="on">Usage history</a>
        </nav>
      </header>
      <div class="rp-body"><router-outlet /></div>
    </div>
  `,
  styles: [`
    .rp-tabs { display: flex; gap: 4px; margin-left: 18px; font-size: 14px; }
    .rp-tabs a { padding: 5px 12px; border-radius: 6px; color: var(--muted); }
    .rp-tabs a:hover { color: var(--text); background: var(--panel); }
    .rp-tabs a.on { color: var(--text); background: var(--panel2); }
    .rp-body { flex: 1; min-height: 0; overflow-y: auto; }
  `],
})
export class ReportsLayout {}
