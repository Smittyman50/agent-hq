import { Component, computed, effect, inject, signal, untracked } from '@angular/core';
import { toSignal } from '@angular/core/rxjs-interop';
import { filter, map } from 'rxjs';
import { DatePipe } from '@angular/common';
import { NavigationEnd, Router, RouterLink, RouterLinkActive, RouterOutlet } from '@angular/router';
import { Avatar } from './avatar';
import { Icon } from './icon';
import { Workspace } from './workspace';
import { Session } from './session';
import { Thread, threadMembers } from './api.service';

/** The app shell: conversations in the sidebar, Settings at its foot. */
@Component({
  imports: [RouterOutlet, RouterLink, RouterLinkActive, DatePipe, Avatar, Icon],
  selector: 'app-root',
  template: `
    @if (session.user()) {
    <aside class="sidebar" [class.collapsed]="collapsed()">
      <div class="side-brand">
        <a routerLink="/chat" class="brand" title="Agent HQ"><hq-icon name="logo" [size]="24" />
          <span class="label">Agent HQ</span></a>
        <button class="icon-btn" (click)="toggle()" [title]="collapsed() ? 'Expand sidebar' : 'Collapse sidebar'">
          <hq-icon name="panel" [size]="17" /></button>
      </div>

      <div class="side-new">
        <button class="nav-item new" (click)="menu.set(!menu())" title="New chat">
          <hq-icon name="plus" /><span class="label">New chat</span></button>
        @if (menu()) {
          <div class="backdrop" (click)="menu.set(false)"></div>
          <div class="agent-menu">
            <div class="menu-label">Chat with…</div>
            @for (a of ws.enabledAgents(); track a.id) {
              <button class="menu-item" (click)="start(a.id!)">
                <hq-avatar [agent]="a" [size]="26" [active]="ws.isActive(a.id)" />
                <span class="row-text"><span class="name">{{ a.name }}</span>
                  @if (a.description) { <span class="sub">{{ a.description }}</span> }</span>
              </button>
            } @empty { <p class="hint">No agents yet. Add one under Settings → Agents.</p> }
          </div>
        }
      </div>

      @if (!collapsed()) {
        <label class="side-search"><hq-icon name="search" [size]="15" />
          <input [value]="query()" (input)="query.set($any($event.target).value)" placeholder="Search conversations"></label>
        <div class="side-label">Conversations</div>
      }
      <nav class="side-threads">
        @for (g of groups(); track g.agentId) {
          @if (collapsed()) {
            <!-- Narrow sidebar: one avatar per agent, opening its latest chat. -->
            <a class="thread" [routerLink]="['/chat', g.threads[0].id]" [class.on]="g.agentId === activeAgentId()"
               [title]="g.name + ' · ' + g.threads.length + ' chat' + (g.threads.length === 1 ? '' : 's')">
              <hq-avatar [agent]="ws.agent(g.agentId)" [size]="28" [active]="ws.isActive(g.agentId)" />
            </a>
          } @else {
            <div class="agent-group" [class.open]="isOpen(g.agentId)">
              <div class="agent-head" [class.has-active]="!isOpen(g.agentId) && g.agentId === activeAgentId()">
                <button class="agent-toggle" (click)="toggleGroup(g.agentId)"
                        [attr.aria-expanded]="isOpen(g.agentId)"
                        [title]="(isOpen(g.agentId) ? 'Collapse ' : 'Expand ') + g.name + ' chats'">
                  <hq-icon name="chevron" [size]="14" class="caret" />
                  <hq-avatar [agent]="ws.agent(g.agentId)" [size]="26" [active]="ws.isActive(g.agentId)" />
                  <span class="row-text label">
                    <span class="name">{{ g.name }}</span>
                    <span class="sub">{{ g.threads.length }} chat{{ g.threads.length === 1 ? '' : 's' }} · {{ ws.lastUpdated(g.threads[0]) | date:'MMM d' }}</span>
                  </span>
                </button>
                <button class="icon-btn agent-new" (click)="start(g.agentId)" [title]="'New chat with ' + g.name">
                  <hq-icon name="plus" [size]="15" /></button>
              </div>
              @if (isOpen(g.agentId)) {
                <div class="agent-threads">
                  @for (t of visible(g); track t.id) {
                    @if (confirming() === t.id) {
                      <div class="thread child confirm">
                        <span class="row-text label"><span class="name">Delete this chat?</span>
                          <span class="sub">{{ title(t) }} · {{ ws.lastUpdated(t) | date:'MMM d, h:mm a' }}</span></span>
                        <button class="mini danger" (click)="remove(t.id)">Delete</button>
                        <button class="mini" (click)="confirming.set(null)">Cancel</button>
                      </div>
                    } @else {
                      <a class="thread child" [routerLink]="['/chat', t.id]" routerLinkActive="on" [title]="title(t)">
                        <span class="row-text label">
                          <span class="name" [class.untitled]="isDefault(t)">{{ title(t) }}</span>
                          <span class="sub">{{ ws.lastUpdated(t) | date:'MMM d, h:mm a' }}</span>
                        </span>
                        <button class="icon-btn thread-del" title="Delete chat"
                                (click)="$event.preventDefault(); $event.stopPropagation(); confirming.set(t.id)">
                          <hq-icon name="trash" [size]="14" /></button>
                      </a>
                    }
                  }
                  @if (g.threads.length > PAGE) {
                    <button class="more-toggle" (click)="toggleMore(g.agentId)">
                      {{ showingAll(g) ? 'Show less' : (g.threads.length - PAGE) + ' more' }}</button>
                  }
                </div>
              }
            </div>
          }
        } @empty {
          @if (!collapsed() && !huddles().length) { <p class="hint">{{ query() ? 'No matches.' : 'No conversations yet.' }}</p> }
        }

        @if (huddles().length) {
          @if (!collapsed()) { <div class="side-label huddles-label">Huddles</div> }
          @for (t of visibleHuddles(); track t.id) {
            @if (collapsed()) {
              <a class="thread" [routerLink]="['/chat', t.id]" routerLinkActive="on"
                 [title]="'Huddle: ' + memberNames(t) + ' · ' + title(t)">
                <span class="huddle-stack small">
                  @for (id of members(t).slice(0, 2); track id) { <hq-avatar [agent]="ws.agent(id)" [size]="20" [active]="ws.isActive(id)" /> }
                </span>
              </a>
            } @else if (confirming() === t.id) {
              <div class="thread child confirm huddle-row">
                <span class="row-text label"><span class="name">Delete this huddle?</span>
                  <span class="sub">{{ title(t) }} · {{ ws.lastUpdated(t) | date:'MMM d, h:mm a' }}</span></span>
                <button class="mini danger" (click)="remove(t.id)">Delete</button>
                <button class="mini" (click)="confirming.set(null)">Cancel</button>
              </div>
            } @else {
              <a class="thread child huddle-row" [routerLink]="['/chat', t.id]" routerLinkActive="on"
                 [title]="memberNames(t) + ' · ' + title(t)">
                <span class="huddle-stack">
                  @for (id of members(t).slice(0, 3); track id) { <hq-avatar [agent]="ws.agent(id)" [size]="22" [active]="ws.isActive(id)" /> }
                  @if (members(t).length > 3) { <span class="huddle-more">+{{ members(t).length - 3 }}</span> }
                </span>
                <span class="row-text label">
                  <span class="name" [class.untitled]="isDefault(t)">{{ title(t) }}</span>
                  <span class="sub">{{ memberNames(t) }} · {{ ws.lastUpdated(t) | date:'MMM d' }}</span>
                </span>
                <button class="icon-btn thread-del" title="Delete huddle"
                        (click)="$event.preventDefault(); $event.stopPropagation(); confirming.set(t.id)">
                  <hq-icon name="trash" [size]="14" /></button>
              </a>
            }
          }
          @if (!collapsed() && huddles().length > PAGE) {
            <button class="more-toggle huddle-more-toggle" (click)="allHuddles.set(!allHuddles())">
              {{ showingAllHuddles() ? 'Show less' : (huddles().length - PAGE) + ' more' }}</button>
          }
        }
      </nav>

      <div class="side-foot">
        <a class="nav-item" routerLink="/requests" routerLinkActive="on"
           [title]="ws.pendingRequests() ? ws.pendingRequests() + ' request(s) waiting for you' : 'Requests'">
          <span class="icon-badge"><hq-icon name="inbox" />
            @if (ws.pendingRequests()) { <span class="badge">{{ ws.pendingRequests() }}</span> }</span>
          <span class="label">Requests</span></a>
        <a class="nav-item" routerLink="/board" routerLinkActive="on" title="Board: standing goals">
          <hq-icon name="board" /><span class="label">Board</span></a>
        <a class="nav-item" routerLink="/reports" routerLinkActive="on" title="Reports: live activity and usage history">
          <hq-icon name="activity" /><span class="label">Reports</span></a>
        <a class="nav-item" routerLink="/schedules" routerLinkActive="on" title="Scheduler">
          <hq-icon name="schedule" /><span class="label">Scheduler</span></a>
        <a class="nav-item" routerLink="/settings" routerLinkActive="on" title="Settings">
          <hq-icon name="settings" /><span class="label">Settings</span></a>
        <button class="nav-item" (click)="session.logout()" [title]="'Sign out ' + session.user()?.username">
          <hq-icon name="logout" /><span class="label">Sign out <span class="muted">{{ session.user()?.username }}</span></span></button>
      </div>
    </aside>
    }
    <main><router-outlet /></main>
  `,
})
export class App {
  ws = inject(Workspace);
  session = inject(Session);
  menu = signal(false);
  query = signal('');
  collapsed = signal(this.loadCollapsed());

