import { Component, OnDestroy, OnInit, computed, inject, signal } from '@angular/core';
import { DatePipe } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { RouterLink } from '@angular/router';
import { Api, ProviderInfo, Skill, SkillImport, SkillReview, SkillUpkeep, SkillVersion } from './api.service';
import { Markdown } from './markdown';
import { ModelPicker } from './model-picker';
import { Workspace } from './workspace';

function blankSkill(): Skill {
  return { name: '', title: '', when_to_use: '', body: '', tools: [], script: null, scope: 'team', agent_id: null };
}

type View = 'active' | 'stale' | 'archived';
type Pane = 'skill' | 'import' | 'upkeep';
const DAYS = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];

/** Written-down know-how: what agents can load and follow, with history and rollback;
 *  Agent Skills (SKILL.md) import and export; usage, staleness, the skill review, archiving. */
@Component({
  selector: 'hq-skills',
  imports: [FormsModule, DatePipe, Markdown, ModelPicker, RouterLink],
  template: `
  <div class="split">
    <aside class="list">
      <div class="list-head"><h2>Skills</h2>
        <span class="inline">
          <button class="ghost small" (click)="openImport()" title="Import Agent Skills (SKILL.md)">Import</button>
          <button (click)="open(null)">+ New</button>
        </span>
      </div>
      <div class="list-filter">
        <div class="seg">
          <button [class.on]="view() === 'active'" (click)="setView('active')">Active</button>
          <button [class.on]="view() === 'stale'" (click)="setView('stale')">Stale @if (staleCount()) { ({{ staleCount() }}) }</button>
          <button [class.on]="view() === 'archived'" (click)="setView('archived')">Archived</button>
        </div>
        <div class="small"><a href="" (click)="$event.preventDefault(); openUpkeep()">Upkeep &amp; review</a>
          · <a [href]="api.skillsExportUrl(view() === 'archived' ? 'archived' : 'active')" download>Export all</a></div>
      </div>
      @for (k of shown(); track k.id) {
        <button class="row" [class.active]="pane() === 'skill' && draft()?.id === k.id" [class.muted]="k.status === 'archived'" (click)="open(k)">
          <span class="name">{{ k.title }} @if (k.stale?.length) { <span class="pill live" [title]="k.stale!.join('; ')">stale</span> }</span>
          <span class="sub wrap"><code>{{ k.name }}</code> · v{{ k.version }}
            · {{ k.stats?.uses ? (k.stats!.uses + ' use' + (k.stats!.uses === 1 ? '' : 's')) : 'unused' }}
            @if (k.stats?.last_used_at) { · last {{ k.stats!.last_used_at | date:'MMM d' }} }
            @if (k.success_rate !== null && k.success_rate !== undefined) { · {{ (k.success_rate * 100).toFixed(0) }}% done }
            @if (k.stats?.up || k.stats?.down) { · 👍 {{ k.stats!.up }} 👎 {{ k.stats!.down }} }
            @if (k.files?.length) { · {{ k.files!.length }} file{{ k.files!.length === 1 ? '' : 's' }} }
            @if (k.scope === 'agent') { · {{ k.agent }} only }
            @if (k.status === 'archived' && k.archived_at) { · archived {{ k.archived_at | date:'MMM d' }} }</span>
        </button>
      } @empty {
        <p class="hint">@switch (view()) {
          @case ('stale') { No stale skills: everything's been used lately and rated well enough. }
          @case ('archived') { No archived skills. }
          @default { No skills yet. Agents propose them as they learn (Requests inbox), or write or import one here. }
        }</p>
      }
    </aside>

    @if (pane() === 'import') {
      <div class="editor wide">
        <h2>Import skills</h2>
        <p class="hint inline-hint">Agent Skills format (<a href="https://agentskills.io" target="_blank" rel="noopener">agentskills.io</a>):
          a folder with a <code>SKILL.md</code> and any scripts, references or assets, as used by Hermes and the Skills Hub.
          Every skill found arrives as a proposal in Requests → Skill proposals and runs through your test cases like any other
          skill change. Nothing is added until you approve it.</p>
        <div class="seg">
          @for (m of importModes; track m.value) {
            <button type="button" [class.on]="importMode() === m.value" (click)="importMode.set(m.value)">{{ m.label }}</button>
          }
        </div>
        @switch (importMode()) {
          @case ('files') {
            <label>A SKILL.md or a zip <small>a zip may hold one skill or many</small>
              <input type="file" multiple accept=".md,.zip,text/markdown,application/zip" (change)="pickFiles($event)"></label>
            <label>Or a skill's folder <small>the folder with SKILL.md in it, or a folder of skills</small>
              <input type="file" webkitdirectory multiple (change)="pickFiles($event)"></label>
            @if (picked().length) { <p class="muted small">{{ picked().length }} file(s) ready: {{ pickedLabel() }}</p> }
          }
          @case ('text') {
            <label>SKILL.md <small>paste it, frontmatter included</small>
              <textarea rows="14" class="mono" [(ngModel)]="importText" placeholder="---&#10;name: my-skill&#10;description: What it does and when to use it.&#10;---&#10;&#10;# My skill&#10;&#10;The steps..."></textarea></label>
          }
          @case ('git') {
            <label>Repository URL <small>or a link to a folder in it (GitHub, Gitea, GitLab)</small>
              <input [(ngModel)]="git.url" placeholder="https://github.com/org/skills or https://gitgard/me/skills/src/branch/main/ops"></label>
            <div class="limit-grid two">
              <label>Branch <small>optional</small><input [(ngModel)]="git.branch" placeholder="default branch"></label>
              <label>Path <small>optional: only skills under it</small><input [(ngModel)]="git.path" placeholder="skills/"></label>
            </div>
            <div class="limit-grid two">
              <label>Username <small>private repos</small><input [(ngModel)]="git.username" autocomplete="off"></label>
              <label>Token <small>used for this clone only, not kept</small><input type="password" [(ngModel)]="git.token" autocomplete="new-password"></label>
            </div>
          }
        }
        <div class="limit-grid two">
          <label>New skills are for
            <select [(ngModel)]="importScope"><option value="team">Every agent</option><option value="agent">One agent</option></select></label>
          @if (importScope === 'agent') {
            <label>Agent
              <select [(ngModel)]="importAgent">
                @for (a of ws.enabledAgents(); track a.id) { <option [value]="a.id">{{ a.name }}</option> }</select></label>
          }
        </div>
        @if (error()) { <p class="error">{{ error() }}</p> }
        <div class="actions"><button type="button" (click)="runImport()" [disabled]="busy()">{{ busy() ? 'Importing…' : 'Import' }}</button></div>

        @if (imported(); as r) {
          <h3>Found {{ r.found }} skill{{ r.found === 1 ? '' : 's' }}</h3>
          <table class="grid">
            <tr><th>Skill</th><th>From</th><th>Result</th></tr>
            @for (x of r.results; track $index) {
              <tr><td><code>{{ x.name }}</code><div class="muted small">{{ x.title }}</div></td>
                <td class="muted small">{{ x.where }}</td>
                <td><span class="pill {{ x.status === 'proposed' ? 'live' : x.status === 'unchanged' ? 'done' : 'failed' }}">{{ x.status }}</span>
                  {{ x.summary }}
                  @if (x.status === 'proposed') { @if (x.tests) { · testing against {{ x.tests }} case{{ x.tests === 1 ? '' : 's' }} }
                    · <a [routerLink]="['/requests/skills']" [queryParams]="{ skill: x.proposal_id }">review</a> }
                  @if (x.notes.length) { <div class="muted small">{{ x.notes.join('; ') }}</div> }</td></tr>
            }
          </table>
          @for (p of r.problems; track $index) { <p class="warn-line small">⚠ {{ p }}</p> }
        }
      </div>
    } @else if (pane() === 'upkeep') {
      <div class="editor wide">
        <h2>Upkeep</h2>
        @if (upkeep(); as u) {
          <h3>Stale skills</h3>
          <p class="hint inline-hint">Flagged on the list; nothing happens to them by itself. Archive one by hand, or let the review suggest it.</p>
          <div class="limit-grid two">
            <label>Unused for <small>days (0 = never stale by age)</small><input type="number" min="0" [(ngModel)]="u.settings.stale_after_days"></label>
            <label>Poorly rated at <small>share of 👎 among ratings</small>
              <select [(ngModel)]="u.settings.poor_share">
                @for (v of [0.34, 0.5, 0.67, 0.75]; track v) { <option [ngValue]="v">{{ (v * 100).toFixed(0) }}% or more</option> }</select></label>
          </div>
          <label>Ratings needed first <small>a skill with fewer 👍/👎 isn't judged on them</small>
            <input type="number" min="1" [(ngModel)]="u.settings.min_ratings"></label>

          <h3>Skill review</h3>
          <p class="hint inline-hint">A model looks over every active skill, how much it's used and how it's rated, and proposes merges,
            rewrites and archiving. Each idea is a proposal you approve in Requests → Skill proposals, tested by the evals first.</p>
          <div class="limit-grid two">
            <label>Runs
              <select [(ngModel)]="u.settings.review">
                <option value="off">Off (Run now only)</option><option value="daily">Every day</option>
                <option value="weekly">Every week</option><option value="monthly">Every month</option></select></label>
            <label>At <small>{{ u.timezone }}</small>
              <select [(ngModel)]="u.settings.review_hour">
                @for (h of hours; track h) { <option [ngValue]="h">{{ clock(h) }}</option> }</select></label>
          </div>
          @if (u.settings.review === 'weekly') {
            <label>On <select [(ngModel)]="u.settings.review_weekday">
              @for (d of days; track $index) { <option [ngValue]="$index">{{ d }}</option> }</select></label>
          }
          @if (u.settings.review === 'monthly') {
            <label>Day of the month <input type="number" min="1" max="28" [(ngModel)]="u.settings.review_day"></label>
          }
          <hq-model-picker label="Review model" [providers]="providers()" [optional]="true" noneLabel="The evals judge (or the first agent's model)"
            [(providerId)]="u.settings.provider_id" [(modelName)]="u.settings.model" />
          <label>Suggestions per review <input type="number" min="1" max="20" [(ngModel)]="u.settings.max_suggestions"></label>
          <p class="muted small">
            @if (u.next_review_at) { Next review {{ u.next_review_at | date:'EEE MMM d, h:mm a' }}. }
            @if (u.last_review_at) { Last ran {{ u.last_review_at | date:'MMM d, h:mm a' }}. }</p>
          @if (error()) { <p class="error">{{ error() }}</p> }
          @if (msg()) { <p class="hint">{{ msg() }}</p> }
          <div class="actions">
            <button type="button" (click)="saveUpkeep()">Save</button>
            <button type="button" class="ghost" (click)="runReview()" [disabled]="reviewRunning()">{{ reviewRunning() ? 'Reviewing…' : 'Run review now' }}</button>
          </div>

          @if (reviews().length) {
            <h3>Recent reviews</h3>
            @for (r of reviews(); track r.id) {
              <div class="mem-card">
                <div class="mem-meta top">
                  <span class="pill {{ r.status === 'done' ? 'done' : r.status === 'failed' ? 'failed' : 'working' }}">{{ r.status }}</span>
                  <span class="muted">{{ r.started_at | date:'MMM d, h:mm a' }} · {{ r.trigger }}</span>
                  @if (r.model) { <span class="muted small">{{ r.model }}</span> }
                </div>
                @if (r.summary) { <div class="mem-text">{{ r.summary }}</div> }
                @if (r.error) { <div class="error small">{{ r.error }}</div> }
                @if (r.proposals.length) {
                  <div class="mem-meta">
                    @for (p of r.proposals; track p.id) {
                      <a class="pill {{ p.status === 'approved' ? 'done' : p.status === 'rejected' ? 'failed' : 'live' }}"
                        [routerLink]="['/requests/skills']" [queryParams]="{ skill: p.id }">{{ actionLabel(p.action) }} {{ p.name }}: {{ p.status }}</a>
                    }
                  </div>
                }
              </div>
            }
          }
        }
      </div>
    } @else if (draft(); as d) {
      <form class="editor wide" (ngSubmit)="save()">
        <h2>{{ d.id ? d.title : 'New skill' }} @if (d.id) { <span class="pill">v{{ d.version }}</span> }
          @if (d.status === 'archived') { <span class="pill failed">archived</span> }
          @if (d.stale?.length) { <span class="pill live">stale</span> }</h2>
        @if (d.id && d.stats) {
          <p class="muted small">Used {{ d.stats.uses }} time(s): {{ d.stats.done }} done, {{ d.stats.partial }} partial,
            {{ d.stats.failed }} failed @if (d.stats.up || d.stats.down) { · you rated 👍 {{ d.stats.up }} 👎 {{ d.stats.down }} }
            @if (d.stats.last_used_at) { · last {{ d.stats.last_used_at | date:'MMM d, h:mm a' }} }
            · created by {{ d.created_by }} @if (d.origin) { · imported from <code>{{ d.origin }}</code> }</p>
        }
        @if (d.stale?.length) { <div class="warn-line small">⚠ Stale: {{ d.stale!.join('; ') }}.</div> }
        @if (d.status === 'archived') {
          <div class="warn-line small">Archived {{ d.archived_at | date:'MMM d, h:mm a' }}@if (d.archived_reason) {: {{ d.archived_reason }}}.
            Agents don't see it until you restore it.</div>
        }
        <div class="limit-grid two">
          <label>Name <small>lowercase_with_underscores</small><input name="name" [(ngModel)]="d.name" required></label>
          <label>Title<input name="title" [(ngModel)]="d.title" required></label>
        </div>
        <label>When to use <small>agents see this line to decide whether to load it (the SKILL.md description)</small>
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
        @if (d.files?.length) {
          <h3>Files</h3>
          <p class="hint inline-hint">They came with the skill; agents read them with <code>read_skill_file</code>. Change them by importing a new version.</p>
          <table class="grid">
            @for (f of d.files; track f.path) {
              <tr><td><code>{{ f.path }}</code></td><td class="muted small">{{ size(f.bytes) }}{{ f.text ? '' : ' · binary' }}</td>
                <td>@if (f.text) { <button type="button" class="ghost small" (click)="viewFile(d, f.path)">{{ fileView()?.path === f.path ? 'Hide' : 'View' }}</button> }</td></tr>
            }
          </table>
          @if (fileView(); as fv) { <pre class="diff">{{ fv.content ?? fv.note }}</pre> }
        }
        @if (d.id) { <label>What changed <small>kept in the history</small><input name="note" [(ngModel)]="d.note" placeholder="e.g. added the verify-job check"></label> }
        @if (error()) { <p class="error">{{ error() }}</p> }
        @if (msg()) { <p class="hint">{{ msg() }}</p> }
        <div class="actions">
          <button type="submit">{{ d.id ? 'Save as v' + ((d.version ?? 0) + 1) : 'Create' }}</button>
          @if (d.id) {
            <a class="file ghost" [href]="api.skillExportUrl(d.id!)" download>Export {{ d.files?.length ? '(zip)' : 'SKILL.md' }}</a>
            @if (d.status === 'archived') { <button type="button" class="ghost" (click)="restore(d)">Restore</button> }
            @else { <button type="button" class="danger" (click)="archive(d)">Archive</button> }
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
    } @else { <div class="empty">Select a skill, write a new one, or import some.</div> }
  </div>`,
})
export class SkillsPage implements OnInit, OnDestroy {
  api = inject(Api);
  ws = inject(Workspace);
  skills = signal<Skill[]>([]);
  view = signal<View>('active');
  pane = signal<Pane>('skill');
  draft = signal<Skill | null>(null);
  preview = signal<SkillVersion | null>(null);
  fileView = signal<{ path: string; content?: string; note?: string } | null>(null);
  error = signal('');
  msg = signal('');
  busy = signal(false);

