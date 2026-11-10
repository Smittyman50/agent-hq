import { Component, OnDestroy, OnInit, computed, inject, signal } from '@angular/core';
import { DatePipe, JsonPipe } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { ActivatedRoute, RouterLink } from '@angular/router';
import { Api, EvalCase, EvalCheck, EvalResult, EvalRun, EvalSettings, ProviderInfo } from './api.service';
import { ModelPicker } from './model-picker';
import { Workspace } from './workspace';

const CHECK_LABELS: Record<EvalCheck['type'], string> = {
  rubric: 'Judge: the reply…',
  contains: 'Reply contains',
  not_contains: 'Reply doesn’t contain',
  tool_called: 'Calls the tool',
  tool_not_called: 'Doesn’t call the tool',
  skill_loaded: 'Loads the skill',
};

/** Settings → Evals: test cases, runs and the judge. */
@Component({
  selector: 'hq-evals',
  imports: [FormsModule, DatePipe, JsonPipe, RouterLink, ModelPicker],
  template: `
  <div class="memory-layout">
    <section class="mem-main">
      <div class="mem-toolbar">
        <div class="tabs grow">
          <button type="button" [class.on]="tab() === 'cases'" (click)="tab.set('cases')">Test cases
            @if (counts()['draft']) { <span class="count-badge">{{ counts()['draft'] }}</span> }</button>
          <button type="button" [class.on]="tab() === 'runs'" (click)="tab.set('runs'); loadRuns()">Runs</button>
        </div>
        <button class="ghost" (click)="newCase()">New case</button>
        <button (click)="runAll()" [disabled]="!counts()['active']">Run all ({{ counts()['active'] ?? 0 }})</button>
      </div>
      @if (notice()) { <p class="hint">{{ notice() }}</p> }

      @if (tab() === 'cases') {
        @if (drafts().length) {
          <h3>Drafts from your feedback <span class="muted small">keep the ones worth testing</span></h3>
          <div class="kn-docs">
            @for (c of drafts(); track c.id) {
              <button type="button" class="kn-doc ev-row" [class.on]="sel()?.id === c.id" (click)="open(c)">
                <span class="t">{{ c.name }}</span><span class="p">{{ ws.agentName(c.agent_id) }} · {{ sourceLabel(c.source) }}</span>
                <span class="muted small">{{ c.created_at | date:'MMM d' }}</span>
              </button>
            }
          </div>
        }
        <h3>Active</h3>
        <div class="kn-docs">
          @for (c of active(); track c.id) {
            <button type="button" class="kn-doc ev-row" [class.on]="sel()?.id === c.id" (click)="open(c)">
              <span class="t"><span class="dot" [class.ok]="c.last_passed === true" [class.bad]="c.last_passed === false"></span>{{ c.name }}</span>
              <span class="p">{{ ws.agentName(c.agent_id) }} · {{ c.checks.length }} check{{ c.checks.length === 1 ? '' : 's' }}@if (c.skill_names.length) { · {{ c.skill_names.join(', ') }} }</span>
              <span class="muted small">@if (c.last_run_at) { {{ c.last_passed ? 'passed' : 'failed' }} {{ c.last_run_at | date:'MMM d' }} } @else { not run }</span>
            </button>
          } @empty { <p class="hint">No active cases yet. Rate a reply 👎 with a note, correct an agent, or use the
            flask button under any reply (“Make this a test”); then keep the draft here.</p> }
        </div>
        @if (archived().length) {
          <details class="recent"><summary>Archived ({{ archived().length }})</summary>
            <div class="kn-docs">
              @for (c of archived(); track c.id) {
                <button type="button" class="kn-doc ev-row" (click)="open(c)"><span class="t">{{ c.name }}</span>
                  <span class="p">{{ ws.agentName(c.agent_id) }}</span><span></span></button>
              }
            </div>
          </details>
        }

        @if (sel(); as c) {
          <form class="mem-card kn-edit ev-editor" (ngSubmit)="save(c)">
            <div class="mem-meta top">
              <strong>{{ c.id ? (c.status === 'draft' ? 'Draft test case' : 'Test case') : 'New test case' }}</strong>
              @if (c.thread_id) { <a [routerLink]="['/chat', c.thread_id]">from this conversation</a> }
              <span class="grow"></span>
              <button type="button" class="ghost small" (click)="sel.set(null)">Close</button>
            </div>
            <div class="inline">
              <label>Name <input name="cname" [(ngModel)]="c.name"></label>
              <label>Agent
                <select name="cagent" [(ngModel)]="c.agent_id">
                  @for (a of ws.agents(); track a.id) { @if (!a.is_subagent) { <option [value]="a.id">{{ a.name }}</option> } }
                </select></label>
              <label>As <select name="ckind" [(ngModel)]="c.kind">
                <option value="turn">a chat message</option><option value="task">a delegated task</option></select></label>
            </div>
            <label>Request <small>what the agent is asked</small>
              <textarea name="cprompt" rows="3" [(ngModel)]="c.prompt"></textarea></label>
            @if (c.context || c.id) {
              <label>Earlier conversation <small>given to the agent as background</small>
                <textarea name="ccontext" rows="3" [(ngModel)]="c.context"></textarea></label>
            }
            <fieldset>
              <legend>Checks <small>all must pass</small></legend>
              @for (k of c.checks; track $index; let i = $index) {
                <div class="ev-check">
                  <select [name]="'ct' + i" [(ngModel)]="k.type">
                    @for (t of checkTypes; track t) { <option [value]="t">{{ checkLabel(t) }}</option> }
                  </select>
                  @if (k.type === 'rubric') {
                    <textarea [name]="'cv' + i" rows="2" [(ngModel)]="k.value" placeholder="e.g. lists failed backups first, in a table"></textarea>
                  } @else {
                    <input [name]="'cv' + i" [(ngModel)]="k.value" [placeholder]="k.type.startsWith('tool') ? 'tool name, e.g. restart_vm' : k.type === 'skill_loaded' ? 'skill name' : 'text'">
                  }
                  <button type="button" class="ghost small" (click)="c.checks.splice(i, 1)" title="Remove">✕</button>
                </div>
              }
              <button type="button" class="ghost small" (click)="c.checks.push({ type: 'rubric', value: '' })">+ Check</button>
            </fieldset>
            <fieldset>
              <legend>Recorded tool results <small>replayed instead of touching your lab</small></legend>
              @if (c.recording_partial) { <p class="warn-line">Only shortened results were left to record (the full ones expire after {{ cfg()?.keep_traces_days }} days).</p> }
              @for (st of c.recording; track $index; let i = $index) {
                <details class="kn-section">
                  <summary><code>{{ st.tool }}</code> <span class="muted small">{{ brief(st.args) }}</span>
                    @if (st.error) { <span class="error small"> error</span> }</summary>
                  <pre class="kn-text">{{ st.error ? 'Error: ' + st.error : (st.result | json) }}</pre>
                  <button type="button" class="ghost small" (click)="c.recording.splice(i, 1)">Remove this step</button>
                </details>
              } @empty { <p class="hint inline-hint">None: tool calls in this test only run if the tool is Allow and safe in
                tests (Settings → Tools); otherwise the agent is told they're not available.</p> }
            </fieldset>
            <div class="actions">
              @if (c.status === 'draft') {
                <button type="button" (click)="keep(c)">Keep</button>
                <button type="submit" class="ghost">Save draft</button>
              } @else {
                <button type="submit">{{ c.id ? 'Save' : 'Add' }}</button>
              }
              @if (c.id && c.status !== 'draft') { <button type="button" class="ghost" (click)="runOne(c)">Run now</button> }
              <span class="grow"></span>
              @if (c.id) {
                @if (c.status === 'active') { <button type="button" class="ghost" (click)="setStatus(c, 'archived')">Archive</button> }
                @if (c.status === 'archived') { <button type="button" class="ghost" (click)="setStatus(c, 'active')">Restore</button> }
                <button type="button" class="danger" (click)="remove(c)">{{ c.status === 'draft' ? 'Discard' : 'Delete' }}</button>
              }
            </div>
            @if (error()) { <p class="error">{{ error() }}</p> }
            @if (c.results?.length) {
              <h3>Recent results</h3>
              @for (r of c.results; track r.id) {
               
                <details class="kn-section">
                  <summary><span class="pill {{ r.passed ? 'done' : 'failed' }}">{{ r.passed ? 'pass' : 'fail' }}</span>
                    {{ r.variant === 'current' ? '' : r.variant + ' the change · ' }}{{ r.created_at | date:'MMM d, h:mm a' }} · {{ r.seconds }} s</summary>
                  <div class="ev-result">
                    @for (k of r.checks; track $index) {
                      <div [class.bad]="!k.passed">{{ k.passed ? '✓' : '✗' }} {{ checkLabel(k.check.type) }} {{ k.check.value }}
                        @if (k.note) { <span class="muted"> · {{ k.note }}</span> }</div>
                    }
                    @if (r.error) { <div class="error small">{{ r.error }}</div> }
                    <pre class="kn-text">{{ r.reply }}</pre>
                  </div>
                </details>
              }
            }
          </form>
        }
      } @else {
        <div class="kn-docs">
          @for (r of runs(); track r.id) {
            <button type="button" class="kn-doc ev-run" [class.on]="run()?.id === r.id" (click)="openRun(r.id)">
              <span class="t">{{ r.label || 'Run' }}</span>
              <span class="p">{{ r.trigger === 'proposal' ? 'skill change' : 'manual' }} · {{ r.created_at | date:'MMM d, h:mm a' }}</span>
              <span class="muted small">
                @if (r.status === 'done') { {{ summaryText(r) }} } @else { <span class="pill {{ r.status === 'failed' ? 'failed' : 'working' }}">{{ r.status }}</span> }
              </span>
            </button>
          } @empty { <p class="hint">No runs yet.</p> }
        </div>
        @if (run(); as r) {
          <div class="mem-card ev-detail">
            <div class="mem-meta top"><strong>{{ r.label }}</strong><span class="muted">{{ summaryText(r) }} · judge: {{ r.judge || '—' }}</span>
              <span class="grow"></span><button class="ghost small" (click)="run.set(null)">Close</button></div>
            @if (r.error) { <div class="error small">{{ r.error }}</div> }
            @for (x of r.results ?? []; track x.id) {
              <details class="kn-section" [open]="!x.passed">
                <summary><span class="pill {{ x.passed ? 'done' : 'failed' }}">{{ x.passed ? 'pass' : 'fail' }}</span>
                  {{ x.case_name }} @if (x.variant !== 'current') { <span class="muted">· {{ x.variant }} the change</span> }
                  <span class="muted small">· {{ x.seconds }} s · {{ x.model }}</span></summary>
                <div class="ev-result">
                  @for (k of x.checks; track $index) {
                    <div [class.bad]="!k.passed">{{ k.passed ? '✓' : '✗' }} {{ checkLabel(k.check.type) }} {{ k.check.value }}
                      @if (k.note) { <span class="muted"> · {{ k.note }}</span> }</div>
                  }
                  @if (x.calls.length) {
                    <div class="muted small">Tool calls: @for (c of x.calls; track $index) { <code>{{ c.tool }}</code> ({{ c.answered }})@if (!$last) {, } }</div>
                  }
                  @if (x.error) { <div class="error small">{{ x.error }}</div> }
                  <pre class="kn-text">{{ x.reply }}</pre>
                </div>
              </details>
            }
          </div>
        }
      }
    </section>

    <aside class="mem-side">
      <h2>Evals settings</h2>
      @if (cfg(); as k) {
        <p class="hint inline-hint">Rubric checks are graded by the judge. Pick a strong model that isn't grading its own
          work; switch any time.</p>
        <hq-model-picker label="Judge" [providers]="providers()" [optional]="true" noneLabel="Each agent's own model (lenient)"
          [(providerId)]="k.judge_provider_id" [(modelName)]="k.judge_model" />
        <h3>Skill changes</h3>
        <label class="check"><input type="checkbox" [(ngModel)]="k.on_proposals"> Test proposed skill changes, without and with the change</label>
        <label class="check"><input type="checkbox" [(ngModel)]="k.gate_auto"> In Auto learning mode, hold a change that
          lowers the pass rate for my review</label>
        <label>Most cases per change <input type="number" min="1" max="200" [(ngModel)]="k.max_cases"></label>
        <h3>Cases and tools</h3>
        <label class="check"><input type="checkbox" [(ngModel)]="k.draft_from_feedback"> Draft cases from 👎 notes and corrections</label>
        <label class="check"><input type="checkbox" [(ngModel)]="k.live_read_only"> Unrecorded calls to tools that are
          safe in tests (Settings → Tools) run for real</label>
        <label>Keep full tool results for <small>days, for making cases from replies</small>
          <input type="number" min="1" max="365" [(ngModel)]="k.keep_traces_days"></label>
        <div class="actions"><button (click)="saveCfg()">Save settings</button> @if (cfgNote()) { <span class="hint">{{ cfgNote() }}</span> }</div>
      }
    </aside>
  </div>`,
})
export class EvalsPage implements OnInit, OnDestroy {
  private api = inject(Api);
  private route = inject(ActivatedRoute);
  ws = inject(Workspace);

