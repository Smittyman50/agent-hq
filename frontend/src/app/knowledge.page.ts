import { Component, OnDestroy, OnInit, computed, inject, signal } from '@angular/core';
import { DatePipe } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { RouterLink } from '@angular/router';
import { Api, KnowledgeCollection, KnowledgeDoc, KnowledgeHit, KnowledgeSettings, KnowledgeSource } from './api.service';
import { Workspace } from './workspace';

type SourceKind = 'upload' | 'url' | 'git' | 'obsidian';

function lines(x: string): string[] { return x.split('\n').map(s => s.trim()).filter(Boolean); }

/** Settings → Knowledge: your documents and notes, searchable by agents. */
@Component({
  selector: 'hq-knowledge',
  imports: [FormsModule, DatePipe, RouterLink],
  template: `
  <div class="memory-layout">
    <section class="mem-main">
      @if (current(); as c) {
        <div class="mem-toolbar">
          <h2 class="grow">{{ c.name }}</h2>
          <span class="muted small">{{ c.docs ?? 0 }} documents · {{ c.chunks ?? 0 }} sections</span>
          <button class="ghost" (click)="editing.set(!editing())">{{ editing() ? 'Close' : 'Edit' }}</button>
        </div>
        @if (c.description && !editing()) { <p class="hint inline-hint">{{ c.description }}</p> }
        @if (!editing()) {
          <p class="muted small">
            Readable by: {{ c.agent_ids.length ? names(c.agent_ids) : 'every agent' }}
            · {{ c.auto_recall ? 'excerpts come up by themselves' : 'search only' }}
            @if (!c.enabled) { · <strong>switched off</strong> }
          </p>
        } @else {
          <form class="kn-edit" (ngSubmit)="saveCollection(c)">
            <label>Name <input name="cname" [(ngModel)]="c.name" required></label>
            <label>Description <small>what's in it, for you and the agents</small>
              <input name="cdesc" [(ngModel)]="c.description"></label>
            <fieldset>
              <legend>Who can read it <small>none ticked = every agent</small></legend>
              <div class="kn-agents">
                @for (a of ws.agents(); track a.id) {
                  @if (!a.is_subagent) {
                    <label class="check"><input type="checkbox" [checked]="c.agent_ids.includes(a.id!)"
                      (change)="toggleAgent(c, a.id!)"> {{ a.name }}</label>
                  }
                }
              </div>
              <p class="hint inline-hint">Sub-agents can read what their parent can. Agents on cloud models send the
                excerpts they read to that provider; keep private collections to local-model agents.</p>
            </fieldset>
            <label class="check"><input type="checkbox" name="crecall" [(ngModel)]="c.auto_recall">
              Excerpts come up by themselves (like memories); otherwise agents search when they need to</label>
            <label class="check"><input type="checkbox" name="cenabled" [(ngModel)]="c.enabled"> Switched on</label>
            <div class="actions">
              <button type="submit">Save</button>
              <span class="grow"></span>
              <button type="button" class="danger" (click)="removeCollection(c)">Delete collection</button>
            </div>
          </form>
        }

        <h3>Sources</h3>
        @for (x of sources(); track x.id) {
          <div class="mem-card kn-source">
            <div class="mem-meta top">
              <span class="kind">{{ kindLabel(x) }}</span>
              <strong>{{ x.name || x.url }}</strong>
              <span class="pill {{ pill(x.status) }}">{{ x.status }}</span>
              <span class="grow"></span>
              <span class="muted small">{{ x.docs }} docs · {{ x.chunks }} sections
                @if (x.last_indexed_at) { · checked {{ x.last_indexed_at | date:'MMM d, h:mm a' }} }</span>
            </div>
            @if (x.url) { <div class="muted small mono">{{ x.url }}@if (x.branch) { · {{ x.branch }} }</div> }
            @if (x.kind !== 'upload') {
              <div class="muted small">{{ x.refresh_minutes ? 'Checks for changes every ' + every(x.refresh_minutes) : 'Checks only when you ask' }}
                @if (x.kind === 'git') { · {{ x.has_token ? 'token saved' : 'no token' }} }</div>
            }
            @if (x.error) { <div class="error small">{{ x.error }}</div> }
            @if (editSource()?.id === x.id) {
              <form class="kn-edit" (ngSubmit)="saveSource()">
                @if (editSource(); as e) {
                  <label>Name <input name="ename" [(ngModel)]="e.name"></label>
                  @if (e.kind !== 'upload') { <label>URL <input name="eurl" [(ngModel)]="e.url"></label> }
                  @if (e.kind === 'git') {
                    <div class="inline">
                      <label>Branch <small>blank = default</small><input name="ebranch" [(ngModel)]="e.branch"></label>
                      <label>User <input name="euser" [(ngModel)]="e.username" autocomplete="off"></label>
                      <label>Token <small>blank = keep the saved one</small>
                        <input name="etoken" type="password" [(ngModel)]="e.token" autocomplete="new-password"></label>
                    </div>
                    <label>Include <small>one pattern per line</small>
                      <textarea name="einc" rows="3" class="mono" [(ngModel)]="incText"></textarea></label>
                    <label>Exclude <small>one pattern per line, e.g. Journal/**</small>
                      <textarea name="eexc" rows="3" class="mono" [(ngModel)]="excText"></textarea></label>
                    <label class="check"><input type="checkbox" name="eobs" [(ngModel)]="e.obsidian"> Obsidian vault
                      <small>(frontmatter, #tags, [[links]]; citations open in Obsidian)</small></label>
                    @if (e.obsidian) { <label>Vault name <small>as Obsidian shows it; blank = the repo's name</small>
                      <input name="evault" [(ngModel)]="e.vault_name"></label> }
                  }
                  @if (e.kind !== 'upload') {
                    <label>Check for changes every <small>minutes; 0 = only when you ask</small>
                      <input name="erefresh" type="number" min="0" [(ngModel)]="e.refresh_minutes"></label>
                  }
                  <div class="actions"><button type="submit">Save</button>
                    <button type="button" class="ghost" (click)="editSource.set(null)">Cancel</button></div>
                }
              </form>
            } @else {
              <div class="actions tight">
                @if (x.kind === 'upload') {
                  <label class="button-like">Add files<input type="file" multiple hidden (change)="upload(x, $event)"
                    accept=".pdf,.docx,.pptx,.xlsx,.xls,.html,.htm,.md,.markdown,.txt,.csv,.json,.yaml,.yml,.epub"></label>
                } @else {
                  <button class="ghost small" (click)="reindex(x, false)" [disabled]="x.status === 'indexing'">Check now</button>
                }
                <button class="ghost small" (click)="reindex(x, true)" [disabled]="x.status === 'indexing'"
                  title="Re-read and re-embed everything (after changing the embedding model)">Re-index all</button>
                <button class="ghost small" (click)="startEdit(x)">Edit</button>
                <button class="ghost small" (click)="removeSource(x)">Remove</button>
              </div>
            }
            @if (uploadNote()[x.id]; as n) { <p class="hint">{{ n }}</p> }
          </div>
        } @empty { <p class="hint">No sources yet. Add one below.</p> }

        @if (adding(); as kind) {
          <form class="mem-card kn-edit" (ngSubmit)="addSource()">
            <h3>{{ addTitle(kind) }}</h3>
            @if (kind === 'upload') {
              <p class="hint inline-hint">PDF, Word, PowerPoint, Excel, HTML, Markdown and text. You'll add files to it next.</p>
              <label>Name <input name="nname" [(ngModel)]="draft.name" placeholder="Uploads"></label>
            }
            @if (kind === 'url') {
              <label>Page URL <input name="nurl" [(ngModel)]="draft.url" placeholder="https://…" required></label>
              <label>Check for changes every <small>minutes; 0 = only when you ask</small>
                <input name="nrefresh" type="number" min="0" [(ngModel)]="draft.refresh_minutes"></label>
            }
            @if (kind === 'git' || kind === 'obsidian') {
              @if (kind === 'obsidian') {
                <p class="hint inline-hint">Push your vault to Gitea with the Obsidian Git plugin, then point this at
                  the repo with a read-only token. Only changed notes are re-read.</p>
              }
              <label>Clone URL <small>https, as Gitea shows it</small>
                <input name="nurl" [(ngModel)]="draft.url" placeholder="https://gitea.home.arpa/mike/obsidian-vault.git" required></label>
              <div class="inline">
                <label>User <small>the token's account</small><input name="nuser" [(ngModel)]="draft.username" placeholder="agent-hq" autocomplete="off"></label>
                <label>Token <input name="ntoken" type="password" [(ngModel)]="draft.token" autocomplete="new-password"></label>
                <label>Branch <small>blank = default</small><input name="nbranch" [(ngModel)]="draft.branch"></label>
              </div>
              @if (kind === 'obsidian') {
                <label>Vault name <small>as Obsidian shows it, for links; blank = the repo's name</small>
                  <input name="nvault" [(ngModel)]="draft.vault_name"></label>
              }
              <label>Exclude <small>one per line; .obsidian, .trash and .git are always skipped</small>
                <textarea name="nexc" rows="3" class="mono" [(ngModel)]="excText" placeholder="Journal/**&#10;Private/**"></textarea></label>
              <label>Check for changes every <small>minutes</small>
                <input name="nrefresh" type="number" min="0" [(ngModel)]="draft.refresh_minutes"></label>
            }
            <div class="actions"><button type="submit">Add</button>
              <button type="button" class="ghost" (click)="adding.set(null)">Cancel</button></div>
            @if (error()) { <p class="error">{{ error() }}</p> }
          </form>
        } @else {
          <div class="actions tight">
            <button class="ghost small" (click)="startAdd('obsidian')">+ Obsidian vault (Git)</button>
            <button class="ghost small" (click)="startAdd('git')">+ Git repository</button>
            <button class="ghost small" (click)="startAdd('url')">+ Web page</button>
            <button class="ghost small" (click)="startAdd('upload')">+ Uploaded files</button>
          </div>
        }

        <div class="mem-toolbar kn-docs-head">
          <h3 class="grow">Documents <span class="muted small">({{ docTotal() }})</span></h3>
          <input class="kn-filter" placeholder="Filter by title or path" [(ngModel)]="docFilter" (ngModelChange)="loadDocs()">
        </div>
        <div class="kn-docs">
          @for (d of docs(); track d.id) {
            <button type="button" class="kn-doc" [class.on]="openDoc()?.id === d.id" (click)="showDoc(d)">
              <span class="t">{{ d.title }}</span>
              <span class="p mono">{{ d.path }}</span>
              <span class="muted small">{{ d.chunks }} §@if (!d.indexed) { · indexing }</span>
            </button>
          } @empty { <p class="hint">Nothing here yet.</p> }
        </div>
        @if (openDoc(); as d) {
          <div class="mem-card kn-viewer">
            <div class="mem-meta top">
              <strong>{{ d.title }}</strong>
              @if (d.link) { <a [href]="d.link" target="_blank" rel="noopener">Open</a> }
              <span class="grow"></span>
              <button class="ghost small" (click)="openDoc.set(null)">Close</button>
            </div>
            <div class="muted small mono">{{ d.path }}</div>
            @if (d.tags.length) { <div class="reasons">@for (t of d.tags; track t) { <span class="reason">#{{ t }}</span> }</div> }
            <div class="kn-propose">
              <span>Turn its procedures into skills, as</span>
              <select [(ngModel)]="proposeAs">
                @for (a of ws.enabledAgents(); track a.id) { @if (!a.is_subagent) { <option [value]="a.id">{{ a.name }}</option> } }
              </select>
              <button class="small" (click)="propose(d)" [disabled]="proposing() || !proposeAs">
                {{ proposing() ? 'Reading…' : 'Propose skills' }}</button>
              @if (sourceKind(d) === 'upload') { <button class="ghost small" (click)="deleteDoc(d)">Delete document</button> }
            </div>
            @if (proposeNote(); as n) { <p class="hint">{{ n }} <a routerLink="/requests/skills">Review in Requests</a></p> }
            @for (s of d.sections ?? []; track $index) {
              <details class="kn-section" [open]="$index === 0">
                <summary>{{ s.heading || '(top)' }} <span class="muted small">{{ s.text.length }} chars{{ s.embedded ? '' : ' · keywords only' }}</span></summary>
                <pre class="kn-text">{{ s.text }}</pre>
              </details>
            }
          </div>
        }
      } @else {
        <h2>Knowledge library</h2>
        <p class="hint">Your documents and notes, for agents to search and cite: an Obsidian vault in Git, runbook
          repos, web pages and uploaded files. Create a collection to start. Search uses the embedding model from
          Settings → Memory; without one it matches keywords only.</p>
      }
    </section>

    <aside class="mem-side">
      <h2>Collections</h2>
      @for (c of collections(); track c.id) {
        <button type="button" class="kn-col" [class.on]="current()?.id === c.id" (click)="select(c)">
          <strong>{{ c.name }}</strong>
          <span class="muted small">{{ c.docs }} docs · {{ c.agent_ids.length ? names(c.agent_ids) : 'everyone' }}
            @if (!c.enabled) { · off }</span>
        </button>
      }
      <form class="inline kn-new" (ngSubmit)="newCollection()">
        <input name="newcol" [(ngModel)]="newName" placeholder="New collection, e.g. Homelab">
        <button type="submit" [disabled]="!newName.trim()">Add</button>
      </form>

      <h3>Try a search</h3>
      <form (ngSubmit)="search()">
        <input name="sq" [(ngModel)]="query" placeholder="e.g. how do I restore a VM">
        <div class="inline">
          <select name="sas" [(ngModel)]="searchAs">
            <option value="">All collections</option>
            @for (a of ws.enabledAgents(); track a.id) { @if (!a.is_subagent) { <option [value]="a.id">As {{ a.name }}</option> } }
          </select>
          <button type="submit" [disabled]="!query.trim()">Search</button>
        </div>
      </form>
      @for (h of hits(); track $index) {
        <div class="kn-hit" [class.dim]="!h.would_recall">
          <div><strong>{{ h.title }}</strong>@if (h.section) { <span class="muted"> › {{ h.section }}</span> }</div>
          <div class="muted small">{{ h.collection }} · score {{ h.score }}@if (h.sim !== null) { · meaning {{ h.sim }} } · words {{ h.keyword }}
            {{ h.would_recall ? '· would come up' : '· below the recall threshold' }}</div>
          <div class="kn-snip">{{ h.text }}</div>
        </div>
      } @empty { @if (searched()) { <p class="hint">No matches.</p> } }

      @if (cfg(); as k) {
        <h3>Library settings</h3>
        <label class="check"><input type="checkbox" [(ngModel)]="k.recall"> Excerpts come up by themselves</label>
        <div class="inline">
          <label>How many <input type="number" min="0" max="10" [(ngModel)]="k.recall_count"></label>
          <label>Threshold <small>0–1</small><input type="number" min="0" max="1" step="0.05" [(ngModel)]="k.min_score"></label>
        </div>
        <div class="inline">
          <label>Section size <small>characters</small><input type="number" min="500" max="8000" step="100" [(ngModel)]="k.chunk_chars"></label>
          <label>Largest file <small>MB</small><input type="number" min="1" max="200" [(ngModel)]="k.max_file_mb"></label>
        </div>
        <p class="hint inline-hint">A new section size applies as documents change; use Re-index all to redo a source now.</p>
        <div class="actions"><button (click)="saveCfg()">Save settings</button> @if (cfgNote()) { <span class="hint">{{ cfgNote() }}</span> }</div>
      }
    </aside>
  </div>`,
})
export class KnowledgePage implements OnInit, OnDestroy {
  private api = inject(Api);
  ws = inject(Workspace);

