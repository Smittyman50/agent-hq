import { Component, OnDestroy, OnInit, computed, inject, signal } from '@angular/core';
import { DecimalPipe } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { RouterLink } from '@angular/router';
import { ActivityAgent, ActivityEvent, ActivityJob, ActivitySnapshot, Api } from './api.service';
import { Avatar } from './avatar';
import { Icon } from './icon';
import { Workspace } from './workspace';

type Metric = 'tokens' | 'output' | 'requests' | 'tools';

const POLL_MS = 3000;

/**
 * Activity: what every agent is doing right now, and how hard it's working. Counted as
 * it happens (each model request and tool call, from every worker), unlike Settings →
 * Usage, which gets one row when a run ends. Refreshes every few seconds.
 */
@Component({
  selector: 'hq-activity',
  imports: [FormsModule, DecimalPipe, RouterLink, Avatar, Icon],
  template: `
  <div class="page-wrap">
    <header class="crumbs board-crumbs">
      <hq-icon name="activity" [size]="18" /><strong>Activity</strong>
      <span class="live" [class.paused]="paused()" [title]="paused() ? 'Paused' : 'Updates every 3 seconds'">
        <span class="dot"></span>{{ paused() ? 'Paused' : 'Live' }}</span>
      <span class="board-tools">
        <select [ngModel]="window()" (ngModelChange)="setWindow($event)" title="Totals and charts cover">
          <option [ngValue]="15">Last 15 minutes</option><option [ngValue]="60">Last hour</option>
          <option [ngValue]="180">Last 3 hours</option><option [ngValue]="1440">Last 24 hours</option>
          <option [ngValue]="10080">Last 7 days</option>
        </select>
        <button class="ghost" (click)="paused.set(!paused())">{{ paused() ? 'Resume' : 'Pause' }}</button>
      </span>
    </header>
    @if (error()) { <p class="error board-error">{{ error() }}</p> }

    <div class="ac-scroll">
    @if (snap(); as s) {
      <div class="cards">
        <div class="card"><span class="k">Running now</span><b>{{ s.jobs.length }} job{{ s.jobs.length === 1 ? '' : 's' }}</b>
          <span class="muted small">{{ busyAgents() }} of {{ s.agents.length }} agents · {{ s.workers.length }} worker{{ s.workers.length === 1 ? '' : 's' }}</span></div>
        <div class="card"><span class="k">Tokens / min now</span><b>{{ compact(rateNow()) }}</b>
          <span class="muted small">last full minute, all agents</span></div>
        <div class="card"><span class="k">Tokens in / out</span><b>{{ compact(s.totals.input) }} / {{ compact(s.totals.output) }}</b>
          <span class="muted small">{{ pctOf(s.totals.cached, s.totals.input) }}% of input from cache</span></div>
        <div class="card"><span class="k">Model requests</span><b>{{ s.totals.requests | number }}</b>
          <span class="muted small">{{ avg(s.totals.llm_ms, s.totals.requests) }} avg
            @if (s.totals.req_errors) { · <span class="bad">{{ s.totals.req_errors }} failed</span> }</span></div>
        <div class="card"><span class="k">Tool calls</span><b>{{ s.totals.tools | number }}</b>
          <span class="muted small">{{ avg(s.totals.tool_ms, s.totals.tools) }} avg
            @if (s.totals.tool_errors) { · <span class="bad">{{ s.totals.tool_errors }} errors</span> }</span></div>
        <div class="card"><span class="k">Time in model / tools</span><b>{{ dur(s.totals.llm_ms / 1000) }} / {{ dur(s.totals.tool_ms / 1000) }}</b>
          <span class="muted small">{{ s.totals.runs }} job{{ s.totals.runs === 1 ? '' : 's' }} finished</span></div>
      </div>

      <h3>Running now</h3>
      @for (j of s.jobs; track j.job_id) {
        <div class="ac-job" [class.indent]="j.depth > 0">
          <hq-avatar [agent]="ws.agent(j.agent_id)" [size]="36" [active]="true" />
          <div class="ac-job-main">
            <div class="ac-job-head">
              <b>{{ j.agent }}</b>
              <span class="pill">{{ kindLabel(j) }}</span>
              <span [class]="'state ' + j.state">{{ stateLabel(j) }} · {{ since(j.since) }}</span>
            </div>
            @if (j.goal; as g) {
              <div class="ac-goal"><a routerLink="/board" [queryParams]="{ item: g.id }">{{ g.title }}</a>
                <span class="muted"> · {{ g.actions_done }}/{{ g.actions_total }} actions · turn {{ g.turns }}</span>
                @if (g.current_action) { <div class="muted small">▸ {{ g.current_action }}</div> }
              </div>
            } @else {
              <div class="ac-goal muted small" [title]="j.task">{{ firstLine(j.task) }}</div>
            }
            <div class="ac-job-stats muted small">
              <span>running {{ since(j.started) }}</span>
              <span>{{ j.requests }} requests</span><span>{{ j.tools }} tool calls</span>
              <span>{{ compact(j.input) }} in / {{ compact(j.output) }} out</span>
              @if (j.last_tool) { <span [title]="j.last_tool">last tool: {{ shortTool(j.last_tool) }}</span> }
              <span>{{ j.worker }}</span>
              <a [routerLink]="['/chat', j.thread_id]">conversation</a>
            </div>
          </div>
        </div>
      } @empty { <p class="muted ac-empty">Nothing running. Every agent is idle.</p> }

      <div class="inline head-row ac-agents-head">
        <h3>By agent</h3><span class="spacer"></span>
        <span class="muted small">Chart:</span>
        <select class="ac-metric" [ngModel]="metric()" (ngModelChange)="metric.set($event)">
          <option value="tokens">Tokens (in + out)</option><option value="output">Output tokens</option>
          <option value="requests">Model requests</option><option value="tools">Tool calls</option>
        </select>
      </div>
      <table class="grid ac-table">
        <tr><th>Agent</th><th class="spark-col">{{ metricLabel() }} per {{ bucketLabel() }}</th><th>Requests</th>
          <th>Tokens in</th><th>Tokens out</th><th>Out tok/s</th><th>Tool calls</th><th>Top tools</th></tr>
        @for (a of s.agents; track a.agent_id) {
          <tr [class.idle]="!running(a.agent_id) && !a.totals.requests">
            <td><div class="ac-who">
              <hq-avatar [agent]="ws.agent(a.agent_id)" [size]="24" [active]="running(a.agent_id)" />
              <span><span class="dotmark" [class.on]="running(a.agent_id)"></span>{{ a.name }}
                @if (a.is_subagent) { <small>sub-agent of {{ ws.agentName(a.parent_agent_id) }}</small> }</span>
            </div></td>
            <td class="spark-col">
              <svg class="spark" [attr.viewBox]="'0 0 ' + s.buckets.length + ' 30'" preserveAspectRatio="none">
                @for (v of series(a); track $index) {
                  <rect [attr.x]="$index + 0.1" [attr.width]="0.8" [attr.y]="30 - bar(v)" [attr.height]="bar(v)">
                    <title>{{ bucketTime($index) }}: {{ v | number }}</title></rect>
                }
              </svg>
            </td>
            <td>{{ a.totals.requests | number }}
              @if (a.totals.req_errors) { <span class="bad small"> ({{ a.totals.req_errors }} failed)</span> }
              @if (a.totals.requests) { <div class="muted small">{{ avg(a.totals.llm_ms, a.totals.requests) }} avg</div> }</td>
            <td>{{ compact(a.totals.input) }}
              @if (a.totals.input) { <div class="muted small">{{ pctOf(a.totals.cached, a.totals.input) }}% cached</div> }</td>
            <td>{{ compact(a.totals.output) }}</td>
            <td>{{ a.totals.llm_ms ? (a.totals.output / (a.totals.llm_ms / 1000) | number:'1.0-1') : '—' }}</td>
            <td>{{ a.totals.tools | number }}
              @if (a.totals.tool_errors) { <span class="bad small"> ({{ a.totals.tool_errors }} errors)</span> }</td>
            <td class="ac-tools">
              @for (t of a.top_tools.slice(0, 3); track t.tool) { <span class="pill" [title]="t.tool + ': ' + t.calls + ' calls'">{{ shortTool(t.tool) }} {{ t.calls }}</span> }
            </td>
          </tr>
        }
      </table>

      <h3>Recent steps <small class="muted">(all agents, newest first)</small></h3>
      <div class="ac-feed">
        @for (e of s.feed; track $index) {
          <div class="ac-ev" [class.err]="!!e.error" [class.job]="e.type.startsWith('job')">
            <span class="ts">{{ clock(e.ts) }}</span> <b>{{ e.agent }}</b> {{ describe(e) }}
          </div>
        } @empty { <div class="muted">No steps yet.</div> }
      </div>
    } @else if (!error()) { <p class="muted ac-empty">Loading…</p> }
    </div>
  </div>`,
  styles: [`
    .ac-scroll { overflow: auto; padding: 12px 24px 32px; flex: 1; min-height: 0; }
    .live { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; color: var(--ok); margin-left: 10px; }
    .live .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--ok); animation: blink 2s infinite; }
    .live.paused { color: var(--muted); } .live.paused .dot { background: var(--muted); animation: none; }
    @keyframes blink { 50% { opacity: .3; } }
    h3 { margin: 22px 0 8px; }
    .small { font-size: 12px; }
    .ac-empty { padding: 4px 0; }
    .ac-job { display: flex; gap: 12px; align-items: flex-start; background: var(--panel); border: 1px solid var(--line);
              border-radius: 8px; padding: 10px 14px; margin-bottom: 8px; }
    .ac-job.indent { margin-left: 28px; }
    .ac-job-main { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 4px; }
    .ac-job-head { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
    .ac-job-head .pill { margin-left: 0; }
    .state { font-size: 12px; border-radius: 10px; padding: 2px 9px; background: var(--panel2); color: var(--muted); }
    .state.model { color: #0b1220; background: var(--accent); }
    .state.tool { color: #0b1220; background: var(--warn); }
    .state.between, .state.starting { color: var(--text); }
    .ac-goal { overflow: hidden; text-overflow: ellipsis; }
    .ac-job-stats { display: flex; gap: 14px; flex-wrap: wrap; }
    .ac-agents-head { margin-top: 22px; gap: 8px; } .ac-agents-head h3 { margin: 0; }
    .ac-metric { width: auto; }
    .ac-table td { vertical-align: middle; }
    .ac-table tr.idle td { opacity: .55; }
    .ac-who { display: flex; gap: 8px; align-items: center; white-space: nowrap; }
    .dotmark { display: inline-block; width: 7px; height: 7px; border-radius: 50%; background: var(--line); margin-right: 6px; vertical-align: middle; }
    .dotmark.on { background: var(--ok); }
    .spark-col { width: 34%; min-width: 180px; }
    .spark { width: 100%; height: 30px; display: block; }
    .spark rect { fill: var(--accent); }
    .spark rect:hover { fill: var(--text); }
    .ac-tools .pill { margin: 0 4px 2px 0; display: inline-block; }
    .ac-feed { font-family: ui-monospace, monospace; font-size: 12px; background: var(--panel); border: 1px solid var(--line);
               border-radius: 8px; padding: 8px 12px; max-height: 420px; overflow: auto; }
    .ac-ev { padding: 1px 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; color: var(--muted); }
    .ac-ev b { color: var(--text); font-weight: 500; }
    .ac-ev.job { color: var(--accent); } .ac-ev.err { color: var(--bad); }
    .ac-ev .ts { opacity: .6; margin-right: 8px; } .ac-ev b { margin-right: 6px; }
  `],
})
export class ActivityPage implements OnInit, OnDestroy {
  private api = inject(Api);
  ws = inject(Workspace);

