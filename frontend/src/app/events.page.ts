import { Component, OnInit, computed, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { Router } from '@angular/router';
import { AgentDef, Api, Subscription, Thread, Webhook, blankSubscription, blankWebhook } from './api.service';

@Component({
  selector: 'hq-events',
  imports: [FormsModule],
  template: `
  <div class="split">
    <aside class="list">
      <div class="list-head"><h2>Subscriptions</h2><button (click)="edit(null)">+ New</button></div>
      @for (x of subs(); track x.id) {
        <button class="row" [class.active]="draft()?.id === x.id" [class.muted]="!x.enabled" (click)="edit(x)">
          <span class="name">{{ x.name }}</span>
          <span class="sub"><code>{{ x.topic_filter }}</code> → {{ agentName(x.agent_id) }}</span>
        </button>
      } @empty { <p class="hint">Subscribe an agent to MQTT topics or webhook events so they wake it up.</p> }

      <div class="list-head"><h2>Webhooks</h2><button (click)="editHook(null)">+ New</button></div>
      @for (h of hooks(); track h.id) {
        <button class="row" [class.active]="hookDraft()?.id === h.id" [class.muted]="!h.enabled" (click)="editHook(h)">
          <span class="name">{{ h.name }}</span>
          <span class="sub"><code>{{ h.topic_prefix }}…</code> · {{ h.auth_mode === 'gitea' ? 'Gitea signature' : 'token' }}</span>
        </button>
      } @empty { <p class="hint">Let any system on your LAN that can send an HTTP POST wake an agent.</p> }
    </aside>

    @if (draft(); as d) {
      <form class="editor" (ngSubmit)="save()">
        <h2>{{ d.id ? 'Edit ' + d.name : 'New subscription' }}</h2>
        <label>Name<input name="name" [(ngModel)]="d.name" required></label>
        <label>Topic filter <small>+ matches one level, # matches the rest; webhooks arrive as webhook/&lt;source&gt;/&lt;event&gt;</small>
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
    } @else if (hookDraft(); as h) {
      <form class="editor" (ngSubmit)="saveHook()">
        <h2>{{ h.id ? 'Edit webhook ' + h.name : 'New webhook' }}</h2>
        <label>Name <small>lowercase letters, digits and dashes; the URL is /hooks/&lt;name&gt;</small>
          <input name="hname" [(ngModel)]="h.name" placeholder="gitea" required></label>
        <label>Source <small>second level of the topic; empty = the name</small>
          <input name="hsource" [(ngModel)]="h.source" [placeholder]="h.name || 'gitea'"></label>
        <label>How the sender proves itself
          <select name="hauth" [(ngModel)]="h.auth_mode">
            <option value="gitea">Gitea signature (X-Gitea-Signature, HMAC of the body)</option>
            <option value="token">Secret token (X-Webhook-Token header, Bearer header or ?token=)</option>
          </select></label>
        <label>Event header <small>its value becomes the last topic level; empty = {{ h.auth_mode === 'gitea' ? 'X-Gitea-Event' : 'none' }}</small>
          <input name="hheader" [(ngModel)]="h.event_header" [placeholder]="h.auth_mode === 'gitea' ? 'X-Gitea-Event' : 'X-Event'"></label>
        <label>Event field <small>used when the header is missing: a dotted JSON path, e.g. heartbeat.status_text; otherwise "event"</small>
          <input name="hfield" [(ngModel)]="h.event_field" placeholder="action"></label>
        <label class="check"><input type="checkbox" name="henabled" [(ngModel)]="h.enabled"> Enabled</label>
        @if (error()) { <p class="error">{{ error() }}</p> }
        <div class="actions">
          <button type="submit">{{ h.id ? 'Save' : 'Create' }}</button>
          @if (h.id) {
            <button type="button" class="ghost" (click)="routeHook(h)">New subscription for it</button>
            <button type="button" class="ghost" (click)="rotate(h)">Rotate secret</button>
            <button type="button" class="danger" (click)="removeHook(h)">Delete</button>
          }
        </div>

        @if (secret(); as s) {
          <h3>Secret</h3>
          <p class="hint inline-hint">Copy it now: it isn't shown again. Rotate to get a new one.</p>
          <div class="inline">
            <input name="hsecret" readonly [value]="s">
            <button type="button" class="ghost" (click)="copy(s)">Copy</button>
          </div>
        }

        @if (h.id) {
          <h3>Sending to it</h3>
          <p>URL: <code>{{ h.url }}</code> <button type="button" class="ghost" (click)="copy(h.url!)">Copy</button></p>
          <p>Events arrive as <code>{{ h.topic_prefix }}&lt;event&gt;</code>. Route them to an agent with a
            subscription such as <code>{{ h.topic_prefix }}#</code>.</p>
          @if (h.auth_mode === 'gitea') {
            <p class="hint inline-hint">In Gitea: repository (or organisation) → Settings → Webhooks → Add webhook → Gitea.
              Target URL as above, method POST, content type application/json, and paste the secret into Secret.
              Gitea's Test Delivery button sends a push event.</p>
          } @else {
            <p class="hint inline-hint">From a script or cron job:</p>
            <pre><code>curl -X POST -H "X-Webhook-Token: $AHQ_HOOK_SECRET" -H "X-Event: done" \\
     --data-binary &#64;report.txt {{ h.url }}</code></pre>
          }
          <p class="hint inline-hint">Last delivery: {{ h.last_delivery_at ? when(h.last_delivery_at) : 'none yet' }}</p>
        }
      </form>
    } @else { <div class="empty">Select a subscription or webhook, or create one.</div> }
  </div>`,
})
export class EventsPage implements OnInit {
  private api = inject(Api);
  private router = inject(Router);
  subs = signal<Subscription[]>([]);
  hooks = signal<Webhook[]>([]);
  agents = signal<AgentDef[]>([]);
  threads = signal<Thread[]>([]);
  draft = signal<Subscription | null>(null);
  hookDraft = signal<Webhook | null>(null);
  secret = signal('');
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

  load() {
    this.api.subscriptions().subscribe(s => this.subs.set(s));
    this.api.webhooks().subscribe(h => this.hooks.set(h));
  }
  agentName(id: string) { return this.agents().find(a => a.id === id)?.name ?? '—'; }
  open(threadId: string) { this.router.navigate(['/chat', threadId]); }
  when(iso: string) { return new Date(iso).toLocaleString(); }
  copy(text: string) { navigator.clipboard?.writeText(text); }

  edit(x: Subscription | null) {
    this.error.set(''); this.testResult.set(''); this.hookDraft.set(null); this.secret.set('');
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

  // ------------------------------------------------------------------ webhooks

  editHook(h: Webhook | null) {
    this.error.set(''); this.draft.set(null); this.secret.set('');
    this.hookDraft.set(h ? structuredClone(h) : blankWebhook());
  }

  private shown(h: Webhook) {
    if (h.secret) this.secret.set(h.secret);
    this.hookDraft.set(structuredClone({ ...h, secret: null }));
    this.error.set('');
    this.load();
  }

  saveHook() {
    this.api.saveWebhook(this.hookDraft()!).subscribe({
      next: h => this.shown(h),
      error: e => this.error.set(e.error?.detail ?? 'Save failed'),
    });
  }

  rotate(h: Webhook) {
    if (!confirm(`Make a new secret for ${h.name}? The current one stops working at once.`)) return;
    this.api.rotateWebhook(h.id!).subscribe({ next: x => this.shown(x) });
  }

  removeHook(h: Webhook) {
    if (!confirm(`Delete webhook ${h.name}? Senders using it will get 404.`)) return;
    this.api.deleteWebhook(h.id!).subscribe(() => { this.hookDraft.set(null); this.secret.set(''); this.load(); });
  }

  routeHook(h: Webhook) {
    this.edit(null);
    const d = this.draft()!;
    d.name = `${h.name} events`;
    d.topic_filter = `${h.topic_prefix}#`;
    this.draft.set({ ...d });
    this.testTopic = `${h.topic_prefix}test`;
  }
}
