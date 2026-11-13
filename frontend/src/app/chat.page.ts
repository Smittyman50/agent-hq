import { Component, ElementRef, OnDestroy, OnInit, computed, effect, inject, signal, viewChild } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { DatePipe } from '@angular/common';
import { DomSanitizer, SafeResourceUrl } from '@angular/platform-browser';
import { ActivatedRoute, Router, RouterLink } from '@angular/router';
import { Subscription } from 'rxjs';
import { Api, Attachment, DesktopState, HqEvent, Message, MessageFeedback } from './api.service';
import { Files } from './files';
import { Avatar } from './avatar';
import { Icon } from './icon';
import { Markdown } from './markdown';
import { RequestCard } from './request-card';
import { ApprovalCard } from './approval-card';
import { SkillProposalCard } from './skill-proposal-card';
import { ProfileProposalCard } from './profile-proposal-card';
import { KnowledgeProposalCard } from './knowledge-proposal-card';
import { Workspace } from './workspace';

interface Activity { ts: string; text: string; kind: string; depth: number; agent?: string; }

@Component({
  selector: 'hq-chat',
  imports: [FormsModule, DatePipe, Avatar, Icon, Markdown, Files, RequestCard, ApprovalCard, SkillProposalCard, ProfileProposalCard, KnowledgeProposalCard, RouterLink],
  template: `
  <div class="chat-layout" [class.with-desktop]="desktopAgent()">
    <section class="conversation" [class.dropping]="dropping()" (dragover)="onDragOver($event)"
             (dragleave)="onDragLeave($event)" (drop)="onDrop($event)">
      @if (threadId()) {
        <header class="conv-head">
          <hq-avatar [agent]="agentById(thread()?.agent_id)" [size]="30" [active]="isActive(thread()?.agent_id)" />
          <strong>{{ agentName(thread()?.agent_id) }}</strong>
          @if (working()) { <span class="pill live">working…</span> }
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
            @if (m.meta['kind'] === 'subagent_request' && m.meta['request_id']) {
              <hq-request-card [requestId]="$any(m.meta['request_id'])" [version]="$any(m.meta['status']) + ':' + $any(m.meta['used'])" />
            } @else if (m.meta['kind'] === 'knowledge_proposal' && m.meta['knowledge_proposal_id']) {
              <hq-knowledge-proposal-card [proposalId]="$any(m.meta['knowledge_proposal_id'])" [version]="m.meta['status']" />
            } @else if (m.meta['kind'] === 'profile_proposal' && m.meta['profile_proposal_id']) {
              <hq-profile-proposal-card [proposalId]="$any(m.meta['profile_proposal_id'])" [version]="m.meta['status']" />
            } @else if (m.meta['kind'] === 'skill_proposal' && m.meta['proposal_id']) {
              <hq-skill-proposal-card [proposalId]="$any(m.meta['proposal_id'])" [version]="m.meta['status']" />
            } @else if (m.meta['kind'] === 'tool_approval' && m.meta['approval_id']) {
              <hq-approval-card [approvalId]="$any(m.meta['approval_id'])" [version]="m.meta['status']" />
            } @else if (m.meta['kind'] === 'compaction') {
              <div class="note-line">
                <hq-icon name="history" [size]="14" /> <span>{{ m.content }}</span>
                @if (m.meta['summary']) {
                  <details><summary>What it kept</summary><pre class="kn-text">{{ m.meta['summary'] }}</pre></details>
                }
              </div>
            } @else if (m.meta['kind'] === 'stopped') {
              <div class="note-line">■ {{ m.content }}</div>
            } @else if (m.meta['kind'] === 'context_warning') {
              <div class="note-line warn">⚠ {{ m.content }} <a routerLink="/settings/agents">Agent settings</a></div>
            } @else if (exchange(m); as x) {
              <div class="crumb" [class.open]="isOpen(m.id)" [style.margin-left.px]="(x.depth - 1) * 28">
                <button type="button" class="crumb-head" (click)="toggle(m.id)" [attr.aria-expanded]="isOpen(m.id)">
                  <span class="crumb-avatars">
                    <hq-avatar [agent]="agentById(x.requesterId)" [size]="20" />
                    <hq-avatar [agent]="x.assigneeAvatar" [size]="20" />
                  </span>
                  <span class="crumb-title">{{ x.requester }} <span class="muted">↔</span> {{ x.subagent ? x.role : x.assignee }}</span>
                  @if (x.subagent) { <span class="pill sub" [title]="x.model">sub-agent</span> }
                  <span class="pill {{ x.status }}">{{ x.status }}</span>
                  <span class="muted crumb-sub">{{ x.subagent ? x.model : (x.background ? 'background task' : 'delegated task') }}
                    · {{ m.created_at | date:'h:mm a' }}</span>
                  <hq-icon name="chevron" [size]="14" class="crumb-chevron" />
                </button>
                @if (isOpen(m.id)) {
                  <div class="crumb-body">
                    @if (x.subagent && x.why) {
                      <div class="crumb-note muted">Created by {{ x.requester }} because: {{ x.why }}
                        @if (x.tools.length) { · tools: {{ x.tools.join(', ') }} } · limit {{ x.maxMinutes }} min</div>
                    }
                    @if (x.task) {
                      <div class="crumb-msg">
                        <div class="who"><hq-avatar [agent]="agentById(x.requesterId)" [size]="18" />
                          {{ x.requester }} <span class="muted">→ {{ x.assignee }}</span></div>
                        <div class="body rich"><hq-md [text]="x.task" /></div>
                        @if (x.given.length) { <hq-files [files]="x.given" /> }
                      </div>
                    }
                    @if (x.finished) {
                      <div class="crumb-msg reply">
                        <div class="who"><hq-avatar [agent]="x.assigneeAvatar" [size]="18" />
                          {{ x.assignee }} <span class="muted">→ {{ x.requester }}</span></div>
                        <div class="body rich"><hq-md [text]="x.summary" /></div>
                        @if (x.details) { <div class="body rich details"><hq-md [text]="x.details" /></div> }
                        @if (x.files.length) { <hq-files [files]="x.files" /> }
                      </div>
                    } @else {
                      <div class="crumb-msg reply working">
                        <div class="who"><hq-avatar [agent]="x.assigneeAvatar" [size]="18" [active]="true" />
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
                @if (filesOf(m).length) { <hq-files [files]="filesOf(m)" /> }
                @if (m.meta?.['job_id']) {
                  <div class="rate" [class.rated]="!!fb(m)">
                    <button type="button" class="rate-btn" [class.on]="fb(m)?.rating === 'up'"
                      [title]="fb(m)?.rating === 'up' ? 'Clear your rating' : 'Good reply'" (click)="rate(m, 'up')">
                      <hq-icon name="thumbs-up" [size]="14" /></button>
                    <button type="button" class="rate-btn down" [class.on]="fb(m)?.rating === 'down'"
                      [title]="fb(m)?.rating === 'down' ? 'Clear your rating' : 'Not right: say what it should have done'"
                      (click)="rate(m, 'down')"><hq-icon name="thumbs-down" [size]="14" /></button>
                    <button type="button" class="rate-btn" title="Make this a test (Settings → Evals)" (click)="makeTest(m)">
                      <hq-icon name="evals" [size]="14" /></button>
                    @if (fb(m)?.comment && noteFor() !== m.id) {
                      <button type="button" class="rate-note" title="Edit your note" (click)="openNote(m)">“{{ fb(m)!.comment }}”</button>
                    }
                  </div>
                  @if (noteFor() === m.id) {
                    <form class="rate-form" (ngSubmit)="saveNote(m)">
                      <input name="ratenote" [(ngModel)]="noteDraft" autofocus
                        [placeholder]="'What should ' + agentName(m.agent_id) + ' have done? (optional; it learns from this)'">
                      <button type="submit">Save</button>
                      <button type="button" class="ghost" (click)="noteFor.set(null)">Skip</button>
                    </form>
                  }
                }
              } @else {
                @if (m.content) { <div class="body">{{ m.content }}</div> }
                @if (filesOf(m).length) { <hq-files [files]="filesOf(m)" /> }
              }
            </div>
            }
          }
        </div>
        <div class="resizer" title="Drag to resize the message box (double-click to reset)"
             (pointerdown)="startResize($event)" (dblclick)="setComposerHeight(DEFAULT_COMPOSER)"></div>
        @if (pending().length || uploading() || uploadError()) {
          <div class="composer-files">
            <hq-files [files]="pending()" [removable]="true" (remove)="unattach($event)" />
            @if (uploading()) { <span class="muted upload-note">Uploading {{ uploading() }}…</span> }
            @if (uploadError(); as e) { <span class="upload-note err">{{ e }}</span> }
          </div>
        }
        <form class="composer" (ngSubmit)="send()" [style.height.px]="composerHeight()">
          <label class="attach" title="Attach files (or drop them here, or paste an image)">
            <hq-icon name="paperclip" [size]="17" />
            <input type="file" multiple hidden (change)="pick($event)">
          </label>
          <textarea name="draft" [(ngModel)]="draft" placeholder="Message…  (Enter to send, Shift+Enter for a new line)"
            (keydown.enter)="onEnter($event)" (paste)="onPaste($event)"></textarea>
          @if (working()) {
            <button type="button" class="danger stop" (click)="stop()" [disabled]="stopping()"
                    title="Stop this turn and everything it delegated">{{ stopping() ? 'Stopping…' : 'Stop' }}</button>
          }
          <button type="submit" [disabled]="(!draft.trim() && !pending().length) || !!uploading()">Send</button>
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
  private router = inject(Router);
  private sanitizer = inject(DomSanitizer);
  private scroller = viewChild<ElementRef<HTMLElement>>('scroller');
  private sub?: Subscription;

  agents = this.ws.agents;
  threads = this.ws.threads;
  threadId = signal<string | null>(null);
  messages = signal<Message[]>([]);
  activity = signal<Activity[]>([]);
  busy = signal(false);
  stopping = signal(false);
  /** Something in this conversation is still running: the turn, or a task it delegated. */
  working = computed(() => this.busy() || this.messages().some(m => {
    const x = this.exchange(m);
    return !!x && !x.finished && (x.status === 'queued' || x.status === 'working');
  }));
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

  // Your 👍 / 👎 on replies; a 👎 opens a note: what the agent should have done.
  noteFor = signal<string | null>(null);
  noteDraft = '';
  fb(m: Message): MessageFeedback | null { return (m.meta?.['feedback'] as MessageFeedback) ?? null; }
  rate(m: Message, rating: 'up' | 'down') {
    const clear = this.fb(m)?.rating === rating;
    const next = clear ? null : rating;
    const comment = clear ? '' : (rating === 'down' ? (this.fb(m)?.comment ?? '') : '');
    this.applyFeedback(m.id, next ? { rating: next, comment } : null);
    this.api.rateMessage(m.id, next, comment).subscribe({ error: () => this.reload() });
    if (next === 'down') this.openNote(m); else if (this.noteFor() === m.id) this.noteFor.set(null);
  }
  makeTest(m: Message) {
    this.api.caseFromMessage(m.id).subscribe({
      next: c => this.router.navigate(['/settings/evals'], { queryParams: { case: c.id } }),
      error: e => alert(e.error?.detail ?? 'Could not make a test from this reply'),
    });
  }
  openNote(m: Message) { this.noteDraft = this.fb(m)?.comment ?? ''; this.noteFor.set(m.id); }
  saveNote(m: Message) {
    const rating = this.fb(m)?.rating ?? 'down';
    const comment = this.noteDraft.trim();
    this.applyFeedback(m.id, { rating, comment });
    this.api.rateMessage(m.id, rating, comment).subscribe({ error: () => this.reload() });
    this.noteFor.set(null);
  }
  private reload() {
    const id = this.threadId();
    if (id) this.api.messages(id).subscribe(m => this.messages.set(m));
  }
  private applyFeedback(id: string, feedback: MessageFeedback | null) {
    this.messages.update(ms => ms.map(x => x.id === id ? { ...x, meta: { ...(x.meta ?? {}), feedback } } : x));
  }

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
    const subagent = !!meta['subagent'];
    return {
      requester, assignee, subagent,
      role: (meta['subagent_role'] ?? assignee) as string,
      model: (meta['model'] ?? '') as string,
      why: (meta['why_not_existing_agents'] ?? '') as string,
      tools: (meta['tools'] ?? []) as string[],
      maxMinutes: meta['max_minutes'] as number | undefined,
      requesterId: meta['requester_id'] ?? this.agents().find(a => a.name === requester)?.id ?? null,
      assigneeId: subagent ? null : m.agent_id,
      // Sub-agents aren't in the agent list: show initials of their role.
      assigneeAvatar: subagent ? ({ name: meta['subagent_role'] ?? assignee } as any) : this.agentById(m.agent_id),
      task: meta['task'] as string | undefined,
      status: (result.status ?? meta['status'] ?? 'done') as string,
      finished: !!meta['result'] || !meta['status'],
      summary: (result.summary ?? m.content) as string,
      details: (result.details ?? '') as string,
      given: (meta['given'] ?? []) as Attachment[],
      files: (meta['attachments'] ?? []) as Attachment[],
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
    this.stopping.set(false);
    this.pending.set([]);
    this.uploadError.set('');
    if (!id) return;
    if (!this.threads().some(t => t.id === id)) this.ws.reloadThreads();  // started elsewhere
    this.api.messages(id).subscribe(m => { this.messages.set(m); this.scroll(); });
    this.sub = this.api.events(id).subscribe(ev => this.onEvent(ev));
    // Opened (or reloaded) mid-run: the live "thinking" event was missed, so ask.
    this.api.jobs(id).subscribe(jobs => {
      if (this.threadId() === id && jobs.some(j => j.status === 'queued' || j.status === 'running')) this.busy.set(true);
    });
  }

  stop() {
    const id = this.threadId();
    if (!id || this.stopping()) return;
    this.stopping.set(true);
    this.api.stop(id).subscribe({
      next: () => { this.stopping.set(false); this.busy.set(false); },
      error: e => { this.stopping.set(false); alert(e.error?.detail ?? 'Could not stop'); },
    });
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
      case 'job_queued': {
        const who = this.agentById(ev['agent_id'])?.name ?? 'a sub-agent';
        this.log(ev, `queued ${ev['kind']} for ${who} on pool ${ev['pool']}`, 'queue');
        break;
      }
      case 'job_stopped': {
        const who = this.agentById(ev['agent_id'])?.name ?? 'a sub-agent';
        this.log(ev, `stopped ${ev['kind']} for ${who}`, 'err');
        break;
      }
      case 'job_retry': this.log(ev, `job ${ev['job_id']} retried on ${ev['worker']} (attempt ${ev['attempt']})`, 'err'); break;
      case 'delegation_started': this.log(ev, `${ev['from_agent'] ?? 'you'} → ${ev['to_agent']} on ${ev['worker']}${ev['background'] ? ' (background)' : ''}: ${ev['task']}`, 'deleg'); break;
      case 'agent_tools': {
        const servers: string[] = ev['servers'] ?? [];
        if (!servers.length && !ev['depth']) break;  // a supervisor without tools is normal
        this.log(ev, servers.length ? `${ev['agent']} has tools from ${servers.join(', ')}`
                                    : ev['subagent'] ? `${ev['agent']} has no tools (none were given to it)`
                                    : `${ev['agent']} has no tool servers (Settings → Agents → Tools)`, 'queue');
        break;
      }
      case 'subagent_spawned': this.log(ev, `${ev['agent']} created sub-agent ${ev['subagent']} on ${ev['model']}`, 'deleg'); break;
      case 'skill_loaded': this.log(ev, `${ev['agent']} is following the skill ${ev['skill']} (v${ev['version']})`, 'deleg'); break;
      case 'skill_proposed': this.log(ev, `${ev['agent']} proposed a skill: ${ev['skill']}`, 'queue'); this.ws.refreshPending(); break;
      case 'approval_requested': this.log(ev, `${ev['agent']} is waiting for your OK to run ${ev['tool']} on ${ev['server']}`, 'queue'); this.ws.refreshPending(); break;
      case 'subagent_request': this.log(ev, `${ev['agent']} asked you for sub-agents`, 'queue'); this.ws.refreshPending(); break;
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
    const files = this.pending();
    if (!id || (!text && !files.length) || this.uploading()) return;
    this.draft = '';
    this.pending.set([]);
    this.uploadError.set('');
    this.busy.set(true);
    this.api.send(id, text, files.map(f => f.id)).subscribe({
      error: e => {
        this.busy.set(false);
        this.draft = text;
        this.pending.set(files);
        this.uploadError.set(e.error?.detail ?? 'Could not send');
      },
    });
  }

  // ---- attachments: picked, dropped or pasted files upload at once; sent with the message
  pending = signal<Attachment[]>([]);
  uploading = signal(0);
  uploadError = signal('');
  dropping = signal(false);

  filesOf(m: Message): Attachment[] { return (m.meta?.['attachments'] as Attachment[]) ?? []; }

  pick(e: Event) {
    const input = e.target as HTMLInputElement;
    this.attach(Array.from(input.files ?? []));
    input.value = '';
  }

  onPaste(e: ClipboardEvent) {
    const files = Array.from(e.clipboardData?.files ?? []);
    if (!files.length) return;  // ordinary text paste
    e.preventDefault();
    // Pasted screenshots are all called image.png; give them a time-stamped name.
    const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-');
    this.attach(files.map((f, i) => f.name === 'image.png'
      ? new File([f], `pasted-${stamp}${files.length > 1 ? '-' + (i + 1) : ''}.png`, { type: f.type }) : f));
  }

  onDragOver(e: DragEvent) {
    if (!this.threadId() || !e.dataTransfer?.types.includes('Files')) return;
    e.preventDefault();
    this.dropping.set(true);
  }

  onDragLeave(e: DragEvent) {
    const to = e.relatedTarget as Node | null;
    if (!to || !(e.currentTarget as HTMLElement).contains(to)) this.dropping.set(false);
  }

  onDrop(e: DragEvent) {
    if (!e.dataTransfer?.files.length) return;
    e.preventDefault();
    this.dropping.set(false);
    this.attach(Array.from(e.dataTransfer.files));
  }

  private attach(files: File[]) {
    const id = this.threadId();
    if (!id || !files.length) return;
    this.uploadError.set('');
    this.uploading.update(n => n + files.length);
    this.api.uploadAttachments(id, files).subscribe({
      next: added => { if (this.threadId() === id) this.pending.update(p => [...p, ...added]); },
      error: e => this.uploadError.set(e.error?.detail ?? 'Upload failed'),
    }).add(() => this.uploading.update(n => Math.max(0, n - files.length)));
  }

  unattach(f: Attachment) {
    this.pending.update(p => p.filter(x => x.id !== f.id));
    this.api.removeAttachment(f.id).subscribe({ error: () => {} });
  }

  private scroll() {
    setTimeout(() => { const el = this.scroller()?.nativeElement; if (el) el.scrollTop = el.scrollHeight; });
  }
}
