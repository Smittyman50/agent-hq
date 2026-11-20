import { Component, OnInit, inject, signal } from '@angular/core';
import { DatePipe } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { RouterLink } from '@angular/router';
import { AgentDef, Api, ChannelLogRow, NoticeRoute, NoticeSettings, NoticeTypes, NotifyChannel, PairingCode } from './api.service';

const KIND_LABEL: Record<string, string> = { telegram: 'Telegram', mqtt: 'MQTT' };

function blankRoute(types: string[] = []): NoticeRoute {
  return { types: [...types], agent_ids: [], min_severity: 'info', enabled: true };
}

/** Where requests and alerts reach you (MQTT, Telegram), which ones, and who may answer. */
@Component({
  selector: 'hq-notifications',
  imports: [FormsModule, DatePipe, RouterLink],
  template: `
  <div class="split">
    <aside class="list">
      <div class="list-head"><h2>Channels</h2>
        <select [ngModel]="''" (ngModelChange)="add($event)" name="addkind">
          <option value="">+ New…</option>
          <option value="telegram">Telegram bot</option>
          <option value="mqtt">MQTT topic</option>
        </select>
      </div>
      @for (c of channels(); track c.id) {
        <button class="row" [class.active]="draft()?.id === c.id" [class.muted]="!c.enabled" (click)="edit(c)">
          <span class="name">{{ c.name }} <span class="pill">{{ kind(c.kind) }}</span>
            @if (c.last_error) { <span class="pill missing">error</span> }</span>
          <span class="sub">
            @if (c.chat) { {{ c.paired?.length || 0 }} paired · {{ c.decisions_allowed ? 'decisions on' : 'link only' }} }
            @else { {{ c.topic || 'agent-hq/notifications' }} }
          </span>
        </button>
      } @empty { <p class="hint">Get requests and alerts on your phone, and answer them there.</p> }

      <div class="list-head"><h2>Settings</h2></div>
      @if (cfg(); as s) {
        <form class="pad" (ngSubmit)="saveCfg()">
          <label>Alerts per agent per hour <small>send_alert calls beyond this are refused; 0 = no cap</small>
            <input type="number" min="0" name="aph" [(ngModel)]="s.alerts_per_hour"></label>
          <label>Follow-ups for (minutes) <small>after you answer from a chat, the conversation's background reports go there too</small>
            <input type="number" min="0" name="ling" [(ngModel)]="s.linger_minutes"></label>
          <div class="actions"><button type="submit" class="ghost">Save</button></div>
          @if (cfgMsg()) { <p class="hint">{{ cfgMsg() }}</p> }
        </form>
      }
    </aside>

    @if (draft(); as d) {
      <form class="editor" (ngSubmit)="save()">
        <h2>{{ d.id ? d.name : 'New ' + kind(d.kind) + ' channel' }}</h2>
        @if (d.last_error) { <p class="error">{{ d.last_error }}</p> }
        <label>Name<input name="name" [(ngModel)]="d.name" required placeholder="Phone"></label>

        @if (d.kind === 'telegram') {
          <label>Bot token <small>from &#64;BotFather (/newbot).
            @if (d.has_token) { Saved{{ d.bot_username ? ' for @' + d.bot_username : '' }}: leave empty to keep it. }
            Checked with Telegram when you save; stored encrypted when AHQ_SECRET_KEY is set.</small>
            <input name="token" type="password" autocomplete="off" [(ngModel)]="d.token"
              [placeholder]="d.has_token ? '••••••••' : '123456789:AA…'"></label>
          <label class="check"><input type="checkbox" name="dec" [(ngModel)]="d.decisions_allowed">
            <span>Decide from the chat <small>buttons and approve/deny replies. Off: requests arrive with a link, and
              you decide them in Agent HQ, behind its sign-in and two-factor.</small></span></label>
          <label>Default agent <small>messages that aren't a reply to a notice go to this agent, in a conversation of the channel's own</small>
            <select name="agent" [(ngModel)]="d.default_agent_id">
              <option [ngValue]="null">None (only replies to notices)</option>
              @for (a of agents(); track a.id) { <option [ngValue]="a.id">{{ a.name }}</option> }
            </select></label>
        } @else {
          <label>Topic <small>empty = AHQ_MQTT_NOTIFY_TOPIC (agent-hq/notifications), on the broker in AHQ_MQTT_URL.
            The payload is the event's JSON, as before.</small>
            <input name="topic" [(ngModel)]="d.topic" placeholder="agent-hq/notifications"></label>
        }
        <label class="check"><input type="checkbox" name="enabled" [(ngModel)]="d.enabled"> Enabled</label>

        <h3>What it gets</h3>
        <p class="hint inline-hint">A notice goes to this channel when any rule matches. Leave types or agents empty for all.</p>
        @for (r of d.routes; track $index; let i = $index) {
          <fieldset class="route">
            <legend>Rule {{ i + 1 }}
              <button type="button" class="ghost small" (click)="d.routes.splice(i, 1)">Remove</button></legend>
            <div class="type-grid">
              @for (g of groups; track g.key) {
                <div>
                  <strong class="small">{{ g.label }}</strong>
                  @for (t of typesIn(g.key); track t.type) {
                    <label class="check"><input type="checkbox" [checked]="r.types.includes(t.type)"
                      (change)="toggle(r.types, t.type)"> {{ t.label }}</label>
                  }
                </div>
              }
            </div>
            <label>Agents <small>none ticked = every agent</small></label>
            <div class="chips">
              @for (a of agents(); track a.id) {
                <label class="check"><input type="checkbox" [checked]="r.agent_ids.includes(a.id!)"
                  (change)="toggle(r.agent_ids, a.id!)"> {{ a.name }}</label>
              }
            </div>
            @if (!r.types.length || r.types.includes('agent_alert')) {
              <label class="narrow">Alerts from severity
                <select [(ngModel)]="r.min_severity" [name]="'sev' + i">
                  <option value="info">info and up</option>
                  <option value="warning">warning and up</option>
                  <option value="critical">critical only</option>
                </select></label>
            }
            <label class="check"><input type="checkbox" [(ngModel)]="r.enabled" [name]="'ren' + i"> Rule on</label>
          </fieldset>
        }
        <div class="actions"><button type="button" class="ghost" (click)="d.routes.push(newRoute(d))">+ Rule</button></div>

        @if (error()) { <p class="error">{{ error() }}</p> }
        <div class="actions">
          <button type="submit">{{ d.id ? 'Save' : 'Create' }}</button>
          @if (d.id) {
            <button type="button" class="ghost" (click)="test(d)">Send a test</button>
            <button type="button" class="danger" (click)="remove(d)">Delete</button>
          }
        </div>
        @if (msg()) { <p class="hint">{{ msg() }}</p> }

        @if (d.id && d.chat) {
          <h3>Who can use it</h3>
          <p class="hint inline-hint">Only paired accounts get notices and can answer. Anyone else who finds the bot
            is ignored (and logged below).</p>
          <table class="grid">
            @for (p of d.paired; track p.user_id) {
              <tr><td>{{ p.name || p.user_id }}</td><td class="muted small">since {{ p.paired_at | date:'d MMM y' }}</td>
                <td><button type="button" class="ghost small" (click)="unpair(d, p.user_id)">Remove</button></td></tr>
            } @empty { <tr><td class="muted">Nobody yet.</td></tr> }
          </table>
          <div class="actions"><button type="button" class="ghost" (click)="pair(d)">Pair an account</button></div>
          @if (code(); as c) {
            <p>Within 10 minutes, open
              @if (c.link) { <a [href]="c.link" target="_blank" rel="noopener">{{ c.link }}</a> and tap Start, or }
              send the bot <code>{{ c.command }}</code>.</p>
          }
          @if (d.default_agent_id && d.chat_thread_id) {
            <p class="hint inline-hint"><a [routerLink]="['/chat', d.chat_thread_id]">Open the chat conversation</a>.
              /new in the chat starts another.</p>
          }
        }

        @if (d.id) {
          <h3>Recent activity</h3>
          <table class="grid deliveries">
            @for (x of log(); track $index) {
              <tr>
                <td class="muted small">{{ x.at | date:'d MMM HH:mm:ss' }}</td>
                <td>{{ x.direction === 'out' ? '→' : '←' }} <span class="pill" [class]="x.status">{{ x.kind }}</span></td>
                <td>{{ x.text }}@if (x.sender) { <span class="muted small"> · {{ x.sender }}</span> }
                  @if (x.outcome) { <span class="muted small"> · {{ x.outcome }}</span> }
                  @if (x.error) { <span class="error small"> {{ x.error }}</span> }</td>
              </tr>
            } @empty { <tr><td class="muted">Nothing yet.</td></tr> }
          </table>
        }
      </form>
    } @else {
      <div class="editor">
        <h2>Notifications</h2>
        <p>Requests for you (tool approvals, sub-agent requests, proposals, goals to confirm, questions) and agents'
          alerts appear in the Requests inbox. Channels send them to you as well:</p>
        <ul>
          <li><strong>Telegram:</strong> make a bot with &#64;BotFather, add it here, then pair your account. Tap the buttons
            to decide, reply to a notice to answer or continue its conversation, or just write to talk to the default agent.</li>
          <li><strong>MQTT:</strong> the event JSON on a topic, for dashboards and automations.</li>
        </ul>
        <p class="hint inline-hint">Agent HQ reads the bot's messages by polling Telegram, so nothing has to reach it from
          the internet. Let agents send alerts under Settings → Agents (Alerts).</p>
      </div>
    }
  </div>`,
  styles: [`
    .pad { padding: 8px 12px; }
    .type-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 4px 16px; }
    .chips { display: flex; flex-wrap: wrap; gap: 0 16px; }
    fieldset.route legend { display: flex; gap: 8px; align-items: center; }
  `],
})
export class NotificationsPage implements OnInit {
  private api = inject(Api);
  channels = signal<NotifyChannel[]>([]);
  agents = signal<AgentDef[]>([]);
  types = signal<NoticeTypes | null>(null);
  cfg = signal<NoticeSettings | null>(null);
  draft = signal<NotifyChannel | null>(null);
  log = signal<ChannelLogRow[]>([]);
  code = signal<PairingCode | null>(null);
  error = signal('');
  msg = signal('');
  cfgMsg = signal('');
  groups = [
    { key: 'request', label: 'Requests' }, { key: 'alert', label: 'Alerts' },
    { key: 'info', label: 'For your information' }, { key: 'decision', label: 'Decision echoes' },
  ];
  typesIn = (g: string) => (this.types()?.types ?? []).filter(t => t.group === g);

