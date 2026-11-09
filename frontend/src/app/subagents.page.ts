import { Component, OnDestroy, OnInit, inject, signal } from '@angular/core';
import { DatePipe } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { RouterLink } from '@angular/router';
import { Api, LiveSubagents, SubagentCaps } from './api.service';
import { Workspace } from './workspace';

/** Team-wide sub-agent caps (each agent's own limits are on its page), and what's running now. */
@Component({
  selector: 'hq-subagents',
  imports: [FormsModule, DatePipe, RouterLink],
  template: `
  <div class="editor wide">
    <h2>Sub-agents</h2>
    <p class="hint inline-hint">Agents are off by default. Choose who may create sub-agents (or must ask you) under
      Settings → Agents → Sub-agents. These caps apply on top, across the whole team.</p>
    @if (caps(); as c) {
      <form (ngSubmit)="save()">
        <div class="limit-grid">
          <label>Running at once (team)<input type="number" min="1" name="live" [(ngModel)]="c.max_live"></label>
          <label>Created per day (team)<input type="number" min="1" name="day" [(ngModel)]="c.max_per_day"></label>
          <label>Open requests per agent<input type="number" min="1" name="pend" [(ngModel)]="c.max_pending_per_agent"></label>
          <label>Requests lapse after (hours)<input type="number" min="1" name="ttl" [(ngModel)]="c.request_ttl_hours"></label>
        </div>
        <label class="narrow">Duplicate check <small>how similar a sub-agent's role may be to an existing agent before it's refused</small>
          <input type="number" min="0.3" max="1" step="0.01" name="sim" [(ngModel)]="c.similarity_threshold"></label>
        <p class="hint inline-hint">Uses the embedding model from Settings → Memory. Lower = stricter. A sub-agent named
          after an existing agent is always refused.</p>
        <label class="check"><input type="checkbox" name="mqtt" [(ngModel)]="c.notify_mqtt">
          Also notify me on MQTT (topic set by AHQ_MQTT_NOTIFY_TOPIC, default agent-hq/notifications)</label>
        @if (msg()) { <p class="hint">{{ msg() }}</p> }
        <div class="actions"><button type="submit">Save</button>
          <a class="ghost-link" routerLink="/requests/subagents">Requests inbox ({{ ws.pendingRequests() }} pending)</a></div>
      </form>
    }

    <h3>Running now</h3>
    @if (live(); as l) {
      <p class="muted small">{{ l.today }} created today (team cap {{ l.caps.max_per_day }}).</p>
      <table class="grid">
        <tr><th>Sub-agent</th><th>Created by</th><th>Model</th><th>Expires</th></tr>
        @for (s of l.live; track s.id) {
          <tr><td>{{ s.name }}</td><td>{{ s.parent }}</td><td>{{ s.model }}</td><td>{{ s.expires_at | date:'HH:mm:ss' }}</td></tr>
        } @empty { <tr><td colspan="4" class="muted">None.</td></tr> }
      </table>
    }
  </div>`,
})
export class SubagentsPage implements OnInit, OnDestroy {
  private api = inject(Api);
  ws = inject(Workspace);
  caps = signal<SubagentCaps | null>(null);
  live = signal<LiveSubagents | null>(null);
  msg = signal('');
  private timer?: ReturnType<typeof setInterval>;

  ngOnInit() {
    this.api.subagentCaps().subscribe(c => this.caps.set(c));
    this.poll();
    this.timer = setInterval(() => this.poll(), 5000);
  }
  ngOnDestroy() { clearInterval(this.timer); }
  poll() { this.api.liveSubagents().subscribe(l => this.live.set(l)); }

  save() {
    this.api.saveSubagentCaps(this.caps()!).subscribe({
      next: c => { this.caps.set(c); this.msg.set('Saved.'); },
      error: e => this.msg.set(e.error?.detail ?? 'Save failed'),
    });
  }
}
