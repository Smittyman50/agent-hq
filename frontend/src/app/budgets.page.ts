import { Component, OnInit, computed, inject, signal } from '@angular/core';
import { DatePipe, DecimalPipe } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { RouterLink } from '@angular/router';
import { Api, BudgetIn, BudgetReport, BudgetStatus, BudgetTargets, RunsOn, SpendTotals } from './api.service';

const RUNS_ON: Record<RunsOn, string> = {
  local: 'Your hardware', plan: 'Plans', api: 'Per-token APIs', unknown: 'Unknown provider',
};

/** Reports → Budgets: the ledger by where the work ran, per goal, estimated API cost, and
 *  budgets that warn near a limit (app/budgets.py). Budgets never stop anything. */
@Component({
  selector: 'hq-budgets',
  imports: [FormsModule, DecimalPipe, DatePipe, RouterLink],
  template: `
  <div class="editor wide">
    <div class="inline head-row">
      <h2>Budgets</h2>
      <span class="spacer"></span>
      <select [ngModel]="days()" (ngModelChange)="setDays($event)">
        <option [ngValue]="1">Last 24 hours</option><option [ngValue]="7">Last 7 days</option>
        <option [ngValue]="30">Last 30 days</option><option [ngValue]="90">Last 90 days</option>
      </select>
    </div>
    <p class="hint inline-hint">The <a routerLink="/reports/history">usage ledger</a> by where the work ran: your own
      hardware (run time stands in for GPU time), your flat-rate plans (requests against the plan's limits), and
      per-token APIs (cost estimated from a public price list that updates itself). Budgets only <b>warn</b>, at your
      early-warning point and again at 100%, through your notification channels; nothing is ever stopped.</p>

    @if (report(); as r) {
      <div class="cards">
        @for (c of classes(); track c.key) {
          <div class="card">
            <span class="k">{{ c.label }}</span>
            @if (c.key === 'local') { <b>{{ hours(c.t.seconds) }}</b> }
            @else if (c.key === 'api') { <b>{{ money(c.t.cost_usd) }}</b> }
            @else { <b>{{ c.t.requests | number }} requests</b> }
            <span class="muted small">{{ c.t.runs | number }} runs · {{ c.t.requests | number }} requests ·
              {{ c.t.input_tokens + c.t.output_tokens | number }} tokens</span>
            @if (c.key === 'api' && c.t.unpriced_runs) {
              <span class="muted small">{{ c.t.unpriced_runs }} runs on models without a known price</span> }
          </div>
        }
      </div>

      <div class="inline head-row">
        <h3>Budgets</h3><span class="spacer"></span>
        @if (!draft()) { <button (click)="add()">Add budget</button> }
      </div>
      @if (draft(); as d) {
        <form class="bud-form" (ngSubmit)="save()">
          <div class="inline wrap">
            <label>On
              <select name="scope" [(ngModel)]="d.scope" (ngModelChange)="d.target_id = ''">
                <option value="agent">An agent (with its sub-agents)</option><option value="provider">A provider</option>
              </select></label>
            <label>{{ d.scope === 'agent' ? 'Agent' : 'Provider' }}
              <select name="target" [(ngModel)]="d.target_id" required>
                <option value="" disabled>Choose…</option>
                @if (d.scope === 'agent') { @for (a of targets()?.agents ?? []; track a.id) { <option [value]="a.id">{{ a.name }}</option> } }
                @else { @for (p of targets()?.providers ?? []; track p.id) { <option [value]="p.id">{{ p.name }} ({{ runsOn(p.runs_on) }})</option> } }
              </select></label>
            <label>Measure
              <select name="metric" [(ngModel)]="d.metric">
                @for (m of metrics(); track m.key) { <option [value]="m.key">{{ m.label }}</option> }
              </select></label>
            <label>Per
              <select name="period" [(ngModel)]="d.period">
                <option value="day">Day</option><option value="week">Week (from Monday)</option><option value="month">Month</option>
              </select></label>
            <label>Limit<input name="limit" type="number" min="0" [step]="d.metric === 'cost_usd' ? 0.5 : 1" [(ngModel)]="d.limit" required></label>
            <label>Early warning at %<input name="warn" type="number" min="1" max="99" [(ngModel)]="d.warn_pct"></label>
          </div>
          <label>Note <small>optional, for you</small><input name="note" [(ngModel)]="d.note"></label>
          <label class="check"><input type="checkbox" name="enabled" [(ngModel)]="d.enabled"> Enabled</label>
          @if (d.metric === 'cost_usd') {
            <p class="hint">Cost counts per-token API providers only; your hardware and your plans cost nothing per token.</p> }
          @if (d.metric === 'local_minutes') {
            <p class="hint">Run time of work on your own hardware (wall clock, tool calls included).</p> }
          @if (error()) { <p class="bad">{{ error() }}</p> }
          <div class="inline">
            <button type="submit">Save</button>
            <button type="button" class="ghost" (click)="draft.set(null); error.set('')">Cancel</button>
            @if (d.id) { <span class="spacer"></span><button type="button" class="danger" (click)="remove(d.id)">Delete</button> }
          </div>
        </form>
      }
      <table class="grid">
        <tr><th>On</th><th>Budget</th><th>Used</th><th class="bar-col"></th><th>Resets</th><th></th></tr>
        @for (b of r.budgets; track b.id) {
          <tr [class.muted]="!b.enabled">
            <td>{{ b.target_name }} <span class="pill">{{ b.scope }}</span></td>
            <td>{{ fmt(b.metric, b.limit) }} {{ b.metric_noun }} / {{ b.period }}
              @if (b.note) { <div class="muted small">{{ b.note }}</div> }</td>
            <td>{{ fmt(b.metric, b.used) }} <span class="muted">({{ b.pct | number:'1.0-0' }}%)</span>
              @if (b.unpriced_runs) { <div class="muted small">{{ b.unpriced_runs }} unpriced runs not counted</div> }</td>
            <td class="bar-col"><div class="meter" [class]="b.level"><span [style.width.%]="min(b.pct, 100)"></span>
              <i [style.left.%]="b.warn_pct"></i></div></td>
            <td class="muted small">{{ b.resets_at | date:'MMM d, h:mm a' }}</td>
            <td><button class="mini" (click)="edit(b)">Edit</button></td>
          </tr>
        } @empty { <tr><td colspan="6" class="muted">No budgets yet. Add one for an agent or a provider.</td></tr> }
      </table>

      <h3>By provider</h3>
      <table class="grid">
        <tr><th>Provider / model</th><th>Runs on</th><th>Requests</th><th>Tokens</th><th>Run time</th><th>Est. cost</th></tr>
        @for (p of r.by_provider; track p.provider_id ?? p.provider) {
          <tr class="prov"><td><b>{{ p.provider }}</b></td><td>{{ runsOn(p.runs_on) }}</td><td>{{ p.requests | number }}</td>
            <td>{{ p.input_tokens + p.output_tokens | number }}</td><td>{{ hours(p.seconds) }}</td>
            <td>{{ p.runs_on === 'api' ? money(p.cost_usd) : '—' }}</td></tr>
          @for (m of p.models; track m.model) {
            <tr class="sub-row"><td>{{ m.model || '—' }}
                @if (m.price_per_mtok) { <span class="muted small"> · {{ money(m.price_per_mtok[0]) }} / {{ money(m.price_per_mtok[1]) }} per M tokens in / out</span> }
                @else if (p.runs_on === 'api') { <span class="muted small"> · no known price</span> }</td>
              <td></td><td>{{ m.requests | number }}</td><td>{{ m.input_tokens + m.output_tokens | number }}</td>
              <td>{{ hours(m.seconds) }}</td><td>{{ p.runs_on === 'api' && m.price_per_mtok ? money(m.cost_usd) : '' }}</td></tr>
          }
        } @empty { <tr><td colspan="6" class="muted">No runs in this period.</td></tr> }
      </table>

      <h3>Goals <small class="muted">(everything that ran in the goal's conversation since it started: its turns,
        delegates and sub-agents)</small></h3>
      <table class="grid">
        <tr><th>Goal</th><th>Agent</th><th>Status</th><th>Turns</th><th>Requests</th><th>On your hardware</th>
          <th>Cloud requests</th><th>Est. cost</th></tr>
        @for (g of r.goals; track g.id) {
          <tr><td><a routerLink="/board">{{ g.title }}</a></td><td>{{ g.agent }}</td>
            <td><span class="pill">{{ g.status.replace('_', ' ') }}</span></td><td>{{ g.turns }}</td>
            <td>{{ g.requests | number }}</td><td>{{ hours(g.local_seconds) }}</td><td>{{ g.cloud_requests | number }}</td>
            <td>{{ g.cost_usd ? money(g.cost_usd) : '—' }}@if (g.unpriced_runs) { <span class="muted small"> +{{ g.unpriced_runs }} unpriced</span> }</td></tr>
        } @empty { <tr><td colspan="8" class="muted">No goal work in this period.</td></tr> }
      </table>

      <h3>Price list</h3>
      <p class="hint">
        @if (r.pricing.enabled) {
          @if (r.pricing.models) {
            {{ r.pricing.models | number }} model prices, updated {{ r.pricing.fetched_at | date:'MMM d, h:mm a' }}.
          } @else { No prices yet. }
          @if (r.pricing.error) { <span class="bad">Last update failed: {{ r.pricing.error }}</span> }
          It updates itself; costs use the current list price and don't discount cached input tokens, so they lean high.
        } @else { Cost estimates are off. }
      </p>
      <div class="inline">
        <label class="check"><input type="checkbox" [ngModel]="r.pricing.enabled" (ngModelChange)="setPricing($event)">
          Estimate API costs from the public price list</label>
        @if (r.pricing.enabled) { <button class="mini" [disabled]="refreshing()" (click)="refresh()">{{ refreshing() ? 'Updating…' : 'Update now' }}</button> }
      </div>
    }
  </div>`,
  styles: [`
    .bud-form { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 12px 14px; margin-bottom: 12px;
      display: flex; flex-direction: column; gap: 8px; }
    .bud-form .wrap { flex-wrap: wrap; gap: 10px; }
    .bud-form label { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--muted); }
    .bud-form label.check { flex-direction: row; align-items: center; color: var(--text); }
    .bud-form .wrap select, .bud-form .wrap input { width: auto; }
    .bud-form .wrap input[type=number] { width: 110px; }
    .bar-col { width: 22%; min-width: 110px; }
    .meter { position: relative; height: 8px; background: var(--panel2); border-radius: 4px; overflow: visible; }
    .meter span { display: block; height: 100%; border-radius: 4px; background: var(--ok); }
    .meter.warn span { background: var(--warn); }
    .meter.over span { background: var(--bad); }
    .meter.off span { background: var(--muted); }
    .meter i { position: absolute; top: -3px; width: 2px; height: 14px; background: var(--muted); }
    tr.sub-row td { font-size: 13px; padding-top: 3px; padding-bottom: 3px; }
    tr.sub-row td:first-child { padding-left: 26px; }
    .cards { flex-wrap: wrap; }
    @media (max-width: 640px) { .bar-col { min-width: 70px; } }
  `],
})
export class BudgetsPage implements OnInit {
  private api = inject(Api);
  days = signal(7);
  report = signal<BudgetReport | null>(null);
  targets = signal<BudgetTargets | null>(null);
  draft = signal<BudgetIn | null>(null);
  error = signal('');
  refreshing = signal(false);

