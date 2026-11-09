import { Component, OnDestroy, OnInit, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { RouterLink } from '@angular/router';
import { Api, LearningSettings, ProviderInfo, Reflection } from './api.service';
import { ModelPicker } from './model-picker';
import { Workspace } from './workspace';

/** The learning loop: when agents look back at their work, and what came of it. */
@Component({
  selector: 'hq-learning',
  imports: [FormsModule, RouterLink, ModelPicker],
  template: `
  <div class="memory-layout">
    <section class="mem-main">
      <div class="mem-toolbar">
        <h2 class="grow">Reflections</h2>
        <span class="muted small">{{ ranToday() }} in the last 24 h @if (maxPerDay()) { (limit {{ maxPerDay() }}) }</span>
        <button class="ghost" (click)="load()">Refresh</button>
      </div>
      <p class="hint inline-hint">After a job that's worth learning from, the agent looks back at the conversation, its tool
        calls and your ratings, then proposes skills (they arrive in Requests → Skill proposals, or are applied at once
        in Auto mode) and notes lessons in its memory. Rate replies with 👍 / 👎 in a conversation to steer it.</p>
      @for (r of items(); track r.id) {
        <div class="mem-card reflection">
          <div class="mem-meta top">
            <strong>{{ r.agent_name }}</strong>
            <span class="pill {{ pillClass(r.status) }}">{{ r.status }}</span>
            <span class="muted">{{ when(r) }}</span>
            <span class="grow"></span>
            <a [routerLink]="['/chat', r.thread_id]">Open conversation</a>
          </div>
          <div class="reasons">
            @for (why of r.reasons; track $index) { <span class="reason">{{ why }}</span> }
          </div>
          @if (r.summary) { <div class="mem-text">{{ r.summary }}</div> }
          @if (r.error) { <div class="error small">{{ r.error }}</div> }
          @if (r.proposals.length || r.memory_ids.length) {
            <div class="mem-meta">
              @for (p of r.proposals; track p.id) {
                <a class="pill {{ pillClass(p.status) }}" [routerLink]="['/requests/skills']" [queryParams]="{ skill: p.id }"
                  [title]="p.title">{{ p.revision ? 'improve' : 'new skill' }}: {{ p.name }} · {{ p.status }}</a>
              }
              @if (r.memory_ids.length) {
                <a class="pill" routerLink="/settings/memory">{{ r.memory_ids.length }} lesson{{ r.memory_ids.length === 1 ? '' : 's' }} in memory</a>
              }
            </div>
          }
          @if (r.model) { <div class="muted small">on {{ r.model }}</div> }
          @if (r.status === 'waiting' || r.status === 'skipped' || r.status === 'failed') {
            <div class="actions tight">
              <button class="ghost small" (click)="run(r)">Run now</button>
              @if (r.status === 'waiting') { <button class="ghost small" (click)="cancel(r)">Cancel</button> }
            </div>
          }
        </div>
      } @empty {
        <p class="hint">No reflections yet. They start once agents finish work worth a look: rate a reply 👎 with a note,
          correct an agent in the conversation, or let one do some multi-step work.</p>
      }
    </section>

    <aside class="mem-side">
      <h2>Learning settings</h2>
      @if (cfg(); as c) {
        <label>Reflect
          <select [(ngModel)]="c.reflect">
            <option value="signals">When there's a reason (recommended)</option>
            <option value="every_job">After every job</option>
            <option value="off">Never</option>
          </select></label>
        <p class="hint inline-hint">Each agent's learning mode (Settings → Agents → Learning) still decides whether its
          skill ideas wait for you (Propose) or apply at once (Auto). Agents set to Off, and sub-agents, never reflect.</p>
        @if (c.reflect !== 'off') {
          <h3>Reasons to look back</h3>
          <label class="check"><input type="checkbox" [(ngModel)]="c.on_feedback"> You rate a reply 👎 (or 👍 on multi-step work)</label>
          <label class="check"><input type="checkbox" [(ngModel)]="c.on_correction"> You correct the agent in the conversation</label>
          <label class="check"><input type="checkbox" [(ngModel)]="c.on_failure"> The job failed or only partly worked</label>
          <div class="inline">
            <label>Tool calls <small>work with at least this many (0 = off)</small>
              <input type="number" min="0" max="100" [(ngModel)]="c.min_tool_calls"></label>
          </div>
          <h3>Timing and limits</h3>
          <div class="inline">
            <label>Wait after a job <small>minutes, so your follow-ups count</small>
              <input type="number" min="0" max="240" [(ngModel)]="c.wait_minutes"></label>
            <label>Wait after a rating <small>minutes</small>
              <input type="number" min="0" max="60" [(ngModel)]="c.feedback_wait_minutes"></label>
          </div>
          <div class="inline">
            <label>Most per day <small>0 = no limit</small>
              <input type="number" min="0" max="1000" [(ngModel)]="c.max_per_day"></label>
          </div>
          <h3>Model</h3>
          <p class="hint inline-hint">Leave empty to reflect on each agent's own model. A stronger model here can write better
            skills for small local agents.</p>
          <hq-model-picker label="Reflection model" [providers]="providers()" [optional]="true" noneLabel="Each agent's own model"
            [(providerId)]="c.provider_id" [(modelName)]="c.model" />
          <h3>Lessons</h3>
          <label class="check"><input type="checkbox" [(ngModel)]="c.lessons_to_memory"> Save lessons to memory (agents with memory on)</label>
        }
        <div class="actions"><button (click)="save()">Save settings</button></div>
        @if (notice()) { <p class="hint">{{ notice() }}</p> }
      }
    </aside>
  </div>`,
})
export class LearningPage implements OnInit, OnDestroy {
  private api = inject(Api);
  ws = inject(Workspace);
  items = signal<Reflection[]>([]);
  ranToday = signal(0);
  maxPerDay = signal(0);
  cfg = signal<LearningSettings | null>(null);
  providers = signal<ProviderInfo[]>([]);
  notice = signal('');
  private timer?: ReturnType<typeof setInterval>;

  ngOnInit() {
    this.load();
    this.api.learningSettings().subscribe(c => this.cfg.set(c));
    this.api.providers().subscribe(p => this.providers.set(p));
    this.timer = setInterval(() => this.load(), 15000);
  }
  ngOnDestroy() { clearInterval(this.timer); }

  load() {
    this.api.reflections(100).subscribe(r => {
      this.items.set(r.reflections); this.ranToday.set(r.ran_today); this.maxPerDay.set(r.max_per_day);
    });
  }

  save() {
    this.notice.set('');
    this.api.saveLearningSettings(this.cfg()!).subscribe({
      next: c => { this.cfg.set(c); this.notice.set('Saved.'); this.load(); },
      error: e => this.notice.set(e.error?.detail ?? 'Save failed'),
    });
  }

  run(r: Reflection) {
    this.api.runReflection(r.id).subscribe({
      next: () => { this.notice.set(''); this.patch(r.id, { status: 'running' }); setTimeout(() => this.load(), 3000); },
      error: e => alert(e.error?.detail ?? 'Could not start it'),
    });
  }

  cancel(r: Reflection) { this.api.cancelReflection(r.id).subscribe(() => this.load()); }

  private patch(id: string, x: Partial<Reflection>) {
    this.items.update(list => list.map(r => r.id === id ? { ...r, ...x } : r));
  }

  pillClass(status: string) {
    return ({ done: 'done', approved: 'done', applied: 'done', failed: 'failed', rejected: 'failed',
              running: 'working', waiting: 'queued', pending: 'live', skipped: '' } as Record<string, string>)[status] ?? '';
  }

  when(r: Reflection) {
    const d = (s: string | null) => s ? new Date(s).toLocaleString([], { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }) : '';
    if (r.status === 'waiting') return `due ${d(r.due_at)}`;
    return d(r.finished_at || r.started_at || r.created_at);
  }
}
