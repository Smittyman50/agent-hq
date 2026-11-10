import { Component, OnDestroy, OnInit, inject, signal } from '@angular/core';
import { DatePipe } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { AvatarSettings } from './avatar';
import { Api, QueueInfo, SystemStatus, WorkerInfo, ConversationSettings, ProviderInfo } from './api.service';
import { ModelPicker } from './model-picker';

@Component({
  selector: 'hq-system',
  imports: [DatePipe, FormsModule, ModelPicker],
  template: `
  <div class="editor wide">
    <h2>System</h2>
    @if (status(); as s) {
      <div class="cards">
        <div class="card"><span class="k">Valkey</span><span [class]="s.valkey ? 'ok' : 'bad'">{{ s.valkey ? 'connected' : 'unreachable' }}</span></div>
        <div class="card"><span class="k">MQTT bridge</span>
          <span [class]="s.mqtt === null ? 'muted' : s.mqtt ? 'ok' : 'bad'">{{ s.mqtt === null ? 'disabled' : s.mqtt ? 'connected' : 'reconnecting' }}</span></div>
        <div class="card"><span class="k">Embedded worker</span><span class="muted">{{ s.embedded_worker ? 'on' : 'off' }}</span></div>
      </div>
    }

    <h3>Display</h3>
    <label class="narrow">Animated avatars <small>this browser only</small>
      <select [ngModel]="avatars.mode()" (ngModelChange)="avatars.set($event)">
        <option value="always">Always animate</option>
        <option value="active">Animate only while the agent is working</option>
        <option value="never">Never animate (still image; hover to play)</option>
      </select></label>

    <h3>Long conversations</h3>
    @if (conv(); as c) {
      <p class="hint inline-hint">Each model call has to fit the agent's context window (set per agent under Agents, or
        read from the provider). When a conversation gets too long, its older turns are summarised and the recent ones
        kept word for word; the conversation shows a note with the summary.</p>
      <label class="check"><input type="checkbox" [(ngModel)]="c.compaction"> Summarise older turns when a conversation
        outgrows the window</label>
      <div class="inline narrow-row">
        <label>Summarise at <small>% of the window</small>
          <input type="number" min="30" max="95" [ngModel]="pct(c.trigger_ratio)" (ngModelChange)="c.trigger_ratio = $event / 100"></label>
        <label>Keep word for word <small>% of the room</small>
          <input type="number" min="10" max="80" [ngModel]="pct(c.keep_ratio)" (ngModelChange)="c.keep_ratio = $event / 100"></label>
        <label>Summary length <small>tokens, at most</small>
          <input type="number" min="200" max="8000" step="100" [(ngModel)]="c.summary_max_tokens"></label>
      </div>
      <div class="inline narrow-row">
        <label>Room for tools and the reply <small>tokens</small>
          <input type="number" min="0" max="32000" step="250" [(ngModel)]="c.reserve_tokens"></label>
        <label>Ollama window if unknown <small>tokens</small>
          <input type="number" min="1024" step="1024" [(ngModel)]="c.local_window"></label>
        <label>Cloud window if unknown <small>tokens</small>
          <input type="number" min="4096" step="1024" [(ngModel)]="c.cloud_window"></label>
      </div>
      <div class="narrow-picker">
        <hq-model-picker label="Summary model" [providers]="providers()" [optional]="true" noneLabel="Each agent's own model"
          [(providerId)]="c.provider_id" [(modelName)]="c.model" />
      </div>
      <label class="check"><input type="checkbox" [(ngModel)]="c.warn_overflow"> Warn in the conversation when an agent's
        instructions alone don't fit its window</label>
      <p class="hint inline-hint">Ollama's window is set on the server (OLLAMA_CONTEXT_LENGTH, or num_ctx in a Modelfile;
        otherwise 4K, 32K or 256K depending on GPU memory). Agent HQ can't change it per request, only fit into it.</p>
      <div class="actions"><button type="button" (click)="saveConv()">Save</button> @if (convNote()) { <span class="hint">{{ convNote() }}</span> }</div>
    }

    <h3>Live workers</h3>
    <table class="grid">
      <tr><th>Name</th><th>Pools</th><th>Busy / slots</th><th>Last heartbeat</th></tr>
      @for (w of workers(); track w.name) {
        <tr><td>{{ w.name }}</td><td>{{ w.pools.join(', ') }}</td><td>{{ w.busy }} / {{ w.concurrency }}</td>
          <td>{{ w.seen | date:'HH:mm:ss' }}</td></tr>
      } @empty { <tr><td colspan="4" class="muted">No workers running. Start one with <code>python -m app.worker</code>.</td></tr> }
    </table>

    <h3>Queues</h3>
    <table class="grid">
      <tr><th>Pool</th><th>Entries</th><th>In progress</th><th></th></tr>
      @for (q of queues(); track q.pool) {
        <tr><td>{{ q.pool }}</td><td>{{ q.entries }}</td><td>{{ q.in_progress }}</td>
          <td class="muted">@if (q.entries > 0 && !served(q.pool)) { no live worker serves this pool }</td></tr>
      }
    </table>
  </div>`,
})
export class SystemPage implements OnInit, OnDestroy {
  private api = inject(Api);
  avatars = inject(AvatarSettings);
  private timer?: ReturnType<typeof setInterval>;
  workers = signal<WorkerInfo[]>([]);
  queues = signal<QueueInfo[]>([]);
  status = signal<SystemStatus | null>(null);

  conv = signal<ConversationSettings | null>(null);
  providers = signal<ProviderInfo[]>([]);
  convNote = signal('');
  pct(x: number) { return Math.round(x * 100); }
  saveConv() {
    this.api.saveConversationSettings(this.conv()!).subscribe({
      next: c => { this.conv.set(c); this.convNote.set('Saved.'); },
      error: e => this.convNote.set(e.error?.detail ?? 'Save failed'),
    });
  }

  ngOnInit() {
    this.refresh(); this.timer = setInterval(() => this.refresh(), 5000);
    this.api.conversationSettings().subscribe(c => this.conv.set(c));
    this.api.providers().subscribe(p => this.providers.set(p));
  }
  ngOnDestroy() { clearInterval(this.timer); }

  served(pool: string) { return this.workers().some(w => w.pools.includes(pool)); }

  refresh() {
    this.api.workers().subscribe(w => this.workers.set(w));
    this.api.queues().subscribe(q => this.queues.set(q));
    this.api.status().subscribe(s => this.status.set(s));
  }
}
