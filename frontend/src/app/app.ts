import { Component, computed, inject, signal } from '@angular/core';
import { DatePipe } from '@angular/common';
import { RouterLink, RouterLinkActive, RouterOutlet } from '@angular/router';
import { Avatar } from './avatar';
import { Icon } from './icon';
import { Workspace } from './workspace';
import { Session } from './session';

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
        @for (t of threads(); track t.id) {
          <a class="thread" [routerLink]="['/chat', t.id]" routerLinkActive="on"
             [title]="ws.agentName(t.agent_id) + ' · ' + t.title">
            <hq-avatar [agent]="ws.agent(t.agent_id)" [size]="collapsed() ? 28 : 26" [active]="ws.isActive(t.agent_id)" />
            <span class="row-text label">
              <span class="name">{{ ws.agentName(t.agent_id) }}</span>
              <span class="sub">{{ subtitle(t) }} · {{ t.created_at | date:'MMM d, h:mm a' }}</span>
            </span>
          </a>
        } @empty {
          @if (!collapsed()) { <p class="hint">{{ query() ? 'No matches.' : 'No conversations yet.' }}</p> }
        }
      </nav>

      <div class="side-foot">
        <a class="nav-item" routerLink="/requests" routerLinkActive="on"
           [title]="ws.pendingRequests() ? ws.pendingRequests() + ' request(s) waiting for you' : 'Requests'">
          <span class="icon-badge"><hq-icon name="inbox" />
            @if (ws.pendingRequests()) { <span class="badge">{{ ws.pendingRequests() }}</span> }</span>
          <span class="label">Requests</span></a>
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
      `${this.ws.agentName(t.agent_id)} ${t.title}`.toLowerCase().includes(q));
  });

  /** The title, unless it's just the default "Chat with <agent>". */
  subtitle(t: { title: string; agent_id: string }) {
    return t.title === `Chat with ${this.ws.agentName(t.agent_id)}` ? 'Chat' : t.title;
  }

  start(agentId: string) { this.menu.set(false); this.ws.newChat(agentId); }

  toggle() {
    this.collapsed.update(c => !c);
    try { localStorage.setItem('ahq.sidebarCollapsed', this.collapsed() ? '1' : '0'); } catch { /* ignore */ }
  }

  private loadCollapsed() {
    try { return localStorage.getItem('ahq.sidebarCollapsed') === '1'; } catch { return false; }
  }
}
