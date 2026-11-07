import { Component, ElementRef, OnDestroy, OnInit, computed, effect, inject, signal, viewChild } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { DatePipe } from '@angular/common';
import { DomSanitizer, SafeResourceUrl } from '@angular/platform-browser';
import { ActivatedRoute, Router } from '@angular/router';
import { Subscription } from 'rxjs';
import { AgentDef, Api, DesktopState, HqEvent, Message, Thread } from './api.service';

interface Activity { ts: string; text: string; kind: string; depth: number; }

@Component({
  selector: 'hq-chat',
  imports: [FormsModule, DatePipe],
  template: `
  <div class="chat-layout" [class.with-desktop]="desktopAgent()">
    <aside class="list">
      <div class="list-head">
        <select [(ngModel)]="newAgentId" name="newAgent">
          @for (a of enabledAgents(); track a.id) { <option [value]="a.id">{{ a.name }}</option> }
        </select>
        <button (click)="newThread()" [disabled]="!newAgentId">+ Chat</button>
      </div>
      @for (t of threads(); track t.id) {
        <button class="row" [class.active]="t.id === threadId()" (click)="open(t.id)">
          <span class="name">{{ agentName(t.agent_id) }}</span>
          <span class="sub">{{ t.title }} · {{ t.created_at | date:'MMM d, h:mm a' }}</span>
        </button>
      } @empty { <p class="hint">No conversations yet.</p> }
    </aside>

    <section class="conversation">
      @if (threadId()) {
        <header class="conv-head">
          <strong>{{ agentName(thread()?.agent_id) }}</strong>
          @if (busy()) { <span class="pill live">working…</span> }
          <span class="spacer"></span>
          <select [ngModel]="desktopAgentId()" (ngModelChange)="desktopAgentId.set($event)" name="desk">
            <option value="">No desktop</option>
            @for (a of desktopAgents(); track a.id) { <option [value]="a.id">{{ a.name }}'s desktop</option> }
          </select>
        </header>
        @if (pendingRequest(); as r) {
          <div class="banner">
            <span><strong>{{ agentName(r.agent_id) }}</strong> needs you on its desktop: {{ r.reason }}</span>
            <button (click)="takeFor(r.agent_id)">Open desktop and take control</button>
          </div>
        }
        <div class="messages" #scroller>
          @for (m of messages(); track m.id) {
            <div class="msg {{ m.role }}">
              @if (m.role !== 'user') { <div class="who">{{ m.role === 'event' ? 'activity' : agentName(m.agent_id) }}</div> }
              <div class="body">{{ m.content }}</div>
            </div>
          }
        </div>
        @if (activity().length) {
          <details class="activity" open>
            <summary>Live activity ({{ activity().length }})</summary>
            @for (a of activity(); track $index) {
              <div class="act {{ a.kind }}" [style.padding-left.px]="8 + a.depth * 16">
                <span class="ts">{{ a.ts | date:'HH:mm:ss' }}</span> {{ a.text }}
              </div>
            }
          </details>
        }
        <form class="composer" (ngSubmit)="send()">
          <textarea name="draft" [(ngModel)]="draft" rows="2" placeholder="Message…"
            (keydown.enter)="onEnter($event)"></textarea>
          <button type="submit" [disabled]="!draft.trim()">Send</button>
        </form>
      } @else {
        <div class="empty">Pick a conversation or start a new chat.</div>
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
            <div class="desk-bar muted">To use take-control, tick "Controls an agent desktop" on this desktop's tool server (Tools page).</div>
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
  private route = inject(ActivatedRoute);
  private router = inject(Router);
  private sanitizer = inject(DomSanitizer);
  private scroller = viewChild<ElementRef<HTMLElement>>('scroller');
  private sub?: Subscription;

  agents = signal<AgentDef[]>([]);
  threads = signal<Thread[]>([]);
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
  newAgentId = '';
  draft = '';

  enabledAgents = computed(() => this.agents().filter(a => a.enabled));
  desktopAgents = computed(() => this.agents().filter(a => a.desktop_url));
  thread = computed(() => this.threads().find(t => t.id === this.threadId()));
  desktopAgent = computed(() => this.agents().find(a => a.id === this.desktopAgentId()));
  desktopUrl = computed<SafeResourceUrl | null>(() => {
    const u = this.desktopAgent()?.desktop_url;
    return u ? this.sanitizer.bypassSecurityTrustResourceUrl(u) : null;
  });

  ngOnInit() {
    this.api.agents().subscribe(a => {
      this.agents.set(a);
      this.newAgentId = (a.find(x => x.is_default && x.enabled) ?? a.find(x => x.enabled))?.id ?? '';
    });
    this.loadThreads();
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

  agentName(id: string | null | undefined) { return this.agents().find(a => a.id === id)?.name ?? '—'; }
  loadThreads() { this.api.threads().subscribe(t => this.threads.set(t)); }
  open(id: string) { this.router.navigate(['/chat', id]); }

  newThread() {
    this.api.createThread(this.newAgentId).subscribe(t => { this.loadThreads(); this.open(t.id); });
  }

  select(id: string | null) {
    this.sub?.unsubscribe();
    this.threadId.set(id);
    this.messages.set([]);
    this.activity.set([]);
    this.busy.set(false);
    if (!id) return;
    this.api.messages(id).subscribe(m => { this.messages.set(m); this.scroll(); });
    this.sub = this.api.events(id).subscribe(ev => this.onEvent(ev));
  }

  onEvent(ev: HqEvent) {
    switch (ev.type) {
      case 'message':
        if (!this.messages().some(m => m.id === ev['message'].id)) {
          this.messages.update(ms => [...ms, ev['message']]);
          this.scroll();
        }
        break;
      case 'status': this.busy.set(ev['state'] === 'thinking'); break;
      case 'tool_call': this.log(ev, `${ev['agent']} → ${ev['tool']}(${JSON.stringify(ev['args'])})`, 'call'); break;
      case 'tool_result': this.log(ev, `${ev['agent']} ← ${ev['tool']}: ${ev['error'] ?? ev['preview']}`, ev['error'] ? 'err' : 'result'); break;
      case 'job_queued': this.log(ev, `queued ${ev['kind']} for ${this.agentName(ev['agent_id'])} on pool ${ev['pool']}`, 'queue'); break;
      case 'job_retry': this.log(ev, `job ${ev['job_id']} retried on ${ev['worker']} (attempt ${ev['attempt']})`, 'err'); break;
      case 'delegation_started': this.log(ev, `${ev['from_agent'] ?? 'you'} → ${ev['to_agent']} on ${ev['worker']}${ev['background'] ? ' (background)' : ''}: ${ev['task']}`, 'deleg'); break;
      case 'delegation_finished': this.log(ev, `${ev['to_agent']} finished [${ev['result'].status}]`, 'deleg'); break;
    }
  }

  private log(ev: HqEvent, text: string, kind: string) {
    this.activity.update(a => [...a.slice(-199), { ts: ev['ts'], text, kind, depth: ev['depth'] ?? 0 }]);
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