  collections = signal<KnowledgeCollection[]>([]);
  current = signal<KnowledgeCollection | null>(null);
  editing = signal(false);
  sources = signal<KnowledgeSource[]>([]);
  docs = signal<KnowledgeDoc[]>([]);
  docTotal = signal(0);
  openDoc = signal<KnowledgeDoc | null>(null);
  adding = signal<SourceKind | null>(null);
  editSource = signal<(KnowledgeSource & { token?: string | null }) | null>(null);
  error = signal('');
  uploadNote = signal<Record<string, string>>({});
  hits = signal<KnowledgeHit[]>([]);
  searched = signal(false);
  cfg = signal<KnowledgeSettings | null>(null);
  cfgNote = signal('');
  proposing = signal(false);
  proposeNote = signal('');
  busy = computed(() => this.sources().some(s => s.status === 'indexing' || s.status === 'queued'));

  draft: Partial<KnowledgeSource> = {};
  incText = '';
  excText = '';
  docFilter = '';
  newName = '';
  query = '';
  searchAs = '';
  proposeAs = '';
  private timer?: ReturnType<typeof setInterval>;

  ngOnInit() {
    this.loadCollections();
    this.api.knowledgeSettings().subscribe(c => this.cfg.set(c));
    // While something is indexing, keep the counts and statuses fresh.
    this.timer = setInterval(() => { if (this.busy()) this.refreshCurrent(); }, 3000);
  }
  ngOnDestroy() { clearInterval(this.timer); }

