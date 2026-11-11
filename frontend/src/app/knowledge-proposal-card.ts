import { Component, effect, inject, input, output, signal } from '@angular/core';
import { DatePipe } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { RouterLink } from '@angular/router';
import { Api, KnowledgeCollection, KnowledgeProposal } from './api.service';
import { Avatar } from './avatar';
import { Markdown } from './markdown';
import { Workspace } from './workspace';

/**
 * An agent suggesting a document for the knowledge library. Approve it (after editing the
 * title or text, or into another collection, or replacing a near-copy it suggested
 * before) or reject it with a note. Shown in the conversation and in Requests → Knowledge.
 */
@Component({
  selector: 'hq-knowledge-proposal-card',
  imports: [FormsModule, DatePipe, RouterLink, Avatar, Markdown],
  template: `
    @if (p(); as r) {
      <div class="req-card skill" [class.pending]="r.status === 'pending'">
        <div class="req-head">
          <hq-avatar [agent]="ws.agent(r.agent_id)" [size]="24" [active]="r.status === 'pending'" />
          <strong>{{ r.agent_name }}</strong>
          <span>suggests a document for <strong>{{ r.collection }}</strong></span>
          <span class="pill {{ cls(r.status) }}">{{ r.status }}</span>
          @if (r.source === 'reflection') { <span class="muted small">after looking back</span> }
          <span class="spacer"></span>
          <span class="muted small">{{ r.created_at | date:'MMM d, h:mm a' }}</span>
        </div>
        @if (r.reason) { <div class="req-why"><span class="k">Why</span>{{ r.reason }}</div> }
        @if (r.similar_title && r.status === 'pending') {
          <div class="req-why warn"><span class="k">Similar</span>“{{ r.similar_title }}” is already in {{ r.collection }}
            @if (r.similar_score !== null) { ({{ (r.similar_score * 100).toFixed(0) }}% alike) }</div>
        }
        @if (r.status === 'pending') {
          @if (editing()) {
            <label class="profile-line">Title <input [(ngModel)]="title"></label>
            <label class="profile-line">Text <small>Markdown</small>
              <textarea class="kp-text mono" rows="14" [(ngModel)]="text"></textarea></label>
          } @else {
            <div class="kp-doc"><h4>{{ title }}</h4><div class="rich"><hq-md [text]="text" /></div></div>
          }
          <div class="kp-options">
            <label>Collection
              <select [(ngModel)]="collectionId" (ngModelChange)="replace = false">
                @for (c of collections(); track c.id) { <option [value]="c.id">{{ c.name }}</option> }
              </select></label>
            @if (r.similar_replaceable && collectionId === r.collection_id) {
              <label class="check"><input type="checkbox" [(ngModel)]="replace"> Replace “{{ r.similar_title }}”</label>
            }
            <button type="button" class="ghost small" (click)="editing.set(!editing())">{{ editing() ? 'Preview' : 'Edit' }}</button>
          </div>
          <div class="approval-actions">
            <input [(ngModel)]="note" placeholder="Note (optional; told to the agent if it suggests this again)">
            <button type="button" (click)="decide(true)" [disabled]="busy() || !title.trim() || !text.trim()">Add to library</button>
            <button type="button" class="danger" (click)="decide(false)" [disabled]="busy()">Reject</button>
          </div>
          @if (error()) { <p class="error">{{ error() }}</p> }
        } @else {
          <div class="req-decided">
            @if (r.status === 'approved') {
              Added {{ r.decided_at | date:'MMM d, h:mm a' }} as “{{ r.title }}”:
              see <a routerLink="/settings/knowledge">Settings → Knowledge</a> ({{ r.collection }}).
            } @else { Rejected {{ r.decided_at | date:'MMM d, h:mm a' }}. }
            @if (r.note) { · “{{ r.note }}” }
          </div>
        }
        @if (showThread() && r.thread_id) { <a class="small" [routerLink]="['/chat', r.thread_id]">Open the conversation</a> }
      </div>
    }
  `,
})
export class KnowledgeProposalCard {
  private api = inject(Api);
  ws = inject(Workspace);
  proposalId = input.required<string>();
  version = input<unknown>(null);
  showThread = input(false);
  decided = output<KnowledgeProposal>();

  p = signal<KnowledgeProposal | null>(null);
  collections = signal<KnowledgeCollection[]>([]);
  busy = signal(false);
  error = signal('');
  editing = signal(false);
  note = ''; title = ''; text = ''; collectionId = ''; replace = false;

  constructor() {
    effect(() => { this.version(); this.load(this.proposalId()); });
  }

  load(id: string) {
    this.api.knowledgeProposal(id).subscribe({
      next: r => {
        this.p.set(r);
        this.title = r.title; this.text = r.text; this.collectionId = r.collection_id;
        if (r.status === 'pending' && !this.collections().length) {
          this.api.collections().subscribe(c => this.collections.set(c));
        }
      },
      error: () => this.error.set('Could not load the suggestion.'),
    });
  }

  cls(s: string) { return s === 'approved' ? 'done' : s === 'rejected' ? 'failed' : 'working'; }

  decide(approve: boolean) {
    const r = this.p()!;
    this.busy.set(true);
    const edits = approve ? {
      title: this.title !== r.title ? this.title : undefined,
      text: this.text !== r.text ? this.text : undefined,
      collection_id: this.collectionId !== r.collection_id ? this.collectionId : undefined,
      replace_doc_id: this.replace && r.similar_doc_id ? r.similar_doc_id : undefined,
    } : {};
    this.api.decideKnowledgeProposal(r.id, { approve, note: this.note, ...edits }).subscribe({
      next: x => { this.busy.set(false); this.p.set(x); this.decided.emit(x); this.ws.refreshPending(); },
      error: e => { this.busy.set(false); this.error.set(e.error?.detail ?? 'Could not save the decision'); },
    });
  }
}
