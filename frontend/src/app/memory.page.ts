import { Component, OnInit, computed, inject, signal } from '@angular/core';
import { DatePipe, DecimalPipe } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { AgentDef, Api, MemoryItem, MemoryKind, MemoryScope, MemorySettings, MemoryStats, ProviderInfo } from './api.service';
import { ModelPicker } from './model-picker';

interface Draft { id?: string; text: string; kind: MemoryKind; scope: MemoryScope; agent_id: string | null; }

@Component({
  selector: 'hq-memory',
  imports: [FormsModule, DatePipe, DecimalPipe, ModelPicker],
  template: `
  <div class="memory-layout">
    <section class="mem-main">
      <div class="mem-toolbar">
        <input class="grow" [(ngModel)]="query" (keyup.enter)="load()" placeholder="Search memory, e.g. pve1 disk, how we patch OPNsense">
        <select [(ngModel)]="scope" (ngModelChange)="load()">
          <option value="">All scopes</option><option value="team">Team</option>
          <option value="agent">Agent</option><option value="thread">Conversation</option>
        </select>
        <label class="check"><input type="checkbox" [(ngModel)]="showRetired" (ngModelChange)="load()"> Show retired</label>
        <button (click)="load()">Search</button>
        <button class="ghost" (click)="newMemory()">+ Add</button>
      </div>

      @if (draft(); as d) {
        <form class="mem-card editing" (ngSubmit)="saveDraft()">
          <textarea name="text" rows="3" [(ngModel)]="d.text" placeholder="One clear, self-contained statement"></textarea>
          <div class="inline">
            <select name="kind" [(ngModel)]="d.kind">
              @for (k of kinds; track k) { <option [value]="k">{{ k }}</option> }
            </select>
            <select name="scope" [(ngModel)]="d.scope">
              <option value="team">Team (every agent)</option><option value="agent">One agent</option>
            </select>
            @if (d.scope === 'agent') {
              <select name="agent" [(ngModel)]="d.agent_id">
                @for (a of agents(); track a.id) { <option [value]="a.id">{{ a.name }}</option> }
              </select>
            }
            <span class="spacer"></span>
            <button type="submit">{{ d.id ? 'Save' : 'Add memory' }}</button>
            <button type="button" class="ghost" (click)="draft.set(null)">Cancel</button>
          </div>
          @if (error()) { <p class="error">{{ error() }}</p> }
        </form>
      }

      @for (m of items(); track m.id) {
        <div class="mem-card" [class.retired]="m.invalidated_at">
          <div class="mem-text">{{ m.text }}</div>
          <div class="mem-meta">
            <span class="pill">{{ m.kind }}</span>
            <span class="pill">{{ scopeLabel(m) }}</span>
            <span class="muted">{{ sourceLabel(m) }}</span>
            <span class="muted">· confirmed {{ m.last_confirmed | date:'MMM d, y' }}</span>
            @if (m.times_seen > 1) { <span class="muted">· seen {{ m.times_seen }}×</span> }
            @if (!m.embedded) { <span class="muted" title="Keyword search only until re-embedded">· not embedded</span> }
            @if (m.score !== null) { <span class="muted">· match {{ m.score | number:'1.2-2' }}</span> }
            <span class="spacer"></span>
            @if (m.invalidated_at) {
              <span class="pill bad-pill" [title]="m.invalidated_reason ?? ''">retired: {{ m.invalidated_reason }}</span>
              <button class="link" (click)="restore(m)">Restore</button>
            } @else {
              <button class="link" (click)="confirm(m)">Confirm</button>
              <button class="link" (click)="edit(m)">Edit</button>
              <button class="link" (click)="retire(m)">Retire</button>
            }
            <button class="link danger-link" (click)="remove(m)">Delete</button>
          </div>
        </div>
      } @empty {
        <p class="hint">{{ query ? 'No memories match.' : 'No memories yet. Agents add them as they work, or add one yourself.' }}</p>
      }
    </section>

    <aside class="mem-side">
      <h2>Memory settings</h2>
      @if (stats(); as st) {
        <p class="hint inline-hint">{{ st.active }} active · {{ st.retired }} retired · {{ st.embedded_with_current_model }} embedded
          · {{ st.pgvector ? 'Postgres + pgvector' : 'basic search (no pgvector)' }}</p>
      }
      @if (cfg(); as c) {
        <label class="check"><input type="checkbox" [(ngModel)]="c.enabled"> Long-term memory on</label>
        <h3>Embeddings</h3>
        <p class="hint inline-hint">Used for meaning-based search. Without one, search uses keywords only.
          A small model such as nomic-embed-text runs fine on CPU.</p>
        <hq-model-picker label="Embedding model" [providers]="embedProviders()" [optional]="true" noneLabel="None (keywords only)"
          [(providerId)]="c.embedding_provider_id" [(modelName)]="c.embedding_model" />
        <h3>Recall</h3>
        <div class="inline">
          <label>Memories per job<input type="number" min="0" max="20" [(ngModel)]="c.recall_count"></label>
          <label>Minimum match<input type="number" min="0" max="1" step="0.05" [(ngModel)]="c.min_score"></label>
        </div>
        <h3>Automatic capture</h3>
        <label class="check"><input type="checkbox" [(ngModel)]="c.capture_delegations"> Results of delegated work</label>
        <label class="check"><input type="checkbox" [(ngModel)]="c.capture_events"> How MQTT events were handled</label>
        <h3>Tidy-up</h3>
        <p class="hint inline-hint">Merges duplicates. With a model, it also merges related memories and retires ones a newer memory contradicts.</p>
        <hq-model-picker label="Tidy-up model" [providers]="providers()" [optional]="true" noneLabel="None (duplicates only)"
          [(providerId)]="c.tidy_provider_id" [(modelName)]="c.tidy_model" />
        <div class="inline">
          <label class="check"><input type="checkbox" [(ngModel)]="c.tidy_nightly"> Run nightly at</label>
          <select [(ngModel)]="c.tidy_hour">
            @for (h of hours; track h) { <option [ngValue]="h">{{ h.toString().padStart(2, '0') }}:00</option> }
          </select>
        </div>
        <div class="actions">
          <button (click)="saveSettings()">Save settings</button>
          <button class="ghost" (click)="reembed()">Re-embed all</button>
          <button class="ghost" (click)="tidy()">Tidy up now</button>
        </div>
        @if (notice()) { <p class="hint">{{ notice() }}</p> }
      }
    </aside>
  </div>`,
})
export class MemoryPage implements OnInit {
  private api = inject(Api);
  kinds: MemoryKind[] = ['fact', 'incident', 'preference', 'procedure', 'note'];
  hours = Array.from({ length: 24 }, (_, i) => i);
  items = signal<MemoryItem[]>([]);
  agents = signal<AgentDef[]>([]);
  providers = signal<ProviderInfo[]>([]);
  embedProviders = computed(() => this.providers().filter(p => p.kind !== 'anthropic' && p.kind !== 'chatgpt' && p.kind !== 'grok'));
  cfg = signal<MemorySettings | null>(null);
  stats = signal<MemoryStats | null>(null);
  draft = signal<Draft | null>(null);
  error = signal('');
  notice = signal('');
  query = '';
  scope = '';
  showRetired = false;

