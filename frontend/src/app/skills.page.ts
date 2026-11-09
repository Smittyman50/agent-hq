import { Component, OnInit, computed, inject, signal } from '@angular/core';
import { DatePipe } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { Api, Skill, SkillVersion } from './api.service';
import { Markdown } from './markdown';
import { Workspace } from './workspace';

function blankSkill(): Skill {
  return { name: '', title: '', when_to_use: '', body: '', tools: [], script: null, scope: 'team', agent_id: null };
}

/** Written-down know-how: what agents can load and follow, with history and rollback. */
@Component({
  selector: 'hq-skills',
  imports: [FormsModule, DatePipe, Markdown],
  template: `
  <div class="split">
    <aside class="list">
      <div class="list-head"><h2>Skills</h2><button (click)="open(null)">+ New</button></div>
      <label class="check small list-filter"><input type="checkbox" [ngModel]="showRetired()" (ngModelChange)="showRetired.set($event); load()"> Show retired</label>
      @for (k of skills(); track k.id) {
        <button class="row" [class.active]="draft()?.id === k.id" [class.muted]="k.status === 'retired'" (click)="open(k)">
          <span class="name">{{ k.title }}</span>
          <span class="sub"><code>{{ k.name }}</code> · v{{ k.version }}
            · {{ k.stats?.uses ? (k.stats!.uses + ' use' + (k.stats!.uses === 1 ? '' : 's')) : 'unused' }}
            @if (k.success_rate !== null && k.success_rate !== undefined) { · {{ (k.success_rate * 100).toFixed(0) }}% done }
            @if (k.stats?.up || k.stats?.down) { · 👍 {{ k.stats!.up }} 👎 {{ k.stats!.down }} }
            @if (k.scope === 'agent') { · {{ k.agent }} only }</span>
        </button>
      } @empty { <p class="hint">No skills yet. Agents propose them as they learn (Requests inbox), or write one here.</p> }
    </aside>

    @if (draft(); as d) {
      <form class="editor wide" (ngSubmit)="save()">
        <h2>{{ d.id ? d.title : 'New skill' }} @if (d.id) { <span class="pill">v{{ d.version }}</span> }
          @if (d.status === 'retired') { <span class="pill failed">retired</span> }</h2>
        @if (d.id && d.stats) {
          <p class="muted small">Used {{ d.stats.uses }} time(s): {{ d.stats.done }} done, {{ d.stats.partial }} partial,
            {{ d.stats.failed }} failed @if (d.stats.up || d.stats.down) { · you rated 👍 {{ d.stats.up }} 👎 {{ d.stats.down }} }
            @if (d.stats.last_used_at) { · last {{ d.stats.last_used_at | date:'MMM d, h:mm a' }} }
            · created by {{ d.created_by }}</p>
        }
        <div class="limit-grid two">
          <label>Name <small>lowercase_with_underscores</small><input name="name" [(ngModel)]="d.name" required></label>
          <label>Title<input name="title" [(ngModel)]="d.title" required></label>
        </div>
        <label>When to use <small>agents see this line to decide whether to load it</small>
          <textarea name="when" rows="2" [(ngModel)]="d.when_to_use"></textarea></label>
        <label>Procedure <small>markdown: the steps, what to check, the pitfalls</small>
          <textarea name="body" rows="14" class="mono" [(ngModel)]="d.body"></textarea></label>
        <div class="limit-grid two">
          <label>Tools it relies on <small>comma-separated, optional</small>
            <input name="tools" [ngModel]="d.tools.join(', ')" (ngModelChange)="d.tools = splitList($event)"></label>
          <label>Workbench script <small>a saved script's name, optional</small>
            <input name="script" [(ngModel)]="d.script"></label>
        </div>
        <div class="limit-grid two">
          <label>Who can use it
            <select name="scope" [(ngModel)]="d.scope">
              <option value="team">Every agent</option><option value="agent">One agent</option></select></label>
          @if (d.scope === 'agent') {
            <label>Agent
              <select name="agent" [(ngModel)]="d.agent_id">
                @for (a of ws.enabledAgents(); track a.id) { <option [value]="a.id">{{ a.name }}</option> }</select></label>
          }
        </div>
        @if (d.id) { <label>What changed <small>kept in the history</small><input name="note" [(ngModel)]="d.note" placeholder="e.g. added the verify-job check"></label> }
        @if (error()) { <p class="error">{{ error() }}</p> }
        @if (msg()) { <p class="hint">{{ msg() }}</p> }
        <div class="actions">
          <button type="submit">{{ d.id ? 'Save as v' + ((d.version ?? 0) + 1) : 'Create' }}</button>
          @if (d.id) {
            @if (d.status === 'retired') { <button type="button" class="ghost" (click)="setStatus(d, 'active')">Restore</button> }
            @else { <button type="button" class="danger" (click)="setStatus(d, 'retired')">Retire</button> }
          }
        </div>

        @if (d.versions?.length) {
          <h3>History</h3>
          <table class="grid">
            <tr><th>Version</th><th>When</th><th>By</th><th>Note</th><th></th></tr>
            @for (v of d.versions; track v.id) {
              <tr [class.current]="v.version === d.version"><td>v{{ v.version }}</td><td>{{ v.created_at | date:'MMM d, h:mm a' }}</td>
                <td>{{ v.created_by }}</td><td class="muted">{{ v.note }}</td>
                <td><button type="button" class="ghost small" (click)="preview.set(preview()?.id === v.id ? null : v)">{{ preview()?.id === v.id ? 'Hide' : 'View' }}</button>
                  @if (v.version !== d.version) { <button type="button" class="ghost small" (click)="rollback(d, v.version)">Restore this</button> }</td></tr>
            }
          </table>
          @if (preview(); as v) {
            <div class="skill-body"><b>v{{ v.version }}: {{ v.title }}</b><div class="muted small">Use when: {{ v.when_to_use }}</div><hq-md [text]="v.body" /></div>
          }
        }
      </form>
    } @else { <div class="empty">Select a skill, or write a new one.</div> }
  </div>`,
})
export class SkillsPage implements OnInit {
  private api = inject(Api);
  ws = inject(Workspace);
  skills = signal<Skill[]>([]);
  draft = signal<Skill | null>(null);
  preview = signal<SkillVersion | null>(null);
  showRetired = signal(false);
  error = signal('');
  msg = signal('');

