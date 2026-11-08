import { Component, ElementRef, OnDestroy, OnInit, computed, effect, inject, signal, viewChild } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { DatePipe } from '@angular/common';
import { DomSanitizer, SafeResourceUrl } from '@angular/platform-browser';
import { ActivatedRoute } from '@angular/router';
import { Subscription } from 'rxjs';
import { Api, DesktopState, HqEvent, Message } from './api.service';
import { Avatar } from './avatar';
import { Icon } from './icon';
import { Markdown } from './markdown';
import { Workspace } from './workspace';

interface Activity { ts: string; text: string; kind: string; depth: number; agent?: string; }

@Component({
  selector: 'hq-chat',
  imports: [FormsModule, DatePipe, Avatar, Icon, Markdown],
  template: `
  <div class="chat-layout" [class.with-desktop]="desktopAgent()">
    <section class="conversation">
      @if (threadId()) {
        <header class="conv-head">
          <hq-avatar [agent]="agentById(thread()?.agent_id)" [size]="30" [active]="isActive(thread()?.agent_id)" />
          <strong>{{ agentName(thread()?.agent_id) }}</strong>
          @if (busy()) { <span class="pill live">working…</span> }
          <span class="spacer"></span>
          <hq-icon name="monitor" [size]="16" class="muted" />
          <select [ngModel]="desktopAgentId()" (ngModelChange)="desktopAgentId.set($event)" name="desk">
            <option value="">No desktop</option>
            @for (a of desktopAgents(); track a.id) { <option [value]="a.id">{{ a.name }}'s desktop</option> }
          </select>
        </header>
        @if (pendingRequest(); as r) {
          <div class="banner">
            <span class="row-with-avatar"><hq-avatar [agent]="agentById(r.agent_id)" [size]="28" [active]="true" />
              <span><strong>{{ agentName(r.agent_id) }}</strong> needs you on its desktop: {{ r.reason }}</span></span>
            <button (click)="takeFor(r.agent_id)">Open desktop and take control</button>
          </div>
        }
        <div class="messages" #scroller>
          @for (m of messages(); track m.id) {
            @if (exchange(m); as x) {
              <div class="crumb" [class.open]="isOpen(m.id)" [style.margin-left.px]="(x.depth - 1) * 28">
                <button type="button" class="crumb-head" (click)="toggle(m.id)" [attr.aria-expanded]="isOpen(m.id)">
                  <span class="crumb-avatars">
                    <hq-avatar [agent]="agentById(x.requesterId)" [size]="20" />
                    <hq-avatar [agent]="agentById(x.assigneeId)" [size]="20" />
                  </span>
                  <span class="crumb-title">{{ x.requester }} <span class="muted">↔</span> {{ x.assignee }}</span>
                  <span class="pill {{ x.status }}">{{ x.status }}</span>
                  <span class="muted crumb-sub">{{ x.background ? 'background task' : 'delegated task' }}
                    · {{ m.created_at | date:'h:mm a' }}</span>
                  <hq-icon name="chevron" [size]="14" class="crumb-chevron" />
                </button>
                @if (isOpen(m.id)) {
                  <div class="crumb-body">
                    @if (x.task) {
                      <div class="crumb-msg">
                        <div class="who"><hq-avatar [agent]="agentById(x.requesterId)" [size]="18" />
                          {{ x.requester }} <span class="muted">→ {{ x.assignee }}</span></div>
                        <div class="body rich"><hq-md [text]="x.task" /></div>
                      </div>
                    }
                    @if (x.finished) {
                      <div class="crumb-msg reply">
                        <div class="who"><hq-avatar [agent]="agentById(x.assigneeId)" [size]="18" />
                          {{ x.assignee }} <span class="muted">→ {{ x.requester }}</span></div>
                        <div class="body rich"><hq-md [text]="x.summary" /></div>
                        @if (x.details) { <div class="body rich details"><hq-md [text]="x.details" /></div> }
                      </div>
                    } @else {
                      <div class="crumb-msg reply working">
                        <div class="who"><hq-avatar [agent]="agentById(x.assigneeId)" [size]="18" [active]="true" />
                          {{ x.assignee }} <span class="muted">{{ x.status === 'queued' ? 'is queued…' : 'is working…' }}</span></div>
                        @for (a of stepsOf(x.assignee, x.depth); track $index) {
                          <div class="crumb-step act {{ a.kind }}"><span class="ts">{{ a.ts | date:'HH:mm:ss' }}</span> {{ a.text }}</div>
                        } @empty { <div class="crumb-step muted">Waiting for the first step…</div> }
                      </div>
                    }
                  </div>
                }
              </div>
            } @else {
            <div class="msg {{ m.role }}">
              @if (m.role === 'agent') {
                <div class="who"><hq-avatar [agent]="agentById(m.agent_id)" [size]="24" [active]="isActive(m.agent_id)" /> {{ agentName(m.agent_id) }}</div>
              } @else if (m.role === 'event') { <div class="who">activity</div> }
              @if (m.role === 'agent') {
                <div class="body rich"><hq-md [text]="m.content" /></div>
              } @else {
                <div class="body">{{ m.content }}</div>
              }
            </div>
            }
          }
        </div>
        <div class="resizer" title="Drag to resize the message box (double-click to reset)"
             (pointerdown)="startResize($event)" (dblclick)="setComposerHeight(DEFAULT_COMPOSER)"></div>
        <form class="composer" (ngSubmit)="send()" [style.height.px]="composerHeight()">
          <textarea name="draft" [(ngModel)]="draft" placeholder="Message…  (Enter to send, Shift+Enter for a new line)"
            (keydown.enter)="onEnter($event)"></textarea>
          <button type="submit" [disabled]="!draft.trim()">Send</button>
        </form>
        @if (activity().length) {
          <details class="activity" open>
            <summary>Live activity ({{ activity().length }})</summary>
            @for (a of activity(); track $index) {
              <div class="act {{ a.kind }}" [style.padding-left.px]="8 + a.depth * 16" [title]="a.text">
                <span class="ts">{{ a.ts | date:'HH:mm:ss' }}</span> {{ a.text }}
              </div>
            }
          </details>
        }
      } @else {
        <div class="start">
          <hq-icon name="logo" [size]="40" />
          <h2>Start a conversation</h2>
          <p class="muted">Pick an agent, or open a conversation from the sidebar.</p>
          <div class="start-grid">
            @for (a of ws.enabledAgents(); track a.id) {
              <button class="start-card" (click)="ws.newChat(a.id!)">
                <hq-avatar [agent]="a" [size]="40" [active]="isActive(a.id)" />
                <span class="row-text"><span class="name">{{ a.name }}</span>
                  <span class="sub">{{ a.description || 'No description' }}</span></span>
              </button>
            } @empty { <p class="muted">No agents yet. Create one under Settings → Agents.</p> }
          </div>
        </div>
      }
    </section>

    @if (desktopAgent(); as d) {
      <section class="desktop">
        <header class="conv-head"><strong>{{ d.name }}'s desktop</strong>
          @if (desk(); as s) {
            @if (s.user_has_control) { <span class="pill you">you have control</span> }
            @else if (s.has_desktop_tools) { <span class="pill">agent has control</span> }
          }
          <span class="spacer"></span>
          <a [href]="d.desktop_url" target="_blank">Open in tab</a></header>
        @if (desk(); as s) {
          @if (!s.has_desktop_tools) {
            <div class="desk-bar muted">To use take-control, tick "Controls an agent desktop" on this desktop's tool server (Settings → Tools).</div>
          } @else if (s.user_has_control) {
            <div class="desk-bar">
              <input [(ngModel)]="deskNote" name="desknote" placeholder="Note for the agent (optional), e.g. logged in, 2FA done">
              <button (click)="handBack(d.id!)">Hand back</button>
            </div>
          } @else {
            <div class="desk-bar">
              <span class="muted">{{ s.request ? 'The agent is waiting for you: ' + s.request.reason : 'The agent can use this desktop. Take control to pause it.' }}</span>
              <span class="spacer"></span>
              <button (click)="take(d.id!)">Take control</button>
            </div>
          }
        }
        <iframe [src]="desktopUrl()" title="Agent desktop"></iframe>
      </section>
    }
  </div>`,
})
export class ChatPage implements OnInit, OnDestroy {
  private api = inject(Api);
  ws = inject(Workspace);
  private route = inject(ActivatedRoute);
  private sanitizer = inject(DomSanitizer);
  private scroller = viewChild<ElementRef<HTMLElement>>('scroller');
  private sub?: Subscription;

