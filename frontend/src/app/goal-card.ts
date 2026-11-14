import { Component, effect, inject, input, output, signal } from '@angular/core';
import { DatePipe } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { Router } from '@angular/router';
import { Api, GoalDetail } from './api.service';
import { Avatar } from './avatar';
import { Icon } from './icon';
import { Markdown } from './markdown';
import { Workspace } from './workspace';

/**
 * A standing goal that needs you: a done claim to confirm or send back, a proposal to
 * approve, a goal a guard blocked, or a question. Shown in the conversation, in
 * Requests → Goals and on the board.
 */
@Component({
  selector: 'hq-goal-card',
  imports: [FormsModule, DatePipe, Avatar, Icon, Markdown],
  template: `
  @if (g(); as g) {
    <div class="req-card goal-card">
      <div class="req-head">
        <span class="row-with-avatar"><hq-avatar [agent]="ws.agent(g.owner_agent_id)" [size]="26" />
          <span><strong>{{ g.title }}</strong> <span class="muted">· {{ g.agent_name }}</span></span></span>
        <span class="pill {{ pillClass(g) }}">{{ label(g) }}</span>
      </div>
      @switch (g.needs_you) {
        @case ('review') {
          <p class="muted">{{ g.agent_name }} says the goal is done. Done when:</p>
          <div class="goal-quote">{{ g.done_criteria }}</div>
          <p class="muted">Evidence:</p>
          <div class="body rich"><hq-md [text]="g.result" /></div>
          <div class="req-form">
            <input name="note" [(ngModel)]="note" placeholder="Note (optional; shown to the agent if you send it back)">
            <div class="actions tight">
              <button (click)="review(true)" [disabled]="busy()">Confirm done</button>
              <button class="ghost" (click)="review(false)" [disabled]="busy()">Send back</button>
            </div>
          </div>
        }
        @case ('proposal') {
          <p class="muted">{{ g.source['agent'] || 'An agent' }} proposes this goal{{ g.source['agent'] !== g.agent_name ? ' for ' + g.agent_name : '' }}:</p>
          <div class="goal-quote">{{ g.proposal_reason }}</div>
          <p class="goal-field"><strong>Objective</strong> {{ g.objective }}</p>
          <p class="goal-field"><strong>Done when</strong> {{ g.done_criteria }}</p>
          <div class="req-form">
            <input name="note" [(ngModel)]="note" placeholder="Note (optional)">
            <div class="actions tight">
              <button (click)="propose(true, true)" [disabled]="busy()">Approve to Ready</button>
              <button class="ghost" (click)="propose(true, false)" [disabled]="busy()">Approve to Backlog</button>
              <button class="danger" (click)="propose(false, false)" [disabled]="busy()">Reject</button>
            </div>
          </div>
        }
        @case ('blocked') {
          <p class="warn-line">{{ g.waiting_on?.detail }}</p>
          <p class="hint inline-hint">Resume it as it is, or raise its limits on the board first.
            Turns {{ g.turns }}/{{ g.limits.max_turns }} · model requests {{ d()?.requests_used ?? '…' }}/{{ g.limits.max_requests }}.</p>
          <div class="actions tight">
            <button (click)="resume()" [disabled]="busy()">Resume</button>
            <button class="ghost" (click)="drop()" [disabled]="busy()">Drop it</button>
          </div>
        }
        @case ('question') {
          <p>{{ g.agent_name }} needs you: <strong>{{ g.waiting_on?.detail }}</strong></p>
          <p class="hint inline-hint">Answer in the goal's conversation; it carries on when you do.</p>
        }
        @default {
          <p class="muted">{{ g.column }}@if (g.finished_at) { · {{ g.finished_at | date:'MMM d, h:mm a' }} }
            @if (g.review_note) { · your note: {{ g.review_note }} }</p>
        }
      }
      @if (error()) { <p class="error">{{ error() }}</p> }
      @if (showLinks()) {
        <div class="actions tight">
          <button class="mini ghost goal-link" (click)="openBoard()"><hq-icon name="board" [size]="14" /> On the board</button>
          @if (g.thread_id && showThread()) {
            <button class="mini ghost goal-link" (click)="openThread()"><hq-icon name="chat" [size]="14" /> Conversation</button>
          }
        </div>
      }
    </div>
  }`,
})
export class GoalCard {
  private api = inject(Api);
  private router = inject(Router);
  ws = inject(Workspace);
  goalId = input.required<string>();
  version = input<unknown>(null);   // change it to refetch (e.g. the message's status)
  showLinks = input(true);
  showThread = input(true);
  decided = output<GoalDetail>();

  d = signal<GoalDetail | null>(null);
  g = this.d;
  busy = signal(false);
  error = signal('');
  note = '';

  constructor() {
    effect(() => { this.version(); this.load(this.goalId()); });
  }

  load(id: string) { this.api.goal(id).subscribe({ next: d => this.d.set(d), error: () => this.d.set(null) }); }

  label(g: GoalDetail) {
    const labels: Record<string, string> = { review: 'confirm done?', proposal: 'proposed', blocked: 'blocked', question: 'question' };
    return labels[g.needs_you ?? ''] ?? g.column.toLowerCase();
  }
  pillClass(g: GoalDetail) {
    const classes: Record<string, string> = { review: 'you', proposal: 'you', blocked: 'failed', question: 'you' };
    return classes[g.needs_you ?? ''] ?? (g.status === 'done' ? 'done' : g.status === 'dropped' ? 'stopped' : '');
  }

  private run(obs: ReturnType<Api['goal']>) {
    this.busy.set(true);
    this.error.set('');
    obs.subscribe({
      next: d => { this.d.set(d); this.busy.set(false); this.note = ''; this.ws.refreshPending(); this.decided.emit(d); },
      error: e => { this.busy.set(false); this.error.set(e.error?.detail ?? 'That didn’t work'); },
    });
  }
  review(approve: boolean) { this.run(this.api.reviewGoal(this.goalId(), approve, this.note)); }
  propose(approve: boolean, ready: boolean) { this.run(this.api.decideGoalProposal(this.goalId(), approve, ready, this.note)); }
  resume() { this.run(this.api.moveGoal(this.goalId(), 'in_progress')); }
  drop() { this.run(this.api.moveGoal(this.goalId(), 'dropped')); }
  openBoard() { this.router.navigate(['/board'], { queryParams: { item: this.goalId() } }); }
  openThread() { const t = this.d()?.thread_id; if (t) this.router.navigate(['/chat', t]); }
}
