import { Component, OnInit, computed, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { RouterLink } from '@angular/router';
import { DatePipe } from '@angular/common';
import { AgentDef, Adoptable, Api, BlockedRequest, Resource, ResourceAction, ResourceKind, ResourceScope } from './api.service';

interface NewResource { kind: ResourceKind; scope: ResourceScope; description: string; network_access: boolean; agent_ids: string[]; }

/** Desktops and workbenches: create them, give them to agents, run their containers. */
@Component({
  selector: 'hq-resources',
  imports: [FormsModule, RouterLink, DatePipe],
  template: `
  <div class="split">
    <aside class="list">
      <div class="list-head"><h2>Resources</h2>
        <span class="inline">
          <button (click)="startNew('desktop')" title="A desktop with Chromium, driven by its MCP servers">+ Desktop</button>
          <button (click)="startNew('workbench')" title="A Python venv, files and shell">+ Workbench</button>
          <button class="ghost" [class.active]="network()" (click)="openNetwork()"
                  title="What every workbench may reach, and what was blocked">Network</button>
        </span></div>
      @if (!docker()) {
        <p class="hint">Agent HQ has no Docker access, so it can register a resource's tool servers but not
          run its container. Start the <code>docker-proxy</code> service and set <code>AHQ_DOCKER_URL</code>
          (README → Resources).</p>
      }
      @if (adoptable().length) {
        <div class="adopt">
          <p class="hint">Found in docker-compose.yml: <b>{{ adoptNames() }}</b>. Import them to give them to agents here.</p>
          <button (click)="adopt()">Import</button>
        </div>
      }
      @for (group of groups(); track group.kind) {
        <div class="side-label">{{ group.label }}</div>
        @for (r of group.items; track r.id) {
          <button class="row" [class.active]="selected()?.id === r.id" (click)="select(r)">
            <span class="name">{{ r.name }} <span class="pill">{{ r.scope }}</span>
              <span class="pill" [class.done]="r.status === 'running'" [class.failed]="r.status === 'error' || r.status === 'missing'">{{ r.status }}</span>
              @if (!r.managed) { <span class="pill">compose</span> }</span>
            <span class="sub">{{ agentNames(r) || 'no agent yet' }}@if (r.description) { · {{ r.description }} }</span>
          </button>
        } @empty { <p class="hint">None yet.</p> }
      }
    </aside>

    @if (network()) {
      <form class="editor" (ngSubmit)="saveDefaults()">
        <h2>Workbench network</h2>
        <p class="hint inline-hint">Workbenches reach the network only through the egress proxy. Each one may reach the
          hosts below, plus its own extra rules (on its page). Ticking <b>Internet and LAN access</b> on a workbench
          lets it reach everything. Changes apply within seconds, without a restart.</p>
        <label>Every workbench may reach <small>one rule per line</small>
          <textarea name="defaults" rows="6" [(ngModel)]="defaultsText"></textarea></label>
        <p class="hint inline-hint">{{ RULES_HELP }}</p>
        @if (error()) { <p class="error">{{ error() }}</p> }
        <div class="actions save-row">
          <button type="submit" [disabled]="busy() || defaultsText === savedDefaults">Save</button>
          <button type="button" class="ghost" (click)="defaultsText = savedDefaults" [disabled]="defaultsText === savedDefaults">Discard changes</button>
        </div>
        <fieldset><legend>Blocked lately <button type="button" class="ghost small-btn" (click)="openNetwork()">Refresh</button></legend>
          @for (b of allBlocked(); track b.workbench + b.host + b.port) {
            <div class="blocked">
              <span><code>{{ b.host }}:{{ b.port }}</code> <span class="muted small">{{ b.workbench }} · {{ b.at * 1000 | date:'MMM d, h:mm a' }}</span></span>
              <span class="inline">
                @if (byName(b.workbench); as w) {
                  <button type="button" class="ghost" (click)="allow(w, b)">Allow for {{ w.name }}</button>
                }
                <button type="button" class="ghost" (click)="allowForAll(b)">Allow for all</button>
              </span>
            </div>
          } @empty { <p class="hint inline-hint">Nothing blocked lately.</p> }
        </fieldset>
      </form>
    } @else if (creating(); as n) {
      <form class="editor" (ngSubmit)="create()">
        <h2>New {{ n.kind }}</h2>
        <p class="hint inline-hint">
          @if (n.kind === 'desktop') { A webtop desktop with Chromium. Agents get its screen tools and its browser
            (Playwright) tools, plus <code>ask_user_to_take_over</code>. They need a vision model. }
          @else { Its own Python venv, files and shell. Network: PyPI only (through the egress proxy) unless you allow more. }
        </p>
        <label>Description <small>optional, e.g. what it's logged in to</small><input name="desc" [(ngModel)]="n.description"></label>
        <label>Scope
          <select name="scope" [(ngModel)]="n.scope" (ngModelChange)="n.agent_ids = n.agent_ids.slice(0, 1)">
            <option value="dedicated">Dedicated: one agent's own</option>
            <option value="shared">Shared: several agents{{ n.kind === 'desktop' ? ', one conversation at a time' : '' }}</option>
          </select></label>
        @if (n.kind === 'workbench') {
          <label class="check"><input type="checkbox" name="net" [(ngModel)]="n.network_access"> Internet and LAN access</label>
        }
        <fieldset><legend>Give it to</legend>
          @if (n.scope === 'dedicated') {
            <select name="owner" [ngModel]="n.agent_ids[0] ?? ''" (ngModelChange)="n.agent_ids = $event ? [$event] : []">
              <option value="">Nobody yet</option>
              @for (a of agents(); track a.id) { <option [value]="a.id">{{ a.name }}</option> }
            </select>
          } @else {
            @for (a of agents(); track a.id) {
              <label class="check"><input type="checkbox" [checked]="n.agent_ids.includes(a.id!)"
                (change)="toggle(n.agent_ids, a.id!)"> {{ a.name }}</label>
            }
          }
        </fieldset>
        @if (error()) { <p class="error">{{ error() }}</p> }
        <div class="actions">
          <button type="submit" [disabled]="busy()">{{ busy() ? 'Creating…' : 'Create' }}</button>
          <button type="button" class="ghost" (click)="creating.set(null)">Cancel</button>
        </div>
      </form>
    } @else if (selected(); as r) {
      <form class="editor" (ngSubmit)="save(r)">
        <h2>{{ r.name }} <span class="pill">{{ r.kind }}</span>
          <span class="pill" [class.done]="r.status === 'running'" [class.failed]="r.status === 'error' || r.status === 'missing'">{{ r.status }}</span></h2>
        @if (r.error) { <p class="error">{{ r.error }}</p> }
        @if (error()) { <p class="error">{{ error() }}</p> }

        <label>Description<input name="desc" [(ngModel)]="edit.description"></label>
        <label>Scope
          <select name="scope" [(ngModel)]="edit.scope" (ngModelChange)="edit.scope === 'dedicated' && (edit.agent_ids = edit.agent_ids.slice(0, 1))">
            <option value="dedicated">Dedicated: one agent's own</option>
            <option value="shared">Shared: several agents{{ r.kind === 'desktop' ? ', one conversation at a time' : '' }}</option>
          </select></label>
        <fieldset><legend>Agents</legend>
          @if (edit.scope === 'dedicated') {
            <select name="owner" [ngModel]="edit.agent_ids[0] ?? ''" (ngModelChange)="edit.agent_ids = $event ? [$event] : []">
              <option value="">Nobody</option>
              @for (a of agents(); track a.id) { <option [value]="a.id">{{ a.name }}</option> }
            </select>
          } @else {
            @for (a of agents(); track a.id) {
              <label class="check"><input type="checkbox" [checked]="edit.agent_ids.includes(a.id!)"
                (change)="toggle(edit.agent_ids, a.id!)"> {{ a.name }}</label>
            }
            @if (r.kind === 'desktop') {
              <p class="hint inline-hint">One conversation uses it at a time; others wait for it (up to 10 minutes by default).
                @if (r.in_use_by) { <b>In use by {{ r.in_use_by.agent }}.</b> } @else { Free right now. }</p>
            }
          }
          @if (r.kind === 'desktop') {
            <p class="hint inline-hint">Agents with this desktop need a vision model (Settings → Agents).</p>
          }
        </fieldset>

        @if (r.kind === 'workbench') {
          <fieldset><legend>Network</legend>
            <label class="check" title="Everything, not only what the rules allow">
              <input type="checkbox" name="net" [(ngModel)]="edit.network_access"> Internet and LAN access</label>
            @if (edit.network_access) {
              <p class="hint inline-hint">It reaches everything directly; the rules below don't apply.</p>
            } @else {
              <p class="hint inline-hint">Every workbench may reach <code>{{ defaults().join(', ') || 'nothing' }}</code>
                (<a href="" (click)="$event.preventDefault(); openNetwork()">change</a>).</p>
              <label>This one may also reach <small>one rule per line</small>
                <textarea name="rules" rows="4" [(ngModel)]="edit.egress_text"
                  placeholder="gitgard.home.arpa:3000&#10;*.github.com&#10;192.168.10.0/24:443"></textarea></label>
              <p class="hint inline-hint">{{ RULES_HELP }}</p>
            }
            @if (r.proxy_current === false) {
              <p class="error">Its container was made before it had its own rules, so it gets only the defaults.
                <b>Recreate</b> it (below) to apply them; its files are kept.</p>
            }
            <div class="blocked-head"><b>Blocked lately</b>
              <button type="button" class="ghost small-btn" (click)="loadBlocked(r)">Refresh</button></div>
            @for (b of blocked(); track b.host + b.port) {
              <div class="blocked">
                <span><code>{{ b.host }}:{{ b.port }}</code> <span class="muted small">{{ b.at * 1000 | date:'MMM d, h:mm a' }}</span></span>
                <button type="button" class="ghost" (click)="allow(r, b)" [disabled]="busy()">Allow</button>
              </div>
            } @empty { <p class="hint inline-hint">Nothing blocked lately.</p> }
          </fieldset>
        }

        <div class="actions save-row">
          <button type="submit" [disabled]="!dirty(r) || busy()">{{ busy() ? 'Saving…' : 'Save' }}</button>
          <button type="button" class="ghost" (click)="revert(r)" [disabled]="!dirty(r) || busy()">Discard changes</button>
          @if (dirty(r)) { <span class="muted small">Unsaved changes</span> } @else if (saved()) { <span class="muted small">Saved</span> }
        </div>

        <fieldset><legend>Container</legend>
          <p class="hint inline-hint">
            @if (r.managed) { Agent HQ runs <code>{{ r.container || 'no container yet' }}</code>. <b>Recreate</b> makes a new
              container from the current image (after <code>docker compose --profile images build</code>) and keeps its data. }
            @else { docker compose runs <code>{{ r.container || r.name + ' (not found)' }}</code>. To manage it here, click
              <b>Take over</b>: Agent HQ replaces it with its own container on the same data. Then delete its block from
              docker-compose.yml before your next <code>docker compose up</code>. }
          </p>
          <div class="actions">
            @if (r.viewer_url) { <button type="button" class="ghost" (click)="openViewer(r.viewer_url)">Open viewer</button> }
            @if (r.status === 'running') {
              <button type="button" class="ghost" (click)="act(r, 'restart')" [disabled]="busy()">Restart</button>
              <button type="button" class="ghost" (click)="act(r, 'stop')" [disabled]="busy()">Stop</button>
            } @else if (r.container && r.status !== 'missing') {
              <button type="button" (click)="act(r, 'start')" [disabled]="busy()">Start</button>
            }
            @if (r.managed) {
              <button type="button" class="ghost" (click)="act(r, 'recreate')" [disabled]="busy()">Recreate</button>
            } @else {
              <button type="button" (click)="takeOver(r)" [disabled]="busy()">Take over</button>
            }
          </div>
        </fieldset>

        <fieldset><legend>Tool servers</legend>
          <ul class="servers">
            @for (t of r.servers; track t.id) {
              <li><code>{{ t.name }}</code><span class="muted small">{{ t.url }}</span></li>
            }
          </ul>
          <p class="hint inline-hint">Set their approvals and shown tools on <a routerLink="/settings/tools">Tools</a>.</p>
        </fieldset>

        <div class="actions">
          @if (!r.managed) {
            <button type="button" class="ghost" (click)="detach(r)">Detach</button>
          }
          <button type="button" class="danger" (click)="remove(r)">Delete</button>
        </div>
      </form>
    } @else {
      <div class="empty">Select a desktop or workbench, or create one. Each is a container of its own,
        dedicated to one agent or shared by several.</div>
    }
  </div>`,
  styles: [`/* The list is as wide as its contents need (header buttons, pills), within limits. */
            .split { grid-template-columns: max-content minmax(0, 1fr); }
            .list { min-width: 280px; max-width: 460px; overflow-x: hidden; }
            .list-head .inline { display: flex; gap: 6px; flex-wrap: nowrap; }
            .list-head button { white-space: nowrap; }
            .save-row { margin-bottom: 20px; }
            .blocked { display: flex; justify-content: space-between; align-items: center; gap: 10px; padding: 4px 0; }
            .blocked .inline { display: flex; gap: 6px; }
            .blocked-head { display: flex; align-items: center; gap: 10px; margin: 12px 0 4px; }
            .small-btn { padding: 2px 8px; font-size: 12px; }
            button.active { border-color: var(--accent, #4a9eff); }
            .servers { list-style: none; margin: 4px 0 10px; padding: 0; display: flex; flex-direction: column; gap: 8px; }
            .servers li { display: flex; flex-direction: column; gap: 2px; }
            fieldset .inline-hint { margin-top: 8px; }
            .adopt { display: flex; gap: 8px; align-items: center; margin: 6px 0 10px; }
            .adopt .hint { margin: 0; flex: 1; }
`],
})
export class ResourcesPage implements OnInit {
  private api = inject(Api);
  list = signal<Resource[]>([]);
  adoptable = signal<Adoptable[]>([]);
  docker = signal(true);
  agents = signal<AgentDef[]>([]);
  selectedId = signal<string | null>(null);
  creating = signal<NewResource | null>(null);
  busy = signal(false);
  error = signal('');
  /** The form: nothing changes until Save. */
  edit: { description: string; scope: ResourceScope; network_access: boolean; agent_ids: string[]; egress_text: string } =
    { description: '', scope: 'dedicated', network_access: false, agent_ids: [], egress_text: '' };
  saved = signal(false);
  readonly RULES_HELP = 'A host (pypi.org: ports 80 and 443), *.domain (it and its subdomains), host:port '
    + '(or host:* for any port), an address (192.168.10.5) or a network (192.168.10.0/24:443; names that '
    + 'resolve into it count too).';
  network = signal(false);
  defaults = signal<string[]>([]);
  defaultsText = '';
  savedDefaults = '';
  allBlocked = signal<BlockedRequest[]>([]);
  blocked = signal<BlockedRequest[]>([]);

  selected = computed(() => this.list().find(r => r.id === this.selectedId()) ?? null);
  groups = computed(() => [
    { kind: 'desktop', label: 'Desktops', items: this.list().filter(r => r.kind === 'desktop') },
    { kind: 'workbench', label: 'Workbenches', items: this.list().filter(r => r.kind === 'workbench') },
  ]);
  adoptNames = computed(() => this.adoptable().map(a => a.name).join(', '));

  ngOnInit() {
    this.load();
    this.api.agents().subscribe(a => this.agents.set(a.filter(x => !x.is_subagent)));
  }

  load(then?: () => void) {
    this.api.resources().subscribe({
      next: l => { this.list.set(l.resources); this.adoptable.set(l.adoptable); this.docker.set(l.docker);
                   this.defaults.set(l.egress_defaults ?? []); then?.(); },
      error: e => this.error.set(e.error?.detail ?? 'Could not load resources'),
    });
  }

  private put(r: Resource) {
    this.list.update(l => l.some(x => x.id === r.id) ? l.map(x => x.id === r.id ? r : x) : [...l, r]);
  }

  select(r: Resource) {
    const cur = this.selected();
    if (cur && cur.id !== r.id && this.dirty(cur) && !confirm(`Discard your unsaved changes to ${cur.name}?`)) return;
    this.creating.set(null); this.network.set(false); this.error.set(''); this.saved.set(false);
    this.selectedId.set(r.id);
    this.revert(r);
    this.blocked.set([]);
    if (r.kind === 'workbench') this.loadBlocked(r);
  }

  private lines(text: string) { return text.split('\n').map(x => x.trim()).filter(Boolean); }
  private sameRules(a: string[], b: string[]) { return a.length === b.length && a.every((x, i) => x === b[i]); }

  loadBlocked(r: Resource) {
    this.api.blockedRequests(r.id).subscribe({ next: b => this.blocked.set(b), error: () => {} });
  }

  /** Allow a blocked request: stored at once, and kept in the form you're editing. */
  allow(r: Resource, b: BlockedRequest) {
    const rule = `${b.host}:${b.port}`;
    this.busy.set(true); this.error.set('');
    this.api.allowEgress(r.id, rule).subscribe({
      next: x => {
        this.busy.set(false); this.put(x);
        if (this.selectedId() === x.id && !this.lines(this.edit.egress_text).includes(rule)) {
          this.edit.egress_text = [...this.lines(this.edit.egress_text), rule].join('\n');
        }
        this.blocked.update(l => l.filter(y => !(y.host === b.host && y.port === b.port)));
        this.allBlocked.update(l => l.filter(y => !(y.workbench === b.workbench && y.host === b.host && y.port === b.port)));
      },
      error: this.fail,
    });
  }

  byName(name: string) { return this.list().find(r => r.name === name && r.kind === 'workbench') ?? null; }

  openNetwork() {
    const cur = this.selected();
    if (cur && this.dirty(cur) && !confirm(`Discard your unsaved changes to ${cur.name}?`)) return;
    this.selectedId.set(null); this.creating.set(null); this.error.set('');
    this.network.set(true);
    this.api.egressNetwork().subscribe({
      next: n => { this.applyNetwork(n.defaults); this.allBlocked.set(n.blocked); }, error: this.fail,
    });
  }

  private applyNetwork(defaults: string[]) {
    this.defaults.set(defaults);
    this.defaultsText = this.savedDefaults = defaults.join('\n');
  }

  saveDefaults() {
    this.busy.set(true); this.error.set('');
    this.api.setEgressDefaults(this.lines(this.defaultsText)).subscribe({
      next: n => { this.busy.set(false); this.applyNetwork(n.defaults); this.allBlocked.set(n.blocked); },
      error: this.fail,
    });
  }

  allowForAll(b: BlockedRequest) {
    const rule = `${b.host}:${b.port}`;
    const rules = [...this.lines(this.savedDefaults), rule];
    this.api.setEgressDefaults(rules).subscribe({
      next: n => { this.applyNetwork(n.defaults);
                   this.allBlocked.set(n.blocked.filter(y => !(y.host === b.host && y.port === b.port))); },
      error: this.fail,
    });
  }

  revert(r: Resource) {
    this.edit = { description: r.description, scope: r.scope, network_access: r.network_access,
                  agent_ids: r.agents.map(a => a.id), egress_text: (r.egress_rules ?? []).join('\n') };
  }

  dirty(r: Resource) {
    const e = this.edit, ids = r.agents.map(a => a.id);
    return e.description !== r.description || e.scope !== r.scope || e.network_access !== r.network_access
      || e.agent_ids.length !== ids.length || e.agent_ids.some(i => !ids.includes(i))
      || (r.kind === 'workbench' && !this.sameRules(this.lines(e.egress_text), r.egress_rules ?? []));
  }

  startNew(kind: ResourceKind) {
    const cur = this.selected();
    if (cur && this.dirty(cur) && !confirm(`Discard your unsaved changes to ${cur.name}?`)) return;
    this.error.set(''); this.selectedId.set(null); this.network.set(false);
    this.creating.set({ kind, scope: 'dedicated', description: '', network_access: false, agent_ids: [] });
  }

  openViewer(url: string) { window.open(url, '_blank', 'noopener'); }

  agentNames(r: Resource) { return r.agents.map(a => a.name).join(', '); }
  toggle(list: string[], id: string) { const i = list.indexOf(id); i >= 0 ? list.splice(i, 1) : list.push(id); }

  private fail = (e: any) => { this.busy.set(false); this.error.set(e.error?.detail ?? 'That didn\'t work'); };

  create() {
    const n = this.creating()!;
    this.busy.set(true); this.error.set('');
    this.api.createResource(n).subscribe({
      next: r => { this.busy.set(false); this.creating.set(null); this.put(r); this.select(r); },
      error: this.fail,
    });
  }

  /** Save the form. Going shared, the scope changes before more agents are added; going
   *  dedicated, the extra agents are taken off first. */
  save(r: Resource) {
    const e = { ...this.edit, agent_ids: [...this.edit.agent_ids] };
    const details = { description: e.description, scope: e.scope, network_access: e.network_access,
                      ...(r.kind === 'workbench' ? { egress_rules: this.lines(e.egress_text) } : {}) };
    const ids = r.agents.map(a => a.id);
    const agentsChanged = e.agent_ids.length !== ids.length || e.agent_ids.some(i => !ids.includes(i));
    const update = () => this.api.updateResource(r.id, details);
    const assign = () => this.api.assignResource(r.id, e.agent_ids);
    const [first, second] = e.scope === 'shared' ? [update, assign] : [assign, update];
    const steps = agentsChanged ? [first, second] : [update];
    this.busy.set(true); this.error.set(''); this.saved.set(false);
    const run = (i: number, last: Resource) => {
      if (i === steps.length) {
        this.busy.set(false); this.put(last); this.revert(last); this.saved.set(true);
        return;
      }
      steps[i]().subscribe({ next: x => run(i + 1, x), error: err => { this.fail(err); this.load(); } });
    };
    run(0, r);
  }

  act(r: Resource, action: ResourceAction) {
    this.busy.set(true); this.error.set('');
    this.api.resourceAction(r.id, action).subscribe({
      next: x => { this.busy.set(false); this.put(x); }, error: e => { this.fail(e); this.load(); },
    });
  }

  takeOver(r: Resource) {
    if (!confirm(`Take over ${r.name}? Its compose container is replaced by one Agent HQ runs, on the same data. ` +
                 'Afterwards, delete its block from docker-compose.yml before your next `docker compose up`, ' +
                 'or compose starts a second one.')) return;
    this.act(r, 'take-over');
  }

  adopt() {
    this.api.adoptResources().subscribe({ next: () => this.load(), error: this.fail });
  }

  detach(r: Resource) {
    if (!confirm(`Stop tracking ${r.name} here? Its tool servers stay as plain tool servers.`)) return;
    this.api.detachResource(r.id).subscribe({ next: () => { this.selectedId.set(null); this.load(); }, error: this.fail });
  }

  remove(r: Resource) {
    if (!confirm(`Delete ${r.name}? It is taken from ${r.agents.length ? this.agentNames(r) : 'no agents'} and its tool servers go too.` +
                 (r.managed ? ' Its container is removed.' : ' docker compose keeps running its container.'))) return;
    const data = r.managed && confirm(`Also delete ${r.name}'s data (its files, browser profile and logins)? ` +
                                      'Cancel keeps it for a new ' + r.kind + ' with the same name.');
    this.api.deleteResource(r.id, data).subscribe({ next: () => { this.selectedId.set(null); this.load(); }, error: this.fail });
  }
}
