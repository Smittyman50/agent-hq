import { Component, OnDestroy, OnInit, computed, inject, signal } from '@angular/core';
import { DatePipe } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { Router } from '@angular/router';
import { Api, Frequency, ScheduleIn, ScheduleInfo } from './api.service';
import { Avatar } from './avatar';
import { Icon } from './icon';
import { Workspace } from './workspace';

const DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

function browserZone() {
  try { return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'; } catch { return 'UTC'; }
}

function blank(agentId = ''): ScheduleIn & { id?: string } {
  return { name: '', agent_id: agentId, task: '', frequency: 'daily', interval_hours: 1, minute: 0, hour: 8,
           weekdays: [1], day_of_month: 1, cron: '0 8 * * *', timezone: browserZone(), enabled: true,
           skip_if_running: true };
}

/** Agent tasks that run on a schedule: each run is a turn in the schedule's own conversation. */
@Component({
  selector: 'hq-schedules',
  imports: [FormsModule, DatePipe, Avatar, Icon],
  template: `
  <div class="page-wrap">
    <header class="crumbs"><hq-icon name="schedule" [size]="18" /><strong>Scheduler</strong></header>
    <div class="split">
      <aside class="list">
        <div class="list-head">
          <h2>Schedules</h2>
          <button (click)="edit(null)">+ New</button>
        </div>
        @for (s of schedules(); track s.id) {
          <button class="row" [class.active]="draft()?.id === s.id" [class.muted]="!s.enabled" (click)="edit(s)">
            <span class="row-with-avatar"><hq-avatar [agent]="ws.agent(s.agent_id)" [size]="32" />
              <span class="row-text">
                <span class="name">{{ s.name }}
                  @if (s.last_status) { <span class="pill {{ statusClass(s.last_status) }}">{{ s.last_status }}</span> }</span>
                <span class="sub">{{ s.summary }}</span>
                <span class="sub">{{ s.enabled ? (s.next_run_at ? 'Next ' + when(s.next_run_at, s.timezone) : '') : 'Paused' }}</span>
              </span></span>
          </button>
        } @empty {
          <p class="hint">No schedules yet. Give an agent a task to run every hour, day, week or month.</p>
        }
      </aside>

      @if (draft(); as d) {
        <form class="editor" (ngSubmit)="save()">
          <h2>{{ d.id ? 'Edit schedule' : 'New schedule' }}</h2>
          <div class="model-row">
            <label>Name<input name="name" [(ngModel)]="d.name" placeholder="e.g. Proxmox release check" required></label>
            <label>Agent
              <select name="agent" [(ngModel)]="d.agent_id" [class.needs]="!d.agent_id">
                <option value="" disabled>Choose an agent…</option>
                @for (a of ws.enabledAgents(); track a.id) { <option [value]="a.id">{{ a.name }}</option> }
              </select></label>
          </div>
          <label>Task <small>what the agent should do each time, as you'd ask it in a chat</small>
            <textarea name="task" rows="5" [(ngModel)]="d.task"
              placeholder="Check the Proxmox VE and TrueNAS release feeds for anything new since last week. List new versions with dates and links, and flag security fixes."></textarea></label>

          <fieldset>
            <legend>When</legend>
            <div class="seg">
              @for (f of frequencies; track f.value) {
                <button type="button" [class.on]="d.frequency === f.value" (click)="setFrequency(f.value)">{{ f.label }}</button>
              }
            </div>
            <div class="when-row">
              @switch (d.frequency) {
                @case ('hourly') {
                  <label>Every
                    <select name="ih" [(ngModel)]="d.interval_hours" (ngModelChange)="changed()">
                      @for (n of hourSteps; track n) { <option [ngValue]="n">{{ n === 1 ? '1 hour' : n + ' hours' }}</option> }
                    </select></label>
                  <label>at minute
                    <input name="min" type="number" min="0" max="59" [(ngModel)]="d.minute" (ngModelChange)="changed()"></label>
                }
                @case ('weekly') {
                  <div class="days">
                    @for (day of days; track $index) {
                      <button type="button" [class.on]="d.weekdays.includes($index)" (click)="toggleDay($index)">{{ day }}</button>
                    }
                  </div>
                  <label>at <input name="t" type="time" [ngModel]="time(d)" (ngModelChange)="setTime($event)"></label>
                }
                @case ('monthly') {
                  <label>On day
                    <select name="dom" [(ngModel)]="d.day_of_month" (ngModelChange)="changed()">
                      @for (n of monthDays; track n) { <option [ngValue]="n">{{ n }}</option> }
                    </select></label>
                  <label>at <input name="t" type="time" [ngModel]="time(d)" (ngModelChange)="setTime($event)"></label>
                }
                @case ('cron') {
                  <label class="grow">Cron expression <small>minute hour day month weekday, e.g. <code>*/30 8-18 * * 1-5</code></small>
                    <input name="cron" [(ngModel)]="d.cron" (ngModelChange)="changed()" class="mono"></label>
                }
                @default {
                  <label>at <input name="t" type="time" [ngModel]="time(d)" (ngModelChange)="setTime($event)"></label>
                }
              }
              <label>Time zone
                <select name="tz" [(ngModel)]="d.timezone" (ngModelChange)="changed()">
                  @for (z of zones; track z) { <option [value]="z">{{ z }}</option> }
                </select></label>
            </div>
            @if (preview(); as p) {
              <p class="preview"><strong>{{ p.summary }}</strong>
                <span class="muted"> · next: {{ upcoming(p.next, d.timezone) }}</span></p>
            } @else if (previewError()) { <p class="error">{{ previewError() }}</p> }
          </fieldset>

          <div class="checks">
            <label class="check"><input type="checkbox" name="en" [(ngModel)]="d.enabled"> Enabled</label>
            <label class="check"><input type="checkbox" name="skip" [(ngModel)]="d.skip_if_running">
              Skip a run if the previous one is still going</label>
          </div>

          @if (error()) { <p class="error">{{ error() }}</p> }
          <div class="actions">
            <button type="submit">Save</button>
            @if (d.id) {
              <button type="button" class="ghost" (click)="runNow()">Run now</button>
              @if (current()?.thread_id) { <button type="button" class="ghost" (click)="openChat()">Open conversation</button> }
              <span class="spacer"></span>
              <button type="button" class="danger" (click)="remove()">Delete</button>
            }
          </div>

          @if (current(); as c) {
            <h3>Recent runs</h3>
            @for (r of c.runs; track $index) {
              <div class="run">
                <span class="pill {{ statusClass(r.status) }}">{{ r.status }}</span>
                <span>{{ r.at | date:'EEE MMM d, h:mm a' }}</span>
                <span class="muted">{{ r.trigger === 'manual' ? 'run now' : 'scheduled' }}{{ r.late ? ' · ran late (Agent HQ was offline)' : '' }}</span>
                @if (r.reason) { <span class="muted">· {{ r.reason }}</span> }
              </div>
            } @empty { <p class="hint">Hasn't run yet. Results appear in its own conversation, “Scheduled: {{ c.name }}”.</p> }
          }
        </form>
      } @else {
        <div class="empty">Pick a schedule, or create a new one.</div>
      }
    </div>
  </div>
  `,
})
export class SchedulesPage implements OnInit, OnDestroy {
  private api = inject(Api);
  private router = inject(Router);
  ws = inject(Workspace);

  schedules = signal<ScheduleInfo[]>([]);
  draft = signal<(ScheduleIn & { id?: string }) | null>(null);
  current = computed(() => this.schedules().find(s => s.id === this.draft()?.id) ?? null);
  preview = signal<{ summary: string; next: string[] } | null>(null);
  previewError = signal('');
  error = signal('');

  frequencies: { value: Frequency; label: string }[] = [
    { value: 'hourly', label: 'Hourly' }, { value: 'daily', label: 'Daily' }, { value: 'weekly', label: 'Weekly' },
    { value: 'monthly', label: 'Monthly' }, { value: 'cron', label: 'Custom (cron)' }];
  hourSteps = [1, 2, 3, 4, 6, 8, 12];
  monthDays = Array.from({ length: 28 }, (_, i) => i + 1);
  days = DAYS;
  zones = this.zoneList();
  private timer?: ReturnType<typeof setInterval>;
  private previewTimer?: ReturnType<typeof setTimeout>;

  ngOnInit() {
    this.ws.reload();
    this.load();
    this.timer = setInterval(() => this.load(), 15000);  // pick up runs as they happen
  }
  ngOnDestroy() { clearInterval(this.timer); }

  load() { this.api.schedules().subscribe(s => this.schedules.set(s)); }

  edit(s: ScheduleInfo | null) {
    this.error.set('');
    const d = s ? structuredClone({ ...s }) : blank(this.ws.enabledAgents()[0]?.id ?? '');
    if (!this.zones.includes(d.timezone)) this.zones = [d.timezone, ...this.zones];
    this.draft.set(d);
    this.changed();
  }

  setFrequency(f: Frequency) { this.draft.update(d => d && { ...d, frequency: f }); this.changed(); }

  toggleDay(i: number) {
    this.draft.update(d => d && { ...d, weekdays: d.weekdays.includes(i) ? d.weekdays.filter(x => x !== i) : [...d.weekdays, i].sort() });
    this.changed();
  }

  time(d: ScheduleIn) { return `${String(d.hour).padStart(2, '0')}:${String(d.minute).padStart(2, '0')}`; }
  setTime(v: string) {
    const [h, m] = (v || '0:0').split(':').map(Number);
    this.draft.update(d => d && { ...d, hour: h || 0, minute: m || 0 });
    this.changed();
  }

  /** Refresh the plain-English summary and next run times (debounced). */
  changed() {
    clearTimeout(this.previewTimer);
    this.previewTimer = setTimeout(() => {
      const d = this.draft();
      if (!d) return;
      this.api.previewSchedule(d).subscribe({
        next: p => { this.preview.set(p); this.previewError.set(''); },
        error: e => { this.preview.set(null); this.previewError.set(e.error?.detail ?? 'Invalid schedule'); },
      });
    }, 200);
  }

  save() {
    const d = this.draft();
    if (!d) return;
    const body: ScheduleIn = { ...d };
    const req = d.id ? this.api.updateSchedule(d.id, body) : this.api.createSchedule(body);
    req.subscribe({
      next: s => { this.error.set(''); this.load(); this.edit(s); },
      error: e => this.error.set(e.error?.detail ?? 'Save failed'),
    });
  }

  runNow() {
    const id = this.draft()?.id;
    if (!id) return;
    this.api.runSchedule(id).subscribe({
      next: s => { this.error.set(''); this.schedules.update(list => list.map(x => x.id === s.id ? s : x));
                   this.ws.reloadThreads(); },
      error: e => this.error.set(e.error?.detail ?? 'Could not run it'),
    });
  }

  openChat() {
    const t = this.current()?.thread_id;
    if (t) this.router.navigate(['/chat', t]);
  }

  remove() {
    const d = this.draft();
    if (!d?.id || !confirm(`Delete the schedule "${d.name}"? Its conversation and past results stay.`)) return;
    this.api.deleteSchedule(d.id).subscribe(() => { this.draft.set(null); this.load(); });
  }

  statusClass(s: string) {
    return { done: 'done', failed: 'failed', skipped: 'partial', running: 'working', queued: 'queued' }[s] ?? '';
  }

  upcoming(times: string[], tz: string) { return times.map(t => this.when(t, tz)).join(', '); }

  /** A UTC time shown in the schedule's own time zone. */
  when(iso: string, tz: string) {
    try {
      return new Intl.DateTimeFormat(undefined, { timeZone: tz, weekday: 'short', month: 'short', day: 'numeric',
                                                  hour: 'numeric', minute: '2-digit' }).format(new Date(iso));
    } catch { return iso; }
  }

  private zoneList() {
    const own = browserZone();
    let all: string[] = [];
    try { all = (Intl as any).supportedValuesOf('timeZone'); } catch { /* older browsers */ }
    return [own, 'UTC', ...all.filter(z => z !== own && z !== 'UTC')];
  }
}
