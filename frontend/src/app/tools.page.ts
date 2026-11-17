import { Component, OnInit, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { SlicePipe } from '@angular/common';
import { RouterLink } from '@angular/router';
import { Api, ApprovalMode, CatalogPackage, CatalogRemote, CatalogServer, ProbedTool, ToolServer, blankServer } from './api.service';

interface Install { entry: CatalogServer; index: number; pkg: CatalogPackage; name: string;
  env: Record<string, string>; argsText: string; egressText: string; }

const SEALED = 'enc:v1:';
const KEPT = '(saved, encrypted)';

@Component({
  selector: 'hq-tools',
  imports: [FormsModule, SlicePipe, RouterLink],
  template: `
  <div class="split">
    <aside class="list">
      <div class="list-head"><h2>Tool servers</h2>
        <span class="inline"><button class="ghost" [class.active]="browsing()" (click)="browse()"
          title="Browse the MCP server catalog: servers others have published, ready to add">Catalog</button>
        <button (click)="edit(null)">+ New</button></span></div>
      @for (t of servers(); track t.id) {
        <button class="row" [class.active]="draft()?.id === t.id" [class.muted]="!t.enabled" (click)="edit(t)">
          <span class="name">{{ t.name }} <span class="pill">{{ t.transport }}</span>
            @if (t.all_agents) { <span class="pill">all agents</span> }
            @if (t.resource_id) { <span class="pill">resource</span> }</span>
          <span class="sub">{{ t.description || t.url || t.command }}</span>
          @if (t.catalog_name) { <span class="sub muted small">from the catalog: {{ t.catalog_name }}</span> }
        </button>
      } @empty { <p class="hint">Register an MCP server, then tick it on any agent (or on all of them).</p> }
    </aside>

    @if (browsing()) {
      <section class="editor">
        <h2>MCP server catalog</h2>
        <p class="hint inline-hint">Servers published to the MCP registry ({{ registry() || 'not loaded yet' }}).
          A listing is a description by its publisher, not a review: check the repository before you add one.
          Whatever you add starts with every tool set to <b>Ask me</b> and isn't given to any agent.</p>
        <form class="inline" (ngSubmit)="search()">
          <input name="q" [(ngModel)]="query" placeholder="Search, e.g. gitea, proxmox, home assistant" autofocus>
          <button type="submit">Search</button>
        </form>
        @if (catalogError()) { <p class="error">{{ catalogError() }}</p> }
        @if (installing(); as n) {
          <form class="install" (ngSubmit)="install(n)">
            <h3>Install {{ n.entry.title || n.entry.name }}</h3>
            <p class="hint inline-hint">Runs <code>{{ n.pkg.registry === 'npm' ? 'npx' : 'uvx' }} {{ n.pkg.identifier }}{{ n.pkg.version ? (n.pkg.registry === 'npm' ? '@' : '==') + n.pkg.version : '' }}</code>
              in a container of its own (like a workbench: locked down, online only through the egress proxy).
              @if (n.entry.repository) { Check the <a [href]="n.entry.repository" target="_blank" rel="noopener">repository</a> first. }</p>
            <label>Name <small>its tool server, container and network name</small>
              <input name="iname" [(ngModel)]="n.name" required></label>
            @for (e of n.pkg.env; track e.name) {
              <label>{{ e.name }} @if (e.required) { <small>required</small> } @if (e.secret) { <small>secret, stored encrypted</small> }
                @if (e.description) { <small>{{ e.description }}</small> }
                <input [name]="'env_' + e.name" [type]="e.secret ? 'password' : 'text'" [(ngModel)]="n.env[e.name]"
                  autocomplete="off"></label>
            }
            <label>Arguments <small>one per line; replace anything in &lt;angle brackets&gt;</small>
              <textarea name="iargs" rows="3" [(ngModel)]="n.argsText"></textarea></label>
            @for (a of n.pkg.arg_inputs; track a.name) {
              <p class="hint inline-hint"><b>{{ a.name }}</b>{{ a.required ? ' (required)' : '' }}: {{ a.description || 'see the repository' }}</p>
            }
            <label>It may also reach <small>one rule per line, besides PyPI{{ n.pkg.registry === 'npm' ? ' and the npm registry' : '' }}; e.g. the API it talks to</small>
              <textarea name="iegress" rows="2" [(ngModel)]="n.egressText" placeholder="api.example.com&#10;gitgard.home.arpa:3000"></textarea></label>
            <p class="hint inline-hint">Every tool starts on <b>Ask me</b> and no agent has it until you tick it. If it needs a
              host you didn't list, its page under Desktops &amp; workbenches shows the blocked request with an Allow button.</p>
            <div class="actions">
              <button type="submit" [disabled]="busy()">{{ busy() ? 'Installing…' : 'Install' }}</button>
              <button type="button" class="ghost" (click)="installing.set(null)">Cancel</button>
            </div>
          </form>
        }
        @if (searching() && !results().length) { <p class="hint">Searching…</p> }
        @for (c of results(); track c.name) {
          <div class="catalog-entry">
            <div><b>{{ c.title || c.name }}</b> <span class="pill">v{{ c.version }}</span>
              @if (c.can_add) { <span class="pill">remote</span> }
              @for (p of c.packages; track $index) { <span class="pill">{{ p.registry }}</span> }
              @if (c.added_as) { <span class="pill done">added as {{ c.added_as }}</span> }</div>
            <div class="muted small"><code>{{ c.name }}</code> ·
              {{ c.publisher.kind === 'github' ? 'GitHub user ' + c.publisher.who : 'domain ' + c.publisher.who }}
              @if (c.repository) { · <a [href]="c.repository" target="_blank" rel="noopener">repository</a> }
              @if (c.website) { · <a [href]="c.website" target="_blank" rel="noopener">website</a> }</div>
            <div>{{ c.description }}</div>
            <div class="inline">
              @for (r of c.remotes; track $index) {
                @if (r.supported) {
                  <button type="button" (click)="addFromCatalog(c, $index)" [title]="r.url">Add {{ r.type }} remote</button>
                } @else { <span class="muted small">{{ r.note }}</span> }
              }
              @for (p of c.packages; track $index) {
                @if (p.supported) {
                  <button type="button" (click)="startInstall(c, $index)" [title]="p.identifier">Install {{ p.registry }} package</button>
                } @else { <span class="muted small">{{ p.identifier }}: {{ p.note }}</span> }
              }
            </div>
          </div>
        } @empty {
          @if (searched() && !searching()) { <p class="hint">Nothing found.</p> }
        }
        @if (nextCursor()) {
          <div class="actions"><button type="button" class="ghost" [disabled]="searching()" (click)="more()">More</button></div>
        }
      </section>
    } @else if (draft(); as d) {
      <form class="editor" (ngSubmit)="save()">
        <h2>{{ d.id ? 'Edit ' + d.name : 'New tool server' }}</h2>
        @if (d.resource_id) {
          <p class="hint inline-hint">This server belongs to a desktop, workbench or package server. Give it to agents
            (or tick it on an agent), run its container and delete it on
            <a routerLink="/settings/resources">Desktops &amp; workbenches</a>. Set its approvals here.</p>
        }
        @if (picked(); as p) {
          <p class="hint inline-hint">From the catalog: <code>{{ p.entry.name }}</code> v{{ p.entry.version }}
            @if (p.entry.repository) { (<a [href]="p.entry.repository" target="_blank" rel="noopener">repository</a>)}.
            Every tool is set to <b>Ask me</b>. Save, then <b>Test connection</b> to see its tools and relax
            the rules for the ones you trust. Then tick it on the agents that need it.</p>
          @for (h of p.remote.headers; track h.name) {
            <p class="hint inline-hint"><b>{{ h.name }}</b>{{ h.required ? ' (required)' : '' }}{{ h.secret ? ', secret' : '' }}:
              {{ h.description || 'fill in under Headers' }}</p>
          }
          @if (p.remote.variables.length) {
            <p class="hint inline-hint">Replace {{ p.remote.variables.join(', ') }} in the URL with your values.</p>
          }
        } @else if (d.catalog_name) {
          <p class="hint inline-hint">Added from the catalog: <code>{{ d.catalog_name }}</code> v{{ d.catalog_version }}.</p>
        }
        <label>Name <small>also used as the tool-name prefix</small><input name="name" [(ngModel)]="d.name" required></label>
        <label>Description<input name="description" [(ngModel)]="d.description"></label>
        <label>Transport
          <select name="transport" [(ngModel)]="d.transport">
            <option value="http">HTTP (streamable / SSE URL)</option>
            <option value="stdio">stdio (command run by the backend)</option>
          </select></label>
        @if (d.transport === 'http') {
          <label>URL<input name="url" [(ngModel)]="d.url" placeholder="http://perceptor:8811/mcp"></label>
          <label>Headers <small>Name: value, one per line; stored encrypted when AHQ_SECRET_KEY is set</small>
            <textarea name="headers" rows="2" [ngModel]="headersText(d)" (ngModelChange)="setHeaders(d, $event)"
              placeholder="Authorization: Bearer …"></textarea></label>
        } @else {
          <label>Command<input name="command" [(ngModel)]="d.command" placeholder="uvx"></label>
          <label>Arguments <small>one per line</small>
            <textarea name="args" rows="3" [ngModel]="d.args.join('\\n')"
              (ngModelChange)="d.args = splitLines($event)"></textarea></label>
          <label>Environment <small>KEY=value, one per line</small>
            <textarea name="env" rows="3" [ngModel]="envText(d)" (ngModelChange)="setEnv(d, $event)"></textarea></label>
        }
        <div class="inline">
          <label class="check"><input type="checkbox" name="prefix" [(ngModel)]="d.prefix_tools"> Prefix tool names</label>
          <label class="check" title="Its tools pause while you have control of the desktop, and agents using it can ask you to take over">
            <input type="checkbox" name="desktop" [(ngModel)]="d.desktop"> Controls an agent desktop</label>
          @if (!d.desktop && !d.resource_id) {
            <label class="check" title="Every agent gets this server without ticking it, e.g. MQTT or the web reader. Sub-agents too.">
              <input type="checkbox" name="all_agents" [(ngModel)]="d.all_agents"> All agents</label>
          }
          <label class="check" title="Every tool counts as read-only (as if the server marked it so): webhook-started work doesn't ask before them, and they start ticked as safe in tests. Only for servers you know can't change anything, e.g. gitea-mcp --read-only.">
            <input type="checkbox" name="readonly" [(ngModel)]="d.read_only"> Only reads</label>
          <label class="check"><input type="checkbox" name="enabled" [(ngModel)]="d.enabled"> Enabled</label>
        </div>
        @if (error()) { <p class="error">{{ error() }}</p> }
        <div class="actions">
          <button type="submit">Save</button>
          @if (d.id) {
            <button type="button" class="ghost" (click)="probe(d)">Test connection</button>
            @if (!d.resource_id) { <button type="button" class="danger" (click)="remove(d)">Delete</button> }
          }
        </div>
        @if (d.id) {
          <h3>Approvals</h3>
          <p class="hint inline-hint"><b>Allow</b> runs the call. <b>Ask me</b> makes the agent wait for your
            OK (in the chat, the Requests inbox and on MQTT). <b>Block</b> refuses it. Your rules here override
            what the server suggests. Sub-agents follow the same rules.</p>
          <table class="grid approvals">
            <tr><th title="Listed to agents. Untick tools agents never need: small local models choose better from a short list. Hidden tools aren't offered at all.">Shown</th><th>Tool</th><th>Approval</th><th title="In a test run (Settings → Evals), may this tool run for real when the test has no recorded result for the call? Only Allow tools ever do.">Safe in tests</th><th></th></tr>
            <tr><td></td><td><i>Every tool without its own rule</i></td>
              <td><select [name]="'rule_all'" [ngModel]="d.approval_rules['*'] ?? ''" (ngModelChange)="setRule(d, '*', $event)">
                <option value="">Server's suggestion, else allow</option>
                @for (m of modes; track m.value) { <option [value]="m.value">{{ m.label }}</option> }</select></td><td></td><td></td></tr>
            @for (t of tools(); track t.name) {
              <tr><td class="center"><input type="checkbox" [name]="'shown_' + t.name" [checked]="isShown(d, t)"
                  (change)="setShown(d, t, $any($event.target).checked)"></td>
                <td><code>{{ t.name }}</code><div class="muted small">{{ t.description | slice:0:140 }}</div></td>
                <td><select [name]="'rule_' + t.name" [ngModel]="d.approval_rules[t.name] ?? ''" (ngModelChange)="setRule(d, t.name, $event)">
                  <option value="">Default ({{ label(defaultFor(d, t)) }})</option>
                  @for (m of modes; track m.value) { <option [value]="m.value">{{ m.label }}</option> }</select></td>
                <td class="center"><input type="checkbox" [name]="'live_' + t.name" [checked]="safe(d, t)"
                  (change)="setSafe(d, t, $any($event.target).checked)"
                  [title]="t.read_only_hint ? 'The server says this tool only reads' : 'Changes things, as far as the server says'"></td>
                <td class="muted small">@if (t.suggested_approval && t.suggested_approval !== 'allow') { server suggests {{ label(t.suggested_approval) }} }
                  @if (t.read_only_hint) { read-only }</td></tr>
            }
            @for (name of ruleOnly(d); track name) {
              <tr><td></td><td><code>{{ name }}</code> <span class="muted small">(not offered right now)</span></td>
                <td><select [name]="'rule_' + name" [ngModel]="d.approval_rules[name]" (ngModelChange)="setRule(d, name, $event)">
                  <option value="">Remove rule</option>
                  @for (m of modes; track m.value) { <option [value]="m.value">{{ m.label }}</option> }</select></td><td></td><td></td></tr>
            }
          </table>
          <p class="hint inline-hint"><b>Shown</b>: {{ shownCount(d) }} of {{ tools().length }} tools are listed to agents.
            All ticked = every tool, including ones the server adds later.</p>
          <p class="hint inline-hint"><b>Safe in tests</b>: when a test case (Settings → Evals) has no recorded result for a
            call, a ticked tool set to Allow runs for real; others answer “not available in a test run”. Tools the
            server marks read-only start ticked.</p>
          @if (!tools().length) {
            <p class="hint inline-hint">Use <b>Test connection</b> to list this server's tools and set a rule per tool.</p>
          }
          <div class="actions"><button type="submit">Save approvals</button></div>
        }
      </form>
    } @else { <div class="empty">Select a tool server or register a new one.</div> }
  </div>`,
})
export class ToolsPage implements OnInit {
  private api = inject(Api);
  servers = signal<ToolServer[]>([]);
  draft = signal<ToolServer | null>(null);
  tools = signal<ProbedTool[]>([]);
  modes: { value: ApprovalMode; label: string }[] = [
    { value: 'allow', label: 'Allow' }, { value: 'ask', label: 'Ask me' }, { value: 'block', label: 'Block' }];
  error = signal('');
  // The catalog
  browsing = signal(false);
  query = '';
  results = signal<CatalogServer[]>([]);
  nextCursor = signal<string | null>(null);
  registry = signal('');
  searching = signal(false);
  searched = signal(false);
  catalogError = signal('');
  picked = signal<{ entry: CatalogServer; remote: CatalogRemote } | null>(null);
  installing = signal<Install | null>(null);
  busy = signal(false);
  private sealed: Record<string, string> = {};

  ngOnInit() { this.load(); }

  browse() {
    this.browsing.set(true); this.draft.set(null); this.installing.set(null);
    if (!this.searched()) this.search();
  }
  search(cursor: string | null = null) {
    this.searching.set(true); this.catalogError.set('');
    if (!cursor) { this.results.set([]); this.nextCursor.set(null); }
    this.api.catalog(this.query, cursor).subscribe({
      next: page => {
        this.registry.set(page.registry);
        this.results.set(cursor ? [...this.results(), ...page.servers] : page.servers);
        this.nextCursor.set(page.next_cursor); this.searching.set(false); this.searched.set(true);
      },
      error: e => { this.catalogError.set(e.error?.detail ?? 'Search failed'); this.searching.set(false); },
    });
  }
  more() { this.search(this.nextCursor()); }
  startInstall(entry: CatalogServer, index: number) {
    const pkg = entry.packages[index];
    this.catalogError.set('');
    this.installing.set({ entry, index, pkg, name: entry.suggested_name,
      env: Object.fromEntries(pkg.env.map(e => [e.name, e.value])), argsText: pkg.args.join('\n'), egressText: '' });
  }
  install(n: Install) {
    this.busy.set(true); this.catalogError.set('');
    this.api.installPackage({ entry: n.entry, package: n.index, name: n.name, env: n.env,
                              args: this.splitLines(n.argsText), egress_rules: this.splitLines(n.egressText) }).subscribe({
      next: r => {
        this.busy.set(false); this.installing.set(null); this.load(); this.edit(r.server);
        if (r.resource.error) this.error.set(`Installed, but its container didn't start: ${r.resource.error}`);
      },
      error: e => { this.busy.set(false); this.catalogError.set(e.error?.detail ?? 'Install failed'); },
    });
  }
  addFromCatalog(entry: CatalogServer, i: number) {
    this.catalogError.set('');
    this.api.catalogDraft(entry, i).subscribe({
      next: d => {
        this.browsing.set(false); this.error.set(''); this.tools.set([]); this.sealed = {};
        this.picked.set({ entry, remote: entry.remotes[i] });
        this.draft.set({ ...blankServer(), ...d });
      },
      error: e => this.catalogError.set(e.error?.detail ?? 'Could not add it'),
    });
  }

  headersText(d: ToolServer) {
    return Object.entries(d.headers ?? {}).map(([k, v]) => `${k}: ${v.startsWith(SEALED) ? KEPT : v}`).join('\n');
  }
  setHeaders(d: ToolServer, v: string) {
    d.headers = Object.fromEntries(this.splitLines(v).map(l => {
      const i = l.indexOf(':');
      const k = (i < 0 ? l : l.slice(0, i)).trim(), val = i < 0 ? '' : l.slice(i + 1).trim();
      return [k, val === KEPT ? (this.sealed[k] ?? '') : val];
    }).filter(([k]) => k));
  }
  load() { this.api.servers().subscribe(s => this.servers.set(s)); }

  edit(t: ToolServer | null) {
    this.error.set(''); this.tools.set([]); this.browsing.set(false); this.picked.set(null);
    const d = t ? structuredClone(t) : blankServer();
    d.headers ??= {};
    this.sealed = Object.fromEntries(Object.entries(d.headers).filter(([, v]) => v.startsWith(SEALED)));
    d.approval_rules ??= {};
    d.eval_live ??= {};
    this.draft.set(d);
    if (d.id) this.probe(d, true);
  }

  label(m: ApprovalMode | null | undefined) {
    return this.modes.find(x => x.value === m)?.label ?? 'Allow';
  }

  isShown(d: ToolServer, t: ProbedTool) { return !d.shown_tools?.length || d.shown_tools.includes(t.name); }
  shownCount(d: ToolServer) { return this.tools().filter(t => this.isShown(d, t)).length; }
  setShown(d: ToolServer, t: ProbedTool, on: boolean) {
    const all = this.tools().map(x => x.name);
    const cur = new Set(d.shown_tools?.length ? d.shown_tools : all);
    if (on) cur.add(t.name); else cur.delete(t.name);
    d.shown_tools = all.every(n => cur.has(n)) ? [] : [...cur].sort();
  }
  safe(d: ToolServer, t: ProbedTool) { return d.eval_live?.[t.name] ?? !!t.read_only_hint; }
  setSafe(d: ToolServer, t: ProbedTool, on: boolean) {
    const next = { ...(d.eval_live ?? {}) };
    if (on === !!t.read_only_hint) delete next[t.name]; else next[t.name] = on;
    d.eval_live = next;
  }

  defaultFor(d: ToolServer, t: ProbedTool): ApprovalMode {
    return d.approval_rules['*'] ?? t.suggested_approval ?? 'allow';
  }

  setRule(d: ToolServer, tool: string, mode: string) {
    if (mode) d.approval_rules[tool] = mode as ApprovalMode;
    else delete d.approval_rules[tool];
    d.approval_rules = { ...d.approval_rules };
  }

  ruleOnly(d: ToolServer) {
    const offered = new Set(this.tools().map(t => t.name));
    return Object.keys(d.approval_rules).filter(k => k !== '*' && !offered.has(k));
  }

  splitLines(v: string) { return v.split('\n').map(x => x.trim()).filter(Boolean); }
  envText(d: ToolServer) { return Object.entries(d.env).map(([k, v]) => `${k}=${v}`).join('\n'); }
  setEnv(d: ToolServer, v: string) {
    d.env = Object.fromEntries(this.splitLines(v).map(l => {
      const i = l.indexOf('='); return [l.slice(0, i), l.slice(i + 1)];
    }).filter(([k]) => k));
  }

  save() {
    this.api.saveServer(this.draft()!).subscribe({
      next: s => { this.load(); this.picked.set(null); this.edit(s); },
      error: e => this.error.set(e.error?.detail ?? 'Save failed'),
    });
  }

  probe(d: ToolServer, quiet = false) {
    if (!quiet) this.error.set('');
    this.tools.set([]);
    this.api.probeServer(d.id!).subscribe({
      next: t => this.tools.set(t),
      error: e => { if (!quiet) this.error.set(e.error?.detail ?? 'Connection failed'); },
    });
  }

  remove(d: ToolServer) {
    if (!confirm(`Delete ${d.name}? It will be removed from every agent.`)) return;
    this.api.deleteServer(d.id!).subscribe(() => { this.draft.set(null); this.load(); });
  }
}
