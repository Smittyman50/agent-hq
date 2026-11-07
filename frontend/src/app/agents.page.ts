import { Component, OnInit, computed, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { Router } from '@angular/router';
import { AgentDef, Api, ProviderInfo, ToolServer, blankAgent } from './api.service';
import { ModelPicker } from './model-picker';
import { Avatar } from './avatar';

@Component({
  selector: 'hq-agents',
  imports: [FormsModule, ModelPicker, Avatar],
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
          <span class="row-with-avatar"><hq-avatar [agent]="a" [size]="34" />
            <span class="row-text">
              <span class="name">{{ a.name }} @if (a.is_default) {<span class="pill">default</span>}</span>
              <span class="sub">{{ modelLabel(a) }} · {{ a.pool }}</span>
            </span></span>
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
        <div class="avatar-edit">
          <hq-avatar [agent]="d" [size]="88" [active]="true" />
          <div>
            <h2>{{ d.id ? 'Edit ' + d.name : 'New agent' }}</h2>
            @if (d.id) {
              <div class="actions">
                <label class="ghost file">{{ d.avatar_version ? 'Change avatar' : 'Upload avatar' }}
                  <input type="file" accept="image/gif,image/png,image/jpeg,image/webp" (change)="uploadAvatar(d, $event)"></label>
                @if (d.avatar_version) { <button type="button" class="ghost" (click)="removeAvatar(d)">Remove</button> }
              </div>
              <p class="hint inline-hint">GIF (animated is fine), PNG, JPEG or WebP, up to 3 MB. Square images look best.</p>
            } @else {
              <p class="hint inline-hint">Save the agent first, then add an avatar.</p>
            }
          </div>
        </div>
        <label>Name<input name="name" [(ngModel)]="d.name" required></label>
        <label>Description <small>shown to other agents deciding whom to delegate to</small>
          <input name="description" [(ngModel)]="d.description"></label>
        <label>Instructions
          <textarea name="instructions" rows="10" [(ngModel)]="d.instructions"
            placeholder="Who this agent is, how it works, what it should report."></textarea></label>
        <hq-model-picker label="Model" [providers]="providers()"
          [(providerId)]="d.provider_id" [(modelName)]="d.model" />
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

        <fieldset>
          <legend>Vision model <small>looks at the desktop for this agent</small></legend>
          <p class="hint inline-hint">Optional. When set, this agent never receives screenshots itself: it gets
            look / locate / click_on tools and this model does the looking. Needs a desktop tool server.
            A small local vision model (e.g. qwen3-vl:8b, Holo2) keeps the main model text-only.</p>
          <hq-model-picker label="Vision model" [providers]="providers()" [optional]="true" noneLabel="No vision model"
            [(providerId)]="d.vision_provider_id" [(modelName)]="d.vision_model" />
          @if (d.vision_model) {
            <div class="inline">
              <label>Coordinates the model returns
                <select name="vcoords" [(ngModel)]="d.vision_coords">
                  <option value="relative_1000">0–1000 scale (Qwen-VL family, Holo)</option>
                  <option value="pixels">Pixels (UI-TARS)</option>
                </select></label>
              <label class="check"><input type="checkbox" name="vzoom" [(ngModel)]="d.vision_zoom">
                Zoom in to double-check clicks</label>
            </div>
          }
        </fieldset>

        <fieldset>
          <legend>Fallback model <small>used when the main model fails or gives up</small></legend>
          <p class="hint inline-hint">Optional. If the main model errors, loops, or calls <code>escalate</code>,
            the turn is rerun on this model. Keep the main model local and put a frontier model here.</p>
          <hq-model-picker label="Fallback model" [providers]="providers()" [optional]="true" noneLabel="No fallback"
            [(providerId)]="d.fallback_provider_id" [(modelName)]="d.fallback_model" />
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
  }

  uploadAvatar(d: AgentDef, ev: Event) {
    const input = ev.target as HTMLInputElement;
    const file = input.files?.[0];
    input.value = '';
    if (!file) return;
    this.api.uploadAvatar(d.id!, file).subscribe({
      next: a => this.avatarSaved(a),
      error: e => this.error.set(e.error?.detail ?? 'Upload failed'),
    });
  }

  removeAvatar(d: AgentDef) {
    this.api.removeAvatar(d.id!).subscribe(a => this.avatarSaved(a));
  }

  private avatarSaved(a: AgentDef) {
    this.error.set('');
    const d = this.draft();
    if (d) this.draft.set({ ...d, avatar_version: a.avatar_version, avatar_animated: a.avatar_animated });
    this.agents.update(list => list.map(x => x.id === a.id ? { ...x, avatar_version: a.avatar_version, avatar_animated: a.avatar_animated } : x));
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