  shown = computed(() => this.view() === 'stale' ? this.skills().filter(k => k.stale?.length) : this.skills());
  staleCount = signal(0);

  // import
  readonly importModes: { value: 'files' | 'text' | 'git'; label: string }[] = [
    { value: 'files', label: 'File, folder or zip' }, { value: 'text', label: 'Paste' }, { value: 'git', label: 'Git repository' }];
  importMode = signal<'files' | 'text' | 'git'>('files');
  picked = signal<{ file: File; path: string }[]>([]);
  pickedLabel = computed(() => {
    const p = this.picked().map(f => f.path);
    return p.length <= 3 ? p.join(', ') : `${p.slice(0, 3).join(', ')} and ${p.length - 3} more`;
  });
  importText = '';
  git = { url: '', branch: '', path: '', username: '', token: '' };
  importScope: 'team' | 'agent' = 'team';
  importAgent: string | null = null;
  imported = signal<SkillImport | null>(null);

  // upkeep
  upkeep = signal<SkillUpkeep | null>(null);
  reviews = signal<SkillReview[]>([]);
  providers = signal<ProviderInfo[]>([]);
  reviewRunning = computed(() => this.reviews().some(r => r.status === 'running'));
  readonly hours = Array.from({ length: 24 }, (_, i) => i);
  readonly days = DAYS;
  private timer?: ReturnType<typeof setInterval>;