  names(ids: string[]) { return ids.map(i => this.ws.agentName(i)).join(', '); }
  every(m: number) { return m % 1440 === 0 ? `${m / 1440} day${m === 1440 ? '' : 's'}` : m % 60 === 0 ? `${m / 60} h` : `${m} min`; }
  pill(s: string) { return ({ ready: 'done', error: 'failed', indexing: 'working', queued: 'queued' } as Record<string, string>)[s] ?? ''; }
  kindLabel(x: KnowledgeSource) { return x.kind === 'git' ? (x.obsidian ? 'Obsidian vault' : 'Git') : x.kind === 'url' ? 'Web page' : 'Uploads'; }
  addTitle(k: SourceKind) { return { obsidian: 'Obsidian vault (Git)', git: 'Git repository', url: 'Web page', upload: 'Uploaded files' }[k]; }
  sourceKind(d: KnowledgeDoc) { return this.sources().find(s => s.id === d.source_id)?.kind; }

  loadCollections(selectId?: string) {
    this.api.collections().subscribe(list => {
      this.collections.set(list);
      const id = selectId ?? this.current()?.id;
      const pick = list.find(c => c.id === id) ?? list[0] ?? null;
      if (pick && pick.id !== this.current()?.id) this.select(pick);
      else if (pick) this.current.set({ ...pick });
      else this.current.set(null);
    });
  }

