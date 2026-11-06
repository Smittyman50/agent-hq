import { Component, OnInit, computed, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { Router } from '@angular/router';
import { AgentDef, Api, ToolServer, blankAgent } from './api.service';

@Component({
  selector: 'hq-agents',
  imports: [FormsModule],
  template: `
  <div class="split">
    <aside class="list">
      <div class="list-head">
        <h2>Agents</h2>
        <button (click)="edit(null)">+ New</button>
      </div>
      @for (a of agents(); track a.id) {
        <button class="row" [class.active]="draft()?.id === a.id" [class.muted]="!a.enabled"
                (click)="edit(a)">
          <span class="name">{{ a.name }} @if (a.is_default) {<span class="pill">default</span>}</span>
          <span class="sub">{{ a.pool }} · {{ a.description || 'No description' }}</span>
        </button>
      } @empty {
        <p class="hint">No agents yet. Create one, or import a JSON file.</p>
      }
      <div class="list-foot">
        <button class="ghost" (click)="exportJson()">Export</button>
        <label class="ghost file">Import<input type="file" accept=".json" (change)="importJson($event)"></label>
      </div>
    </aside>

    @if (draft(); as d) {
      <form class="editor" (ngSubmit)="save()">
        <h2>{{ d.id ? 'Edit ' + d.name : 'New agent' }}</h2>
        <label>Name<input name="name" [(ngModel)]="d.name" required></label>
        <label>Description <small>shown to other agents deciding whom to delegate to</small>
          <input name="description" [(ngModel)]="d.description"></label>
        <label>Instructions
          <textarea name="instructions" rows="10" [(ngModel)]="d.instructions"
            placeholder="Who this agent is, how it works, what it should report."></textarea></label>
        <label>Model <small>e.g. ollama:gpt-oss:20b · openai:gpt-4.1 · anthropic:claude-sonnet-4-5</small>
          <input name="model" [(ngModel)]="d.model" list="models"></label>
        <label>Worker pool <small>which workers run this agent, e.g. default · gpu · desktop</small>
          <input name="pool" [(ngModel)]="d.pool" list="pools"></label>
        <datalist id="pools">@for (p of pools(); track p) { <option [value]="p"></option> }</datalist>
        <datalist id="models">
          <option value="ollama:gpt-oss:20b"></option><option value="ollama:qwen3:14b"></option>
          <option value="openai:gpt-4.1"></option><option value="anthropic:claude-sonnet-4-5"></option>
        </datalist>

        <fieldset>
          <legend>Tools (MCP servers)</legend>
          @for (t of servers(); track t.id) {
            <label class="check"><input type="checkbox" [checked]="d.tool_server_ids.includes(t.id!)"
              (change)="toggle(d.tool_server_ids, t.id!)"> {{ t.name }} <small>{{ t.description }}</small></label>
          } @empty { <p class="hint">No tool servers registered yet (Tools tab).</p> }
        </fieldset>

        <fieldset>
          <legend>Can delegate to</legend>
          <label class="check"><input type="checkbox" [checked]="d.delegates.includes('*')"
            (change)="toggle(d.delegates, '*')"> Everyone</label>
          @if (!d.delegates.includes('*')) {
            @for (a of others(); track a.id) {
              <label class="check"><input type="checkbox" [checked]="d.delegates.includes(a.id!)"
                (change)="toggle(d.delegates, a.id!)"> {{ a.name }}</label>
            }
          }
        </fieldset>

        <label>Desktop viewer URL <small>optional KasmVNC/noVNC address for this agent's desktop</small>
          <input name="desktop_url" [(ngModel)]="d.desktop_url" placeholder="http://shockwave:3001"></label>
        <div class="inline">
          <label class="check"><input type="checkbox" name="is_default" [(ngModel)]="d.is_default"> Default chat agent</label>
          <label class="check"><input type="checkbox" name="enabled" [(ngModel)]="d.enabled"> Enabled</label>
        </div>

        @if (error()) { <p class="error">{{ error() }}</p> }
        <div class="actions">
          <button type="submit">Save</button>
          @if (d.id) {
            <button type="button" class="ghost" (click)="chat(d)">Chat</button>
            <button type="button" class="danger" (click)="remove(d)">Disable</button>
          }
        </div>
      </form>
    } @else {
      <div class="empty">Select an agent or create a new one.</div>
    }
  </div>`,
})
export class AgentsPage implements OnInit {
  private api = inject(Api);
  private router = inject(Router);
  agents = signal<AgentDef[]>([]);
  servers = signal<ToolServer[]>([]);
  draft = signal<AgentDef | null>(null);
  error = signal('');
  pools = computed(() => [...new Set(['default', ...this.agents().map(a => a.pool)])]);
  others = computed(() => this.agents().filter(a => a.id !== this.draft()?.id && a.enabled));

  ngOnInit() { this.load(); }

  load() {
    this.api.agents().subscribe(a => this.agents.set(a));
    this.api.servers().subscribe(s => this.servers.set(s));
  }

  edit(a: AgentDef | null) {
    this.error.set('');
    this.draft.set(a ? structuredClone(a) : blankAgent());
  }

  toggle(list: string[], id: string) {
    const i = list.indexOf(id);
    i >= 0 ? list.splice(i, 1) : list.push(id);
  }

  save() {
    const d = this.draft()!;
    this.api.saveAgent(d).subscribe({
      next: saved => { this.load(); this.draft.set(structuredClone(saved)); this.error.set(''); },
      error: e => this.error.set(e.error?.detail ?? 'Save failed'),
    });
  }

  remove(d: AgentDef) {
    if (!confirm(`Disable ${d.name}? Old conversations stay readable.`)) return;
    this.api.deleteAgent(d.id!).subscribe(() => { this.draft.set(null); this.load(); });
  }

  chat(d: AgentDef) {
    this.api.createThread(d.id!).subscribe(t => this.router.navigate(['/chat', t.id]));
  }

  exportJson() {
    this.api.exportAgents().subscribe(data => {
      const a = document.createElement('a');
      a.href = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' }));
      a.download = 'agents.json';
      a.click();
    });
  }

  importJson(ev: Event) {
    const file = (ev.target as HTMLInputElement).files?.[0];
    if (!file) return;
    file.text().then(txt => this.api.importAgents(JSON.parse(txt)).subscribe(() => this.load()));
  }
}
