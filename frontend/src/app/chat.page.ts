import { Component, ElementRef, OnDestroy, OnInit, computed, effect, inject, signal, untracked, viewChild } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { DatePipe, NgTemplateOutlet } from '@angular/common';
import { DomSanitizer, SafeResourceUrl } from '@angular/platform-browser';
import { ActivatedRoute, Router, RouterLink } from '@angular/router';
import { Subscription } from 'rxjs';
import { AgentDef, Api, Attachment, DesktopState, HqEvent, JobNotes, Message, MessageFeedback, PadNote, Plan, PlanStep, Thread, attachmentUrl, threadLead, threadMembers } from './api.service';
import { FileView, Files, fileSize, isArchive, isViewable } from './files';
import { Avatar } from './avatar';
import { Icon } from './icon';
import { Markdown } from './markdown';
import { RequestCard } from './request-card';
import { ApprovalCard } from './approval-card';
import { SkillProposalCard } from './skill-proposal-card';
import { ProfileProposalCard } from './profile-proposal-card';
import { KnowledgeProposalCard } from './knowledge-proposal-card';
import { GoalCard } from './goal-card';
import { Workspace } from './workspace';

interface Activity { ts: string; text: string; kind: string; depth: number; agent?: string; }
/** A file an agent made in this chat, as listed under Outputs. */
interface Output { file: Attachment; messageId: string; who: string; at: string | null; }
/** How a reply's files show in the chat: pictures inline; text files open in Outputs; the rest download from Outputs. */
interface FileSplit { images: Attachment[]; texts: Attachment[]; others: Attachment[]; }
/** Per chat: how many outputs it had when you closed the panel. It opens again only when more arrive. */
const outputsClosedAt = new Map<string, number>();
/** Per chat: how many plans it had when you closed Next actions. It opens again when a new one starts. */
const plansClosedAt = new Map<string, number>();
const LIVE_PLAN = ['working', 'writing'];
const STEP_MARK: Record<PlanStep['status'], string> = {
  pending: '○', running: '●', done: '✓', partial: '!', failed: '✕', skipped: '–', stopped: '■' };
/** The @huddle entry in the mention list: every member at once (app/huddles.py). */
const EVERYONE = '__huddle__';