  ngOnInit() { this.load(); }
  load() { this.api.skills(this.showRetired()).subscribe(k => this.skills.set(k)); }

  open(k: Skill | null) {
    this.error.set(''); this.msg.set(''); this.preview.set(null);
    if (!k) { this.draft.set(blankSkill()); return; }
    this.api.skill(k.id!).subscribe(full => this.draft.set({ ...full, note: '' }));
  }

  splitList(v: string) { return v.split(',').map(x => x.trim()).filter(Boolean); }

  save() {
    this.error.set('');
    this.api.saveSkill(this.draft()!).subscribe({
      next: k => { this.draft.set({ ...k, note: '' }); this.msg.set(`Saved as v${k.version}.`); this.load(); },
      error: e => this.error.set(e.error?.detail ?? 'Save failed'),
    });
  }

  setStatus(d: Skill, status: 'active' | 'retired') {
    this.api.setSkillStatus(d.id!, status).subscribe(k => { this.draft.set({ ...k, note: '' }); this.load(); });
  }

  rollback(d: Skill, version: number) {
    if (!confirm(`Restore v${version}? It becomes a new version; nothing is lost.`)) return;
    this.api.rollbackSkill(d.id!, version).subscribe(k => {
      this.draft.set({ ...k, note: '' }); this.msg.set(`Restored v${version} as v${k.version}.`); this.load();
    });
  }
}