  ngOnInit() {
    this.api.noticeTypes().subscribe(t => this.types.set(t));
    this.api.noticeSettings().subscribe(s => this.cfg.set(s));
    this.api.agents().subscribe(a => this.agents.set(a.filter(x => x.enabled)));
    this.reload();
  }

  reload(keep?: string) {
    this.api.channels().subscribe(cs => {
      this.channels.set(cs);
      const id = keep ?? this.draft()?.id;
      const again = cs.find(c => c.id === id);
      if (again) { this.edit(again, false); }
    });
  }

  kind(k: string) { return KIND_LABEL[k] ?? k; }

  add(kind: string) {
    if (!kind) { return; }
    const chat = kind !== 'mqtt';
    this.code.set(null); this.error.set(''); this.msg.set(''); this.log.set([]);
    this.draft.set({ name: chat ? 'Phone' : 'MQTT', kind: kind as NotifyChannel['kind'], enabled: true,
      decisions_allowed: chat, default_agent_id: null, topic: '', token: '',
      routes: [blankRoute(chat ? this.types()?.default_chat_types : [])] });
  }

  edit(c: NotifyChannel, reset = true) {
    if (reset) { this.code.set(null); this.error.set(''); this.msg.set(''); }
    this.draft.set(structuredClone({ ...c, token: '' }));
    if (c.id) { this.api.channelLog(c.id).subscribe(l => this.log.set(l)); }
  }

