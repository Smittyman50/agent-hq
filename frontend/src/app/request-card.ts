import { Component, effect, inject, input, output, signal } from '@angular/core';
import { DatePipe, DecimalPipe } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { RouterLink } from '@angular/router';
import { Api, ProviderInfo, SubagentRequest } from './api.service';
import { Avatar } from './avatar';
import { Workspace } from './workspace';

interface Form {
  count: number; concurrent: number; max_minutes: number; max_requests: number; hours: number; note: string;
  models: Record<string, boolean>; tools: Record<string, boolean>;
}

/** A sub-agent request: the agent's case, the system's estimate, and your decision. */
@Component({
  selector: 'hq-request-card',
  imports: [FormsModule, DatePipe, DecimalPipe, RouterLink, Avatar],
  template: `
    @if (req(); as r) {
      <div class="req-card" [class.pending]="r.status === 'pending'">
        <div class="req-head">
          <hq-avatar [agent]="ws.agent(r.agent_id)" [size]="26" [active]="r.status === 'pending'" />
          <strong>{{ r.agent }}</strong>
          <span>asks for {{ r.subagents.length }} sub-agent{{ r.subagents.length === 1 ? '' : 's' }}
            for {{ r.hours }} h</span>
          <span class="pill {{ statusClass(r.status) }}">{{ r.status }}</span>
          <span class="spacer"></span>
          <span class="muted small">{{ r.created_at | date:'MMM d, h:mm a' }}</span>
        </div>

        <div class="req-why"><span class="k">Why</span>{{ r.why }}</div>
        <div class="req-why"><span class="k">Why not an existing agent</span>{{ r.why_not_existing_agents }}</div>
        @if (r.thread_owner && r.thread_owner !== r.agent) {
          <div class="req-why"><span class="k">Context</span>Working on a task for {{ r.thread_owner }}
            in “{{ r.thread_title }}”.</div>
        }

        <table class="req-table">
          <thead><tr><th>Role</th><th>Purpose</th><th>Model</th><th>Runs on</th><th>Est.</th><th>History</th></tr></thead>
          <tbody>
            @for (p of r.subagents; track $index; let i = $index) {
              @let e = r.estimate.subagents[i];
              <tr>
                <td><b>{{ p.role }}</b>@if (p.tools.length) { <div class="muted small">tools: {{ p.tools.join(', ') }}</div> }</td>
                <td>{{ p.purpose }}</td>
                <td>{{ p.label }}@if (e?.model_details) { <div class="muted small">{{ e.model_details }}</div> }</td>
                <td>{{ e?.runs_on }}
                  @if (e?.provider_slots) { <div class="muted small">{{ e.slots_free_now }} of {{ e.provider_slots }} slots free now</div> }</td>
                <td>{{ p.est_minutes }} min</td>
                <td>@if (e?.ledger?.runs) {
                    {{ e.ledger.avg_seconds | number:'1.0-0' }} s, {{ e.ledger.avg_tokens | number }} tokens / run
                    <div class="muted small">avg of {{ e.ledger.runs }} runs, {{ e.ledger.days }} days</div>
                  } @else { <span class="muted">no runs yet</span> }</td>
              </tr>
            }
          </tbody>
        </table>
        <div class="req-total">
          Total ≈ <b>{{ r.estimate.total_minutes }}</b> model-minutes, about {{ r.estimate.wall_minutes }} min of
          wall time with {{ r.estimate.peak_concurrent }} at once.
          @for (o of r.estimate.overlaps; track $index) {
            <div class="warn-line">⚠ “{{ o.role }}” looks similar to {{ o.closest_agent }} (similarity {{ o.score }}).
              Consider having {{ o.closest_agent }} do it.</div>
          }
          @if (!r.estimate.overlap_check) {
            <div class="muted small">Overlap with existing agents wasn't checked: no embedding model is set (Settings → Memory).</div>
          }
        </div>

        @if (r.status === 'pending') {
          @if (form(); as f) {
            <div class="req-form">
              <div class="limit-grid">
                <label>Sub-agents<input type="number" min="1" max="20" [(ngModel)]="f.count"></label>
                <label>At once<input type="number" min="1" max="10" [(ngModel)]="f.concurrent"></label>
                <label>Minutes each<input type="number" min="1" max="240" [(ngModel)]="f.max_minutes"></label>
                <label>Model requests each<input type="number" min="1" max="500" [(ngModel)]="f.max_requests"></label>
                <label>Valid for (hours)<input type="number" min="0.5" max="72" step="0.5" [(ngModel)]="f.hours"></label>
              </div>
              <div class="checks">
                @for (m of modelKeys(r); track m.key) {
                  <label class="check"><input type="checkbox" [(ngModel)]="f.models[m.key]"> {{ m.label }}</label>
                }
                @for (t of toolIds(r); track t.id) {
                  <label class="check"><input type="checkbox" [(ngModel)]="f.tools[t.id]"> tool: {{ t.name }}</label>
                }
              </div>
              <label>Note to {{ r.agent }} <small>optional</small>
                <input [(ngModel)]="f.note" placeholder="e.g. fine, but keep it to the feeds you listed"></label>
              @if (error()) { <p class="error">{{ error() }}</p> }
              <div class="actions">
                <button type="button" (click)="decide(true)" [disabled]="busy()">Approve</button>
                <button type="button" class="danger" (click)="decide(false)" [disabled]="busy()">Deny</button>
                @if (showLink()) { <a class="ghost-link" routerLink="/requests/subagents" [queryParams]="{ id: r.id }">Open in Requests</a> }
              </div>
            </div>
          }
        } @else if (r.status === 'approved' && r.grant; as g) {
          <div class="req-decided">Approved: {{ g.count }} sub-agent(s), {{ g.concurrent }} at once,
            {{ g.max_minutes }} min and {{ g.max_requests }} model requests each · used {{ r.used }}
            · until {{ r.expires_at | date:'MMM d, h:mm a' }}@if (r.note) { · “{{ r.note }}” }</div>
        } @else if (r.status === 'denied') {
          <div class="req-decided">Denied {{ r.decided_at | date:'MMM d, h:mm a' }}@if (r.note) { · “{{ r.note }}” }</div>
        } @else {
          <div class="req-decided muted">Expired without a decision.</div>
        }
      </div>
    } @else if (error()) { <p class="error">{{ error() }}</p> }
  `,
})
export class RequestCard {
  private api = inject(Api);
  ws = inject(Workspace);
  requestId = input.required<string>();
  /** Changes when the chat message changes (e.g. decided elsewhere), to reload. */
  version = input<unknown>(null);
  showLink = input(true);
  decided = output<SubagentRequest>();

