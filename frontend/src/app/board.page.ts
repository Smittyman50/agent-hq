import { Component, OnDestroy, OnInit, computed, inject, signal } from '@angular/core';
import { DatePipe, NgTemplateOutlet } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { ActivatedRoute, Router } from '@angular/router';
import { Api, Board, Goal, GoalDetail, GoalIn, GoalLimits, WorkStatus } from './api.service';
import { Avatar } from './avatar';
import { GoalCard } from './goal-card';
import { Icon } from './icon';
import { Markdown } from './markdown';
import { Workspace } from './workspace';

/** Columns as shown: Blocked goals stay in In progress (flagged), Dropped ones in Done. */
const SHOWN_IN: Record<WorkStatus, WorkStatus> = {
  backlog: 'backlog', ready: 'ready', in_progress: 'in_progress', blocked: 'in_progress',
  waiting: 'waiting', review: 'review', done: 'done', dropped: 'done',
};
/** Where you may drop a card (Waiting and Review are the agent's). */
const DROP_TARGETS: WorkStatus[] = ['backlog', 'ready', 'in_progress', 'done'];

const WAITING: Record<string, string> = {
  delegation: 'a delegated task', approval: 'a tool approval', handoff: 'you on its desktop',
  user: 'your answer', time: 'a time', team: 'its team', stalled: 'no progress', limit: 'a limit', stopped: 'stopped by you',
};

function blankGoal(agentId = ''): GoalIn {
  return { title: '', agent_id: agentId, objective: '', done_criteria: '', status: 'ready', priority: 0, limits: {} };
}

/**
 * The board: standing goals by agent (swimlanes) and column. Agents pull from Ready
 * when they have a free WIP slot; Backlog is yours. Open a card for its next actions,
 * its log, and anything it needs from you.
 */
