import { Component, OnInit, computed, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { Router, RouterLink } from '@angular/router';
import { AgentDef, Api, ProviderInfo, ToolServer, blankAgent, defaultSubagentLimits, AgentContextInfo, ThinkingSupport, Resource, ResourceKind } from './api.service';
import { ModelPicker } from './model-picker';
import { Avatar } from './avatar';

@Component({
  selector: 'hq-agents',
  imports: [FormsModule, ModelPicker, Avatar, RouterLink],
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
        <div class="model-row">
          <label>Thinking <small>whether the model reasons before it answers</small>
            <select name="thinking" [ngModel]="thinkingMode(d)" (ngModelChange)="setThinkingMode(d, $event)">
              @for (t of thinkingModes; track t.value) { <option [value]="t.value">{{ t.label }}</option> }
            </select></label>
          <label>Reasoning level <small>how hard it thinks when thinking is on</small>
            <select name="reasoning" [ngModel]="reasoningLevel(d)" (ngModelChange)="setReasoningLevel(d, $event)"
              [disabled]="thinkingMode(d) !== 'on' || thinkingInfo()?.levels === false">
              @for (t of reasoningLevels; track t.value) { <option [value]="t.value">{{ t.label }}</option> }
            </select></label>
        </div>
        <p class="hint inline-hint" [class.needs-hint]="d.thinking && thinkingInfo()?.supported === false">
          {{ thinkingHint(d) }}
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
        <label class="narrow">Max output tokens <small>per response, thinking included; blank = no limit</small>
          <input name="maxout" type="number" min="256" step="1024" [(ngModel)]="d.max_output_tokens"
            placeholder="No limit"></label>
        <label class="narrow">Request time limit <small>seconds per model request; blank = no limit</small>
          <input name="reqtimeout" type="number" min="10" step="30" [(ngModel)]="d.request_timeout_s"
            placeholder="No limit"></label>
        <p class="hint inline-hint">
          Guardrails for its main model, so a local model that keeps thinking fails in minutes instead
          of running on. If it hits one, it tries once more with less thinking, then hands over to its
          fallback model. They don't apply to the fallback. Leave room for big files: one response can
          hold a whole file the agent writes.
        </p>
        <label>Worker pool <small>which workers run this agent, e.g. default · gpu · desktop</small>
          <input name="pool" [(ngModel)]="d.pool" list="pools"></label>
        <datalist id="pools">@for (p of pools(); track p) { <option [value]="p"></option> }</datalist>

        <fieldset>
          <legend>Tools (MCP servers)</legend>
          @for (t of plainServers(); track t.id) {
            @if (t.all_agents) {
              <label class="check" title="Set on Settings → Tools: every agent gets it"><input type="checkbox" checked disabled>
                {{ t.name }} <small>all agents · {{ t.description }}</small></label>
            } @else {
              <label class="check"><input type="checkbox" [checked]="d.tool_server_ids.includes(t.id!)"
                (change)="toggle(d.tool_server_ids, t.id!)"> {{ t.name }} <small>{{ t.description }}</small></label>
            }
          } @empty { <p class="hint">No tool servers registered yet (Settings → Tools).</p> }
        </fieldset>

        <fieldset>
          <legend>Desktops &amp; workbenches</legend>
          @for (r of resources(); track r.id) {
            <label class="check" [class.muted]="takenBy(d, r)">
              <input type="checkbox" [checked]="hasRes(d, r)" [disabled]="!!takenBy(d, r)" (change)="toggleRes(d, r)">
              {{ r.name }} <span class="pill">{{ r.scope }}</span>
              <small>{{ takenBy(d, r) ? 'dedicated to ' + takenBy(d, r) : r.description }}</small></label>
          } @empty { <p class="hint inline-hint">None yet.</p> }
          <div class="actions">
            <button type="button" class="ghost" (click)="newOwn(d, 'desktop')" [disabled]="!d.id || resBusy()">+ Its own desktop</button>
            <button type="button" class="ghost" (click)="newOwn(d, 'workbench')" [disabled]="!d.id || resBusy()">+ Its own workbench</button>
          </div>
          <p class="hint inline-hint">@if (!d.id) { Save the agent first to give it its own. }
            Shared ones and containers are on <a routerLink="/settings/resources">Desktops &amp; workbenches</a>.</p>
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
          <legend>Standing goals <small>outcomes it keeps working until they're done (the Board)</small></legend>
          <label class="inline-num">Goals at once (WIP limit)
            <input name="goalwip" type="number" min="0" max="10" [(ngModel)]="d.goal_wip"></label>
          <p class="hint inline-hint">It pulls the next goal from Ready when it has a free slot, and works one next
            action at a time. Goals that are in progress, waiting or blocked count. 0 = it takes no goals.</p>
          <label class="inline-num">Team size (parallel actions per goal)
            <input name="goalteam" type="number" min="1" max="10" [(ngModel)]="d.goal_team_size"
                   [disabled]="d.subagent_mode === 'off' && !d.delegates.length"></label>
          <p class="hint inline-hint">
            @if (d.subagent_mode === 'off' && !d.delegates.length) { Give it agents to delegate to above, or turn on sub-agents below, to let it form a team. }
            @else { Itself plus up to {{ (d.goal_team_size ?? 1) - 1 }} team member(s) working other next actions of
              the same goal in parallel{{ d.delegates.length ? ': the agents it can delegate to first, each on one action at a time' : '' }}{{
              d.subagent_mode !== 'off' ? (d.delegates.length ? ', then sub-agents' : ': sub-agents') + ' within its sub-agent limits' + (d.subagent_mode === 'ask' ? ' and after you approve its request' : '') : '' }}.
              1 = no team. You still confirm when a goal is done.
              @if (d.subagent_mode === 'allowed' && !d.delegates.length && (d.goal_team_size ?? 1) - 1 > (d.subagent_limits.concurrent ?? 1)) {
                <span class="warn-line">Its sub-agent limit allows {{ d.subagent_limits.concurrent }} at once, so the team
                  is {{ 1 + (d.subagent_limits.concurrent ?? 1) }} until you raise “At once” below.</span> } }</p>
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
            @if (savedEnabled(d)) {
              <button type="button" class="danger" (click)="remove(d)">Disable</button>
            } @else {
              <button type="button" class="danger" (click)="purge(d)"
                      title="Deletes the agent and its conversations, schedules, memories and skills for good">Delete permanently</button>
            }
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
  thinkingModes = [
    { value: '', label: 'Model default' },
    { value: 'off', label: 'Off' },
    { value: 'on', label: 'On' },
  ] as const;
  reasoningLevels = [
    { value: '', label: 'Provider default' },
    { value: 'minimal', label: 'Minimal' },
    { value: 'low', label: 'Low' },
    { value: 'medium', label: 'Medium' },
    { value: 'high', label: 'High' },
    { value: 'xhigh', label: 'Extra high' },
    { value: 'max', label: 'Max' },
  ] as const;
  /** The level last chosen, restored when thinking is switched off and back on. */
  private lastLevel = '';

  // The two boxes edit the agent's one `thinking` field: '' | off | on | <level>.
  thinkingMode(d: AgentDef): '' | 'off' | 'on' {
    const t = d.thinking ?? '';
    return t === '' ? '' : t === 'off' ? 'off' : 'on';
  }

  reasoningLevel(d: AgentDef): string {
    const t = d.thinking ?? '';
    return t === '' || t === 'off' || t === 'on' ? '' : t;
  }

  setThinkingMode(d: AgentDef, mode: '' | 'off' | 'on') {
    const level = this.reasoningLevel(d);
    if (level) this.lastLevel = level;
    d.thinking = mode === 'on' ? ((this.lastLevel || 'on') as AgentDef['thinking']) : mode;
  }

  setReasoningLevel(d: AgentDef, level: string) {
    this.lastLevel = level;
    d.thinking = (level || 'on') as AgentDef['thinking'];
  }
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

  thinkingHint(d: AgentDef) {
    const mode = this.thinkingMode(d);
    if (!mode) return 'Nothing is sent, so the model does what it does by default.';
    if (mode === 'off') return 'Asks the model not to reason first: fastest and cheapest.';
    if (this.thinkingInfo()?.levels === false) return 'This model only turns thinking on or off, so there\'s no level to pick.';
    if (!this.reasoningLevel(d)) return 'Thinking on, at the provider\'s default effort.';
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

  /** Tool servers that aren't part of a desktop or workbench resource. */
  plainServers = computed(() => this.servers().filter(t => !t.resource_id));
  resources = signal<Resource[]>([]);
  resBusy = signal(false);

  hasRes(d: AgentDef, r: Resource) { return r.servers.some(t => d.tool_server_ids.includes(t.id)); }
  /** The other agent a dedicated resource belongs to, if any. */
  takenBy(d: AgentDef, r: Resource) {
    return r.scope === 'dedicated' ? (r.agents.find(a => a.id !== d.id)?.name ?? '') : '';
  }
  toggleRes(d: AgentDef, r: Resource) {
    const ids = r.servers.map(t => t.id);
    if (this.hasRes(d, r)) {
      d.tool_server_ids = d.tool_server_ids.filter(i => !ids.includes(i));
      if (r.viewer_url && d.desktop_url === r.viewer_url) d.desktop_url = null;
    } else {
      d.tool_server_ids = [...d.tool_server_ids, ...ids.filter(i => !d.tool_server_ids.includes(i))];
      if (r.viewer_url && !d.desktop_url) d.desktop_url = r.viewer_url;
    }
  }
  /** Create a dedicated desktop or workbench for this (saved) agent and tick it. */
  newOwn(d: AgentDef, kind: ResourceKind) {
    this.resBusy.set(true); this.error.set('');
    this.api.createResource({ kind, scope: 'dedicated', agent_ids: [d.id!] }).subscribe({
      next: r => {
        this.resBusy.set(false);
        this.servers.update(list => [...list, ...r.servers.map(t => ({ ...t, resource_id: r.id }) as unknown as ToolServer)]);
        this.toggleRes(d, r);
        this.resources.update(list => [...list, r]);
        this.api.servers().subscribe(s => this.servers.set(s));
        if (r.error) this.error.set(`${r.name} was created, but: ${r.error}`);
      },
      error: e => { this.resBusy.set(false); this.error.set(e.error?.detail ?? 'Could not create it'); },
    });
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
    this.api.resources().subscribe({ next: l => this.resources.set(l.resources), error: () => {} });
  }

  edit(a: AgentDef | null) {
    this.error.set('');
    const d = a ? structuredClone(a) : blankAgent();
    d.subagent_mode ??= 'off';
    d.learning_mode ??= 'propose';
    d.use_profile ??= true;
    d.goal_wip ??= 1;
    d.goal_team_size ??= 1;
    d.thinking ??= '';
    this.lastLevel = this.reasoningLevel(d);
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
    if (!confirm(`Disable ${d.name}? Old conversations stay readable. Once disabled, you can delete it permanently.`)) return;
    this.api.deleteAgent(d.id!).subscribe(() => { this.draft.set(null); this.load(); });
  }

  /** Enabled as saved, not as the checkbox currently shows. */
  savedEnabled(d: AgentDef) {
    return this.agents().find(a => a.id === d.id)?.enabled ?? true;
  }

  purge(d: AgentDef) {
    this.api.agentDeletePlan(d.id!).subscribe({
      next: p => {
        if (!p.can_delete) { this.error.set(p.reason ?? 'This agent can\'t be deleted right now'); return; }
        const n = (k: number, one: string, many = one + 's') => `${k} ${k === 1 ? one : many}`;
        // Only what's there, on one line: Chrome's prompt box is short and cuts long text off.
        const parts = [
          p.conversations ? n(p.conversations, 'conversation') + ` (${n(p.messages, 'message')})` : '',
          p.schedules ? n(p.schedules, 'schedule') : '',
          p.subscriptions ? n(p.subscriptions, 'event subscription') : '',
          p.memories ? n(p.memories, 'memory', 'memories') : '',
          p.skills ? n(p.skills, 'skill') : '',
          p.test_cases ? n(p.test_cases, 'test case') : '',
          p.subagents ? n(p.subagents, 'sub-agent') : '',
        ].filter(Boolean);
        const also = parts.length ? `\n\nAlso deletes: ${parts.join(', ')}.` : '';
        const goals = p.goals_unassigned ? `\n${n(p.goals_unassigned, 'goal')} it owns will go back to your backlog.` : '';
        const typed = prompt(`To permanently delete ${d.name}, type its name below: ${d.name}\n`
          + `This can't be undone.${also}${goals}\n\nUsage history and team memories, skills and knowledge are kept.`);
        if (typed === null) return;
        if (typed.trim() !== d.name) { this.error.set('The name didn\'t match, so nothing was deleted'); return; }
        this.api.purgeAgent(d.id!).subscribe({
          next: () => { this.draft.set(null); this.error.set(''); this.load(); },
          error: e => this.error.set(e.error?.detail ?? `Delete failed (${e.status} ${e.statusText || 'no response'})`),
        });
      },
      error: e => this.error.set(e.error?.detail ?? `Delete failed (${e.status} ${e.statusText || 'no response'})`),
    });
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
