import { Component, computed, effect, inject, input, output, signal } from '@angular/core';
import { DatePipe } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { RouterLink } from '@angular/router';
import { Api, SkillProposal } from './api.service';
import { Avatar } from './avatar';
import { Markdown } from './markdown';
import { Workspace } from './workspace';

/** An agent proposing a new skill or an improvement: review, edit, approve or reject. */
@Component({
  selector: 'hq-skill-proposal-card',
  imports: [FormsModule, DatePipe, Avatar, Markdown, RouterLink],
  template: `
    @if (p(); as r) {
      <div class="req-card skill" [class.pending]="r.status === 'pending'">
        <div class="req-head">
          <hq-avatar [agent]="ws.agent(r.agent_id)" [size]="24" [active]="r.status === 'pending'" />
          <strong>{{ r.agent_name }}</strong>
          <span>{{ r.skill_id && r.status === 'pending' ? 'proposes an improvement to' : 'proposes a new skill:' }}
            <code>{{ r.name }}</code></span>
          <span class="pill {{ cls(r.status) }}">{{ r.status }}</span>
          <span class="spacer"></span>
          <span class="muted small">{{ r.created_at | date:'MMM d, h:mm a' }}</span>
        </div>
        <div class="req-why"><span class="k">Why</span>{{ r.reason }}</div>
        @if (r.status === 'testing') { <div class="req-why eval-line"><span class="k">Testing</span>Auto mode: it's applied if it doesn't lower the pass rate; you can decide now instead.</div> }
        @if (r.eval_summary; as e) {
          @if (e.with) {
            <div class="req-why eval-line" [class.bad]="e.regressed">
              <span class="k">Tests</span>
              passes {{ e.with.passed }}/{{ e.with.total }} with this change vs {{ e.without?.passed }}/{{ e.without?.total }} without
              @if (e.regressed) { · <strong>worse, so it wasn't applied automatically</strong> }
              <a [routerLink]="['/settings/evals']" [queryParams]="{ run: e.run_id }">details</a>
            </div>
          } @else if (e.status === 'queued' || e.status === 'running') {
            <div class="req-why eval-line"><span class="k">Tests</span>running {{ e.cases }} test case{{ e.cases === 1 ? '' : 's' }} with and without this change…</div>
          }
        }

        @if (r.status === 'pending' && editing()) {
          <label>Title<input [(ngModel)]="edit.title"></label>
          <label>When to use<textarea rows="2" [(ngModel)]="edit.when_to_use"></textarea></label>
          <label>Procedure <small>markdown</small><textarea rows="10" class="mono" [(ngModel)]="edit.body"></textarea></label>
        } @else if (r.diff) {
          @if (r.stale) { <div class="warn-line">⚠ The skill changed since this was written (now v{{ r.current?.version }}); check the diff.</div> }
          <div class="req-why"><span class="k">Changes to v{{ r.current?.version }}</span></div>
          <pre class="diff">@for (l of diffLines(); track $index) {<span [class]="l.cls">{{ l.text }}
</span>}</pre>
        } @else {
          <div class="req-why"><span class="k">{{ r.title }}</span>Use when: {{ r.when_to_use }}</div>
          <div class="skill-body"><hq-md [text]="r.body" /></div>
        }

        @if (r.status === 'pending' || r.status === 'testing') {
          <div class="approval-actions">
            <input [(ngModel)]="note" placeholder="Note (optional)">
            <label class="check small"><input type="checkbox" [ngModel]="editing()" (ngModelChange)="startEdit($event)"> Edit first</label>
            <button type="button" (click)="decide(true)" [disabled]="busy()">Approve</button>
            <button type="button" class="danger" (click)="decide(false)" [disabled]="busy()">Reject</button>
          </div>
          @if (error()) { <p class="error">{{ error() }}</p> }
        } @else {
          <div class="req-decided">
            @switch (r.status) {
              @case ('approved') { Approved {{ r.decided_at | date:'MMM d, h:mm a' }}: it's in Settings → Skills. }
              @case ('applied') { Saved automatically (learning mode: auto). Review or roll back in Settings → Skills. }
              @default { Rejected {{ r.decided_at | date:'MMM d, h:mm a' }}. }
            }
            @if (r.note) { · “{{ r.note }}” }
          </div>
        }
      </div>
    }
  `,
})
export class SkillProposalCard {
  private api = inject(Api);
  ws = inject(Workspace);
  proposalId = input.required<string>();
  version = input<unknown>(null);
  decided = output<SkillProposal>();

  p = signal<SkillProposal | null>(null);
  editing = signal(false);
  busy = signal(false);
  error = signal('');
  note = '';
  edit = { title: '', when_to_use: '', body: '' };

  diffLines = computed(() => (this.p()?.diff ?? '').split('\n').filter(l => !l.startsWith('---') && !l.startsWith('+++'))
    .map(text => ({ text, cls: text.startsWith('+') ? 'add' : text.startsWith('-') ? 'del' : text.startsWith('@@') ? 'hunk' : '' })));

  constructor() {
    effect(() => { this.version(); this.load(this.proposalId()); });
  }

  load(id: string) { this.api.skillProposal(id).subscribe({ next: r => this.p.set(r), error: () => this.error.set('Could not load the proposal.') }); }

  cls(s: string) { return s === 'approved' || s === 'applied' ? 'done' : s === 'rejected' ? 'failed' : 'working'; }

  startEdit(on: boolean) {
    const r = this.p()!;
    this.edit = { title: r.title, when_to_use: r.when_to_use, body: r.body };
    this.editing.set(on);
  }

  decide(approve: boolean) {
    this.busy.set(true);
    const d = { approve, note: this.note, ...(approve && this.editing() ? this.edit : {}) };
    this.api.decideSkillProposal(this.p()!.id, d).subscribe({
      next: r => { this.busy.set(false); this.p.set(r); this.editing.set(false); this.decided.emit(r); this.ws.refreshPending(); },
      error: e => { this.busy.set(false); this.error.set(e.error?.detail ?? 'Could not save the decision'); },
    });
  }
}