  ngOnInit() { this.load(); }
  ngOnDestroy() { clearInterval(this.timer); }

  load() {
    const status = this.view() === 'archived' ? 'archived' : 'active';
    this.api.skills(status).subscribe(k => {
      this.skills.set(k);
      if (status === 'active') this.staleCount.set(k.filter(x => x.stale?.length).length);
    });
  }

  setView(v: View) { this.view.set(v); this.load(); }

  private reset() { this.error.set(''); this.msg.set(''); this.preview.set(null); this.fileView.set(null); }

  open(k: Skill | null) {
    this.reset();
    this.pane.set('skill');
    if (!k) { this.draft.set(blankSkill()); return; }
    this.api.skill(k.id!).subscribe(full => this.draft.set({ ...full, note: '' }));
  }

  openImport() { this.reset(); this.imported.set(null); this.pane.set('import'); }

  openUpkeep() {
    this.reset();
    this.pane.set('upkeep');
    this.api.skillUpkeep().subscribe(u => this.upkeep.set(u));
    if (!this.providers().length) this.api.providers().subscribe(p => this.providers.set(p));
    this.loadReviews();
  }

  loadReviews() {
    this.api.skillReviews().subscribe(r => {
      this.reviews.set(r);
      clearInterval(this.timer);
      if (r.some(x => x.status === 'running')) this.timer = setInterval(() => this.loadReviews(), 4000);
    });
  }