  agents = this.ws.agents;
  threads = this.ws.threads;
  threadId = signal<string | null>(null);
  messages = signal<Message[]>([]);
  activity = signal<Activity[]>([]);
  busy = signal(false);
  desktopAgentId = signal('');
  desk = signal<DesktopState | null>(null);
  deskNote = '';
  private deskTimer?: ReturnType<typeof setInterval>;
  pendingRequest = computed(() => {
    let req: { agent_id: string; reason: string } | null = null;
    for (const m of this.messages()) {
      const kind = m.meta?.['kind'];
      if (kind === 'desktop_request') req = { agent_id: m.agent_id!, reason: m.content.split(': ').slice(1).join(': ') };
      else if (kind === 'desktop_control' || kind === 'desktop_release' || kind === 'desktop_timeout') req = null;
    }
    return req;
  });
  draft = '';

  // Delegations between agents show as collapsed threads; these are the expanded ones.
  private opened = signal<Set<string>>(new Set());
  isOpen(id: string) { return this.opened().has(id); }
  toggle(id: string) {
    this.opened.update(s => { const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n; });
  }

  /** The latest live steps of an agent working on a delegated task (this page's activity). */
  stepsOf(agent: string, depth: number) {
    return this.activity().filter(a => a.agent === agent && a.depth >= depth).slice(-6);
  }