  threads = computed(() => {
    const q = this.query().trim().toLowerCase();
    if (!q) return this.ws.threads();
    return this.ws.threads().filter(t =>
      `${this.memberNames(t)} ${t.title}`.toLowerCase().includes(q));
  });

  /** Conversations with several agents, newest first (their own sidebar section). */
  huddles = computed(() => {
    const at = (t: Thread) => Date.parse(this.ws.lastUpdated(t)) || 0;
    return this.threads().filter(t => t.huddle).sort((a, b) => at(b) - at(a));
  });
  allHuddles = signal(false);
  showingAllHuddles() {
    if (this.query().trim() || this.allHuddles()) return true;
    return this.huddles().findIndex(t => t.id === this.activeThreadId()) >= this.PAGE;
  }
  visibleHuddles() { return this.showingAllHuddles() ? this.huddles() : this.huddles().slice(0, this.PAGE); }

  members(t: Thread) { return threadMembers(t); }
  memberNames(t: Thread) { return this.members(t).map(id => this.ws.agentName(id)).join(', '); }

  /** One-agent threads grouped by agent; groups ordered by their most recent chat. */
  groups = computed(() => {
    const by = new Map<string, Thread[]>();
    for (const t of this.threads()) {
      if (t.huddle) continue;
      const list = by.get(t.agent_id) ?? [];
      list.push(t);
      by.set(t.agent_id, list);
    }
    const at = (t: Thread) => Date.parse(this.ws.lastUpdated(t)) || 0;
    const newest = (ts: Thread[]) => Math.max(...ts.map(at));
    return [...by.entries()]
      .map(([agentId, ts]) => ({
        agentId,
        name: this.ws.agentName(agentId),
        threads: [...ts].sort((a, b) => at(b) - at(a)),
      }))
      .sort((a, b) => newest(b.threads) - newest(a.threads));
  });

