import { Component, OnInit, computed, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { Router } from '@angular/router';
import { AgentDef, Api, ProviderInfo, ToolServer, blankAgent, defaultSubagentLimits, AgentContextInfo, ThinkingSupport } from './api.service';
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
        <div class="model-row">
          <label>Name<input name="name" [(ngModel)]="d.name" required></label>
          <label>Pronouns <small>how teammates refer to them</small>
            <input name="pronouns" [(ngModel)]="d.pronouns" placeholder="e.g. he/him, she/her, they/them" list="pronoun-list"></label>
          <datalist id="pronoun-list"><option value="he/him"><option value="she/her"><option value="they/them"><option value="it/its"></datalist>
        </div>
        <label>Description <small>shown to other agents deciding whom to delegate to</small>
          <input name="description" [(ngModel)]="d.description"></label>
        <label>Instructions
          <textarea name="instructions" rows="18" [(ngModel)]="d.instructions"
            placeholder="Who this agent is, how it works, what it should report."></textarea></label>
        <hq-model-picker label="Model" [providers]="providers()"
          [(providerId)]="d.provider_id" [(modelName)]="d.model"
          (providerIdChange)="checkThinking()" (modelNameChange)="checkThinking()" />
        <label class="narrow">Thinking <small>how much the model reasons before it answers</small>
          <select name="thinking" [(ngModel)]="d.thinking">
            @for (t of thinkingLevels; track t.value) { <option [value]="t.value">{{ t.label }}</option> }
          </select></label>
        <p class="hint inline-hint" [class.needs-hint]="d.thinking && thinkingInfo()?.supported === false">
          {{ thinkingHint(d.thinking ?? '') }}
          @if (thinkingInfo()?.note) { {{ thinkingInfo()!.note }} }
          Also used by its fallback model and the sub-agents it creates.
        </p>
        <label class="narrow">Context window <small>tokens; blank = ask the provider</small>
          <input name="ctxwin" type="number" min="1024" step="1024" [(ngModel)]="d.context_window"
            [placeholder]="ctx()?.detected ? 'Auto: ' + ctx()!.detected!.toLocaleString() : 'Auto'"></label>
        @if (ctx(); as c) {
          <p class="hint inline-hint" [class.needs-hint]="c.room_for_conversation < 1000">
            Using {{ c.window.toLocaleString() }} tokens ({{ c.source }}). Its instructions take about
            {{ c.instruction_tokens.toLocaleString() }}, leaving about {{ c.room_for_conversation.toLocaleString() }}
            for the conversation before older turns are summarised.
            @if (c.room_for_conversation < 1000) { That's very little: raise the window (for Ollama, OLLAMA_CONTEXT_LENGTH
              on the server, then set it here) or trim the instructions. }
          </p>
        }
        <label>Worker pool <small>which workers run this agent, e.g. default · gpu · desktop</small>
          <input name="pool" [(ngModel)]="d.pool" list="pools"></label>
        <datalist id="pools">@for (p of pools(); track p) { <option [value]="p"></option> }</datalist>

        <fieldset>
          <legend>Tools (MCP servers)</legend>
          @for (t of servers(); track t.id) {
            <label class="check"><input type="checkbox" [checked]="d.tool_server_ids.includes(t.id!)"
              (change)="toggle(d.tool_server_ids, t.id!)"> {{ t.name }} <small>{{ t.description }}</small></label>
          } @empty { <p class="hint">No tool servers registered yet (Settings → Tools).</p> }
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
          <legend>Learning <small>skills: written-down know-how agents use and improve</small></legend>
          <div class="seg">
            @for (m of learningModes; track m.value) {
              <label class="seg-opt" [class.on]="d.learning_mode === m.value">
                <input type="radio" name="learnmode" [value]="m.value" [(ngModel)]="d.learning_mode"> {{ m.label }}</label>
            }
          </div>
          <p class="hint inline-hint">{{ learningHint(d.learning_mode) }} Every agent can use skills either way
            (Settings → Skills).</p>
          <label class="check"><input type="checkbox" name="useprofile" [(ngModel)]="d.use_profile">
            Knows your profile <small>(Settings → Profile, in its instructions every time)</small></label>
        </fieldset>

        <fieldset>
          <legend>Sub-agents <small>temporary helpers this agent creates for parts of a task</small></legend>
          <div class="seg">
            @for (m of modes; track m.value) {
              <label class="seg-opt" [class.on]="d.subagent_mode === m.value">
                <input type="radio" name="submode" [value]="m.value" [(ngModel)]="d.subagent_mode"> {{ m.label }}</label>
            }
          </div>
          <p class="hint inline-hint">{{ modeHint(d.subagent_mode) }}</p>
          @if (d.subagent_mode !== 'off') {
            <div class="limit-grid">
              <label>At once<input type="number" min="1" max="10" name="sl_conc" [(ngModel)]="d.subagent_limits.concurrent"></label>
              @if (d.subagent_mode === 'allowed') {
                <label>Per task<input type="number" min="1" max="20" name="sl_task" [(ngModel)]="d.subagent_limits.per_task"></label>
                <label>Per day<input type="number" min="1" max="200" name="sl_day" [(ngModel)]="d.subagent_limits.per_day"></label>
              }
              <label>Minutes each<input type="number" min="1" max="240" name="sl_min" [(ngModel)]="d.subagent_limits.max_minutes"></label>
              <label>Model requests each<input type="number" min="1" max="500" name="sl_req" [(ngModel)]="d.subagent_limits.max_requests"></label>
            </div>
            @if (d.subagent_mode === 'ask') {
              <p class="hint inline-hint">For requests these are the starting point; you set the real limits when you approve.</p>
            }

            <div class="sub-label">Models sub-agents may use</div>
            @for (m of d.subagent_limits.models; track $index) {
              <div class="chip-row"><span class="chip">{{ refLabel(m.provider_id, m.model) }}</span>
                <button type="button" class="ghost small" (click)="d.subagent_limits.models.splice($index, 1)">Remove</button></div>
            } @empty {
              <p class="hint inline-hint">None listed: sub-agents use this agent's own model
                ({{ d.model ? refLabel(d.provider_id, d.model) : 'not set' }}).</p>
            }
            <hq-model-picker label="Add model" [providers]="providers()" [optional]="true" noneLabel="Choose…"
              [(providerId)]="addProvider" [(modelName)]="addModel" />
            <div class="actions tight"><button type="button" class="ghost small" [disabled]="!addModel"
              (click)="addSubModel(d)">Add model</button></div>

            <div class="sub-label">Tools sub-agents may have</div>
            @if (subTools(d).length) {
              <label class="check"><input type="checkbox" [checked]="d.subagent_limits.tool_server_ids === null"
                (change)="toggleAllTools(d)"> All of this agent's tools</label>
              @if (d.subagent_limits.tool_server_ids !== null) {
                @for (t of subTools(d); track t.id) {
                  <label class="check"><input type="checkbox" [checked]="d.subagent_limits.tool_server_ids.includes(t.id!)"
                    (change)="toggle(d.subagent_limits.tool_server_ids, t.id!)"> {{ t.name }}</label>
                }
              }
            } @else {
              <p class="hint inline-hint">This agent has no non-desktop tools to share. Desktops are never given to
                sub-agents (two agents can't drive one desktop).</p>
            }
            <p class="hint inline-hint">Sub-agents can't delegate or create sub-agents, and are refused when they'd
              duplicate an agent you've defined. Team-wide caps are under Settings → Sub-agents.</p>
          }
        </fieldset>

        <fieldset>
          <legend>Vision model <small>looks at the desktop and attached images for this agent</small></legend>
          <p class="hint inline-hint">Optional. When set, this agent never receives screenshots itself: it gets
            look / locate / click_on tools (with a desktop tool server) and look_at_attachment, and this model does the looking.
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
          <label class="check"><input type="checkbox" name="seesimg" [(ngModel)]="d.sees_images">
            Model sees images: send image attachments straight to this agent's own model (it must be multimodal)</label>
          <p class="hint inline-hint">Off: the agent asks its vision model about attached images with look_at_attachment.
            With neither, it can read documents but not images.</p>
        </fieldset>

        <fieldset>
          <legend>Fallback model <small>used when the main model fails or gives up</small></legend>
          <p class="hint inline-hint">Optional. If the main model errors, loops, or calls <code>escalate</code>,
            the turn is rerun on this model. Keep the main model local and put a frontier model here.</p>
          <hq-model-picker label="Fallback model" [providers]="providers()" [optional]="true" noneLabel="No fallback"
            [(providerId)]="d.fallback_provider_id" [(modelName)]="d.fallback_model" />
        </fieldset>

        <label>Desktop viewer URL <small>optional KasmVNC/noVNC address for this agent's desktop</small>
          <input name="desktop_url" [(ngModel)]="d.desktop_url" placeholder="/desktops/desktop-1/"></label>
        <div class="inline">
          <label class="check"><input type="checkbox" name="is_default" [(ngModel)]="d.is_default"> Default chat agent</label>
          <label class="check"><input type="checkbox" name="enabled" [(ngModel)]="d.enabled"> Enabled</label>
          <label class="check" title="Recall relevant memories before each job, memory tools, automatic capture of its results">
            <input type="checkbox" name="memory" [(ngModel)]="d.memory_enabled"> Long-term memory</label>
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
  ctx = signal<AgentContextInfo | null>(null);
  private loadCtx(id?: string | null) {
    this.ctx.set(null);
    if (id) this.api.agentContext(id).subscribe({ next: c => this.ctx.set(c), error: () => {} });
  }
  thinkingInfo = signal<ThinkingSupport | null>(null);
  private thinkingCheck = 0;
  /** Ask the backend whether a thinking level reaches the chosen model (after the picker updates the draft). */
  checkThinking() {
    queueMicrotask(() => {
      const d = this.draft();
      const n = ++this.thinkingCheck;
      this.thinkingInfo.set(null);
      if (!d?.model) return;
      this.api.thinkingSupport(d.provider_id, d.model).subscribe({
        next: t => { if (n === this.thinkingCheck) this.thinkingInfo.set(t); }, error: () => {},
      });
    });
  }
  error = signal('');
  providers = signal<ProviderInfo[]>([]);
  pools = computed(() => [...new Set(['default', ...this.agents().map(a => a.pool)])]);
  others = computed(() => this.agents().filter(a => a.id !== this.draft()?.id && a.enabled));

  modes = [
    { value: 'off', label: 'Off' },
    { value: 'ask', label: 'Ask me first' },
    { value: 'allowed', label: 'Allowed within limits' },
  ] as const;
  thinkingLevels = [
    { value: '', label: 'Model default' },
    { value: 'off', label: 'Off' },
    { value: 'on', label: 'On (provider\'s default effort)' },
    { value: 'minimal', label: 'Minimal' },
    { value: 'low', label: 'Low' },
    { value: 'medium', label: 'Medium' },
    { value: 'high', label: 'High' },
    { value: 'xhigh', label: 'Extra high' },
    { value: 'max', label: 'Max' },
  ] as const;
  learningModes = [
    { value: 'off', label: 'Off' },
    { value: 'propose', label: 'Propose to me' },
    { value: 'auto', label: 'Save automatically' },
  ] as const;
  addProvider: string | null = null;
  addModel = '';

  ngOnInit() { this.load(); }

  learningHint(mode: string) {
    if (mode === 'auto') return 'New skills and improvements this agent writes are saved at once; you\'re told, and can roll back.';
    if (mode === 'off') return 'This agent can\'t propose skills.';
    return 'New skills and improvements this agent writes wait in your Requests inbox for you to approve, edit or reject.';
  }

  thinkingHint(level: string) {
    if (!level) return 'Nothing is sent, so the model does what it does by default.';
    if (level === 'off') return 'Asks the model not to reason first: fastest and cheapest.';
    return 'Higher levels give better answers on hard tasks but are slower and, on local models, use more GPU time. Levels a model doesn\'t offer use the closest one it does.';
  }

  modeHint(mode: string) {
    if (mode === 'ask') return 'The agent can send you a request (which sub-agents, why, how much). It can only create them after you approve, within what you approve.';
    if (mode === 'allowed') return 'The agent may create sub-agents on its own, within these limits.';
    return 'This agent can\'t create sub-agents.';
  }

  refLabel(providerId: string | null, model: string) {
    const p = this.providers().find(x => x.id === providerId);
    return p ? `${model} @ ${p.name}` : model;
  }

  addSubModel(d: AgentDef) {
    if (!this.addModel) return;
    if (!d.subagent_limits.models.some(m => m.provider_id === this.addProvider && m.model === this.addModel)) {
      d.subagent_limits.models.push({ provider_id: this.addProvider, model: this.addModel });
    }
    this.addModel = '';
  }

  subTools(d: AgentDef) {
    return this.servers().filter(t => d.tool_server_ids.includes(t.id!) && !t.desktop);
  }

  toggleAllTools(d: AgentDef) {
    d.subagent_limits.tool_server_ids = d.subagent_limits.tool_server_ids === null
      ? this.subTools(d).map(t => t.id!) : null;
  }

  load() {
    this.api.agents().subscribe(a => this.agents.set(a));
    this.api.servers().subscribe(s => this.servers.set(s));
    this.api.providers().subscribe(p => this.providers.set(p));
  }

  edit(a: AgentDef | null) {
    this.error.set('');
    const d = a ? structuredClone(a) : blankAgent();
    d.subagent_mode ??= 'off';
    d.learning_mode ??= 'propose';
    d.use_profile ??= true;
    d.thinking ??= '';
    d.subagent_limits = { ...defaultSubagentLimits(), ...(d.subagent_limits ?? {}) };
    d.subagent_limits.models ??= [];
    if (!a) d.provider_id = this.providers().find(p => p.enabled)?.id ?? null;
    this.draft.set(d);
    this.loadCtx(a?.id);
    this.checkThinking();
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
      next: saved => { this.load(); this.draft.set(structuredClone(saved)); this.error.set(''); this.loadCtx(saved.id); },
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
