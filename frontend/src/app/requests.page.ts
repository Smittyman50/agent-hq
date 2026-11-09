import { Component, OnInit, computed, inject, signal } from '@angular/core';
import { DatePipe, NgTemplateOutlet } from '@angular/common';
import { ActivatedRoute, Router } from '@angular/router';
import { Api, SubagentRequest } from './api.service';
import { Avatar } from './avatar';
import { Icon } from './icon';
import { RequestCard } from './request-card';
import { ApprovalCard } from './approval-card';
import { SkillProposalCard } from './skill-proposal-card';
import { SkillProposal, ToolApproval } from './api.service';
import { Workspace } from './workspace';

/** Your inbox of sub-agent requests: pending ones first, then decided ones. */
@Component({
  selector: 'hq-requests',
  imports: [DatePipe, NgTemplateOutlet, Avatar, Icon, RequestCard, ApprovalCard, SkillProposalCard],
  template: `
  <div class="page-wrap">
    <header class="crumbs"><hq-icon name="inbox" [size]="18" /><strong>Requests</strong>
      <span class="muted small">tool calls waiting for your OK, and agents asking for sub-agents</span></header>
    <section class="approvals-list">
      <h2>Tool approvals ({{ pendingApprovals().length }} waiting)</h2>
      @for (a of pendingApprovals(); track a.id) {
        <hq-approval-card [approvalId]="a.id" [showLink]="true" (decided)="reload()" />
      } @empty { <p class="hint">No tool calls are waiting. Set which tools need your OK under Settings → Tools → Approvals.</p> }
      @if (recentApprovals().length) {
        <details class="recent"><summary>Recent decisions ({{ recentApprovals().length }})</summary>
          @for (a of recentApprovals(); track a.id) { <hq-approval-card [approvalId]="a.id" [showLink]="true" /> }
        </details>
      }
    </section>
    <section class="approvals-list">
      <h2>Skill proposals ({{ pendingSkills().length }} waiting)</h2>
      @for (p of pendingSkills(); track p.id) {
        <hq-skill-proposal-card [proposalId]="p.id" (decided)="reload()" />
      } @empty { <p class="hint">No proposals waiting. Agents propose skills as they work out how to do things.</p> }
    </section>
    <h2 class="section-title">Sub-agent requests</h2>
    <div class="split">
      <aside class="list">
        <div class="list-head"><h2>Pending ({{ pending().length }})</h2></div>
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
      <div class="editor wide">
        @if (selected(); as id) {
          <hq-request-card [requestId]="id" [showLink]="false" (decided)="reload()" />
          @if (current(); as r) {
            <div class="actions"><button class="ghost" (click)="goToChat(r.thread_id)">Open the conversation</button></div>
          }
        } @else { <div class="empty">Select a request.</div> }
      </div>
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
export class RequestsPage implements OnInit {
  private api = inject(Api);
  private route = inject(ActivatedRoute);
  private router = inject(Router);
  ws = inject(Workspace);
  all = signal<SubagentRequest[]>([]);
  selected = signal<string | null>(null);
  pending = computed(() => this.all().filter(r => r.status === 'pending'));
  decided = computed(() => this.all().filter(r => r.status !== 'pending'));
  current = computed(() => this.all().find(r => r.id === this.selected()));
  approvals = signal<ToolApproval[]>([]);
  pendingApprovals = computed(() => this.approvals().filter(a => a.status === 'pending'));
  proposals = signal<SkillProposal[]>([]);
  pendingSkills = computed(() => this.proposals().filter(p => p.status === 'pending'));
  recentApprovals = computed(() => this.approvals().filter(a => a.status !== 'pending').slice(0, 20));

  ngOnInit() {
    this.route.queryParamMap.subscribe(p => { if (p.get('id')) this.selected.set(p.get('id')); });
    this.reload();
  }

  reload() {
    this.api.toolApprovals().subscribe(a => this.approvals.set(a));
    this.api.skillProposals('pending').subscribe(p => this.proposals.set(p));
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
