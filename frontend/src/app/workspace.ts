import { Injectable, computed, inject, signal } from '@angular/core';
import { Router } from '@angular/router';
import { AgentDef, Api, Thread } from './api.service';

/**
 * State the sidebar and the chat page share: agents, conversations, and which agents
 * are busy right now (so their avatars animate in both places).
 */
@Injectable({ providedIn: 'root' })
export class Workspace {
  private api = inject(Api);
  private router = inject(Router);

  agents = signal<AgentDef[]>([]);
  threads = signal<Thread[]>([]);
  enabledAgents = computed(() => this.agents().filter(a => a.enabled));
  /** Sub-agent requests waiting for you (the sidebar badge). */
  pendingRequests = signal(0);

  private lastSeen = signal<Record<string, number>>({});
  private now = signal(Date.now());

  constructor() {
    this.reload();
    setInterval(() => this.now.set(Date.now()), 1500);
    this.refreshPending();
    setInterval(() => this.refreshPending(), 20000);
  }

  refreshPending() {
    this.api.pendingRequests().subscribe({ next: r => this.pendingRequests.set(r.pending), error: () => {} });
  }

  reload() {
    this.api.agents().subscribe(a => this.agents.set(a));
    this.reloadThreads();
  }

  reloadThreads() { this.api.threads().subscribe(t => this.threads.set(t)); }

  agent(id: string | null | undefined) { return this.agents().find(a => a.id === id); }
  agentName(id: string | null | undefined) { return this.agent(id)?.name ?? '—'; }

  newChat(agentId: string) {
    this.api.createThread(agentId).subscribe(t => {
      this.threads.update(ts => [t, ...ts.filter(x => x.id !== t.id)]);
      this.router.navigate(['/chat', t.id]);
    });
  }

  /** Something happened involving these agents: animate them for a few seconds. */
  touch(ids: string[]) {
    if (!ids.length) return;
    const t = Date.now();
    this.lastSeen.update(m => ({ ...m, ...Object.fromEntries(ids.map(i => [i, t])) }));
    this.now.set(t);
  }

  isActive(id: string | null | undefined) {
    return !!id && this.now() - (this.lastSeen()[id] ?? 0) < 6000;
  }
}