  window = signal(60);
  metric = signal<Metric>('tokens');
  paused = signal(false);
  snap = signal<ActivitySnapshot | null>(null);
  error = signal('');
  private now = signal(Date.now());
  private timer?: ReturnType<typeof setInterval>;
  private ticker?: ReturnType<typeof setInterval>;
  private loading = false;

  private runningIds = computed(() => new Set((this.snap()?.jobs ?? []).map(j => j.agent_id)));
  busyAgents = computed(() => this.runningIds().size);
  /** Tallest bar across all agents, so rows can be compared at a glance. */
  private peak = computed(() => Math.max(1, ...(this.snap()?.agents ?? []).flatMap(a => this.series(a))));
  /** Tokens in the last finished minute (only for windows with minute-or-finer buckets). */
  rateNow = computed(() => {
    const s = this.snap();
    if (!s) return 0;
    const n = s.buckets.length;
    const i = n >= 2 ? n - 2 : n - 1;
    const sum = s.agents.reduce((t, a) => t + (a.series.input[i] ?? 0) + (a.series.output[i] ?? 0), 0);
    return Math.round(sum / (s.bucket_s / 60));
  });
  metricLabel = computed(() => ({ tokens: 'Tokens', output: 'Output tokens', requests: 'Requests', tools: 'Tool calls' })[this.metric()]);
  bucketLabel = computed(() => {
    const b = this.snap()?.bucket_s ?? 60;
    return b < 3600 ? (b === 60 ? 'minute' : `${b / 60} min`) : (b === 3600 ? 'hour' : `${b / 3600} h`);
  });