  classes = computed(() => {
    const by = this.report()?.by_class ?? {};
    return (Object.keys(RUNS_ON) as RunsOn[]).filter(k => by[k])
      .map(k => ({ key: k, label: RUNS_ON[k], t: by[k] as SpendTotals }));
  });
  metrics = computed(() => Object.entries(this.targets()?.metrics ?? {}).map(([key, label]) => ({ key, label })));

  ngOnInit() {
    this.load();
    this.api.budgetTargets().subscribe(t => this.targets.set(t));
  }
  setDays(d: number) { this.days.set(d); this.load(); }
  load() { this.api.budgetReport(this.days()).subscribe(r => this.report.set(r)); }

  add() {
    this.error.set('');
    this.draft.set({ scope: 'agent', target_id: '', period: 'day', metric: 'requests', limit: 500,
                     warn_pct: 80, enabled: true, note: '' });
  }
  edit(b: BudgetStatus) {
    this.error.set('');
    this.draft.set({ id: b.id, scope: b.scope, target_id: b.target_id, period: b.period, metric: b.metric,
                     limit: b.limit, warn_pct: b.warn_pct, enabled: b.enabled, note: b.note });
  }
  save() {
    const d = this.draft();
    if (!d) return;
    this.api.saveBudget(d).subscribe({
      next: () => { this.draft.set(null); this.error.set(''); this.load(); },
      error: e => this.error.set(e?.error?.detail ?? 'Could not save the budget'),
    });
  }
  remove(id: string) {
    if (!confirm('Delete this budget?')) return;
    this.api.deleteBudget(id).subscribe(() => { this.draft.set(null); this.load(); });
  }
  setPricing(on: boolean) {
    this.api.saveBudgetSettings({ pricing: on }).subscribe(() => {
      if (on) this.refresh(); else this.load();
    });
  }
  refresh() {
    this.refreshing.set(true);
    this.api.refreshPrices().subscribe({
      next: () => { this.refreshing.set(false); this.load(); },
      error: () => { this.refreshing.set(false); this.load(); },
    });
  }

  runsOn(k: RunsOn) { return RUNS_ON[k] ?? k; }
  min(a: number, b: number) { return Math.min(a, b); }
  money(v: number) {
    if (!v) return '$0';
    return v < 0.01 ? `$${v.toFixed(4)}` : `$${v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  }
  fmt(metric: string, v: number) {
    if (metric === 'cost_usd') return this.money(v);
    if (metric === 'local_minutes') return `${Math.round(v).toLocaleString()} min`;
    return Math.round(v).toLocaleString();
  }
  hours(s: number) {
    if (!s) return '0 s';
    if (s < 60) return `${Math.round(s)} s`;
    if (s < 3600) return `${Math.round(s / 60)} min`;
    return `${(s / 3600).toFixed(1)} h`;
  }
}