  ngOnInit() {
    this.load();
    this.api.agents().subscribe(a => this.agents.set(a));
    this.api.providers().subscribe(p => this.providers.set(p));
    this.api.memorySettings().subscribe(c => this.cfg.set(c));
    this.loadStats();
  }

  load() {
    this.api.memories(this.query, { scope: this.scope, include_retired: this.showRetired })
      .subscribe(m => this.items.set(m));
  }
  loadStats() { this.api.memoryStats().subscribe(s => this.stats.set(s)); }

  agentName(id: string | null) { return this.agents().find(a => a.id === id)?.name ?? 'an agent'; }
  scopeLabel(m: MemoryItem) {
    return m.scope === 'team' ? 'team' : m.scope === 'agent' ? `only ${this.agentName(m.agent_id)}` : 'one conversation';
  }
  sourceLabel(m: MemoryItem) {
    switch (m.source) {
      case 'auto:delegation': return 'captured from delegated work';
      case 'auto:event': return 'captured from an MQTT event';
      case 'agent': return `saved by ${this.agentName(m.created_by)}`;
      case 'tidy': return 'merged by tidy-up';
      default: return 'added by you';
    }
  }

  newMemory() { this.error.set(''); this.draft.set({ text: '', kind: 'fact', scope: 'team', agent_id: null }); }
  edit(m: MemoryItem) {
    this.error.set('');
    this.draft.set({ id: m.id, text: m.text, kind: m.kind, scope: m.scope === 'thread' ? 'team' : m.scope, agent_id: m.agent_id });
  }

  saveDraft() {
    const d = this.draft()!;
    const body = { text: d.text, kind: d.kind, scope: d.scope, agent_id: d.scope === 'agent' ? d.agent_id : null };
    const req = d.id ? this.api.editMemory(d.id, body) : this.api.addMemory(body);
    req.subscribe({
      next: () => { this.draft.set(null); this.load(); this.loadStats(); },
      error: e => this.error.set(e.error?.detail ?? 'Save failed'),
    });
  }

  confirm(m: MemoryItem) { this.api.confirmMemory(m.id).subscribe(() => this.load()); }
  retire(m: MemoryItem) {
    const reason = prompt('Why is this no longer true?', 'outdated');
    if (reason === null) return;
    this.api.retireMemory(m.id, reason || 'retired by user').subscribe(() => { this.load(); this.loadStats(); });
  }
  restore(m: MemoryItem) { this.api.restoreMemory(m.id).subscribe(() => { this.load(); this.loadStats(); }); }
  remove(m: MemoryItem) {
    if (!confirm('Delete this memory permanently? (Retire keeps it as history.)')) return;
    this.api.deleteMemory(m.id).subscribe(() => { this.load(); this.loadStats(); });
  }

  saveSettings() {
    this.api.saveMemorySettings(this.cfg()!).subscribe({
      next: c => { this.cfg.set(c); this.notice.set('Saved. A new embedding model re-embeds existing memories in the background.'); this.loadStats(); },
      error: e => this.notice.set(e.error?.detail ?? 'Save failed'),
    });
  }
  reembed() {
    this.notice.set('Re-embedding…');
    this.api.reembedMemories().subscribe({
      next: r => { this.notice.set(r.reason ?? `Embedded ${r.embedded} memories.`); this.loadStats(); this.load(); },
      error: e => this.notice.set(e.error?.detail ?? 'Re-embed failed'),
    });
  }
  tidy() {
    this.notice.set('Tidying up…');
    this.api.tidyMemories().subscribe({
      next: r => { this.notice.set(`Reviewed ${r.groups} group(s): merged ${r.merged}, retired ${r.retired}${r.model_used ? '' : ' (duplicates only)'}.`); this.load(); this.loadStats(); },
      error: e => this.notice.set(e.error?.detail ?? 'Tidy-up failed'),
    });
  }
}