  /** A delegation result message, as both sides of the exchange (null for other messages). */
  exchange(m: Message) {
    const meta = (m.meta ?? {}) as Record<string, any>;
    if (m.role !== 'event' || meta['kind'] !== 'delegation_result') return null;
    const result = meta['result'] ?? {};
    // Older messages only have "Scarlett → Duke: [done] summary" text.
    const names = /^(.+?) → (.+?): \[/.exec(m.content);
    const assignee = meta['assignee'] ?? names?.[1] ?? this.agentName(m.agent_id);
    const requester = meta['requester'] ?? names?.[2] ?? 'you';
    return {
      requester, assignee,
      requesterId: meta['requester_id'] ?? this.agents().find(a => a.name === requester)?.id ?? null,
      assigneeId: m.agent_id,
      task: meta['task'] as string | undefined,
      status: (result.status ?? meta['status'] ?? 'done') as string,
      finished: !!meta['result'] || !meta['status'],
      summary: (result.summary ?? m.content) as string,
      details: (result.details ?? '') as string,
      background: !!meta['background'],
      depth: Math.max(1, Number(meta['depth'] ?? 1)),
    };
  }

  // The message box's height: drag the line above it to resize; remembered per browser.
  readonly DEFAULT_COMPOSER = 76;
  composerHeight = signal(this.loadComposerHeight());

  startResize(e: PointerEvent) {
    e.preventDefault();
    const startY = e.clientY, startH = this.composerHeight();
    const box = (e.target as HTMLElement).closest('.conversation') as HTMLElement | null;
    const max = Math.max(120, (box?.clientHeight ?? 800) * 0.7);
    const move = (ev: PointerEvent) =>
      this.composerHeight.set(Math.round(Math.min(max, Math.max(56, startH + startY - ev.clientY))));
    const up = () => {
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', up);
      document.body.classList.remove('resizing');
      this.setComposerHeight(this.composerHeight());
    };
    document.body.classList.add('resizing');
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
  }

  setComposerHeight(h: number) {
    this.composerHeight.set(h);
    try { localStorage.setItem('ahq.composerHeight', String(h)); } catch { /* ignore */ }
  }

  private loadComposerHeight() {
    try { return Number(localStorage.getItem('ahq.composerHeight')) || this.DEFAULT_COMPOSER; }
    catch { return this.DEFAULT_COMPOSER; }
  }

  desktopAgents = computed(() => this.agents().filter(a => a.desktop_url));
  thread = computed(() => this.threads().find(t => t.id === this.threadId()));
  desktopAgent = computed(() => this.agents().find(a => a.id === this.desktopAgentId()));
  desktopUrl = computed<SafeResourceUrl | null>(() => {
    const u = this.desktopAgent()?.desktop_url;
    return u ? this.sanitizer.bypassSecurityTrustResourceUrl(u) : null;
  });

  ngOnInit() {
    this.ws.reload();  // pick up agent edits made under Settings
    this.route.paramMap.subscribe(p => this.select(p.get('id')));
  }

  ngOnDestroy() { this.sub?.unsubscribe(); clearInterval(this.deskTimer); }

  constructor() {
    effect(() => {
      const id = this.desktopAgentId();
      clearInterval(this.deskTimer);
      this.desk.set(null);
      if (!id) return;
      const poll = () => this.api.desktopState(id).subscribe({ next: s => this.desk.set(s), error: () => {} });
      poll();
      this.deskTimer = setInterval(poll, 3000);
    });
  }

  take(agentId: string) {
    this.api.takeDesktop(agentId, this.threadId()).subscribe({
      next: s => this.desk.set(s), error: e => alert(e.error?.detail ?? 'Could not take control') });
  }

  takeFor(agentId: string) {
    this.desktopAgentId.set(agentId);
    this.take(agentId);
  }

  handBack(agentId: string) {
    const note = this.deskNote.trim();
    this.deskNote = '';
    this.api.releaseDesktop(agentId, this.threadId(), note).subscribe(s => this.desk.set(s));
  }

  agentName(id: string | null | undefined) { return this.ws.agentName(id); }

  select(id: string | null) {
    this.sub?.unsubscribe();
    this.threadId.set(id);
    this.messages.set([]);
    this.activity.set([]);
    this.busy.set(false);
    if (!id) return;
    if (!this.threads().some(t => t.id === id)) this.ws.reloadThreads();  // started elsewhere
    this.api.messages(id).subscribe(m => { this.messages.set(m); this.scroll(); });
    this.sub = this.api.events(id).subscribe(ev => this.onEvent(ev));
  }

  agentById(id: string | null | undefined) { return this.agents().find(a => a.id === id); }

  /** Agents seen doing something in the last few seconds animate in "while working" mode. */
  isActive(id: string | null | undefined) {
    if (!id) return false;
    if (this.busy() && this.thread()?.agent_id === id) return true;
    return this.ws.isActive(id);
  }

  private markActive(ev: HqEvent) {
    const ids: string[] = [];
    if (ev['agent_id']) ids.push(ev['agent_id']);
    for (const key of ['to_agent', 'from_agent', 'agent']) {
      const a = this.agents().find(x => x.name === ev[key]);
      if (a?.id) ids.push(a.id);
    }
    this.ws.touch(ids);
  }

  onEvent(ev: HqEvent) {
    if (ev.type !== 'message') this.markActive(ev);
    switch (ev.type) {
      case 'message':
        if (!this.messages().some(m => m.id === ev['message'].id)) {
          this.messages.update(ms => [...ms, ev['message']]);
          this.scroll();
        }
        break;
      case 'message_updated':  // e.g. a delegation thread going from working to done
        this.messages.update(ms => ms.map(m => m.id === ev['message'].id ? ev['message'] : m));
        break;
      case 'status': this.busy.set(ev['state'] === 'thinking'); break;
      case 'tool_call': this.log(ev, `${ev['agent']} → ${ev['tool']}(${JSON.stringify(ev['args'])})`, 'call'); break;
      case 'tool_result': this.log(ev, `${ev['agent']} ← ${ev['tool']}: ${ev['error'] ?? ev['preview']}`, ev['error'] ? 'err' : 'result'); break;
      case 'job_queued': this.log(ev, `queued ${ev['kind']} for ${this.agentName(ev['agent_id'])} on pool ${ev['pool']}`, 'queue'); break;
      case 'job_retry': this.log(ev, `job ${ev['job_id']} retried on ${ev['worker']} (attempt ${ev['attempt']})`, 'err'); break;
      case 'delegation_started': this.log(ev, `${ev['from_agent'] ?? 'you'} → ${ev['to_agent']} on ${ev['worker']}${ev['background'] ? ' (background)' : ''}: ${ev['task']}`, 'deleg'); break;
      case 'agent_tools': {
        const servers: string[] = ev['servers'] ?? [];
        if (!servers.length && !ev['depth']) break;  // a supervisor without tools is normal
        this.log(ev, servers.length ? `${ev['agent']} has tools from ${servers.join(', ')}`
                                    : `${ev['agent']} has no tool servers (Settings → Agents → Tools)`, 'queue');
        break;
      }
      case 'memory_recall': this.log(ev, `${ev['agent']} recalled ${ev['count']} memor${ev['count'] === 1 ? 'y' : 'ies'}`, 'queue'); break;
      case 'model_fallback': this.log(ev, `${ev['agent']} → fallback model ${ev['to_model']}: ${ev['reason']}`, 'err'); break;
      case 'delegation_finished': {
        const r = ev['result'] ?? {};
        const why = r.status !== 'done' && r.summary ? `: ${String(r.summary).slice(0, 300)}` : '';
        this.log(ev, `${ev['to_agent']} finished [${r.status}]${why}`, r.status === 'done' ? 'deleg' : 'err');
        break;
      }
    }
  }

  private log(ev: HqEvent, text: string, kind: string) {
    this.activity.update(a => [...a.slice(-199),
      { ts: ev['ts'], text, kind, depth: ev['depth'] ?? 0, agent: ev['agent'] ?? ev['to_agent'] }]);
  }

  onEnter(e: Event) {
    const k = e as KeyboardEvent;
    if (!k.shiftKey) { k.preventDefault(); this.send(); }
  }

  send() {
    const id = this.threadId(); const text = this.draft.trim();
    if (!id || !text) return;
    this.draft = '';
    this.busy.set(true);
    this.api.send(id, text).subscribe();
  }

  private scroll() {
    setTimeout(() => { const el = this.scroller()?.nativeElement; if (el) el.scrollTop = el.scrollHeight; });
  }
}