  select(c: KnowledgeCollection) {
    this.current.set({ ...c, agent_ids: [...c.agent_ids] });
    this.editing.set(false);
    this.openDoc.set(null);
    this.adding.set(null);
    this.editSource.set(null);
    this.docFilter = '';
    this.loadSources();
    this.loadDocs();
  }

  refreshCurrent() {
    this.loadSources();
    this.loadDocs();
    this.api.collections().subscribe(list => {
      this.collections.set(list);
      const c = list.find(x => x.id === this.current()?.id);
      if (c && !this.editing()) this.current.set({ ...c });
    });
  }

  loadSources() {
    const c = this.current();
    if (c) this.api.sources(c.id).subscribe(s => this.sources.set(s));
  }

  loadDocs() {
    const c = this.current();
    if (c) this.api.knowledgeDocs(c.id, this.docFilter).subscribe(r => { this.docs.set(r.docs); this.docTotal.set(r.total); });
  }

  newCollection() {
    this.api.createCollection({ name: this.newName.trim() }).subscribe(c => {
      this.newName = '';
      this.loadCollections(c.id);
      this.editing.set(true);
    });
  }

  toggleAgent(c: KnowledgeCollection, id: string) {
    c.agent_ids = c.agent_ids.includes(id) ? c.agent_ids.filter(x => x !== id) : [...c.agent_ids, id];
  }