@Component({
  selector: 'hq-board',
  imports: [FormsModule, DatePipe, NgTemplateOutlet, Avatar, GoalCard, Icon, Markdown],
  template: `
  <div class="page-wrap">
    <header class="crumbs board-crumbs">
      <hq-icon name="board" [size]="18" /><strong>Board</strong>
      <span class="board-tools">
        <select [ngModel]="agentFilter()" (ngModelChange)="agentFilter.set($event)" title="Show one agent">
          <option value="">All agents</option>
          @for (l of board()?.agents ?? []; track l.id) { <option [value]="l.id">{{ l.name }}</option> }
        </select>
        <select [ngModel]="doneDays()" (ngModelChange)="doneDays.set(+$event); load()" title="Done goals shown">
          <option [ngValue]="7">Done: last 7 days</option><option [ngValue]="30">Done: last 30 days</option>
          <option [ngValue]="365">Done: last year</option>
        </select>
        <button (click)="newGoal()">+ New goal</button>
      </span>
    </header>
    @if (error()) { <p class="error board-error">{{ error() }} <button class="mini" (click)="error.set('')">✕</button></p> }
    <div class="board-body" [class.with-panel]="panelOpen()">
      <div class="board-scroll">
        @if (board(); as b) {
          <div class="board-grid" [style.grid-template-columns]="'150px repeat(' + b.columns.length + ', minmax(165px, 1fr))'">
            <div class="board-corner"></div>
            @for (c of b.columns; track c.status) {
              <div class="board-colhead">{{ c.label }} <span class="muted">{{ count(c.status) }}</span></div>
            }
            @for (lane of lanes(); track lane.id) {
              <div class="board-lane" [class.muted]="!lane.enabled">
                <hq-avatar [agent]="ws.agent(lane.id)" [size]="30" [active]="ws.isActive(lane.id)" />
                <span class="row-text"><span class="name">{{ lane.name }}</span>
                  <span class="sub" [class.full]="lane.goal_wip > 0 && lane.wip_used >= lane.goal_wip">
                    WIP {{ lane.wip_used }}/{{ lane.goal_wip }}</span></span>
              </div>
              @for (c of b.columns; track c.status) {
                <div class="board-cell" [class.droppable]="canDrop(c.status)" [class.over]="over() === lane.id + c.status"
                     (dragover)="dragOver($event, lane.id, c.status)" (dragleave)="over.set('')"
                     (drop)="dropOn($event, lane.id, c.status)">
                  @for (g of cell(lane.id, c.status); track g.id) {
                    <button class="goal-tile" [class.on]="selectedId() === g.id" [class.blocked]="g.status === 'blocked'"
                            [class.needs]="!!g.needs_you" [class.dropped]="g.status === 'dropped'"
                            draggable="true" (dragstart)="dragging.set(g)" (dragend)="dragging.set(null)" (click)="open(g.id)">
                      <span class="tile-title">{{ g.title }}</span>
                      <span class="tile-meta">
                        @if (g.needs_you) { <span class="pill you">{{ needLabel(g) }}</span> }
                        @else if (g.status === 'blocked') { <span class="pill failed">blocked</span> }
                        @else if (g.status === 'dropped') { <span class="pill stopped">dropped</span> }
                        @if (g.status === 'waiting' && g.waiting_on) { <span class="muted">on {{ waitLabel(g) }}</span> }
                        @if (g.priority > 0) { <span class="pill">P{{ g.priority }}</span> }
                        @if (g.actions_total) { <span class="muted">{{ g.actions_done }}/{{ g.actions_total }} actions</span> }
                        @if (g.team_working) { <span class="pill team" [title]="g.team_working + ' team member(s) working'">team ×{{ g.team_working }}</span> }
                      </span>
                      @if (g.current_action) { <span class="tile-now">▸ {{ g.current_action }}</span> }
                    </button>
                  }
                </div>
              }
            } @empty {
              <div class="board-emptyrow" [style.grid-column]="'1 / -1'">
                No agents take goals yet. Set an agent's goal WIP limit under Settings → Agents.</div>
            }
          </div>
          @if (!b.goals.length) {
            <div class="empty-box board-hint">No goals yet. A goal is an outcome with a definition of done:
              the agent breaks it into next actions and keeps working it, one action at a time, until it's done.
              Put it in Ready and the agent starts when it has a free slot.</div>
          }
        } @else { <div class="empty">Loading…</div> }
      </div>

      @if (draft(); as d) {
        <aside class="board-panel">
          <form (ngSubmit)="create()">
            <div class="panel-head"><h2>New goal</h2><button type="button" class="icon-btn" (click)="close()" title="Close"><hq-icon name="x" /></button></div>
            <label>Title<input name="title" [(ngModel)]="d.title" placeholder="e.g. Document every VLAN" required></label>
            <label>Agent
              <select name="agent" [(ngModel)]="d.agent_id" [class.needs]="!d.agent_id">
                <option value="" disabled>Choose an agent…</option>
                @for (a of goalAgents(); track a.id) { <option [value]="a.id">{{ a.name }}</option> }
              </select></label>
            <label>Objective <small>the outcome you want</small>
              <textarea name="obj" rows="4" [(ngModel)]="d.objective"
                placeholder="Every VLAN on the network has a note in the vault saying what it's for and what's on it."></textarea></label>
            <label>Done when <small>how anyone can check it's finished</small>
              <textarea name="done" rows="3" [(ngModel)]="d.done_criteria"
                placeholder="One note per VLAN with its ID, subnet, gateway and purpose; none missing compared with the core switch."></textarea></label>
            <div class="model-row">
              <label>Column
                <select name="col" [(ngModel)]="d.status">
                  <option value="ready">Ready (start when a slot is free)</option>
                  <option value="backlog">Backlog (not yet)</option>
                </select></label>
              <label>Priority <input name="prio" type="number" min="0" max="9" [(ngModel)]="d.priority"></label>
            </div>
            <ng-container *ngTemplateOutlet="limitsTpl; context: { $implicit: d.limits }" />
            <div class="actions"><button type="submit" [disabled]="busy()">Create goal</button>
              <button type="button" class="ghost" (click)="close()">Cancel</button></div>
          </form>
        </aside>
      } @else if (detail(); as g) {
        <aside class="board-panel">
          <div class="panel-head">
            <span class="row-with-avatar"><hq-avatar [agent]="ws.agent(g.owner_agent_id)" [size]="28" />
              <span class="row-text"><span class="name">{{ g.title }}</span>
                <span class="sub">{{ g.agent_name }} · {{ g.column }}@if (g.status === 'waiting' && g.waiting_on) { on {{ waitLabel(g) }} }</span></span></span>
            <button class="icon-btn" (click)="close()" title="Close"><hq-icon name="x" /></button>
          </div>

          @if (g.needs_you) { <hq-goal-card [goalId]="g.id" [version]="g.updated_at" [showLinks]="false" (decided)="refresh()" /> }

          <div class="actions tight">
            @if (g.status === 'backlog' && !g.proposed) { <button class="mini" (click)="move(g, 'ready')">Move to Ready</button> }
            @if (g.status === 'ready') { <button class="mini" (click)="move(g, 'backlog')">Back to Backlog</button> }
            @if ((g.status === 'ready' || g.status === 'backlog') && !g.proposed) { <button class="mini" (click)="move(g, 'in_progress')">Start now</button> }
            @if (g.status === 'blocked' && !g.needs_you) { <button class="mini" (click)="move(g, 'in_progress')">Resume</button> }
            @if (g.status === 'done' || g.status === 'dropped') { <button class="mini" (click)="move(g, 'ready')">Reopen</button> }
            @if (g.thread_id) { <button class="mini ghost" (click)="openThread(g.thread_id)">Conversation</button> }
            @if (g.status !== 'done' && g.status !== 'dropped') { <button class="mini danger" (click)="move(g, 'dropped')">Drop</button> }
            <button class="mini ghost" (click)="editing() ? editing.set(false) : startEdit(g)">{{ editing() ? 'Cancel edit' : 'Edit' }}</button>
          </div>

          @if (editing()) {
            <form class="goal-edit" (ngSubmit)="saveEdit(g)">
              <label>Title<input name="t" [(ngModel)]="edit.title"></label>
              <label>Objective<textarea name="o" rows="4" [(ngModel)]="edit.objective"></textarea></label>
              <label>Done when<textarea name="dc" rows="3" [(ngModel)]="edit.done_criteria"></textarea></label>
              <div class="model-row">
                <label>Priority <input name="p" type="number" min="0" max="9" [(ngModel)]="edit.priority"></label>
                <label>Agent
                  <select name="a" [(ngModel)]="edit.owner_agent_id" [disabled]="g.status !== 'backlog' && g.status !== 'ready'">
                    @for (a of goalAgents(); track a.id) { <option [value]="a.id">{{ a.name }}</option> }
                  </select></label>
              </div>
              <ng-container *ngTemplateOutlet="limitsTpl; context: { $implicit: edit.limits }" />
              <div class="actions tight"><button type="submit" [disabled]="busy()">Save</button></div>
            </form>
          } @else {
            <h3>Objective</h3><div class="goal-text">{{ g.objective }}</div>
            <h3>Done when</h3><div class="goal-text">{{ g.done_criteria }}</div>
            @if (g.result && g.status !== 'review') { <h3>Done claim</h3><div class="body rich"><hq-md [text]="g.result" /></div> }
          }

          <h3>Next actions <small>{{ g.actions_done }}/{{ g.actions_total }} done ·
            @if (g.team_size > 1) { team of {{ g.team_size }}: {{ g.agent_name }} + {{ g.team_size - 1 }} at once
              ({{ teamFrom(g) }}), {{ g.team_working }} working }
            @else { one at a time }</small></h3>
          <ul class="goal-actions">
            @for (a of g.actions; track a.id) {
              <li [class]="'act-' + a.status + (a.on_team ? ' act-team' : '')">
                <span class="act-mark">{{ a.on_team ? '⇉' : mark(a.status) }}</span>
                <span class="act-body"><span class="act-title">{{ a.title }}</span>
                  @if (a.assignee) { <span class="sub act-who">{{ a.on_team ? 'working: ' : 'by ' }}{{ a.assignee }} <span class="pill sub">{{ a.assignee_kind === 'agent' ? 'agent' : 'sub-agent' }}</span></span> }
                  @if (a.waiting_for.length) { <span class="sub act-after">waits for {{ titlesOf(g, a.waiting_for) }}</span> }
                  @else if (a.after.length && a.status === 'backlog') { <span class="sub act-after">after {{ titlesOf(g, a.after) }} (done)</span> }
                  @if (a.details) { <span class="sub">{{ a.details }}</span> }
                  @if (a.result) { <span class="sub act-result">{{ a.result }}</span> }</span>
                <span class="act-ctl">
                  @if (a.status === 'backlog' && a.startable) { <button class="mini ghost" (click)="setAction(g, a.id, 'in_progress')" title="Start">▸</button> }
                  @if (a.status !== 'done' && a.status !== 'dropped' && !a.on_team) {
                    <button class="mini ghost" (click)="setAction(g, a.id, 'done')" title="Mark done">✓</button>
                  }
                  @if (a.status !== 'done' && a.status !== 'dropped') {
                    <button class="mini ghost" (click)="setAction(g, a.id, 'dropped')"
                            [title]="a.on_team ? 'Drop it (a late result from its team member is ignored)' : 'Drop'">✕</button>
                  }
                </span>
              </li>
            } @empty { <li class="act-none muted">None yet: the agent adds them on its first turn, or add one yourself.</li> }
          </ul>
          <form class="inline-add" (ngSubmit)="addAction(g)">
            <input name="na" [(ngModel)]="newAction" placeholder="Add a next action">
            <button type="submit" class="mini" [disabled]="!newAction.trim()">Add</button>
          </form>

          <h3>Log</h3>
          <ul class="goal-log">
            @for (e of g.events.slice().reverse(); track e.id) {
              <li><span class="ts">{{ e.at | date:'MMM d, HH:mm' }}</span> <strong>{{ e.actor }}</strong> {{ e.text }}</li>
            }
          </ul>
          <form class="inline-add" (ngSubmit)="addNote(g)">
            <input name="nn" [(ngModel)]="newNote" placeholder="Add a note for the agent">
            <button type="submit" class="mini" [disabled]="!newNote.trim()">Add</button>
          </form>

          <p class="hint inline-hint">Goal turns {{ g.turns }}/{{ g.limits.max_turns }} ·
            model requests {{ g.requests_used }}/{{ g.limits.max_requests }} · up to {{ g.limits.max_hours }} h ·
            blocked after {{ g.limits.stall_turns }} turns without progress.
            @if (g.started_at) { Started {{ g.started_at | date:'MMM d, h:mm a' }}. }</p>
        </aside>
      }
    </div>
  </div>

  <ng-template #limitsTpl let-lim>
    <details class="goal-limits"><summary>Limits</summary>
      <div class="limit-grid">
        <label>Goal turns <input type="number" min="1" [ngModel]="lim.max_turns" (ngModelChange)="lim.max_turns = $event" [placeholder]="'' + defaults.max_turns" [ngModelOptions]="{standalone: true}"></label>
        <label>Hours <input type="number" min="1" [ngModel]="lim.max_hours" (ngModelChange)="lim.max_hours = $event" [placeholder]="'' + defaults.max_hours" [ngModelOptions]="{standalone: true}"></label>
        <label>Model requests <input type="number" min="10" [ngModel]="lim.max_requests" (ngModelChange)="lim.max_requests = $event" [placeholder]="'' + defaults.max_requests" [ngModelOptions]="{standalone: true}"></label>
        <label>Turns without progress <input type="number" min="1" [ngModel]="lim.stall_turns" (ngModelChange)="lim.stall_turns = $event" [placeholder]="'' + defaults.stall_turns" [ngModelOptions]="{standalone: true}"></label>
      </div>
      <p class="hint inline-hint">When a goal reaches one, it's blocked and you're asked what to do. Empty = the default.</p>
    </details>
  </ng-template>
  `,
})
export class BoardPage implements OnInit, OnDestroy {
  private api = inject(Api);
  private route = inject(ActivatedRoute);
  private router = inject(Router);
  ws = inject(Workspace);

