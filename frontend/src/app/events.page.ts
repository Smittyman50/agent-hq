import { Component, OnInit, computed, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { Router } from '@angular/router';
import { AgentDef, Api, DeliveryDetail, DeliveryReply, DeliveryStatus, DeliverySummary, EventOutcome, Subscription, Thread, Webhook, WebhookSettings, blankSubscription, blankWebhook } from './api.service';

const STATUS_LABEL: Record<DeliveryStatus, string> = {
  accepted: 'accepted', no_match: 'no match', cooldown: 'cooldown', duplicate: 'duplicate',
  rejected: 'rejected', too_large: 'too large', blocked: 'blocked address', rate_limited: 'rate limited',
};

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
          <input name="topic" [(ngModel)]="d.topic_filter" (blur)="loadPickable(d.topic_filter)" placeholder="homelab/alerts/#"></label>
        <label>Agent that handles it
          <select name="agent" [(ngModel)]="d.agent_id">
            @for (a of enabledAgents(); track a.id) { <option [value]="a.id">{{ a.name }}</option> }
          </select></label>
        <label>Conversation <small>where the agent reports; empty = a dedicated thread</small>
          <select name="thread" [(ngModel)]="d.thread_id">
            <option [ngValue]="null">Dedicated events thread</option>
            @for (t of agentThreads(); track t.id) { <option [ngValue]="t.id">{{ t.title }}</option> }
          </select></label>
        <label>Prompt template <small>{{ '{' }}topic{{ '}' }}, {{ '{' }}payload{{ '}' }}, {{ '{' }}event{{ '}' }}, {{ '{' }}source{{ '}' }}, {{ '{' }}hook{{ '}' }}
            and JSON fields like {{ '{' }}payload.repository.full_name{{ '}' }} or {{ '{' }}payload.commits.0.message{{ '}' }}; empty = default</small>
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

        <h3>Try it</h3>
        @if (pickable().length) {
          <label>Start from a recent webhook delivery <small>matching this filter</small>
            <select name="pick" [ngModel]="''" (ngModelChange)="pickDelivery($event)">
              <option value="">—</option>
              @for (p of pickable(); track p.id) {
                <option [value]="p.id">{{ when(p.at) }} · {{ p.topic }} ({{ size(p.body_bytes) }})</option>
              }
            </select></label>
        }
        <label>Topic<input name="ttopic" [(ngModel)]="testTopic" placeholder="homelab/alerts/test"></label>
        <label>Payload<textarea name="tpayload" rows="4" [(ngModel)]="testPayload" placeholder="payload"></textarea></label>
        <div class="actions">
          <button type="button" class="ghost" (click)="preview()">Preview prompt</button>
          @if (d.id) { <button type="button" class="ghost" (click)="sendTest()">Send test event</button> }
        </div>
        @if (previewText() !== null) {
          @if (previewMissing().length) {
            <p class="hint inline-hint"><span class="pill missing">not in payload</span> {{ previewMissing().join(', ') }} (filled in as empty)</p>
          }
          <pre class="payload">{{ previewText() }}</pre>
        }
        @if (testResult()) { <p class="hint">{{ testResult() }}</p> }
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
        <label>Delivery ID header <small>a repeat of the same ID within 24 hours is logged as a duplicate and not run; empty = {{ h.auth_mode === 'gitea' ? 'X-Gitea-Delivery' : 'no duplicate check' }}</small>
          <input name="hdelivery" [(ngModel)]="h.delivery_header" [placeholder]="h.auth_mode === 'gitea' ? 'X-Gitea-Delivery' : 'X-Request-Id'"></label>
        <label>Accept from <small>addresses or networks, comma-separated; empty = {{ cfg()?.default_networks || 'your LAN' }}</small>
          <input name="hnets" [(ngModel)]="h.allowed_networks" placeholder="e.g. 192.168.1.10 or 192.168.10.0/24"></label>
        <label>Requests per minute <small>more get 429; 0 = no limit</small>
          <input name="hrate" type="number" min="0" [(ngModel)]="h.rate_per_minute" [placeholder]="'' + (cfg()?.default_rate_per_minute ?? 60)"></label>
        <label class="check"><input type="checkbox" name="hcautious" [(ngModel)]="h.cautious">
          <span>Ask before tools that change things <small>turns this hook starts, and work they delegate, ask before any tool not marked read-only, even where your rules say allow</small></span></label>
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

        @if (cfg() && !cfg()!.encryption) {
          <p class="hint" style="padding: 10px 0 0"><span class="pill cooldown" style="margin-left: 0">not encrypted</span> Webhook secrets are stored as they are.
            Set <code>AHQ_SECRET_KEY</code> in <code>.env</code> and restart to encrypt them.</p>
        }
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

          <h3>Send a test delivery</h3>
          <p class="hint inline-hint">Routed as this hook would route it, without a signature (you're signed in). Logged as a test.</p>
          <label>Event<input name="hevent" [(ngModel)]="hookTestEvent" placeholder="push"></label>
          <label>Body<textarea name="hbody" rows="4" [(ngModel)]="hookTestBody" placeholder='{"ref": "refs/heads/main"}'></textarea></label>
          <div class="actions">
            <button type="button" class="ghost" (click)="sendHookTest(h)">Send</button>
            <label class="check"><input type="checkbox" name="hrespect" [(ngModel)]="respectCooldown"> Respect cooldowns</label>
          </div>
          @if (hookResult()) { <p class="hint inline-hint">{{ hookResult() }}</p> }

          <div class="list-head" style="padding: 12px 0 8px; border: 0">
            <h3 style="margin: 0">Recent deliveries</h3>
            <button type="button" class="ghost" (click)="loadDeliveries(h)">Refresh</button>
          </div>
          @if (deliveries().length) {
            <table class="grid deliveries">
              <tr><th>When</th><th>Event</th><th>Status</th><th>Subscriptions</th></tr>
              @for (x of deliveries(); track x.id) {
                <tr class="pick" [class.active]="detail()?.id === x.id" (click)="showDelivery(x.id)">
                  <td>{{ when(x.at) }}</td>
                  <td><code>{{ x.event || '—' }}</code>@if (x.test) { <span class="pill">test</span> }@if (x.replay_of) { <span class="pill">replay</span> }</td>
                  <td><span class="pill" [class]="x.status">{{ label(x.status) }}</span></td>
                  <td>{{ x.subscriptions.length ? x.subscriptions.join(', ') : '—' }}</td>
                </tr>
              }
            </table>
          } @else { <p class="hint inline-hint">No deliveries yet.</p> }

          @if (detail(); as x) {
            <h3>Delivery {{ when(x.at) }} <span class="pill" [class]="x.status">{{ label(x.status) }}</span></h3>
            <p class="hint inline-hint">
              @if (x.topic) { <code>{{ x.topic }}</code> · }
              from {{ x.ip || '?' }} · {{ size(x.body_bytes) }}
              @if (x.delivery_id) { · delivery ID <code>{{ x.delivery_id }}</code> }
              @if (x.detail) { · {{ x.detail }} }
            </p>
            @for (o of x.outcomes; track o.subscription_id) {
              <div class="outcome">
                <strong>{{ o.subscription }}</strong> <span class="pill" [class]="o.result">{{ o.result }}</span>
                @if (o.thread_id) { <button type="button" class="ghost" (click)="open(o.thread_id)">Open conversation</button> }
                @if (o.missing.length) { <p class="hint inline-hint"><span class="pill missing">not in payload</span> {{ o.missing.join(', ') }}</p> }
                @if (o.prompt) { <details><summary>Prompt the agent got</summary><pre class="payload">{{ o.prompt }}</pre></details> }
              </div>
            }
            @if (x.body !== null) {
              <details open><summary>Body</summary><pre class="payload">{{ pretty(x.body) }}</pre></details>
            } @else { <p class="hint inline-hint">Body not kept for {{ label(x.status) }} deliveries.</p> }
            <details><summary>Headers</summary><pre class="payload">{{ headersText(x.headers) }}</pre></details>
            @if (x.body !== null) {
              <div class="actions">
                <button type="button" class="ghost" (click)="replay(h, x.id)">Replay</button>
                <button type="button" class="ghost" (click)="useAsTest(x.event, x.body)">Use as test delivery</button>
                <label class="check"><input type="checkbox" name="rrespect" [(ngModel)]="respectCooldown"> Respect cooldowns</label>
              </div>
            }
          }
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
  previewText = signal<string | null>(null);
  previewMissing = signal<string[]>([]);
  pickable = signal<DeliverySummary[]>([]);
  deliveries = signal<DeliverySummary[]>([]);
  detail = signal<DeliveryDetail | null>(null);
  hookResult = signal('');
  hookTestEvent = 'test';
  hookTestBody = '';
  respectCooldown = false;
  cfg = signal<WebhookSettings | null>(null);

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
    this.api.webhookSettings().subscribe(c => this.cfg.set(c));
  }
  agentName(id: string) { return this.agents().find(a => a.id === id)?.name ?? '—'; }
  open(threadId: string) { this.router.navigate(['/chat', threadId]); }
  when(iso: string) { return new Date(iso).toLocaleString(); }
  copy(text: string) { navigator.clipboard?.writeText(text); }
  label(s: DeliveryStatus) { return STATUS_LABEL[s] ?? s; }
  size(n: number) { return n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(1)} KB` : `${(n / 1048576).toFixed(1)} MB`; }
  pretty(body: string) {
    try { return JSON.stringify(JSON.parse(body), null, 2); } catch { return body; }
  }
  headersText(h: Record<string, string>) { return Object.entries(h).map(([k, v]) => `${k}: ${v}`).join('\n'); }

  private outcomeText(outcomes: EventOutcome[]): string {
    if (!outcomes.length) return 'No subscription matched.';
    return outcomes.map(o => {
      const what = o.result === 'queued' ? 'queued a turn' : 'skipped (cooldown)';
      const miss = o.missing.length ? `; not in payload: ${o.missing.join(', ')}` : '';
      return `${o.subscription}: ${what}${miss}`;
    }).join(' · ') + (outcomes.some(o => o.result === 'queued') ? '. The answer appears in its conversation.' : '');
  }

  edit(x: Subscription | null) {
    this.error.set(''); this.testResult.set(''); this.hookDraft.set(null); this.secret.set('');
    const first = this.enabledAgents().find(a => a.is_default) ?? this.enabledAgents()[0];
    this.draft.set(x ? structuredClone(x) : blankSubscription(first?.id ?? ''));
    if (x) this.testTopic = x.topic_filter.replace('#', 'test').replace(/\+/g, 'x');
    this.previewText.set(null);
    this.loadPickable(x?.topic_filter ?? '');
  }

  loadPickable(filter: string) {
    this.pickable.set([]);
    if (!filter.startsWith('webhook/') && !filter.startsWith('+') && !filter.startsWith('#')) return;
    this.api.recentDeliveries(filter, true, 20).subscribe(r => this.pickable.set(r));
  }

  pickDelivery(id: string) {
    if (!id) return;
    this.api.delivery(id).subscribe(x => {
      this.testTopic = x.topic;
      this.testPayload = x.body ?? '';
      this.preview();
    });
  }

  preview() {
    const d = this.draft();
    if (!d) return;
    this.api.previewTemplate(d.prompt_template, this.testTopic, this.testPayload).subscribe({
      next: r => { this.previewText.set(r.prompt); this.previewMissing.set(r.missing); },
      error: e => { this.previewText.set(null); this.testResult.set(e.error?.detail ?? 'Preview failed'); },
    });
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
        this.testResult.set(this.outcomeText(r.outcomes ?? []));
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
    this.detail.set(null); this.hookResult.set(''); this.deliveries.set([]);
    if (h?.id) this.loadDeliveries(h);
  }

  loadDeliveries(h: Webhook) {
    this.api.hookDeliveries(h.id!).subscribe(r => this.deliveries.set(r));
  }

  showDelivery(id: string) {
    this.api.delivery(id).subscribe(x => this.detail.set(x));
  }

  private afterSend(h: Webhook, r: DeliveryReply, what: string) {
    const n = r.queued_jobs.length;
    this.hookResult.set(`${what} as ${r.topic}: ${this.label(r.status)}` +
      (n ? `, ${n} turn(s) queued.` : r.status === 'no_match' ? '. Add a subscription for it.' : '.'));
    this.loadDeliveries(h);
    this.showDelivery(r.delivery);
    this.load();
  }

  sendHookTest(h: Webhook) {
    this.api.sendHookTest(h.id!, this.hookTestEvent, this.hookTestBody, this.respectCooldown).subscribe({
      next: r => this.afterSend(h, r, 'Sent'),
      error: e => this.hookResult.set(e.error?.detail ?? 'Failed'),
    });
  }

  replay(h: Webhook, id: string) {
    this.api.replayDelivery(id, this.respectCooldown).subscribe({
      next: r => this.afterSend(h, r, 'Replayed'),
      error: e => this.hookResult.set(e.error?.detail ?? 'Replay failed'),
    });
  }

  useAsTest(event: string, body: string) {
    this.hookTestEvent = event || 'test';
    this.hookTestBody = body;
    this.hookResult.set('Copied into Send a test delivery above; edit it and press Send.');
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
    this.loadPickable(d.topic_filter);
  }
}