  saveCollection(c: KnowledgeCollection) {
    this.api.updateCollection(c).subscribe(() => { this.editing.set(false); this.loadCollections(c.id); });
  }

  removeCollection(c: KnowledgeCollection) {
    if (!confirm(`Delete “${c.name}” and everything indexed in it? Your originals (the vault, the pages) aren't touched.`)) return;
    this.api.deleteCollection(c.id).subscribe(() => { this.current.set(null); this.loadCollections(); });
  }

  startAdd(kind: SourceKind) {
    this.error.set('');
    this.excText = '';
    this.draft = { kind: kind === 'obsidian' ? 'git' : kind, obsidian: kind === 'obsidian', name: '',
                   url: '', branch: '', username: kind === 'obsidian' || kind === 'git' ? 'agent-hq' : '',
                   token: '', vault_name: '', refresh_minutes: kind === 'upload' ? 0 : kind === 'url' ? 1440 : 15 };
    this.adding.set(kind);
  }

  addSource() {
    const c = this.current()!;
    const body: Partial<KnowledgeSource> = { ...this.draft };
    if (body.kind === 'git') body.exclude = [...new Set([...this.defaultExclude, ...lines(this.excText)])];
    this.error.set('');
    this.api.createSource(c.id, body).subscribe({
      next: () => { this.adding.set(null); this.refreshCurrent(); },
      error: e => this.error.set(e.error?.detail ?? 'Could not add it'),
    });
  }
  private defaultExclude = ['.obsidian/**', '.trash/**', '.git/**', '**/node_modules/**'];