  tab = signal<'cases' | 'runs'>('cases');
  cases = signal<EvalCase[]>([]);
  counts = signal<Record<string, number>>({});
  drafts = computed(() => this.cases().filter(c => c.status === 'draft'));
  active = computed(() => this.cases().filter(c => c.status === 'active'));
  archived = computed(() => this.cases().filter(c => c.status === 'archived'));
  sel = signal<EvalCase | null>(null);
  runs = signal<EvalRun[]>([]);
  run = signal<EvalRun | null>(null);
  cfg = signal<EvalSettings | null>(null);
  providers = signal<ProviderInfo[]>([]);
  notice = signal('');
  error = signal('');
  cfgNote = signal('');
  checkTypes = Object.keys(CHECK_LABELS) as EvalCheck['type'][];
  private timer?: ReturnType<typeof setInterval>;

  ngOnInit() {
    this.api.evalSettings().subscribe(c => this.cfg.set(c));
    this.api.providers().subscribe(p => this.providers.set(p));
    const q = this.route.snapshot.queryParamMap;
    this.load(q.get('case'));
    if (q.get('run')) { this.tab.set('runs'); this.loadRuns(); this.openRun(q.get('run')!); }
    // Keep runs fresh while any is queued or running.
    this.timer = setInterval(() => {
      if (this.runs().some(r => r.status === 'queued' || r.status === 'running')) {
        this.loadRuns();
        const cur = this.run();
        if (cur) this.openRun(cur.id);
        const c = this.sel();
        if (c?.id) this.api.evalCase(c.id).subscribe(x => this.sel.update(s => s && s.id === x.id ? { ...s, results: x.results, last_passed: x.last_passed } : s));
        this.load();
      }
    }, 3000);
  }
  ngOnDestroy() { clearInterval(this.timer); }