  newRoute(d: NotifyChannel) { return blankRoute(d.kind === 'mqtt' ? [] : this.types()?.default_chat_types); }

  toggle(list: string[], v: string) {
    const i = list.indexOf(v);
    if (i >= 0) { list.splice(i, 1); } else { list.push(v); }
  }

  save() {
    const d = this.draft()!;
    this.error.set(''); this.msg.set('');
    this.api.saveChannel(d).subscribe({
      next: c => { this.msg.set(d.id ? 'Saved.' : (c.chat ? 'Created. Now pair your account.' : 'Created.')); this.reload(c.id); },
      error: e => this.error.set(e.error?.detail ?? 'Save failed'),
    });
  }

  remove(d: NotifyChannel) {
    if (!confirm(`Delete ${d.name}? Its notices stop at once.`)) { return; }
    this.api.deleteChannel(d.id!).subscribe(() => { this.draft.set(null); this.reload(); });
  }

  test(d: NotifyChannel) {
    this.msg.set(''); this.error.set('');
    this.api.testChannel(d.id!).subscribe({
      next: () => { this.msg.set('Sent.'); this.reload(d.id); },
      error: e => this.error.set(e.error?.detail ?? 'Not sent'),
    });
  }

  pair(d: NotifyChannel) {
    this.api.pairChannel(d.id!).subscribe({
      next: c => { this.code.set(c); this.watchPairing(d.id!, (d.paired ?? []).length); },
      error: e => this.error.set(e.error?.detail ?? 'Could not make a code'),
    });
  }

  /** Refresh until someone pairs (or the code lapses), so the list shows them. */
  private watchPairing(id: string, before: number, tries = 0) {
    if (tries > 120) { return; }
    setTimeout(() => this.api.channels().subscribe(cs => {
      const c = cs.find(x => x.id === id);
      if (c && (c.paired?.length ?? 0) > before) {
        this.channels.set(cs); this.code.set(null); this.msg.set('Paired.');
        if (this.draft()?.id === id) { this.edit(c, false); }
      } else if (c?.pairing_open) { this.watchPairing(id, before, tries + 1); }
    }), 5000);
  }

  unpair(d: NotifyChannel, userId: string) {
    this.api.unpair(d.id!, userId).subscribe(() => this.reload(d.id));
  }

  saveCfg() {
    this.api.saveNoticeSettings(this.cfg()!).subscribe({
      next: s => { this.cfg.set(s); this.cfgMsg.set('Saved.'); },
      error: e => this.cfgMsg.set(e.error?.detail ?? 'Save failed'),
    });
  }
}
