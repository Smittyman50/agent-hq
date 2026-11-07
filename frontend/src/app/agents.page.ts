import { Component, OnInit, computed, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { Router } from '@angular/router';
import { AgentDef, Api, ModelInfo, ProviderInfo, ToolServer, blankAgent } from './api.service';

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
          <span class="sub">{{ modelLabel(a) }} · {{ a.pool }}</span>
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
        <div class="inline model-row">
          <label>Model provider <small>set these up on the Models page</small>
            <select name="provider" [ngModel]="d.provider_id" (ngModelChange)="setProvider(d, $event)">
              @for (p of providers(); track p.id) {
                <option [ngValue]="p.id">{{ p.name }}{{ p.enabled ? '' : ' (disabled)' }}</option>
              }
              <option [ngValue]="null">Custom model spec (advanced)</option>
            </select></label>
          <label>Model
            @if (d.provider_id) {
              <small>{{ modelsStatus() }} <a href="" (click)="$event.preventDefault(); loadModels(d.provider_id)">refresh</a></small>
            } @else { <small>a PydanticAI spec, e.g. openai:gpt-4.1</small> }
            <input name="model" [(ngModel)]="d.model" list="provider-models"
              [placeholder]="d.provider_id ? 'pick or type a model name' : 'provider:model'"></label>
        </div>
        @if (modelDetails(d); as det) { <p class="hint inline-hint">{{ det }}</p> }
        <datalist id="provider-models">
          @for (m of models(); track m.id) { <option [value]="m.id">{{ m.details }}</option> }
        </datalist>
        <label>Worker pool <small>which workers run this agent, e.g. default · gpu · desktop</small>
          <input name="pool" [(ngModel)]="d.pool" list="pools"></label>
        <datalist id="pools">@for (p of pools(); track p) { <option [value]="p"></option> }</datalist>

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
  providers = signal<ProviderInfo[]>([]);
  models = signal<ModelInfo[]>([]);
  modelsStatus = signal('');
  pools = computed(() => [...new Set(['default', ...this.agents().map(a => a.pool)])]);
  others = computed(() => this.agents().filter(a => a.id !== this.draft()?.id && a.enabled));

  ngOnInit() { this.load(); }

  load() {
    this.api.agents().subscribe(a => this.agents.set(a));
    this.api.servers().subscribe(s => this.servers.set(s));
    this.api.providers().subscribe(p => this.providers.set(p));
  }

  edit(a: AgentDef | null) {
    this.error.set('');
    const d = a ? structuredClone(a) : blankAgent();
    if (!a) d.provider_id = this.providers().find(p => p.enabled)?.id ?? null;
    this.draft.set(d);
    this.loadModels(d.provider_id);
  }

  setProvider(d: AgentDef, id: string | null) {
    if (d.provider_id !== id) d.model = '';
    d.provider_id = id;
    this.draft.set({ ...d });
    this.loadModels(id);
  }

  loadModels(providerId: string | null) {
    this.models.set([]);
    if (!providerId) { this.modelsStatus.set(''); return; }
    this.modelsStatus.set('loading models…');
    this.api.providerModels(providerId).subscribe({
      next: m => { this.models.set(m); this.modelsStatus.set(`${m.length} available`); },
      error: e => this.modelsStatus.set(e.error?.detail ?? 'could not reach provider'),
    });
  }

  modelDetails(d: AgentDef) {
    return d.provider_id ? this.models().find(m => m.id === d.model)?.details ?? '' : '';
  }

  modelLabel(a: AgentDef) {
    if (!a.model) return 'no model';
    const p = this.providers().find(x => x.id === a.provider_id);
    return p ? `${a.model} @ ${p.name}` : a.model;
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
