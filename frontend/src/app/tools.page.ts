import { Component, OnInit, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { SlicePipe } from '@angular/common';
import { Api, ApprovalMode, ProbedTool, ToolServer, blankServer } from './api.service';

@Component({
  selector: 'hq-tools',
  imports: [FormsModule, SlicePipe],
  template: `
  <div class="split">
    <aside class="list">
      <div class="list-head"><h2>Tool servers</h2><button (click)="edit(null)">+ New</button></div>
      @for (t of servers(); track t.id) {
        <button class="row" [class.active]="draft()?.id === t.id" [class.muted]="!t.enabled" (click)="edit(t)">
          <span class="name">{{ t.name }} <span class="pill">{{ t.transport }}</span></span>
          <span class="sub">{{ t.description || t.url || t.command }}</span>
        </button>
      } @empty { <p class="hint">Register an MCP server, then tick it on any agent.</p> }
    </aside>

    @if (draft(); as d) {
      <form class="editor" (ngSubmit)="save()">
        <h2>{{ d.id ? 'Edit ' + d.name : 'New tool server' }}</h2>
        <label>Name <small>also used as the tool-name prefix</small><input name="name" [(ngModel)]="d.name" required></label>
        <label>Description<input name="description" [(ngModel)]="d.description"></label>
        <label>Transport
          <select name="transport" [(ngModel)]="d.transport">
            <option value="http">HTTP (streamable / SSE URL)</option>
            <option value="stdio">stdio (command run by the backend)</option>
          </select></label>
        @if (d.transport === 'http') {
          <label>URL<input name="url" [(ngModel)]="d.url" placeholder="http://perceptor:8811/mcp"></label>
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
          <label class="check" title="Every tool counts as read-only (as if the server marked it so): webhook-started work doesn't ask before them, and they start ticked as safe in tests. Only for servers you know can't change anything, e.g. gitea-mcp --read-only.">
            <input type="checkbox" name="readonly" [(ngModel)]="d.read_only"> Only reads</label>
          <label class="check"><input type="checkbox" name="enabled" [(ngModel)]="d.enabled"> Enabled</label>
        </div>
        @if (error()) { <p class="error">{{ error() }}</p> }
        <div class="actions">
          <button type="submit">Save</button>
          @if (d.id) {
            <button type="button" class="ghost" (click)="probe(d)">Test connection</button>
            <button type="button" class="danger" (click)="remove(d)">Delete</button>
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

  ngOnInit() { this.load(); }
  load() { this.api.servers().subscribe(s => this.servers.set(s)); }

  edit(t: ToolServer | null) {
    this.error.set(''); this.tools.set([]);
    const d = t ? structuredClone(t) : blankServer();
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
      next: s => { this.load(); this.draft.set(structuredClone(s)); this.error.set(''); },
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
