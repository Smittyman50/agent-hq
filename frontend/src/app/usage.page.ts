import { Component, OnInit, computed, inject, signal } from '@angular/core';
import { DecimalPipe } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { Api, UsageReport } from './api.service';

/** The usage ledger: every agent run's model requests, tokens and time. */
@Component({
  selector: 'hq-usage',
  imports: [FormsModule, DecimalPipe],
  template: `
  <div class="editor wide">
    <div class="inline head-row">
      <h2>Usage</h2>
      <span class="spacer"></span>
      <select [ngModel]="days()" (ngModelChange)="setDays($event)">
        <option [ngValue]="1">Last 24 hours</option><option [ngValue]="7">Last 7 days</option>
        <option [ngValue]="30">Last 30 days</option><option [ngValue]="90">Last 90 days</option>
      </select>
    </div>
    <p class="hint inline-hint">One row per agent run (a chat turn, a delegated task or a sub-agent task). Time is
      wall-clock time of the run, including tool calls, so it's a rough stand-in for GPU/CPU time on local models.</p>
    @if (report(); as r) {
      <div class="cards">
        <div class="card"><span class="k">Runs</span><b>{{ r.total?.runs ?? 0 | number }}</b>
          @if (r.total?.failed) { <span class="muted small">{{ r.total?.failed }} failed</span> }</div>
        <div class="card"><span class="k">Model requests</span><b>{{ r.total?.requests ?? 0 | number }}</b></div>
        <div class="card"><span class="k">Tokens in / out</span>
          <b>{{ r.total?.input_tokens ?? 0 | number }} / {{ r.total?.output_tokens ?? 0 | number }}</b></div>
        <div class="card"><span class="k">Run time</span><b>{{ hours(r.total?.seconds ?? 0) }}</b></div>
        <div class="card"><span class="k">Sub-agents</span><b>{{ r.subagents?.runs ?? 0 }} runs</b>
          <span class="muted small">{{ hours(r.subagents?.seconds ?? 0) }} · {{ share() }}% of run time</span></div>
      </div>

      @if (r.by_day.length > 1) {
        <h3>Run time per day</h3>
        <div class="bars">
          @for (d of r.by_day; track d.day) {
            <div class="bar" [title]="d.day + ': ' + hours(d.seconds) + ', ' + d.runs + ' runs'">
              <span [style.height.%]="pct(d.seconds)"></span><small>{{ d.day.slice(5) }}</small></div>
          }
        </div>
      }

      <h3>By agent <small class="muted">(sub-agent runs counted under the agent that created them)</small></h3>
      <table class="grid">
        <tr><th>Agent</th><th>Runs</th><th>Sub-agent runs</th><th>Requests</th><th>Tokens</th><th>Run time</th><th>Avg / run</th></tr>
        @for (g of r.by_agent; track g.agent) {
          <tr><td>{{ g.agent }}</td><td>{{ g.runs }}@if (g.failed) { <span class="muted"> ({{ g.failed }} failed)</span> }</td>
            <td>{{ g.subagent_runs }}</td><td>{{ g.requests | number }}</td>
            <td>{{ g.input_tokens + g.output_tokens | number }}</td><td>{{ hours(g.seconds) }}</td><td>{{ g.avg_seconds }} s</td></tr>
        } @empty { <tr><td colspan="7" class="muted">No runs in this period.</td></tr> }
      </table>

      <h3>By model</h3>
      <table class="grid">
        <tr><th>Model</th><th>Provider</th><th>Runs</th><th>Requests</th><th>Tokens</th><th>Run time</th><th>Avg / run</th></tr>
        @for (g of r.by_model; track g.provider + g.model) {
          <tr><td>{{ g.model || '—' }}</td><td>{{ g.provider || '—' }}</td><td>{{ g.runs }}</td><td>{{ g.requests | number }}</td>
            <td>{{ g.input_tokens + g.output_tokens | number }}</td><td>{{ hours(g.seconds) }}</td><td>{{ g.avg_seconds }} s</td></tr>
        } @empty { <tr><td colspan="7" class="muted">No runs in this period.</td></tr> }
      </table>
    }
  </div>`,
})
export class UsagePage implements OnInit {
  private api = inject(Api);
  days = signal(7);
  report = signal<UsageReport | null>(null);
  private maxDay = computed(() => Math.max(1, ...(this.report()?.by_day ?? []).map(d => d.seconds)));
  share = computed(() => {
    const r = this.report();
    return r?.total?.seconds ? Math.round(100 * (r.subagents?.seconds ?? 0) / r.total.seconds) : 0;
  });

  ngOnInit() { this.load(); }
  setDays(d: number) { this.days.set(d); this.load(); }
  load() { this.api.usage(this.days()).subscribe(r => this.report.set(r)); }
  pct(s: number) { return Math.max(2, 100 * s / this.maxDay()); }
  hours(s: number) {
    if (s < 60) return `${Math.round(s)} s`;
    if (s < 3600) return `${Math.round(s / 60)} min`;
    return `${(s / 3600).toFixed(1)} h`;
  }
}