  startEdit(x: KnowledgeSource) {
    this.incText = x.include.join('\n');
    this.excText = x.exclude.join('\n');
    this.editSource.set({ ...x, token: '' });
  }

  saveSource() {
    const e = this.editSource()!;
    const body = { ...e, include: lines(this.incText), exclude: lines(this.excText), token: e.token ? e.token : null };
    this.api.updateSource(body).subscribe({
      next: () => { this.editSource.set(null); this.loadSources(); },
      error: err => alert(err.error?.detail ?? 'Could not save'),
    });
  }

  removeSource(x: KnowledgeSource) {
    if (!confirm(`Remove “${x.name || x.url}” and its ${x.docs} documents from the library?`)) return;
    this.api.deleteSource(x.id).subscribe(() => this.refreshCurrent());
  }

  reindex(x: KnowledgeSource, full: boolean) {
    this.api.reindexSource(x.id, full).subscribe({
      next: () => this.sources.update(list => list.map(s => s.id === x.id ? { ...s, status: 'indexing', error: null } : s)),
      error: e => alert(e.error?.detail ?? 'Could not start'),
    });
  }

  upload(x: KnowledgeSource, ev: Event) {
    const input = ev.target as HTMLInputElement;
    const files = Array.from(input.files ?? []);
    input.value = '';
    if (!files.length) return;
    this.setNote(x.id, `Uploading ${files.length} file${files.length === 1 ? '' : 's'}…`);
    this.api.uploadKnowledge(x.id, files).subscribe({
      next: r => {
        this.setNote(x.id, `Added ${r.added.length}.` + (r.errors.length ? ` Skipped: ${r.errors.join('; ')}` : ''));
        this.refreshCurrent();
      },
      error: e => this.setNote(x.id, e.error?.detail ?? 'Upload failed'),
    });
  }
  private setNote(id: string, text: string) { this.uploadNote.update(n => ({ ...n, [id]: text })); }

  showDoc(d: KnowledgeDoc) {
    this.proposeNote.set('');
    this.proposeAs ||= this.ws.enabledAgents().find(a => !a.is_subagent)?.id ?? '';
    this.api.knowledgeDoc(d.id).subscribe(full => this.openDoc.set(full));
  }

  deleteDoc(d: KnowledgeDoc) {
    if (!confirm(`Delete “${d.title}” from the library?`)) return;
    this.api.deleteKnowledgeDoc(d.id).subscribe(() => { this.openDoc.set(null); this.refreshCurrent(); });
  }

  propose(d: KnowledgeDoc) {
    this.proposing.set(true);
    this.proposeNote.set('');
    this.api.proposeSkillsFromDoc(d.id, this.proposeAs).subscribe({
      next: r => {
        this.proposing.set(false);
        const n = r.proposals.length;
        this.proposeNote.set(`${r.summary} ${n ? `${n} skill proposal${n === 1 ? '' : 's'}: ${r.proposals.map(p => p.name).join(', ')}.` : 'No new skills.'}`
          + (r.notes.length ? ` (${r.notes.join('; ')})` : ''));
        this.ws.refreshPending();
      },
      error: e => { this.proposing.set(false); this.proposeNote.set(e.error?.detail ?? 'Could not read it'); },
    });
  }

  search() {
    this.api.searchKnowledge(this.query, { agent_id: this.searchAs || undefined }).subscribe(r => {
      this.hits.set(r.results);
      this.searched.set(true);
    });
  }

  saveCfg() {
    this.api.saveKnowledgeSettings(this.cfg()!).subscribe({
      next: c => { this.cfg.set(c); this.cfgNote.set('Saved.'); },
      error: e => this.cfgNote.set(e.error?.detail ?? 'Save failed'),
    });
  }
}
