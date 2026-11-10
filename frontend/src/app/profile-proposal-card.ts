import { Component, effect, inject, input, output, signal } from '@angular/core';
import { DatePipe } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { RouterLink } from '@angular/router';
import { Api, ProfileProposal } from './api.service';
import { Avatar } from './avatar';
import { Workspace } from './workspace';

/** An agent suggesting a line for your profile: approve (optionally reworded) or reject. */
@Component({
  selector: 'hq-profile-proposal-card',
  imports: [FormsModule, DatePipe, RouterLink, Avatar],
  template: `
    @if (p(); as r) {
      <div class="req-card skill" [class.pending]="r.status === 'pending'">
        <div class="req-head">
          <hq-avatar [agent]="ws.agent(r.agent_id)" [size]="24" [active]="r.status === 'pending'" />
          <strong>{{ r.agent_name }}</strong>
          <span>suggests adding to your profile</span>
          <span class="pill {{ cls(r.status) }}">{{ r.status }}</span>
          <span class="spacer"></span>
          <span class="muted small">{{ r.created_at | date:'MMM d, h:mm a' }}</span>
        </div>
        @if (r.status === 'pending') {
          <label class="profile-line">Line <small>reword it if you like; it's added as a bullet</small>
            <input [(ngModel)]="text"></label>
        } @else {
          <blockquote class="profile-quote">{{ r.text }}</blockquote>
        }
        @if (r.reason) { <div class="req-why"><span class="k">Why</span>{{ r.reason }}</div> }
        @if (r.status === 'pending') {
          <div class="approval-actions">
            <input [(ngModel)]="note" placeholder="Note (optional)">
            <button type="button" (click)="decide(true)" [disabled]="busy() || !text.trim()">Add to profile</button>
            <button type="button" class="danger" (click)="decide(false)" [disabled]="busy()">Reject</button>
          </div>
          @if (error()) { <p class="error">{{ error() }}</p> }
        } @else {
          <div class="req-decided">
            @if (r.status === 'approved') { Added {{ r.decided_at | date:'MMM d, h:mm a' }}: see <a routerLink="/settings/profile">Settings → Profile</a>. }
            @else { Rejected {{ r.decided_at | date:'MMM d, h:mm a' }}. }
            @if (r.note) { · “{{ r.note }}” }
          </div>
        }
      </div>
    }
  `,
})
export class ProfileProposalCard {
  private api = inject(Api);
  ws = inject(Workspace);
  proposalId = input.required<string>();
  version = input<unknown>(null);
  decided = output<ProfileProposal>();

  p = signal<ProfileProposal | null>(null);
  busy = signal(false);
  error = signal('');
  note = '';
  text = '';

  constructor() {
    effect(() => { this.version(); this.load(this.proposalId()); });
  }

  load(id: string) {
    this.api.profileProposal(id).subscribe({
      next: r => { this.p.set(r); this.text = r.text; },
      error: () => this.error.set('Could not load the suggestion.'),
    });
  }

  cls(s: string) { return s === 'approved' ? 'done' : s === 'rejected' ? 'failed' : 'working'; }

  decide(approve: boolean) {
    this.busy.set(true);
    this.api.decideProfileProposal(this.p()!.id, { approve, note: this.note, text: approve ? this.text : undefined }).subscribe({
      next: r => { this.busy.set(false); this.p.set(r); this.decided.emit(r); this.ws.refreshPending(); },
      error: e => { this.busy.set(false); this.error.set(e.error?.detail ?? 'Could not save the decision'); },
    });
  }
}