  board = signal<Board | null>(null);
  detail = signal<GoalDetail | null>(null);
  selectedId = signal<string | null>(null);
  draft = signal<GoalIn | null>(null);
  agentFilter = signal('');
  doneDays = signal(7);
  error = signal('');
  busy = signal(false);
  editing = signal(false);
  dragging = signal<Goal | null>(null);
  over = signal('');
  newAction = '';
  newNote = '';
  edit: { title: string; objective: string; done_criteria: string; priority: number; owner_agent_id: string;
          limits: Partial<GoalLimits> } = { title: '', objective: '', done_criteria: '', priority: 0, owner_agent_id: '', limits: {} };
  defaults: GoalLimits = { max_turns: 40, max_hours: 72, max_requests: 400, stall_turns: 3 };
  private timer?: ReturnType<typeof setInterval>;
  private fromThread: string | null = null;
  private fromMessage: string | null = null;

  goalAgents = computed(() => this.ws.agents().filter(a => a.enabled && !a.is_subagent));
  lanes = computed(() => (this.board()?.agents ?? []).filter(l => !this.agentFilter() || l.id === this.agentFilter()));
  panelOpen = computed(() => !!this.draft() || !!this.detail());

  ngOnInit() {
    const q = this.route.snapshot.queryParamMap;
    if (q.get('item')) this.open(q.get('item')!);
    if (q.get('new')) {
      // "Make this a goal" from a chat message: the text comes along in the navigation state.
      const text = (history.state?.text as string | undefined) ?? '';
      this.fromThread = q.get('from_thread');
      this.fromMessage = q.get('from_message');
      const d = blankGoal(q.get('agent') ?? '');
      d.objective = text.trim();
      d.title = text.trim().split('\n')[0].replace(/^[#>*\-\s]+/, '').slice(0, 80);
      this.draft.set(d);
    }
    this.load();
    this.timer = setInterval(() => this.refresh(), 5000);
  }
  ngOnDestroy() { clearInterval(this.timer); }

  load() { this.api.board(this.doneDays()).subscribe({ next: b => this.board.set(b), error: () => {} }); }
  refresh() {
    this.load();
    const id = this.selectedId();
    if (id && !this.editing()) this.api.goal(id).subscribe({ next: d => this.detail.set(d), error: () => {} });
    this.ws.refreshPending();
  }

  cell(agentId: string, column: WorkStatus): Goal[] {
    return (this.board()?.goals ?? []).filter(g => g.owner_agent_id === agentId && SHOWN_IN[g.status] === column);
  }
  count(column: WorkStatus) {
    return (this.board()?.goals ?? []).filter(g => SHOWN_IN[g.status] === column
      && (!this.agentFilter() || g.owner_agent_id === this.agentFilter())).length || '';
  }
  needLabel(g: Goal) { return { review: 'confirm done?', proposal: 'proposed', blocked: 'blocked', question: 'question' }[g.needs_you!]; }
  waitLabel(g: Goal) {
    const w = g.waiting_on;
    if (w?.kind === 'time' && w.until) {
      const at = new Date(w.until);
      const sameDay = at.toDateString() === new Date().toDateString();
      return 'a check-back at ' + at.toLocaleString([], sameDay ? { hour: 'numeric', minute: '2-digit' }
        : { weekday: 'short', hour: 'numeric', minute: '2-digit' });
    }
    return WAITING[w?.kind ?? ''] ?? w?.kind;
  }
  teamFrom(g: GoalDetail) {
    const who = [...g.team_agents, ...(g.team_sub_agents ? ['sub-agents'] : [])];
    return who.length > 1 ? who.slice(0, -1).join(', ') + ' or ' + who[who.length - 1] : (who[0] ?? 'sub-agents');
  }
  titlesOf(g: GoalDetail, ids: string[]) {
    return ids.map(id => g.actions.find(a => a.id === id)?.title ?? id).join(', ');
  }
  mark(s: WorkStatus) { return ({ done: '✓', in_progress: '▸', blocked: '!', dropped: '–' } as Record<string, string>)[s] ?? '○'; }

  open(id: string) {
    this.draft.set(null);
    this.editing.set(false);
    this.selectedId.set(id);
    this.router.navigate([], { queryParams: { item: id }, replaceUrl: true });
    this.api.goal(id).subscribe({ next: d => { this.detail.set(d); this.defaults = d.default_limits; },
                                  error: () => this.error.set('That goal no longer exists.') });
  }
  close() {
    this.draft.set(null); this.detail.set(null); this.selectedId.set(null); this.editing.set(false);
    this.router.navigate([], { queryParams: {}, replaceUrl: true });
  }
  newGoal() {
    this.close();
    this.fromThread = this.fromMessage = null;
    this.draft.set(blankGoal(this.agentFilter() || this.goalAgents()[0]?.id || ''));
  }

  private limits(l: Partial<GoalLimits> | undefined) {
    const out: Partial<GoalLimits> = {};
    for (const [k, v] of Object.entries(l ?? {})) {
      if (v !== null && v !== undefined && `${v}` !== '') (out as any)[k] = +v;
    }
    return out;
  }

  create() {
    const d = this.draft();
    if (!d) return;
    this.busy.set(true);
    this.api.createGoal({ ...d, limits: this.limits(d.limits), from_thread_id: this.fromThread, from_message_id: this.fromMessage })
      .subscribe({
        next: g => { this.busy.set(false); this.error.set(''); this.load(); this.open(g.id); },
        error: e => { this.busy.set(false); this.error.set(e.error?.detail ?? 'Could not create the goal'); },
      });
  }

  private after(obs: ReturnType<Api['goal']>) {
    this.busy.set(true);
    obs.subscribe({
      next: d => { this.busy.set(false); this.error.set(''); this.detail.set(d); this.load(); this.ws.refreshPending(); },
      error: e => { this.busy.set(false); this.error.set(e.error?.detail ?? 'That didn’t work'); this.load(); },
    });
  }
  move(g: Goal, status: WorkStatus) { this.after(this.api.moveGoal(g.id, status)); }
  setAction(g: Goal, actionId: string, status: WorkStatus) { this.after(this.api.setGoalAction(g.id, actionId, status)); }
  addAction(g: Goal) {
    const t = this.newAction.trim();
    if (!t) return;
    this.newAction = '';
    this.after(this.api.addGoalAction(g.id, t));
  }
  addNote(g: Goal) {
    const t = this.newNote.trim();
    if (!t) return;
    this.newNote = '';
    this.after(this.api.addGoalNote(g.id, t));
  }
  saveEdit(g: GoalDetail) {
    const e = this.edit;
    const patch: Parameters<Api['updateGoal']>[1] = {
      title: e.title, objective: e.objective, done_criteria: e.done_criteria, priority: +e.priority,
      limits: this.limits(e.limits),
    };
    if (e.owner_agent_id !== g.owner_agent_id) patch.owner_agent_id = e.owner_agent_id;
    this.busy.set(true);
    this.api.updateGoal(g.id, patch).subscribe({
      next: d => { this.busy.set(false); this.editing.set(false); this.detail.set(d); this.load(); },
      error: err => { this.busy.set(false); this.error.set(err.error?.detail ?? 'Save failed'); },
    });
  }
  openThread(id: string) { this.router.navigate(['/chat', id]); }

  /** Editing starts from the goal as it is now. */
  startEdit(g: GoalDetail) {
    this.edit = { title: g.title, objective: g.objective, done_criteria: g.done_criteria, priority: g.priority,
                  owner_agent_id: g.owner_agent_id ?? '', limits: { ...g.custom_limits } };
    this.editing.set(true);
  }

  canDrop(column: WorkStatus) { return !!this.dragging() && DROP_TARGETS.includes(column); }
  dragOver(ev: DragEvent, laneId: string, column: WorkStatus) {
    const g = this.dragging();
    if (!g || g.owner_agent_id !== laneId || !DROP_TARGETS.includes(column)) return;
    ev.preventDefault();
    this.over.set(laneId + column);
  }
  dropOn(ev: DragEvent, laneId: string, column: WorkStatus) {
    ev.preventDefault();
    this.over.set('');
    const g = this.dragging();
    this.dragging.set(null);
    if (!g || g.owner_agent_id !== laneId || SHOWN_IN[g.status] === column) return;
    const target: WorkStatus = column === 'in_progress' && g.status === 'blocked' ? 'in_progress' : column;
    this.api.moveGoal(g.id, target).subscribe({
      next: d => { this.error.set(''); this.load(); if (this.selectedId() === g.id) this.detail.set(d); this.ws.refreshPending(); },
      error: e => this.error.set(e.error?.detail ?? 'Can’t move it there'),
    });
  }
}