@Component({
  selector: 'hq-chat',
  imports: [FormsModule, DatePipe, NgTemplateOutlet, Avatar, Icon, Markdown, Files, FileView, RequestCard, ApprovalCard, SkillProposalCard, ProfileProposalCard, KnowledgeProposalCard, GoalCard, RouterLink],
  template: `
  <div class="chat-layout" [class.with-desktop]="desktopAgent()" [class.with-outputs]="threadId() && (showOutputs() || showPlans())"
       [class.viewing]="threadId() && showOutputs() && viewing().size > 0">
    <section class="conversation" [class.dropping]="dropping()" (dragover)="onDragOver($event)"
             (dragleave)="onDragLeave($event)" (drop)="onDrop($event)">
      @if (threadId()) {
        <header class="conv-head">
          @if (thread()?.huddle) {
            <span class="huddle-members">
              @for (id of members(); track id) {
                <button type="button" class="member" [class.lead]="id === lead()" (click)="memberMenu.set(memberMenu() === id ? null : id)"
                        [title]="agentName(id) + (id === lead() ? ' (lead: answers messages that mention nobody)' : '')">
                  <hq-avatar [agent]="agentById(id)" [size]="28" [active]="isActive(id)" />
                </button>
                @if (memberMenu() === id) {
                  <div class="backdrop" (click)="memberMenu.set(null)"></div>
                  <div class="member-menu">
                    <div class="menu-label">{{ agentName(id) }}{{ id === lead() ? ' · lead' : '' }}</div>
                    <button class="menu-item" (click)="mention(id)">Mention in my message</button>
                    @if (id !== lead()) { <button class="menu-item" (click)="makeLead(id)">Make lead</button> }
                    @if (members().length > 1) { <button class="menu-item danger" (click)="removeMember(id)">Remove from huddle</button> }
                  </div>
                }
              }
            </span>
            <strong>Huddle</strong>
            <span class="muted huddle-lead">{{ agentName(lead()) }} leads</span>
          } @else {
            <hq-avatar [agent]="agentById(thread()?.agent_id)" [size]="30" [active]="isActive(thread()?.agent_id)" />
            <strong>{{ agentName(thread()?.agent_id) }}</strong>
          }
          <span class="add-member">
            <button type="button" class="icon-btn" (click)="addMenu.set(!addMenu())"
                    [title]="thread()?.huddle ? 'Add an agent to this huddle' : 'Add an agent: this chat becomes a huddle'">
              <hq-icon name="plus" [size]="15" /></button>
            @if (addMenu()) {
              <div class="backdrop" (click)="addMenu.set(false)"></div>
              <div class="member-menu">
                <div class="menu-label">Add to {{ thread()?.huddle ? 'huddle' : 'this chat (makes it a huddle)' }}</div>
                @for (a of addable(); track a.id) {
                  <button class="menu-item" (click)="addMember(a.id!)">
                    <hq-avatar [agent]="a" [size]="22" /> <span>{{ a.name }}</span></button>
                } @empty { <p class="hint">Every agent is already here.</p> }
              </div>
            }
          </span>
          @if (memberError()) { <span class="upload-note err">{{ memberError() }}</span> }
          @if (working()) { <span class="pill live">{{ workingName() ? workingName() + ' working…' : 'working…' }}</span> }
          <span class="spacer"></span>
          <button type="button" class="ghost outputs-toggle" [class.on]="showOutputs()" (click)="toggleOutputs()"
                  [title]="showOutputs() ? 'Hide the Outputs panel' : 'Show the files made in this chat'">
            <hq-icon name="package" [size]="15" /> Outputs
            @if (outputs().length) { <span class="count">{{ outputs().length }}</span> }
          </button>
          <button type="button" class="ghost outputs-toggle" [class.on]="showPlans()" (click)="togglePlans()"
                  [title]="showPlans() ? 'Hide the Next actions panel' : 'Show the steps the agents broke your requests into'">
            <hq-icon name="board" [size]="15" /> Next actions
            @if (livePlan(); as lp) { <span class="count">{{ doneCount(lp) }}/{{ lp.steps.length }}</span> }
          </button>
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
            } @else if (m.meta['kind'] === 'goal_proposal' && m.meta['goal_id']) {
              <hq-goal-card [goalId]="$any(m.meta['goal_id'])" [version]="m.meta['status']" [showThread]="false" />
            } @else if (m.meta['kind'] === 'goal' && m.meta['goal_id'] && isGoalAsk(m)) {
              <hq-goal-card [goalId]="$any(m.meta['goal_id'])" [version]="m.meta['status']" [showThread]="false" />
            } @else if (m.meta['kind'] === 'goal') {
              <div class="note-line goal-line"><hq-icon name="goal" [size]="14" /> <span>{{ m.content }}</span>
                @if (m.meta['goal_id']) { <a routerLink="/board" [queryParams]="{ item: m.meta['goal_id'] }">On the board</a> }</div>
            } @else if (exchange(m); as x) {
              <div class="crumb" [id]="'msg-' + m.id" [class.open]="isOpen(m.id)" [style.margin-left.px]="(x.depth - 1) * 28">
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
                        @if (x.files.length) {
                          @let xf = split(x.files);
                          @if (xf.images.length) { <hq-files [files]="xf.images" /> }
                                                    @if (xf.texts.length || xf.others.length) { <ng-container *ngTemplateOutlet="toOutputs; context: { $implicit: xf }" /> }
                        }
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
                    @if (x.jobId && padOf(x.jobId); as pad) {
                      @if (pad.notes.length || !x.finished) {
                        <details class="pad" [open]="!x.finished">
                          <summary>
                            <hq-icon name="notes" [size]="13" /> Scratch pad
                            <span class="muted">· {{ pad.notes.length }} note{{ pad.notes.length === 1 ? '' : 's' }}
                              @if (pad.expiresIn && x.finished) { · kept {{ hoursLeft(pad.expiresIn) }} more }</span>
                          </summary>
                          @for (n of pad.notes; track n.n) {
                            <div class="pad-note"><span class="pad-n">{{ n.n }}</span><hq-md [text]="n.text" /></div>
                          } @empty { <div class="pad-note muted">No notes yet.</div> }
                          @if (pad.notes.length && x.finished) {
                            <div class="pad-actions">
                              @if (pad.kept || keptFile(x.files, x.jobId)) { <span class="muted">Saved as a file on this card.</span> }
                              @else { <button type="button" class="mini" (click)="keepPad(x.jobId)" [disabled]="pad.saving">Keep as file</button> }
                              @if (pad.error) { <span class="upload-note err">{{ pad.error }}</span> }
                            </div>
                          }
                        </details>
                      }
                    } @else if (x.jobId && x.finished && padGone(x.jobId)) {
                      <div class="crumb-note muted pad-gone">Scratch-pad notes have expired.</div>
                    }
                  </div>
                }
              </div>
            } @else {
            <div class="msg {{ m.role }}" [id]="'msg-' + m.id">
              @if (m.role === 'agent') {
                <div class="who"><hq-avatar [agent]="agentById(m.agent_id)" [size]="24" [active]="isActive(m.agent_id)" /> {{ agentName(m.agent_id) }}
                  @if (m.meta?.['kind'] === 'huddle_finding') { <span class="muted finding-for">· findings for {{ m.meta?.['requester'] ?? 'the huddle' }}</span> }</div>
              } @else if (m.role === 'event') { <div class="who">activity</div> }
              @if (m.role === 'agent') {
                <div class="body rich"><hq-md [text]="m.content" /></div>
                @if (filesOf(m).length) {
                  @let mf = split(filesOf(m));
                  @if (mf.images.length) { <hq-files [files]="mf.images" /> }
                                    @if (mf.texts.length || mf.others.length) { <ng-container *ngTemplateOutlet="toOutputs; context: { $implicit: mf }" /> }
                }
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
                    <button type="button" class="rate-btn" title="Make this a goal (the Board)" (click)="makeGoal(m)">
                      <hq-icon name="goal" [size]="14" /></button>
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
              } @else if (lessonsOf(m).length) {
                <div class="body lessons">
                  <div>{{ agentName(m.agent_id) }} noted for next time:</div>
                  @for (l of lessonsOf(m); track l.id) {
                    <div class="lesson" [class.retired]="l.rating === 'down'">
                      <span class="lesson-text">{{ l.text }}</span>
                      <span class="rate" [class.rated]="!!l.rating">
                        <button type="button" class="rate-btn" [class.on]="l.rating === 'up'"
                          [title]="l.rating === 'up' ? 'Clear your rating' : 'Good lesson: keep it'"
                          (click)="rateLesson(m, l.id, 'up')"><hq-icon name="thumbs-up" [size]="13" /></button>
                        <button type="button" class="rate-btn down" [class.on]="l.rating === 'down'"
                          [title]="l.rating === 'down' ? 'Bring this lesson back' : 'Wrong lesson: forget it'"
                          (click)="rateLesson(m, l.id, 'down')"><hq-icon name="thumbs-down" [size]="13" /></button>
                      </span>
                    </div>
                  }
                </div>
              } @else {
                @if (m.content) { <div class="body">{{ m.content }}</div> }
                @if (filesOf(m).length) {
                  @if (m.role === 'user') { <hq-files [files]="filesOf(m)" /> }
                  @else {
                    @let ef = split(filesOf(m));
                    @if (ef.images.length) { <hq-files [files]="ef.images" /> }
                                        @if (ef.texts.length || ef.others.length) { <ng-container *ngTemplateOutlet="toOutputs; context: { $implicit: ef }" /> }
                  }
                }
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
          @if (mentionOptions().length) {
            <div class="mention-menu" role="listbox">
              @for (a of mentionOptions(); track a.id; let i = $index) {
                <button type="button" class="menu-item" role="option" [class.on]="i === mentionIndex()"
                        (mousedown)="$event.preventDefault(); pickMention(a)">
                  @if (a.id === EVERYONE) {
                    <span class="huddle-stack small">@for (id of members().slice(0, 3); track id) { <hq-avatar [agent]="agentById(id)" [size]="18" /> }</span>
                    <span>huddle</span> <span class="muted">everyone answers</span>
                  } @else {
                    <hq-avatar [agent]="a" [size]="20" /> <span>{{ a.name }}</span>
                    @if (!isMember(a.id)) { <span class="muted">joins the huddle</span> }
                  }
                </button>
              }
            </div>
          }
          <textarea #draftBox name="draft" [(ngModel)]="draft" [placeholder]="placeholder()"
            (keydown)="onDraftKey($event)" (input)="onDraftInput()" (click)="onDraftInput()" (blur)="mentionAt.set(null)"
            (paste)="onPaste($event)"></textarea>
          @if (working()) {
            <button type="button" class="danger stop" (click)="stop()" [disabled]="stopping()"
                    title="Stop this turn and everything it delegated">{{ stopping() ? 'Stopping…' : 'Stop' }}</button>
          }
          <button type="submit" [disabled]="(!draft.trim() && !pending().length) || !!uploading()">Send</button>
        </form>
        @if (activity().length) {
          <details class="activity" open>
            <summary>Live activity ({{ activity().length }})</summary>
            @for (a of activityNewestFirst(); track $index) {
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

    @if (threadId() && (showOutputs() || showPlans())) {
     <aside class="side-panels" [class.both]="showOutputs() && showPlans()">
     @if (showOutputs()) {
      <section class="outputs" aria-label="Outputs">
        <header class="conv-head">
          <hq-icon name="package" [size]="16" class="muted" /><strong>Outputs</strong>
          @if (outputs().length) { <span class="pill">{{ outputs().length }}</span> }
          <span class="spacer"></span>
          @if (viewing().size) {
            <button type="button" class="ghost small" title="Close every open file and narrow the panel" (click)="closeViews()">Collapse all</button>
          }
          <button type="button" class="icon-btn" title="Hide Outputs" (click)="toggleOutputs()"><hq-icon name="x" [size]="14" /></button>
        </header>
        <div class="outputs-list">
          @for (o of outputs(); track o.file.id) {
            <div class="output" [id]="'out-' + o.file.id" [class.flash]="flashed().has(o.file.id)" [class.open]="viewing().has(o.file.id)">
              @if (viewable(o.file)) {
                <button type="button" class="output-main" (click)="toggleView(o.file.id)" [attr.aria-expanded]="viewing().has(o.file.id)"
                        [title]="(viewing().has(o.file.id) ? 'Collapse ' : 'View ') + o.file.name">
                  <span class="output-icon"><hq-icon name="file" [size]="16" /></span>
                  <span class="output-text">
                    <span class="file-name">{{ o.file.name }}</span>
                    <span class="sub">{{ size(o.file.size) }} · {{ o.who }}@if (o.at) { · {{ o.at | date:'MMM d, h:mm a' }} }</span>
                  </span>
                  <hq-icon name="chevron" [size]="14" class="muted output-chev" />
                </button>
                @if (!viewing().has(o.file.id)) {
                  <a class="icon-btn" [href]="fileUrl(o.file.id, true)" [title]="'Download ' + o.file.name">
                    <hq-icon name="download" [size]="14" /></a>
                }
              } @else {
                <a class="output-main" [href]="fileUrl(o.file.id, true)" [title]="'Download ' + o.file.name">
                  @if (o.file.kind === 'image') {
                    <img class="output-thumb" [src]="fileUrl(o.file.id)" [alt]="o.file.name" loading="lazy">
                  } @else {
                    <span class="output-icon"><hq-icon [name]="archive(o.file) ? 'package' : 'file'" [size]="16" /></span>
                  }
                  <span class="output-text">
                    <span class="file-name">{{ o.file.name }}</span>
                    <span class="sub">{{ size(o.file.size) }} · {{ o.who }}@if (o.at) { · {{ o.at | date:'MMM d, h:mm a' }} }</span>
                  </span>
                  <hq-icon name="download" [size]="14" class="muted output-dl" />
                </a>
              }
              <button type="button" class="icon-btn" title="Show the reply that made it" (click)="jumpTo(o.messageId)">
                <hq-icon name="locate" [size]="14" /></button>
              @if (viewing().has(o.file.id)) { <hq-file-view [file]="o.file" /> }
            </div>
          } @empty {
            <p class="hint">Files the agents make in this chat collect here, newest first.</p>
          }
        </div>
      </section>
     }
     @if (showPlans()) {
      <section class="plans" aria-label="Next actions">
        <header class="conv-head">
          <hq-icon name="board" [size]="16" class="muted" /><strong>Next actions</strong>
          @if (livePlan(); as lp) { <span class="pill working">{{ doneCount(lp) }}/{{ lp.steps.length }}</span> }
          <span class="spacer"></span>
          <button type="button" class="icon-btn" title="Hide Next actions" (click)="togglePlans()"><hq-icon name="x" [size]="14" /></button>
        </header>
        <div class="plans-list">
          @for (p of plansShown(); track p.job_id; let first = $first) {
            <details class="plan" [open]="first || isLive(p)" [style.margin-left.px]="p.depth * 14">
              <summary>
                <span class="plan-who">{{ p.agent }}</span>
                @if (p.kind === 'task') { <span class="muted plan-for">delegated</span> }
                <span class="pill {{ planPill(p) }}">{{ planLabel(p) }}</span>
                <span class="muted plan-count">{{ doneCount(p) }}/{{ p.steps.length }}@if (p.revisions) { · replanned {{ p.revisions }}× }</span>
              </summary>
              @if (p.brief) { <div class="plan-brief muted">{{ p.brief }}</div> }
              <ol class="plan-steps">
                @for (st of p.steps; track st.n) {
                  <li class="plan-step {{ st.status }}" [title]="st.details || st.title">
                    <span class="step-mark">{{ mark(st.status) }}</span>
                    <span class="step-text"><span class="step-title">{{ st.title }}</span>
                      @if (st.summary && st.status !== 'running') { <span class="step-sum">{{ st.summary }}</span> }</span>
                  </li>
                }
              </ol>
              @if (p.stopped_because) { <div class="plan-brief warn">Stopped early: {{ p.stopped_because }}</div> }
              @if (p.status === 'writing') { <div class="plan-brief muted">Writing the reply from the results…</div> }
            </details>
          } @empty {
            <p class="hint">When an agent breaks a request into next actions, the steps show here as it works through them.</p>
          }
        </div>
      </section>
     }
     </aside>
    }

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
  </div>

  <ng-template #toOutputs let-fs>
    <div class="to-outputs-row">
      @for (f of fs.texts; track f.id) {
        <button type="button" class="to-outputs" (click)="reveal([f], true)" [title]="'View ' + f.name + ' in Outputs'">
          <hq-icon name="file" [size]="14" />
          <span class="file-name">{{ f.name }}</span>
          <span class="muted">view →</span>
        </button>
      }
      @if (fs.others.length) {
        <button type="button" class="to-outputs" (click)="reveal(fs.others)" [title]="'Find in Outputs: ' + names(fs.others)">
          <hq-icon name="package" [size]="14" />
          <span class="file-name">{{ fs.others.length === 1 ? fs.others[0].name : fs.others.length + ' files' }}</span>
          <span class="muted">in Outputs →</span>
        </button>
      }
    </div>
  </ng-template>`,
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
  /** The live activity log for display: newest step at the top. */
  activityNewestFirst = computed(() => [...this.activity()].reverse());
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
  /** The lessons in an agent's "noted for next time" message, each with your rating. */
  lessonsOf(m: Message): { id: string; text: string; rating: 'up' | 'down' | null }[] {
    if (m.meta?.['kind'] !== 'lesson') return [];
    const ids = (m.meta?.['memory_ids'] as string[] | undefined) ?? [];
    // Older messages have no lessons list: take the "- " lines of the text.
    const texts = (m.meta?.['lessons'] as string[] | undefined)
      ?? m.content.split('\n').filter(x => x.startsWith('- ')).map(x => x.slice(2));
    if (!ids.length || ids.length !== texts.length) return [];
    const ratings = (m.meta?.['lesson_ratings'] as Record<string, 'up' | 'down'> | undefined) ?? {};
    return ids.map((id, i) => ({ id, text: texts[i], rating: ratings[id] ?? null }));
  }
  /** 👍 keeps a lesson (confirms the memory); 👎 retires the memory; clicking again clears it. */
  rateLesson(m: Message, memoryId: string, rating: 'up' | 'down') {
    const ratings = { ...((m.meta?.['lesson_ratings'] as Record<string, string> | undefined) ?? {}) };
    const next = ratings[memoryId] === rating ? null : rating;
    if (next) ratings[memoryId] = next; else delete ratings[memoryId];
    this.messages.update(ms => ms.map(x => x.id === m.id
      ? { ...x, meta: { ...(x.meta ?? {}), lesson_ratings: ratings } } : x));
    this.api.rateLesson(m.id, memoryId, next).subscribe({
      error: e => { alert(e.error?.detail ?? 'Could not rate this lesson'); this.reload(); },
    });
  }
  makeTest(m: Message) {
    this.api.caseFromMessage(m.id).subscribe({
      next: c => this.router.navigate(['/settings/evals'], { queryParams: { case: c.id } }),
      error: e => alert(e.error?.detail ?? 'Could not make a test from this reply'),
    });
  }
  /** Start a standing goal from a reply: the Board opens a new goal with its text. */
  makeGoal(m: Message) {
    this.router.navigate(['/board'], {
      queryParams: { new: 1, agent: m.agent_id, from_thread: this.threadId(), from_message: m.id },
      state: { text: m.content },
    });
  }
  /** Goal notices that ask something of you (a done claim, a block, a question) show as a card. */
  isGoalAsk(m: Message) { return ['review', 'blocked', 'question'].includes(m.meta?.['status'] as string); }
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
      jobId: (meta['job_id'] ?? null) as string | null,
    };
  }

  // ---- scratch pads: notes a delegate keeps while it works (app/scratchpad.py)
  private pads = signal<Record<string, { notes: PadNote[]; expiresIn: number | null; kept?: boolean; saving?: boolean; error?: string }>>({});
  private padsGone = signal<Set<string>>(new Set());
  private padsAsked = new Set<string>();

  /** A job's pad, fetched the first time its card is drawn open; null if none (yet). */
  padOf(jobId: string) {
    if (!this.padsAsked.has(jobId)) {
      this.padsAsked.add(jobId);
      this.api.jobNotes(jobId).subscribe({
        next: (r: JobNotes) => {
          if (r.gone) { this.padsGone.update(s => new Set(s).add(jobId)); return; }
          this.pads.update(p => {
            const live = p[jobId]?.notes ?? [];
            const seen = new Set(r.notes.map(n => n.n));
            return { ...p, [jobId]: { ...(p[jobId] ?? {}), notes: [...r.notes, ...live.filter(n => !seen.has(n.n))].sort((a, b) => a.n - b.n), expiresIn: r.expires_in } };
          });
        },
        error: () => {},
      });
    }
    return this.pads()[jobId] ?? null;
  }
  keptFile(files: Attachment[], jobId: string) { return files.some(f => f.name.endsWith(`-notes-${jobId}.md`)); }
  padGone(jobId: string) { return this.padsGone().has(jobId); }
  hoursLeft(s: number) { const h = Math.round(s / 3600); return h >= 1 ? `${h} h` : `${Math.max(1, Math.round(s / 60))} min`; }

  private addNote(jobId: string, n: PadNote) {
    this.pads.update(p => {
      const cur = p[jobId] ?? { notes: [], expiresIn: null };
      if (cur.notes.some(x => x.n === n.n)) return p;
      return { ...p, [jobId]: { ...cur, notes: [...cur.notes, n].sort((a, b) => a.n - b.n) } };
    });
  }

  keepPad(jobId: string) {
    const set = (patch: object) => this.pads.update(p => ({ ...p, [jobId]: { ...p[jobId], ...patch } }));
    set({ saving: true, error: '' });
    this.api.keepNotes(jobId).subscribe({
      next: () => set({ saving: false, kept: true }),
      error: e => set({ saving: false, error: e.error?.detail ?? 'Could not save the notes' }),
    });
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
    this.viewing.set(new Set());
    this.showOutputs.set(false);
    this.showPlans.set(false);
    this.plans.set({});
    this.jobParents.clear();
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
      if (this.threadId() !== id) return;
      for (const j of jobs) this.jobParents.set(j.id, j.parent_job_id ?? null);
      if (jobs.some(j => j.status === 'queued' || j.status === 'running')) this.busy.set(true);
    });
    this.api.threadPlans(id).subscribe(ps => {
      if (this.threadId() !== id) return;
      for (const p of ps) this.gotPlan(p, false);
      const shut = plansClosedAt.get(id);
      if (ps.some(p => this.isLive(p)) && (shut === undefined || ps.length > shut)) this.showPlans.set(true);
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
    if (this.busy() && (this.workingAgents().size ? this.workingAgents().has(id!) : this.thread()?.agent_id === id)) return true;
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
    // Replayed events (sent when the conversation opens) only fill in the activity log
    // and the delegation cards' steps; they don't re-run side effects.
    const replay = !!ev['replay'];
    if (ev.type !== 'message' && !replay) this.markActive(ev);
    switch (ev.type) {
      case 'message':
        if (replay) break;
        if (!this.messages().some(m => m.id === ev['message'].id)) {
          this.messages.update(ms => [...ms, ev['message']]);
          if (ev['message'].role !== 'event') this.ws.touchThread(ev['message'].thread_id, ev['message'].created_at);
          this.scroll();
        }
        break;
      case 'thread_updated':  // the chat got its title
        if (!replay) this.ws.upsertThread(ev['thread']);
        break;
      case 'message_updated':  // e.g. a delegation thread going from working to done
        if (!replay) this.messages.update(ms => ms.map(m => m.id === ev['message'].id ? ev['message'] : m));
        break;
      case 'status':
        if (!replay) {
          // Several huddle members can work at once: busy until the last one is done.
          const who = ev['agent_id'] as string | undefined;
          this.workingAgents.update(set => {
            const next = new Set(set);
            if (ev['state'] === 'thinking') { if (who) next.add(who); }
            else if (who) next.delete(who);
            else next.clear();  // stopped: nobody is working
            return next;
          });
          this.busy.set(ev['state'] === 'thinking' || this.workingAgents().size > 0);
        }
        break;
      case 'huddle':  // someone joined, left, or now leads
        if (!replay) {
          const t = this.thread();
          if (t) this.ws.upsertThread({ ...t, members: ev['members'], lead_agent_id: ev['lead'], huddle: true });
        }
        break;
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
      case 'skill_proposed': this.log(ev, `${ev['agent']} proposed a skill: ${ev['skill']}`, 'queue'); if (!replay) this.ws.refreshPending(); break;
      case 'approval_requested': this.log(ev, `${ev['agent']} is waiting for your OK to run ${ev['tool']} on ${ev['server']}`, 'queue'); if (!replay) this.ws.refreshPending(); break;
      case 'subagent_request': this.log(ev, `${ev['agent']} asked you for sub-agents`, 'queue'); if (!replay) this.ws.refreshPending(); break;
      case 'memory_recall': this.log(ev, `${ev['agent']} recalled ${ev['count']} memor${ev['count'] === 1 ? 'y' : 'ies'}`, 'queue'); break;
      case 'model_fallback': this.log(ev, `${ev['agent']} → fallback model ${ev['to_model']}: ${ev['reason']}`, 'err'); break;
      case 'note':
        // The tool call itself is already in the activity log; this fills the card's pad.
        if (ev['job_id']) this.addNote(String(ev['job_id']), { n: Number(ev['n']), text: String(ev['text'] ?? '') });
        break;
      case 'planning': this.log(ev, `${ev['agent']} is deciding whether to break this into next actions`, 'queue'); break;
      case 'plan': {
        const p = ev['plan'] as Plan;
        if (!p || p.thread_id !== this.threadId()) break;
        if (!this.plans()[p.job_id]) this.log(ev, `${p.agent} broke it into ${p.steps.length} next actions`, 'deleg');
        const before = this.plans()[p.job_id];
        for (const st of p.steps) {
          const was = before?.steps.find(x => x.n === st.n)?.status;
          if (st.status !== was && st.status === 'running') this.log(ev, `${p.agent} → step ${st.n}: ${st.title}`, 'call');
          else if (st.status !== was && ['done', 'partial', 'failed'].includes(st.status))
            this.log(ev, `${p.agent} ← step ${st.n} [${st.status}]${st.status === 'done' ? '' : ': ' + st.summary.slice(0, 200)}`, st.status === 'done' ? 'result' : 'err');
        }
        this.gotPlan(p, !replay);
        break;
      }
      case 'plan_revised': this.log(ev, `${ev['agent']} replanned: now ${ev['steps']} next actions`, 'queue'); break;
      case 'delegation_started':
        if (ev['job_id']) this.jobParents.set(String(ev['job_id']), (ev['parent_job_id'] as string) ?? null);
        this.log(ev, `${ev['from_agent'] ?? 'you'} → ${ev['to_agent']} on ${ev['worker']}${ev['background'] ? ' (background)' : ''}: ${ev['task']}`, 'deleg');
        break;
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

  // ---- huddles: several agents in one chat; @mention addresses one (app/huddles.py)
  private draftBox = viewChild<ElementRef<HTMLTextAreaElement>>('draftBox');
  members = computed(() => threadMembers(this.thread()));
  readonly EVERYONE = EVERYONE;
  lead = computed(() => threadLead(this.thread()));
  isMember(id: string | null | undefined) { return !!id && this.members().includes(id); }
  memberMenu = signal<string | null>(null);
  addMenu = signal(false);
  memberError = signal('');
  /** The agent whose turn is running, from the live "thinking" status. */
  workingAgents = signal<Set<string>>(new Set());
  workingName = computed(() => {
    const ids = [...this.workingAgents()];
    if (!this.thread()?.huddle || !ids.length) return '';
    const names = ids.map(id => this.agentName(id));
    return names.length === 1 ? `${names[0]} is` : `${names.slice(0, -1).join(', ')} and ${names.at(-1)} are`;
  });
  addable = computed(() => this.ws.enabledAgents().filter(a => !this.isMember(a.id)));
  placeholder = computed(() => this.thread()?.huddle
    ? `Message the huddle… @name for one agent, @huddle for everyone; otherwise ${this.agentName(this.lead())} answers`
    : 'Message…  (Enter to send, Shift+Enter for a new line, @name brings in another agent)');

  private applyThread(t: Thread | undefined | null) {
    if (t?.id) this.ws.upsertThread(t);
  }

  addMember(agentId: string) {
    const id = this.threadId(); if (!id) return;
    this.addMenu.set(false);
    this.api.addMember(id, agentId).subscribe({
      next: t => { this.memberError.set(''); this.applyThread(t); },
      error: e => this.memberError.set(e.error?.detail ?? 'Could not add the agent'),
    });
  }

  removeMember(agentId: string) {
    const id = this.threadId(); if (!id) return;
    this.memberMenu.set(null);
    this.api.removeMember(id, agentId).subscribe({
      next: t => { this.memberError.set(''); this.applyThread(t); },
      error: e => this.memberError.set(e.error?.detail ?? 'Could not remove the agent'),
    });
  }

  makeLead(agentId: string) {
    const id = this.threadId(); if (!id) return;
    this.memberMenu.set(null);
    this.api.setLead(id, agentId).subscribe({
      next: t => { this.memberError.set(''); this.applyThread(t); },
      error: e => this.memberError.set(e.error?.detail ?? 'Could not change the lead'),
    });
  }

  /** Put "@Name " in the message box. */
  mention(agentId: string) {
    this.memberMenu.set(null);
    const name = this.agentName(agentId);
    const sep = this.draft && !/\s$/.test(this.draft) ? ' ' : '';
    this.draft = `${this.draft}${sep}@${name} `;
    setTimeout(() => this.draftBox()?.nativeElement.focus());
  }

  /** Where the "@…" being typed starts, and what's typed after it (null: not typing one). */
  mentionAt = signal<{ start: number; query: string } | null>(null);
  mentionIndex = signal(0);
  mentionOptions = computed<AgentDef[]>(() => {
    const at = this.mentionAt();
    if (!at) return [];
    const q = at.query.toLowerCase().replace(/[\s_\-.]+/g, '');
    const norm = (n: string) => n.toLowerCase().replace(/[\s_\-.]+/g, '');
    const hits = this.ws.enabledAgents().filter(a => norm(a.name).startsWith(q));
    // Members first, then the rest, each alphabetically.
    const agents = hits.sort((a, b) => Number(this.isMember(b.id)) - Number(this.isMember(a.id)) || a.name.localeCompare(b.name)).slice(0, 8);
    // @huddle: every member at once (only in a huddle, and only if no agent is called that).
    const everyone = this.thread()?.huddle && 'huddle'.startsWith(q) && !this.ws.enabledAgents().some(a => norm(a.name) === 'huddle');
    return everyone ? [{ id: EVERYONE, name: 'huddle' } as AgentDef, ...agents] : agents;
  });

  onDraftInput() {
    const box = this.draftBox()?.nativeElement;
    if (!box) return;
    const before = box.value.slice(0, box.selectionStart ?? box.value.length);
    // An @ at the start or after a space, then up to two words of a name.
    const m = /(?:^|[\s(])@([\w.\-]*(?: [\w.\-]*)?)$/.exec(before);
    if (!m) { this.mentionAt.set(null); return; }
    const prev = this.mentionAt();
    this.mentionAt.set({ start: before.length - m[1].length - 1, query: m[1] });
    if (!prev || prev.query !== m[1]) this.mentionIndex.set(0);
  }

  onDraftKey(e: KeyboardEvent) {
    const opts = this.mentionOptions();
    if (opts.length) {
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault();
        const n = opts.length;
        this.mentionIndex.update(i => (i + (e.key === 'ArrowDown' ? 1 : n - 1)) % n);
        return;
      }
      if ((e.key === 'Enter' && !e.shiftKey) || e.key === 'Tab') {
        e.preventDefault();
        this.pickMention(opts[Math.min(this.mentionIndex(), opts.length - 1)]);
        return;
      }
      if (e.key === 'Escape') { e.preventDefault(); this.mentionAt.set(null); return; }
    }
    if (e.key === 'Enter') this.onEnter(e);
  }

  pickMention(a: AgentDef) {
    const at = this.mentionAt();
    const box = this.draftBox()?.nativeElement;
    if (!at || !box) return;
    const end = box.selectionStart ?? this.draft.length;
    const insert = `@${a.name} `;
    this.draft = this.draft.slice(0, at.start) + insert + this.draft.slice(end).replace(/^ /, '');
    this.mentionAt.set(null);
    const caret = at.start + insert.length;
    setTimeout(() => { box.value = this.draft; box.focus(); box.setSelectionRange(caret, caret); });
  }

  send() {
    const id = this.threadId(); const text = this.draft.trim();
    const files = this.pending();
    if (!id || (!text && !files.length) || this.uploading()) return;
    this.draft = '';
    this.pending.set([]);
    this.uploadError.set('');
    this.busy.set(true);
    this.mentionAt.set(null);
    this.api.send(id, text, files.map(f => f.id)).subscribe({
      next: r => this.applyThread(r?.thread),  // a mention may have made it a huddle
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

  // ---- Next actions: the steps a request was broken into (app/nextactions.py)
  plans = signal<Record<string, Plan>>({});
  /** Closed by default; opens when a plan starts in this chat, unless you closed it since. */
  showPlans = signal(false);
  isLive(p: Plan) { return LIVE_PLAN.includes(p.status); }
  mark(status: PlanStep['status']) { return STEP_MARK[status] ?? '○'; }
  doneCount(p: Plan) { return p.steps.filter(s => ['done', 'partial', 'failed', 'skipped', 'stopped'].includes(s.status)).length; }
  planPill(p: Plan) { return this.isLive(p) ? 'working' : p.status; }
  planLabel(p: Plan) { return p.status === 'writing' ? 'replying' : p.status; }
  /** The running plan of the conversation's own turn, else the newest running one. */
  livePlan = computed(() => {
    const live = Object.values(this.plans()).filter(p => this.isLive(p));
    return live.find(p => p.depth === 0) ?? live.at(-1) ?? null;
  });
  /** Newest request first; a delegate's plan right under the plan of the job that delegated it. */
  plansShown = computed(() => {
    const all = Object.values(this.plans()).sort((a, b) => a.created_at.localeCompare(b.created_at));
    const ids = new Set(all.map(p => p.job_id));
    const kids = new Map<string, Plan[]>();
    const nearest = (p: Plan) => {  // nearest ancestor with a plan, through jobs that have none
      let parent = p.parent_job_id;
      const seen = new Set<string>();
      while (parent && !ids.has(parent) && !seen.has(parent)) { seen.add(parent); parent = this.jobParents.get(parent) ?? null; }
      return parent && ids.has(parent) ? parent : null;
    };
    const roots: Plan[] = [];
    for (const p of all) {
      const parent = nearest(p);
      if (parent) kids.set(parent, [...(kids.get(parent) ?? []), p]); else roots.push(p);
    }
    const out: Plan[] = [];
    const walk = (p: Plan) => { out.push(p); for (const k of kids.get(p.job_id) ?? []) walk(k); };
    for (const r of roots.reverse()) walk(r);
    return out;
  });
  /** Parent job of each job seen in this chat, to nest plans of deeper delegations. */
  private jobParents = new Map<string, string | null>();

  togglePlans() {
    const on = !this.showPlans();
    this.showPlans.set(on);
    const id = this.threadId();
    if (!id) return;
    if (on) plansClosedAt.delete(id); else plansClosedAt.set(id, Object.keys(this.plans()).length);
  }

  private gotPlan(p: Plan, live: boolean) {
    const isNew = !this.plans()[p.job_id];
    this.plans.update(all => ({ ...all, [p.job_id]: p }));
    const id = this.threadId();
    if (!id || !isNew || !live || !this.isLive(p) || this.showPlans()) return;
    const shut = plansClosedAt.get(id);
    if (shut === undefined || Object.keys(this.plans()).length > shut) this.showPlans.set(true);
  }

  // ---- Outputs: every file the agents made in this chat, in a panel beside it
  fileUrl = attachmentUrl;
  size = fileSize;
  archive = isArchive;
  /** Closed by default; opens by itself when a chat has files you haven't dismissed, and stays shut once you close it. */
  showOutputs = signal(false);
  private autoOpenOutputs = effect(() => {
    const id = this.threadId();
    const n = this.outputs().length;
    if (!id || !n || untracked(this.showOutputs)) return;
    const shut = outputsClosedAt.get(id);
    if (shut === undefined || n > shut) this.showOutputs.set(true);
  });
  flashed = signal<Set<string>>(new Set());
  /** Text files open in the Outputs panel (the panel widens while any is open). */
  viewing = signal<Set<string>>(new Set());
  viewable = isViewable;

  outputs = computed<Output[]>(() => {
    const seen = new Set<string>();
    const out: Output[] = [];
    for (const m of this.messages()) {
      if (m.role === 'user') continue;
      const x = this.exchange(m);
      for (const f of x ? x.files : this.filesOf(m)) {
        if (f.source === 'user' || seen.has(f.id)) continue;
        seen.add(f.id);
        const who = x ? x.assignee : this.agentName(f.agent_id ?? m.agent_id);
        out.push({ file: f, messageId: m.id, who, at: f.created_at ?? m.created_at });
      }
    }
    return out.reverse();  // newest first
  });

  /** Pictures show in the chat; text files and everything else are found under Outputs. */
  split(files: Attachment[]): FileSplit {
    const s: FileSplit = { images: [], texts: [], others: [] };
    for (const f of files) {
      if (f.kind === 'image') s.images.push(f);
      else if (isViewable(f)) s.texts.push(f);
      else s.others.push(f);
    }
    return s;
  }

  names(files: Attachment[]) { return files.map(f => f.name).join(', '); }

  toggleOutputs() {
    const on = !this.showOutputs();
    this.showOutputs.set(on);
    const id = this.threadId();
    if (!id) return;
    if (on) outputsClosedAt.delete(id); else outputsClosedAt.set(id, this.outputs().length);
  }

  closeViews() { this.viewing.set(new Set()); }

  /** Open or close a text file's viewer in Outputs. */
  toggleView(id: string) {
    const v = new Set(this.viewing());
    if (v.has(id)) v.delete(id); else v.add(id);
    this.viewing.set(v);
  }

  /** Open Outputs and point at these files (and open them, for text files). */
  reveal(files: Attachment[], open = false) {
    if (!this.showOutputs()) this.toggleOutputs();
    if (open) this.viewing.set(new Set([...this.viewing(), ...files.map(f => f.id)]));
    this.flashed.set(new Set(files.map(f => f.id)));
    setTimeout(() => document.getElementById('out-' + files[0]?.id)?.scrollIntoView({ block: 'nearest', behavior: 'smooth' }));
    setTimeout(() => this.flashed.set(new Set()), 1800);
  }

  /** Scroll the chat to the reply that made a file (opening a delegation thread if it's in one). */
  jumpTo(messageId: string) {
    const m = this.messages().find(x => x.id === messageId);
    if (m && this.exchange(m) && !this.isOpen(m.id)) this.toggle(m.id);
    setTimeout(() => {
      const el = document.getElementById('msg-' + messageId);
      if (!el) return;
      el.scrollIntoView({ block: 'center', behavior: 'smooth' });
      el.classList.remove('flash'); void el.offsetWidth; el.classList.add('flash');
      setTimeout(() => el.classList.remove('flash'), 1800);
    });
  }

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