  private router = inject(Router);
  private url = toSignal(
    this.router.events.pipe(filter(e => e instanceof NavigationEnd), map(e => (e as NavigationEnd).urlAfterRedirects)),
    { initialValue: this.router.url });
  /** The thread open in the main pane, if any. */
  activeThreadId = computed(() => /^\/chat\/([^/?#]+)/.exec(this.url())?.[1] ?? null);
  activeAgentId = computed(() => {
    const id = this.activeThreadId();
    const t = id ? this.ws.threads().find(x => x.id === id) : null;
    return t && !t.huddle ? t.agent_id : null;  // huddles aren't in an agent's group
  });

  /** Agents whose chat lists are expanded (remembered across reloads). */
  expanded = signal<Set<string>>(this.loadExpanded());
  /** The thread we last auto-expanded for, so it happens once per navigation. */
  private autoExpandedFor: string | null = null;

  constructor() {
    // Opening a chat (from anywhere) expands its agent so it's visible in the list.
    // Only on navigation: reading `expanded` untracked lets you collapse that agent afterwards.
    effect(() => {
      const id = this.activeThreadId();
      const a = this.activeAgentId();
      untracked(() => {
        if (!a || id === this.autoExpandedFor) return;
        this.autoExpandedFor = id;
        if (!this.expanded().has(a)) this.setExpanded(a, true);
      });
    });
  }

  /** How many of an agent's chats show before "more". */
  readonly PAGE = 5;
  /** Agents whose full chat list is showing. */
  showAll = signal<Set<string>>(new Set());
  /** The chat waiting for a delete confirmation. */
  confirming = signal<string | null>(null);

  /** All of a group when searching, showing all, or when the open chat is past the first page. */
  showingAll(g: { agentId: string; threads: Thread[] }) {
    if (this.query().trim() || this.showAll().has(g.agentId)) return true;
    const i = g.threads.findIndex(t => t.id === this.activeThreadId());
    return i >= this.PAGE;
  }

  visible(g: { agentId: string; threads: Thread[] }) {
    return this.showingAll(g) ? g.threads : g.threads.slice(0, this.PAGE);
  }

  toggleMore(agentId: string) {
    const open = this.showingAll(this.groups().find(g => g.agentId === agentId)!);
    this.showAll.update(s => {
      const n = new Set(s);
      if (open) n.delete(agentId); else n.add(agentId);
      return n;
    });
  }

  remove(id: string) {
    this.confirming.set(null);
    this.ws.deleteThread(id, id === this.activeThreadId());
  }

  /** Searching shows every matching chat, regardless of collapse state. */
  isOpen(agentId: string) { return !!this.query().trim() || this.expanded().has(agentId); }

  toggleGroup(agentId: string) { this.setExpanded(agentId, !this.expanded().has(agentId)); }

  private setExpanded(agentId: string, open: boolean) {
    this.expanded.update(s => {
      const n = new Set(s);
      if (open) n.add(agentId); else n.delete(agentId);
      return n;
    });
    try { localStorage.setItem('ahq.expandedAgents', JSON.stringify([...this.expanded()])); } catch { /* ignore */ }
  }

  private loadExpanded() {
    try { return new Set<string>(JSON.parse(localStorage.getItem('ahq.expandedAgents') ?? '[]')); } catch { return new Set<string>(); }
  }

  /** Still called "Chat with <agent>": named after its first reply. */
  isDefault(t: Thread) { return t.title.startsWith('Chat with ') || t.title === 'New conversation'; }

  /** What the sidebar calls a chat. */
  title(t: Thread) { return this.isDefault(t) ? 'New chat' : t.title; }

  start(agentId: string) { this.menu.set(false); this.ws.newChat(agentId); }

  toggle() {
    this.collapsed.update(c => !c);
    try { localStorage.setItem('ahq.sidebarCollapsed', this.collapsed() ? '1' : '0'); } catch { /* ignore */ }
  }

  private loadCollapsed() {
    try { return localStorage.getItem('ahq.sidebarCollapsed') === '1'; } catch { return false; }
  }
}