  req = signal<SubagentRequest | null>(null);
  form = signal<Form | null>(null);
  error = signal('');
  busy = signal(false);
  private providers: ProviderInfo[] = [];

  constructor() {
    this.api.providers().subscribe(p => this.providers = p);
    effect(() => { this.version(); this.load(this.requestId()); });
  }

  load(id: string) {
    this.api.subagentRequest(id).subscribe({
      next: r => { this.req.set(r); this.form.set(this.initialForm(r)); this.error.set(''); },
      error: e => this.error.set(e.error?.detail ?? 'Could not load the request'),
    });
  }

  statusClass(s: string) {
    return s === 'approved' ? 'done' : s === 'denied' ? 'failed' : s === 'pending' ? 'working' : 'queued';
  }

  modelKeys(r: SubagentRequest) {
    const seen = new Map<string, string>();
    for (const p of r.subagents) seen.set(`${p.provider_id ?? ''}|${p.model}`, p.label);
    return [...seen].map(([key, label]) => ({ key, label }));
  }

  toolIds(r: SubagentRequest) {
    const out = new Map<string, string>();
    for (const p of r.subagents) p.tool_server_ids.forEach((id, i) => out.set(id, p.tools[i] ?? id));
    return [...out].map(([id, name]) => ({ id, name }));
  }

  private initialForm(r: SubagentRequest): Form {
    const lim = r.limits;
    return {
      count: r.subagents.length,
      concurrent: Math.min(r.subagents.length, Math.max(1, lim?.concurrent ?? 1)),
      max_minutes: Math.max(...r.subagents.map(p => p.est_minutes), 1),
      max_requests: lim?.max_requests ?? 25,
      hours: r.hours, note: '',
      models: Object.fromEntries(this.modelKeys(r).map(m => [m.key, true])),
      tools: Object.fromEntries(this.toolIds(r).map(t => [t.id, true])),
    };
  }

  decide(approve: boolean) {
    const r = this.req()!, f = this.form()!;
    const models = Object.entries(f.models).filter(([, on]) => on).map(([k]) => {
      const [pid, ...rest] = k.split('|');
      return { provider_id: pid || null, model: rest.join('|') };
    });
    if (approve && !models.length) { this.error.set('Leave at least one model ticked.'); return; }
    this.busy.set(true);
    this.api.decideRequest(r.id, approve ? {
      approve, note: f.note, count: f.count, concurrent: f.concurrent, max_minutes: f.max_minutes,
      max_requests: f.max_requests, hours: f.hours, models,
      tool_server_ids: Object.entries(f.tools).filter(([, on]) => on).map(([k]) => k),
    } : { approve, note: f.note }).subscribe({
      next: done => { this.busy.set(false); this.req.set(done); this.decided.emit(done); this.ws.refreshPending(); },
      error: e => { this.busy.set(false); this.error.set(e.error?.detail ?? 'Could not save the decision'); },
    });
  }
}