  checkLabel(t: string) { return CHECK_LABELS[t as EvalCheck['type']] ?? t; }
  sourceLabel(s: string) { return ({ feedback: 'from your 👎', correction: 'from your correction', reply: 'from a reply', manual: 'written by you' } as Record<string, string>)[s] ?? s; }
  brief(x: unknown) { const t = typeof x === 'string' ? x : JSON.stringify(x); return t.length > 90 ? t.slice(0, 90) + '…' : t; }
  summaryText(r: EvalRun) {
    const s = r.summary ?? {};
    if (s['with']) return `with the change ${s['with'].passed}/${s['with'].total} · without ${s['without']?.passed}/${s['without']?.total}`;
    if (s['current']) return `${s['current'].passed}/${s['current'].total} passed`;
    return r.status;
  }

  load(openId?: string | null) {
    this.api.evalCases().subscribe(r => {
      this.cases.set(r.cases);
      this.counts.set(r.counts);
      if (openId) { const c = r.cases.find(x => x.id === openId); if (c) this.open(c); }
    });
  }

  open(c: EvalCase) {
    this.error.set('');
    this.api.evalCase(c.id).subscribe(full => this.sel.set({ ...full, checks: full.checks.map(k => ({ ...k })), recording: [...full.recording] }));
  }

