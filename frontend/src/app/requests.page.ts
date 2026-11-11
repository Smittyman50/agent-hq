import { Component, OnInit, computed, inject, signal } from '@angular/core';
import { DatePipe, NgTemplateOutlet } from '@angular/common';
import { ActivatedRoute, Router } from '@angular/router';
import { Api, KnowledgeProposal, ProfileProposal, SkillProposal, SubagentRequest, ToolApproval } from './api.service';
import { ApprovalCard } from './approval-card';
import { Avatar } from './avatar';
import { RequestCard } from './request-card';
import { SkillProposalCard } from './skill-proposal-card';
import { Workspace } from './workspace';
import { ProfileProposalCard } from './profile-proposal-card';
import { KnowledgeProposalCard } from './knowledge-proposal-card';

/** Requests → Tool approvals: calls waiting for your OK, then recent decisions. */
@Component({
  selector: 'hq-approvals-inbox',
  imports: [ApprovalCard],
  template: `
  <div class="inbox">
    <p class="hint inline-hint">Tool calls waiting for your OK. Choose which tools need it under Settings → Tools → Approvals.</p>
    @if (linked(); as id) { <hq-approval-card [approvalId]="id" [showLink]="true" (decided)="reload()" class="linked" /> }
    @for (a of pending(); track a.id) {
      @if (a.id !== linked()) { <hq-approval-card [approvalId]="a.id" [showLink]="true" (decided)="reload()" /> }
    } @empty { @if (!linked()) { <div class="empty-box">No tool calls are waiting.</div> } }
    @if (recent().length) {
      <details class="recent"><summary>Recent decisions ({{ recent().length }})</summary>
        @for (a of recent(); track a.id) { <hq-approval-card [approvalId]="a.id" [showLink]="true" /> }
      </details>
    }
  </div>`,
})
export class ApprovalsInbox implements OnInit {
  private api = inject(Api);
  private route = inject(ActivatedRoute);
  private ws = inject(Workspace);
  all = signal<ToolApproval[]>([]);
  linked = signal<string | null>(null);
  pending = computed(() => this.all().filter(a => a.status === 'pending'));
  recent = computed(() => this.all().filter(a => a.status !== 'pending' && a.id !== this.linked()).slice(0, 20));

  ngOnInit() {
    this.linked.set(this.route.snapshot.queryParamMap.get('approval'));
    this.reload();
  }
  reload() { this.api.toolApprovals().subscribe(a => this.all.set(a)); this.ws.refreshPending(); }
}

/** Requests → Skill proposals: new skills and improvements agents propose. */
@Component({
  selector: 'hq-skills-inbox',
  imports: [SkillProposalCard],
  template: `
  <div class="inbox">
    <p class="hint inline-hint">New skills and improvements from agents whose learning is set to “Propose to me”.
      Approved ones go to Settings → Skills.</p>
    @if (linked(); as id) { <hq-skill-proposal-card [proposalId]="id" (decided)="reload()" class="linked" /> }
    @for (p of pending(); track p.id) {
      @if (p.id !== linked()) { <hq-skill-proposal-card [proposalId]="p.id" (decided)="reload()" /> }
    } @empty { @if (!linked()) { <div class="empty-box">No proposals waiting.</div> } }
    @if (recent().length) {
      <details class="recent"><summary>Recent decisions ({{ recent().length }})</summary>
        @for (p of recent(); track p.id) { <hq-skill-proposal-card [proposalId]="p.id" /> }
      </details>
    }
  </div>`,
})
export class SkillsInbox implements OnInit {
  private api = inject(Api);
  private route = inject(ActivatedRoute);
  private ws = inject(Workspace);
  all = signal<SkillProposal[]>([]);
  linked = signal<string | null>(null);
  pending = computed(() => this.all().filter(p => p.status === 'pending'));
  recent = computed(() => this.all().filter(p => p.status !== 'pending' && p.id !== this.linked()).slice(0, 20));

  ngOnInit() {
    this.linked.set(this.route.snapshot.queryParamMap.get('skill'));
    this.reload();
  }
  reload() { this.api.skillProposals().subscribe(p => this.all.set(p)); this.ws.refreshPending(); }
}

/** Requests → Sub-agent requests: the list on the left, the selected request on the right. */
@Component({
  selector: 'hq-subagent-inbox',
  imports: [DatePipe, NgTemplateOutlet, Avatar, RequestCard],
  template: `
  <div class="split">
    <aside class="list">
      <div class="list-head"><h2>Waiting ({{ pending().length }})</h2></div>
      @for (r of pending(); track r.id) { <ng-container *ngTemplateOutlet="row; context: { $implicit: r }" /> }
      @empty { <p class="hint">Nothing waiting. Agents set to “Ask me first” send their requests here.</p> }
      @if (decided().length) {
        <div class="list-head"><h2>Decided</h2></div>
        @for (r of decided(); track r.id) {
          <button class="row" [class.active]="selected() === r.id" (click)="open(r.id)">
            <span class="row-with-avatar"><hq-avatar [agent]="ws.agent(r.agent_id)" [size]="30" />
              <span class="row-text"><span class="name">{{ r.agent }} <span class="pill {{ cls(r.status) }}">{{ r.status }}</span></span>
                <span class="sub">{{ roles(r) }} · {{ r.created_at | date:'MMM d, h:mm a' }}</span></span></span>
          </button>
        }
      }
    </aside>
    <div class="editor">
      @if (selected(); as id) {
        <hq-request-card [requestId]="id" [showLink]="false" (decided)="reload()" />
        @if (current(); as r) {
          <div class="actions"><button class="ghost" (click)="goToChat(r.thread_id)">Open the conversation</button></div>
        }
      } @else { <div class="empty">No sub-agent requests yet.</div> }
    </div>
  </div>
  <ng-template #row let-r>
    <button class="row" [class.active]="selected() === r.id" (click)="open(r.id)">
      <span class="row-with-avatar"><hq-avatar [agent]="ws.agent(r.agent_id)" [size]="30" [active]="true" />
        <span class="row-text"><span class="name">{{ r.agent }}</span>
          <span class="sub">{{ roles(r) }} · ≈{{ r.estimate.total_minutes }} min</span></span></span>
    </button>
  </ng-template>`,
})
export class SubagentInbox implements OnInit {
  private api = inject(Api);
  private route = inject(ActivatedRoute);
  private router = inject(Router);
  ws = inject(Workspace);
  all = signal<SubagentRequest[]>([]);
  selected = signal<string | null>(null);
  pending = computed(() => this.all().filter(r => r.status === 'pending'));
  decided = computed(() => this.all().filter(r => r.status !== 'pending'));
  current = computed(() => this.all().find(r => r.id === this.selected()));

