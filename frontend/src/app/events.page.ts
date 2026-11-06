import { Component, OnInit, computed, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { Router } from '@angular/router';
import { AgentDef, Api, Subscription, Thread, blankSubscription } from './api.service';

@Component({
  selector: 'hq-events',
  imports: [FormsModule],
  template: `
  <div class="split">
    <aside class="list">
      <div class="list-head"><h2>MQTT subscriptions</h2><button (click)="edit(null)">+ New</button></div>
      @for (x of subs(); track x.id) {
        <button class="row" [class.active]="draft()?.id === x.id" [class.muted]="!x.enabled" (click)="edit(x)">
          <span class="name">{{ x.name }}</span>
          <span class="sub"><code>{{ x.topic_filter }}</code> → {{ agentName(x.agent_id) }}</span>
        </button>
      } @empty { <p class="hint">Subscribe an agent to MQTT topics so events wake it up.</p> }
    </aside>

    @if (draft(); as d) {
      <form class="editor" (ngSubmit)="save()">
        <h2>{{ d.id ? 'Edit ' + d.name : 'New subscription' }}</h2>
        <label>Name<input name="name" [(ngModel)]="d.name" required></label>
        <label>Topic filter <small>+ matches one level, # matches the rest</small>
          <input name="topic" [(ngModel)]="d.topic_filter" placeholder="homelab/alerts/#"></label>
        <label>Agent that handles it
          <select name="agent" [(ngModel)]="d.agent_id">
            @for (a of enabledAgents(); track a.id) { <option [value]="a.id">{{ a.name }}</option> }
          </select></label>
        <label>Conversation <small>where the agent reports; empty = a dedicated thread</small>
          <select name="thread" [(ngModel)]="d.thread_id">
            <option [ngValue]="null">Dedicated events thread</option>
            @for (t of agentThreads(); track t.id) { <option [ngValue]="t.id">{{ t.title }}</option> }
          </select></label>
        <label>Prompt template <small>{{ '{' }}topic{{ '}' }} and {{ '{' }}payload{{ '}' }} are filled in; empty = default</small>
          <textarea name="tpl" rows="5" [(ngModel)]="d.prompt_template"
            placeholder="An event arrived on MQTT topic {topic}: {payload} …"></textarea></label>
        <label>Cooldown (seconds) <small>ignore repeats for this long after one fires</small>
          <input name="cool" type="number" min="0" [(ngModel)]="d.min_interval_seconds"></label>
        <label class="check"><input type="checkbox" name="enabled" [(ngModel)]="d.enabled"> Enabled</label>
        @if (error()) { <p class="error">{{ error() }}</p> }
        <div class="actions">
          <button type="submit">Save</button>
          @if (d.id) {
            @if (d.thread_id) { <button type="button" class="ghost" (click)="open(d.thread_id)">Open conversation</button> }
            <button type="button" class="danger" (click)="remove(d)">Delete</button>
          }
        </div>

        @if (d.id) {
          <h3>Send a test event</h3>
          <div class="inline">
            <input name="ttopic" [(ngModel)]="testTopic" placeholder="homelab/alerts/test">
            <input name="tpayload" [(ngModel)]="testPayload" placeholder="payload">
            <button type="button" class="ghost" (click)="sendTest()">Send</button>
          </div>
          @if (testResult()) { <p class="hint">{{ testResult() }}</p> }
        }
      </form>
    } @else { <div class="empty">Select a subscription or create one.</div> }
  </div>`,
})
export class EventsPage implements OnInit {
  private api = inject(Api);
  private router = inject(Router);
  subs = signal<Subscription[]>([]);
  agents = signal<AgentDef[]>([]);
  threads = signal<Thread[]>([]);
  draft = signal<Subscription | null>(null);
  error = signal('');
  testResult = signal('');
  testTopic = 'homelab/alerts/test';
  testPayload = 'Test alert from the UI';

  enabledAgents = computed(() => this.agents().filter(a => a.enabled));
  agentThreads = computed(() => this.threads().filter(t => t.agent_id === this.draft()?.agent_id));

  ngOnInit() {
    this.load();
    this.api.agents().subscribe(a => this.agents.set(a));
    this.api.threads().subscribe(t => this.threads.set(t));
  }

  load() { this.api.subscriptions().subscribe(s => this.subs.set(s)); }
  agentName(id: string) { return this.agents().find(a => a.id === id)?.name ?? '—'; }
  open(threadId: string) { this.router.navigate(['/chat', threadId]); }

  edit(x: Subscription | null) {
    this.error.set(''); this.testResult.set('');
    const first = this.enabledAgents().find(a => a.is_default) ?? this.enabledAgents()[0];
    this.draft.set(x ? structuredClone(x) : blankSubscription(first?.id ?? ''));
    if (x) this.testTopic = x.topic_filter.replace('#', 'test').replace(/\+/g, 'x');
  }

  save() {
    this.api.saveSubscription(this.draft()!).subscribe({
      next: x => { this.load(); this.draft.set(structuredClone(x)); this.error.set(''); },
      error: e => this.error.set(e.error?.detail ?? 'Save failed'),
    });
  }

  remove(d: Subscription) {
    if (!confirm(`Delete subscription ${d.name}?`)) return;
    this.api.deleteSubscription(d.id!).subscribe(() => { this.draft.set(null); this.load(); });
  }

  sendTest() {
    this.api.testEvent(this.testTopic, this.testPayload).subscribe({
      next: r => {
        this.testResult.set(r.queued_jobs.length
          ? `Queued ${r.queued_jobs.length} turn(s). The agent's answer appears in its conversation.`
          : 'No subscription matched (or it is in cooldown).');
        this.load();
        this.api.subscriptions().subscribe(s => {
          const cur = s.find(x => x.id === this.draft()?.id);
          if (cur) this.draft.set(structuredClone(cur));
        });
      },
      error: e => this.testResult.set(e.error?.detail ?? 'Failed'),
    });
  }
}