  newCase() {
    this.tab.set('cases');
    const agent = this.ws.agents().find(a => !a.is_subagent);
    this.sel.set({ id: '', name: '', agent_id: agent?.id ?? '', kind: 'turn', prompt: '', context: '',
      checks: [{ type: 'rubric', value: '' }], recording: [], recording_partial: false, skill_names: [],
      status: 'active', source: 'manual', source_job_id: null, thread_id: null, last_passed: null,
      last_run_at: null, created_at: '', updated_at: '' });
  }

  save(c: EvalCase, then?: (saved: EvalCase) => void) {
    this.error.set('');
    const body = { ...c, checks: c.checks.filter(k => k.value.trim()) };
    const req = c.id ? this.api.saveEvalCase(body) : this.api.createEvalCase(body);
    req.subscribe({
      next: saved => { this.notice.set('Saved.'); this.load(); this.open(saved); then?.(saved); },
      error: e => this.error.set(e.error?.detail ?? 'Could not save'),
    });
  }

  keep(c: EvalCase) { this.save({ ...c, status: 'active' }); }
  setStatus(c: EvalCase, status: EvalCase['status']) { this.save({ ...c, status }); }

  remove(c: EvalCase) {
    if (!confirm(`${c.status === 'draft' ? 'Discard' : 'Delete'} “${c.name}”?`)) return;
    this.api.deleteEvalCase(c.id).subscribe(() => { this.sel.set(null); this.load(); });
  }

  runOne(c: EvalCase) {
    this.api.startEvalRun({ case_ids: [c.id], label: c.name }).subscribe({
      next: r => { this.notice.set('Running… results appear below when it finishes.'); this.runs.update(x => [r, ...x]); },
      error: e => this.error.set(e.error?.detail ?? 'Could not start'),
    });
  }

  runAll() {
    this.api.startEvalRun({ label: `All active cases (${this.counts()['active'] ?? 0})` }).subscribe({
      next: r => { this.tab.set('runs'); this.runs.update(x => [r, ...x]); this.openRun(r.id); },
      error: e => this.notice.set(e.error?.detail ?? 'Could not start'),
    });
  }

  loadRuns() { this.api.evalRuns().subscribe(r => this.runs.set(r)); }
  openRun(id: string) { this.api.evalRun(id).subscribe(r => this.run.set(r)); }

  saveCfg() {
    this.api.saveEvalSettings(this.cfg()!).subscribe({
      next: c => { this.cfg.set(c); this.cfgNote.set('Saved.'); },
      error: e => this.cfgNote.set(e.error?.detail ?? 'Save failed'),
    });
  }
}
