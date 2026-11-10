import { Component, OnInit, computed, inject, signal } from '@angular/core';
import { DatePipe } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { RouterLink } from '@angular/router';
import { Api, Profile, ProfileProposal } from './api.service';
import { ProfileProposalCard } from './profile-proposal-card';
import { Workspace } from './workspace';

const STARTER = `## Who I am
- Name:
- What I do:

## How I like things done
- 

## My environment
- `;

/** Settings → Profile: what every agent always knows about you. */
@Component({
  selector: 'hq-profile',
  imports: [FormsModule, DatePipe, RouterLink, ProfileProposalCard],
  template: `
  <div class="memory-layout">
    <section class="mem-main profile-main">
      <div class="mem-toolbar">
        <h2 class="grow">Your profile</h2>
        <span class="muted small" [class.over]="words() > (p()?.max_words ?? 600)">{{ words() }} / {{ p()?.max_words }} words</span>
        @if (p()?.updated_at) { <span class="muted small">· saved {{ p()!.updated_at | date:'MMM d, h:mm a' }}</span> }
      </div>
      <p class="hint inline-hint">Goes into every agent's instructions, every time: who you are, how you like things done,
        your environment. Keep it short; anything past the word limit is cut. Details only some tasks need belong in
        memory instead. Markdown is fine.</p>
      @if (p(); as c) {
        <textarea class="mono profile-text" [(ngModel)]="text" (ngModelChange)="dirty.set(true); live.set($event)"
          [placeholder]="starter"></textarea>
        <div class="actions">
          <button (click)="save()" [disabled]="!dirty()">Save profile</button>
          @if (!text.trim()) { <button class="ghost" (click)="useStarter()">Start from a template</button> }
          @if (notice()) { <span class="hint">{{ notice() }}</span> }
        </div>
      }
    </section>

    <aside class="mem-side">
      @if (p(); as c) {
        <h2>Settings</h2>
        <label class="check"><input type="checkbox" [(ngModel)]="c.enabled" (ngModelChange)="saveSettings()"> Give agents my profile</label>
        <p class="hint inline-hint">Each agent can opt out under Settings → Agents → Learning.</p>
        <label>Word limit <small>what goes into instructions is cut here</small>
          <input type="number" min="50" max="5000" [(ngModel)]="c.max_words" (change)="saveSettings()"></label>
        <label class="check"><input type="checkbox" [(ngModel)]="c.suggestions" (ngModelChange)="saveSettings()">
          Agents may suggest additions</label>
        <p class="hint inline-hint">When a reflection (Settings → Learning) finds a standing preference, such as
          “from now on…”, the agent suggests a line. Nothing is added until you approve it.</p>
      }
      <h3>Suggestions waiting @if (pending().length) { ({{ pending().length }}) }</h3>
      @for (s of pending(); track s.id) {
        <hq-profile-proposal-card [proposalId]="s.id" (decided)="reload()" />
      } @empty { <p class="hint">None right now. <a routerLink="/requests/profile">History in Requests</a></p> }
    </aside>
  </div>`,
})
export class ProfilePage implements OnInit {
  private api = inject(Api);
  private ws = inject(Workspace);
  p = signal<Profile | null>(null);
  all = signal<ProfileProposal[]>([]);
  pending = computed(() => this.all().filter(x => x.status === 'pending'));
  dirty = signal(false);
  notice = signal('');
  text = '';
  starter = STARTER;
  live = signal('');
  words = computed(() => (this.live().trim() ? this.live().trim().split(/\s+/).length : 0));

  ngOnInit() {
    this.api.profile().subscribe(p => { this.p.set(p); this.setText(p.text); });
    this.reload();
  }

  reload() {
    this.api.profileProposals('pending').subscribe(x => this.all.set(x));
    // An approved suggestion changes the text: pick it up unless you're mid-edit.
    if (!this.dirty()) this.api.profile().subscribe(p => { this.p.set(p); this.setText(p.text); });
    this.ws.refreshPending();
  }

  private setText(t: string) { this.text = t; this.live.set(t); }

  useStarter() { this.setText(STARTER); this.dirty.set(true); }

  save() {
    this.notice.set('');
    this.api.saveProfile({ text: this.text }).subscribe({
      next: p => { this.p.set(p); this.setText(p.text); this.dirty.set(false); this.notice.set('Saved. Agents use it from their next message.'); },
      error: e => this.notice.set(e.error?.detail ?? 'Save failed'),
    });
  }

  saveSettings() {
    const c = this.p()!;
    this.api.saveProfile({ enabled: c.enabled, max_words: c.max_words, suggestions: c.suggestions }).subscribe({
      next: p => this.p.update(x => ({ ...p, text: x?.text ?? p.text })),
    });
  }
}