  ngOnInit() {
    this.load();
    this.timer = setInterval(() => {
      if (!this.paused() && !document.hidden) this.load();
    }, POLL_MS);
    this.ticker = setInterval(() => this.now.set(Date.now()), 1000);
  }
  ngOnDestroy() { clearInterval(this.timer); clearInterval(this.ticker); }

  setWindow(w: number) { this.window.set(w); this.load(); }

  load() {
    if (this.loading) return;
    this.loading = true;
    this.api.activity(this.window()).subscribe({
      next: s => { this.snap.set(s); this.error.set(''); this.loading = false; },
      error: e => { this.error.set(e?.error?.detail ?? 'Could not load activity.'); this.loading = false; },
    });
  }

  running(id: string) { return this.runningIds().has(id); }

  series(a: ActivityAgent): number[] {
    const s = a.series;
    switch (this.metric()) {
      case 'tokens': return s.input.map((v, i) => v + (s.output[i] ?? 0));
      case 'output': return s.output;
      case 'requests': return s.requests;
      case 'tools': return s.tools;
    }
  }
  bar(v: number) { return v ? Math.max(1.5, 30 * v / this.peak()) : 0; }
  bucketTime(i: number) {
    const b = this.snap()?.buckets[i];
    if (!b) return '';
    const d = new Date(b);
    return (this.snap()?.bucket_s ?? 60) >= 3600
      ? d.toLocaleString([], { weekday: 'short', hour: '2-digit', minute: '2-digit' })
      : d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  }

