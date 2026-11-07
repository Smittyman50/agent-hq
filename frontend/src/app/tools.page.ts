import { Component, OnInit, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { Api, ToolServer, blankServer } from './api.service';

@Component({
  selector: 'hq-tools',
  imports: [FormsModule],
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
        @if (tools().length) {
          <h3>Tools offered</h3>
          <ul class="tools">@for (t of tools(); track t.name) { <li><code>{{ t.name }}</code> {{ t.description }}</li> }</ul>
        }
      </form>
    } @else { <div class="empty">Select a tool server or register a new one.</div> }
  </div>`,
})
export class ToolsPage implements OnInit {
  private api = inject(Api);
  servers = signal<ToolServer[]>([]);
  draft = signal<ToolServer | null>(null);
  tools = signal<{ name: string; description: string }[]>([]);
  error = signal('');

  ngOnInit() { this.load(); }
  load() { this.api.servers().subscribe(s => this.servers.set(s)); }

  edit(t: ToolServer | null) {
    this.error.set(''); this.tools.set([]);
    this.draft.set(t ? structuredClone(t) : blankServer());
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

  probe(d: ToolServer) {
    this.error.set(''); this.tools.set([]);
    this.api.probeServer(d.id!).subscribe({
      next: t => this.tools.set(t),
      error: e => this.error.set(e.error?.detail ?? 'Connection failed'),
    });
  }

  remove(d: ToolServer) {
    if (!confirm(`Delete ${d.name}? It will be removed from every agent.`)) return;
    this.api.deleteServer(d.id!).subscribe(() => { this.draft.set(null); this.load(); });
  }
}
