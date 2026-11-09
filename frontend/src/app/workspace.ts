import { Injectable, computed, effect, inject, signal } from '@angular/core';
import { Router } from '@angular/router';
import { AgentDef, Api, Thread } from './api.service';
import { Session } from './session';

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
  /** What's waiting, by kind (the Requests sub-menu counts). */
  inbox = signal({ requests: 0, approvals: 0, skills: 0, total: 0 });

  private lastSeen = signal<Record<string, number>>({});
  private now = signal(Date.now());

  private session = inject(Session);

  constructor() {
    // Load (and keep polling) only while signed in; clear everything on sign-out.
    effect(() => {
      if (this.session.user()) {
        this.reload();
        this.refreshPending();
      } else {
        this.agents.set([]);
        this.threads.set([]);
        this.pendingRequests.set(0);
        this.inbox.set({ requests: 0, approvals: 0, skills: 0, total: 0 });
      }
    });
    setInterval(() => this.now.set(Date.now()), 1500);
    setInterval(() => { if (this.session.user()) this.refreshPending(); }, 20000);
  }

  refreshPending() {
    this.api.inboxCount().subscribe({ next: r => { this.pendingRequests.set(r.total); this.inbox.set(r); }, error: () => {} });
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