  kindLabel(j: ActivityJob) {
    if (j.goal) return 'goal turn';
    return j.kind === 'turn' ? 'chat turn' : j.depth > 0 ? 'delegated task' : j.kind;
  }
  stateLabel(j: ActivityJob) {
    switch (j.state) {
      case 'model': return `Waiting on ${j.detail || 'the model'}`;
      case 'tool': return `Running ${this.shortTool(j.detail)}`;
      case 'between': return 'Between steps';
      default: return 'Starting';
    }
  }
  /** "workbench-2_read_file" -> "read_file" (the server prefix is in the tooltip). */
  shortTool(name: string) { return (name || '').replace(/^[a-z0-9]+-[a-z0-9-]*_/i, ''); }
  firstLine(t: string) { return (t || '').split('\n')[0].slice(0, 200); }

  since(iso: string) {
    if (!iso) return '';
    return this.dur(Math.max(0, (this.now() - new Date(iso).getTime()) / 1000));
  }
  dur(s: number) {
    s = Math.round(s);
    if (s < 60) return `${s}s`;
    if (s < 3600) return `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, '0')}s`;
    return `${Math.floor(s / 3600)}h ${String(Math.floor(s % 3600 / 60)).padStart(2, '0')}m`;
  }
  avg(ms: number, n: number) { return n ? this.dur(ms / n / 1000).replace(/^0s$/, '<1s') : '—'; }
  pctOf(part: number, whole: number) { return whole ? Math.round(100 * part / whole) : 0; }
  compact(n: number) {
    if (n < 1000) return String(Math.round(n));
    if (n < 1e6) return `${(n / 1e3).toFixed(n < 1e4 ? 1 : 0)}k`;
    return `${(n / 1e6).toFixed(n < 1e7 ? 2 : 1)}M`;
  }
  clock(iso: string) { return new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }); }

  describe(e: ActivityEvent) {
    const secs = e.ms != null ? ` · ${(e.ms / 1000).toFixed(1)}s` : '';
    switch (e.type) {
      case 'model':
        if (e.error) return `model request failed on ${e.model}${secs}: ${e.error}`;
        return `${e.model} · ${this.compact(e.input ?? 0)} in (${this.pctOf(e.cached ?? 0, e.input ?? 0)}% cached) → ${this.compact(e.output ?? 0)} out${secs}`
          + (e.finish && e.finish !== 'stop' && e.finish !== 'tool_call' ? ` · ${e.finish}` : '');
      case 'tool': return `${e.tool}${secs}${e.error ? ' · ' + e.error : ''}`;
      case 'job_started': return `started a ${e.kind ?? 'job'}: ${e.detail ?? ''}`;
      case 'job_finished': return `finished after ${this.dur(e.seconds ?? 0)}`;
      case 'job_ended': return `stopped or failed after ${this.dur(e.seconds ?? 0)}`;
    }
  }
}