  ngOnInit() {
    this.route.queryParamMap.subscribe(p => { if (p.get('id')) this.selected.set(p.get('id')); });
    this.reload();
  }

  reload() {
    this.api.subagentRequests().subscribe(rs => {
      this.all.set(rs);
      if (!this.selected() && rs.length) this.selected.set((rs.find(r => r.status === 'pending') ?? rs[0]).id);
    });
    this.ws.refreshPending();
  }

  open(id: string) { this.selected.set(id); this.router.navigate([], { queryParams: { id }, replaceUrl: true }); }
  roles(r: SubagentRequest) { return r.subagents.map(s => s.role).join(', '); }
  cls(s: string) { return s === 'approved' ? 'done' : s === 'denied' ? 'failed' : 'queued'; }
  goToChat(threadId: string) { this.router.navigate(['/chat', threadId]); }
}

/** Requests → Profile updates: lines agents suggest for your profile. */
@Component({
  selector: 'hq-profile-inbox',
  imports: [ProfileProposalCard],
  template: `
  <div class="editor inbox">
    <p class="hint inline-hint">When an agent notices a standing preference (“from now on…”), it suggests a line for
      your profile, which every agent sees. Reword it if you like before adding it.</p>
    @if (linked(); as id) { <hq-profile-proposal-card [proposalId]="id" (decided)="reload()" class="linked" /> }
    @for (p of pending(); track p.id) {
      @if (p.id !== linked()) { <hq-profile-proposal-card [proposalId]="p.id" (decided)="reload()" /> }
    } @empty { @if (!linked()) { <div class="empty-box">No profile suggestions waiting.</div> } }
    @if (recent().length) {
      <details class="recent"><summary>Recent decisions ({{ recent().length }})</summary>
        @for (p of recent(); track p.id) { <hq-profile-proposal-card [proposalId]="p.id" /> }
      </details>
    }
  </div>`,
})
export class ProfileInbox implements OnInit {
  private api = inject(Api);
  private route = inject(ActivatedRoute);
  private ws = inject(Workspace);
  all = signal<ProfileProposal[]>([]);
  linked = signal<string | null>(null);
  pending = computed(() => this.all().filter(p => p.status === 'pending'));
  recent = computed(() => this.all().filter(p => p.status !== 'pending' && p.id !== this.linked()).slice(0, 20));

  ngOnInit() {
    this.linked.set(this.route.snapshot.queryParamMap.get('suggestion'));
    this.reload();
  }
  reload() { this.api.profileProposals().subscribe(p => this.all.set(p)); this.ws.refreshPending(); }
}

/** Requests → Knowledge: documents agents suggest for the knowledge library. */
@Component({
  selector: 'hq-knowledge-inbox',
  imports: [KnowledgeProposalCard],
  template: `
  <div class="editor inbox">
    <p class="hint inline-hint">Documents agents think the team should keep: runbooks they worked out, research findings,
      summaries. They go to Agent Findings unless you allow agents to suggest for another collection (Settings → Knowledge).
      Edit, move or reject them; nothing reaches the library until you add it.</p>
    @if (linked(); as id) { <hq-knowledge-proposal-card [proposalId]="id" [showThread]="true" (decided)="reload()" class="linked" /> }
    @for (p of pending(); track p.id) {
      @if (p.id !== linked()) { <hq-knowledge-proposal-card [proposalId]="p.id" [showThread]="true" (decided)="reload()" /> }
    } @empty { @if (!linked()) { <div class="empty-box">No documents waiting.</div> } }
    @if (recent().length) {
      <details class="recent"><summary>Recent decisions ({{ recent().length }})</summary>
        @for (p of recent(); track p.id) { <hq-knowledge-proposal-card [proposalId]="p.id" [showThread]="true" /> }
      </details>
    }
  </div>`,
})
export class KnowledgeInbox implements OnInit {
  private api = inject(Api);
  private route = inject(ActivatedRoute);
  private ws = inject(Workspace);
  all = signal<KnowledgeProposal[]>([]);
  linked = signal<string | null>(null);
  pending = computed(() => this.all().filter(p => p.status === 'pending'));
  recent = computed(() => this.all().filter(p => p.status !== 'pending' && p.id !== this.linked()).slice(0, 20));

  ngOnInit() {
    this.linked.set(this.route.snapshot.queryParamMap.get('document'));
    this.reload();
  }
  reload() { this.api.knowledgeProposals().subscribe(p => this.all.set(p)); this.ws.refreshPending(); }
}
