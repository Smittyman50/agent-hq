import { Component, OnDestroy, OnInit, inject, signal } from '@angular/core';
import { DatePipe } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { AvatarSettings } from './avatar';
import { Api, QueueInfo, SystemStatus, WorkerInfo } from './api.service';

@Component({
  selector: 'hq-system',
  imports: [DatePipe, FormsModule],
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

  ngOnInit() { this.refresh(); this.timer = setInterval(() => this.refresh(), 5000); }
  ngOnDestroy() { clearInterval(this.timer); }

  served(pool: string) { return this.workers().some(w => w.pools.includes(pool)); }

  refresh() {
    this.api.workers().subscribe(w => this.workers.set(w));
    this.api.queues().subscribe(q => this.queues.set(q));
    this.api.status().subscribe(s => this.status.set(s));
  }
}