  splitList(v: string) { return v.split(',').map(x => x.trim()).filter(Boolean); }
  size(n: number) { return n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(1)} KB` : `${(n / 1048576).toFixed(1)} MB`; }
  clock(h: number) { return `${h % 12 || 12}:00 ${h < 12 ? 'AM' : 'PM'}`; }
  actionLabel(a: string) { return a === 'archive' ? 'archive' : a === 'merge' ? 'merge into' : 'rewrite'; }

  save() {
    this.error.set('');
    this.api.saveSkill(this.draft()!).subscribe({
      next: k => { this.draft.set({ ...k, note: '' }); this.msg.set(`Saved as v${k.version}.`); this.load(); },
      error: e => this.error.set(e.error?.detail ?? 'Save failed'),
    });
  }

  archive(d: Skill) {
    const reason = prompt(`Archive ${d.name}? Agents stop seeing it; you can restore it any time.\nReason (optional):`, d.stale?.join('; ') ?? '');
    if (reason === null) return;
    this.api.archiveSkill(d.id!, reason).subscribe(k => { this.draft.set({ ...k, note: '' }); this.msg.set('Archived.'); this.load(); });
  }

  restore(d: Skill) {
    this.api.restoreSkill(d.id!).subscribe(k => { this.draft.set({ ...k, note: '' }); this.msg.set('Restored: agents can use it again.'); this.load(); });
  }

  viewFile(d: Skill, path: string) {
    if (this.fileView()?.path === path) { this.fileView.set(null); return; }
    this.api.skillFile(d.id!, path).subscribe({ next: f => this.fileView.set(f), error: () => this.fileView.set({ path, note: 'Could not load it.' }) });
  }

  rollback(d: Skill, version: number) {
    if (!confirm(`Restore v${version}? It becomes a new version; nothing is lost.`)) return;
    this.api.rollbackSkill(d.id!, version).subscribe(k => {
      this.draft.set({ ...k, note: '' }); this.msg.set(`Restored v${version} as v${k.version}.`); this.load();
    });
  }

  pickFiles(ev: Event) {
    const input = ev.target as HTMLInputElement;
    const files = Array.from(input.files ?? []);
    this.picked.set(files.map(f => ({ file: f, path: (f as File & { webkitRelativePath?: string }).webkitRelativePath || f.name })));
  }

  runImport() {
    this.error.set(''); this.imported.set(null);
    const scope = this.importScope, agent = scope === 'agent' ? this.importAgent : null;
    if (scope === 'agent' && !agent) { this.error.set('Choose the agent.'); return; }
    const mode = this.importMode();
    let call;
    if (mode === 'files') {
      if (!this.picked().length) { this.error.set('Choose a file, a folder or a zip.'); return; }
      call = this.api.importSkillFiles(this.picked(), scope, agent);
    } else if (mode === 'text') {
      if (!this.importText.trim()) { this.error.set('Paste a SKILL.md.'); return; }
      call = this.api.importSkillText(this.importText, scope, agent);
    } else {
      if (!this.git.url.trim()) { this.error.set('Give the repository URL.'); return; }
      call = this.api.importSkillGit({ ...this.git, scope, agent_id: agent });
    }
    this.busy.set(true);
    call.subscribe({
      next: r => { this.busy.set(false); this.imported.set(r); this.git.token = ''; this.ws.refreshPending(); },
      error: e => { this.busy.set(false); this.error.set(e.error?.detail ?? 'Import failed'); },
    });
  }

  saveUpkeep() {
    this.error.set(''); this.msg.set('');
    this.api.saveSkillUpkeep(this.upkeep()!.settings).subscribe({
      next: u => { this.upkeep.set(u); this.msg.set('Saved.'); this.load(); },
      error: e => this.error.set(e.error?.detail ?? 'Save failed'),
    });
  }

  runReview() {
    this.error.set('');
    this.api.runSkillReview().subscribe({
      next: () => { this.msg.set('Reviewing… suggestions arrive in Requests → Skill proposals.'); this.loadReviews(); },
      error: e => this.error.set(e.error?.detail ?? 'Could not start the review'),
    });
  }
}
