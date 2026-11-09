import { Component, computed, effect, inject, input, output, signal } from '@angular/core';
import { DatePipe } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { RouterLink } from '@angular/router';
import { Api, ToolApproval } from './api.service';
import { Avatar } from './avatar';
import { Workspace } from './workspace';

/** A tool call waiting for your OK: what the agent wants to run, and your decision. */
@Component({
  selector: 'hq-approval-card',
  imports: [FormsModule, DatePipe, RouterLink, Avatar],
  template: `
    @if (row(); as r) {
      <div class="req-card approval" [class.pending]="r.status === 'pending'">
        <div class="req-head">
          <hq-avatar [agent]="ws.agent(r.agent_id)" [size]="24" [active]="r.status === 'pending'" />
          <strong>{{ r.agent_name }}</strong>
          <span>wants to run <code>{{ r.tool }}</code> on {{ r.server_name }}</span>
          <span class="pill {{ cls(r.status) }}">{{ r.status }}</span>
          <span class="spacer"></span>
          <span class="muted small">{{ r.created_at | date:'MMM d, h:mm a' }}</span>
        </div>
        @for (a of shown(); track a.key) {
          <div class="arg">
            <span class="k">{{ a.key }}</span>
            @if (a.code) { <pre class="code">{{ a.value }}</pre> } @else { <span class="v">{{ a.value }}</span> }
          </div>
        } @empty { <div class="muted small">No arguments.</div> }

        @if (r.status === 'pending') {
          <div class="approval-actions">
            <input [(ngModel)]="note" placeholder="Note to {{ r.agent_name }} (optional)">
            <button type="button" (click)="decide(true, 0)" [disabled]="busy()">Approve</button>
            <button type="button" class="ghost" (click)="decide(true, 60)" [disabled]="busy()"
              title="Approve, and don't ask again for {{ r.tool }} from {{ r.agent_name }} in this conversation for an hour">
              Approve, don't ask again for 1 h</button>
            <button type="button" class="danger" (click)="decide(false, 0)" [disabled]="busy()">Deny</button>
            @if (showLink()) { <a class="ghost-link" routerLink="/chat/{{ r.thread_id }}">Open conversation</a> }
          </div>
          @if (error()) { <p class="error">{{ error() }}</p> }
        } @else {
          <div class="req-decided">
            @switch (r.status) {
              @case ('approved') { Approved {{ r.decided_at | date:'h:mm a' }}@if (r.remember_until) {; not asking again until {{ r.remember_until | date:'h:mm a' }}} }
              @case ('denied') { Denied {{ r.decided_at | date:'h:mm a' }} }
              @default { Not decided in time; the call wasn't made. }
            }
            @if (r.note) { · “{{ r.note }}” }
          </div>
        }
      </div>
    }
  `,
})
export class ApprovalCard {
  private api = inject(Api);
  ws = inject(Workspace);
  approvalId = input.required<string>();
  version = input<unknown>(null);
  showLink = input(false);
  decided = output<ToolApproval>();

  row = signal<ToolApproval | null>(null);
  error = signal('');
  busy = signal(false);
  note = '';

  /** Arguments to show; code and commands as code blocks. */
  shown = computed(() => Object.entries(this.row()?.args ?? {}).map(([key, v]) => {
    const value = typeof v === 'string' ? v : JSON.stringify(v, null, 2);
    return { key, value, code: ['code', 'command', 'content'].includes(key) || value.includes('\n') };
  }));

  constructor() {
    effect(() => { this.version(); this.load(this.approvalId()); });
  }

  load(id: string) {
    this.api.toolApproval(id).subscribe({ next: r => this.row.set(r), error: () => this.error.set('Could not load this approval.') });
  }

  cls(s: string) { return s === 'approved' ? 'done' : s === 'denied' ? 'failed' : s === 'pending' ? 'working' : 'queued'; }

  decide(approve: boolean, remember: number) {
    this.busy.set(true);
    this.api.decideApproval(this.row()!.id, approve, this.note, remember).subscribe({
      next: r => { this.busy.set(false); this.row.set(r); this.decided.emit(r); this.ws.refreshPending(); },
      error: e => { this.busy.set(false); this.error.set(e.error?.detail ?? 'Could not save the decision'); this.load(this.approvalId()); },
    });
  }
}
