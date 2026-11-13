import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable } from 'rxjs';

export interface AgentDef {
  id?: string;
  name: string;
  description: string;
  pronouns?: string;       // how teammates refer to this agent, e.g. he/him
  instructions: string;
  provider_id: string | null;
  model: string;
  vision_provider_id: string | null;
  vision_model: string;
  vision_coords: 'relative_1000' | 'pixels';
  vision_zoom: boolean;
  sees_images?: boolean;   // image attachments go to its own (multimodal) model
  fallback_provider_id: string | null;
  fallback_model: string;
  thinking?: ThinkingLevel; // how much the model reasons; '' = the model's default
  pool: string;
  tool_server_ids: string[];
  delegates: string[];
  desktop_url: string | null;
  is_default: boolean;
  enabled: boolean;
  memory_enabled: boolean;
  avatar_version?: number;
  avatar_animated?: boolean;
  subagent_mode: SubagentMode;
  subagent_limits: SubagentLimits;
  learning_mode: LearningMode;
  use_profile?: boolean;
  context_window?: number | null;
  is_subagent?: boolean;
}

export type LearningMode = 'off' | 'propose' | 'auto';
export interface SkillStats { uses: number; done: number; partial: number; failed: number; pending: number;
  up?: number; down?: number; last_used_at: string | null; }
export interface SkillVersion { id: string; version: number; title: string; when_to_use: string; body: string;
  tools: string[]; script: string | null; created_by: string; note: string; created_at: string; }
export interface Skill {
  id?: string; name: string; title: string; when_to_use: string; body: string; tools: string[];
  script: string | null; scope: 'team' | 'agent'; agent_id: string | null; agent?: string;
  status?: 'active' | 'retired'; version?: number; created_by?: string; created_at?: string; updated_at?: string;
  stats?: SkillStats; success_rate?: number | null; versions?: SkillVersion[]; note?: string;
}
export interface SkillProposal {
  id: string; skill_id: string | null; name: string; title: string; when_to_use: string; body: string;
  tools: string[]; script: string | null; scope: 'team' | 'agent'; reason: string; agent_id: string;
  agent_name: string; thread_id: string | null; base_version: number | null;
  status: 'pending' | 'approved' | 'rejected' | 'applied' | 'testing'; note: string; created_at: string; decided_at: string | null;
  source?: string; eval_run_id?: string | null; eval_summary?: EvalSummary;
  current?: { version: number; title: string; when_to_use: string; body: string }; diff?: string; stale?: boolean;
}

export type SubagentMode = 'off' | 'ask' | 'allowed';
export interface ModelRef { provider_id: string | null; model: string; }
export interface SubagentLimits {
  concurrent: number; per_task: number; per_day: number; max_minutes: number; max_requests: number;
  models: ModelRef[];
  tool_server_ids: string[] | null;   // null = all of the agent's own (except desktops)
}
export function defaultSubagentLimits(): SubagentLimits {
  return { concurrent: 1, per_task: 2, per_day: 5, max_minutes: 10, max_requests: 25, models: [],
    tool_server_ids: null };
}

export interface SubagentPlan {
  role: string; purpose: string; provider_id: string | null; model: string; label: string;
  tools: string[]; tool_server_ids: string[]; est_minutes: number;
}
export interface SubagentEstimate {
  subagents: { role: string; model: string; model_details: string; runs_on: string; est_minutes: number;
    provider_slots: number; slots_free_now: number | null;
    ledger: { runs: number; avg_seconds?: number; avg_requests?: number; avg_tokens?: number; days?: number } }[];
  total_minutes: number; peak_concurrent: number; wall_minutes: number;
  overlaps: { role: string; closest_agent: string; score: number }[];
  overlap_check: boolean;
}
export interface SubagentGrant {
  count: number; concurrent: number; max_minutes: number; max_requests: number; hours: number;
  models: ModelRef[]; tool_server_ids: string[];
}
export interface SubagentRequest {
  id: string; agent_id: string; agent: string; thread_id: string; thread_title: string; thread_owner: string;
  why: string; why_not_existing_agents: string; subagents: SubagentPlan[]; hours: number;
  estimate: SubagentEstimate; status: 'pending' | 'approved' | 'denied' | 'expired';
  grant: SubagentGrant | null; used: number; note: string; expires_at: string | null;
  decided_at: string | null; created_at: string; limits: SubagentLimits | null;
}
export interface Decision {
  approve: boolean; note?: string; count?: number; concurrent?: number; max_minutes?: number;
  max_requests?: number; hours?: number; models?: ModelRef[]; tool_server_ids?: string[];
}
export interface SubagentCaps {
  max_live: number; max_per_day: number; similarity_threshold: number; max_pending_per_agent: number;
  request_ttl_hours: number; notify_mqtt: boolean;
}
export interface UsageGroup {
  runs: number; failed: number; requests: number; input_tokens: number; output_tokens: number;
  seconds: number; avg_seconds: number; avg_tokens: number;
}
export interface UsageReport {
  days: number; total: UsageGroup | null; subagents: UsageGroup | null;
  by_agent: (UsageGroup & { agent: string; subagent_runs: number })[];
  by_model: (UsageGroup & { provider: string; model: string })[];
  by_day: (UsageGroup & { day: string })[];
}
export interface LiveSubagents {
  live: { id: string; name: string; parent: string; model: string; expires_at: string | null }[];
  today: number; caps: SubagentCaps;
}

export interface ToolServer {
  id?: string;
  name: string;
  description: string;
  transport: 'http' | 'stdio';
  url: string | null;
  command: string | null;
  args: string[];
  env: Record<string, string>;
  prefix_tools: boolean;
  desktop: boolean;
  enabled: boolean;
  approval_rules: Record<string, ApprovalMode>;
  eval_live?: Record<string, boolean>;
  shown_tools?: string[];                    // empty = all
  read_only?: boolean;
}

export type ApprovalMode = 'allow' | 'ask' | 'block';
export interface ProbedTool {
  name: string; description: string;
  suggested_approval: ApprovalMode | null;   // what the server suggests in its metadata
  approval: ApprovalMode;                    // what applies now
  read_only_hint?: boolean;                  // the server says it only reads
  safe_in_tests?: boolean;                   // may run for real in an eval
  shown?: boolean;                           // listed to agents
}
export interface ToolApproval {
  id: string; agent_id: string; agent_name: string; thread_id: string; thread_title: string;
  job_id: string | null; server_id: string; server_name: string; tool: string;
  args: Record<string, unknown>; status: 'pending' | 'approved' | 'denied' | 'expired';
  note: string; remember_until: string | null; created_at: string; decided_at: string | null;
  reason?: string;
}

export interface Thread { id: string; agent_id: string; title: string; created_at: string; }

export interface Message {
  id: string;
  thread_id: string;
  role: 'user' | 'agent' | 'event';
  agent_id: string | null;
  content: string;
  meta: Record<string, unknown>;
  created_at: string;
}

/** A file in a conversation (yours, or one an agent made or sent from its workbench). */
export interface Attachment {
  id: string; ref: string; name: string; mime: string; kind: 'image' | 'document' | 'text' | 'file';
  size: number; width: number; height: number; source: 'user' | 'agent' | 'workbench' | 'desktop';
  agent_id: string | null; has_text: boolean; message_id: string | null; created_at: string | null;
}

export function attachmentUrl(id: string, download = false) {
  return `/api/attachments/${id}` + (download ? '?download=1' : '');
}

export type ProviderKind = 'ollama' | 'openai' | 'openai_compatible' | 'anthropic' | 'chatgpt';

export type Frequency = 'hourly' | 'daily' | 'weekly' | 'monthly' | 'cron';

export interface ScheduleIn {
  name: string; agent_id: string; task: string; frequency: Frequency;
  interval_hours: number; minute: number; hour: number; weekdays: number[]; day_of_month: number;
  cron: string; timezone: string; enabled: boolean; skip_if_running: boolean;
}

export interface ScheduleRun {
  at: string; trigger: 'schedule' | 'manual'; status: string; job_id?: string; thread_id?: string;
  skipped?: boolean; reason?: string; late?: boolean;
}

export interface ScheduleInfo extends ScheduleIn {
  id: string; summary: string; agent_name: string | null; thread_id: string | null;
  next_run_at: string | null; last_run_at: string | null; last_status: string | null; runs: ScheduleRun[];
}

export interface ChatGPTStatus {
  connected: boolean; account: string | null; plan_usage: boolean; connected_at: string | null;
}

export interface ProviderInfo {
  id: string; name: string; kind: ProviderKind; base_url: string | null;
  has_api_key: boolean; max_concurrent: number; enabled: boolean; created_at: string;
  chatgpt: ChatGPTStatus | null;   // ChatGPT plan providers only
}

/** What the editor sends. api_key: undefined = keep, '' = clear, value = replace. */
export interface ProviderIn {
  id?: string; name: string; kind: ProviderKind; base_url: string | null;
  api_key?: string; max_concurrent: number; enabled: boolean;
}

export interface ModelInfo { id: string; details: string; }

export type MemoryKind = 'fact' | 'incident' | 'preference' | 'procedure' | 'note';
export type MemoryScope = 'team' | 'agent' | 'thread';
export interface MemoryItem {
  id: string; text: string; kind: MemoryKind; scope: MemoryScope; agent_id: string | null;
  thread_id: string | null; source: string; source_job_id: string | null; created_by: string | null;
  invalidated_at: string | null; invalidated_reason: string | null; superseded_by: string | null;
  last_confirmed: string; times_seen: number; embedded: boolean; created_at: string; score: number | null;
}
export interface MemorySettings {
  enabled: boolean; embedding_provider_id: string | null; embedding_model: string;
  recall_count: number; min_score: number; capture_delegations: boolean; capture_events: boolean;
  duplicate_threshold: number; related_threshold: number;
  tidy_provider_id: string | null; tidy_model: string; tidy_nightly: boolean; tidy_hour: number;
}
export interface MemoryStats { total: number; active: number; retired: number; embedded_with_current_model: number; pgvector: boolean; }

export interface DesktopRequest { agent_id: string; agent: string; thread_id: string; reason: string; at: string; }
export interface DesktopState {
  agent_id: string; desktop_url: string | null; has_desktop_tools: boolean;
  user_has_control: boolean; request: DesktopRequest | null;
}

export interface Subscription {
  id?: string;
  name: string;
  topic_filter: string;
  agent_id: string;
  thread_id: string | null;
  prompt_template: string;
  min_interval_seconds: number;
  enabled: boolean;
}

export interface Webhook {
  id?: string;
  name: string;
  source: string;
  auth_mode: 'gitea' | 'token';
  event_header: string;
  event_field: string;
  delivery_header: string;
  allowed_networks: string;
  rate_per_minute: number | null;
  cautious: boolean;
  enabled: boolean;
  created_at?: string;
  last_delivery_at?: string | null;
  url?: string;
  topic_prefix?: string;
  has_secret?: boolean;
  encrypted?: boolean;
  secret?: string | null;  // only right after create or rotate
}

export interface EventOutcome {
  subscription: string; subscription_id: string; result: 'queued' | 'cooldown';
  job_id: string | null; thread_id: string | null; missing: string[]; prompt: string;
}

export type DeliveryStatus = 'accepted' | 'no_match' | 'cooldown' | 'duplicate' | 'rejected' | 'too_large'
  | 'blocked' | 'rate_limited';

export interface WebhookSettings {
  encryption: boolean; default_networks: string; default_rate_per_minute: number;
  keep_days: number; keep_per_hook: number; dedupe_hours: number; max_body: number;
}

export interface DeliverySummary {
  id: string; hook_id: string; hook: string; at: string; status: DeliveryStatus;
  topic: string; event: string; ip: string; delivery_id: string; body_bytes: number;
  has_body: boolean; test: boolean; replay_of: string | null; detail: string;
  subscriptions: string[]; queued: number;
}

export interface DeliveryDetail extends DeliverySummary {
  headers: Record<string, string>; body: string | null; outcomes: EventOutcome[];
}

export interface DeliveryReply {
  accepted: boolean; delivery: string; status: DeliveryStatus; topic: string; queued_jobs: string[];
}

export function blankWebhook(): Webhook {
  return { name: '', source: '', auth_mode: 'token', event_header: '', event_field: '', delivery_header: '',
    allowed_networks: '', rate_per_minute: null, cautious: true, enabled: true };
}

export interface WorkerInfo { name: string; pools: string[]; concurrency: number; busy: number; seen: string; }
export interface QueueInfo { pool: string; entries: number; in_progress: number; }
export interface SystemStatus { valkey: boolean; mqtt: boolean | null; embedded_worker: boolean; }

export interface Job {
  id: string; kind: 'turn' | 'task'; status: string; task: string; background: boolean;
  pool: string; attempts: number; worker: string | null;
  requester_agent_id: string; assignee_agent_id: string;
  result: { status: string; summary: string; details: string } | null;
  created_at: string; finished_at: string | null;
}

export interface HqEvent { type: string; [k: string]: any; }

export function blankAgent(): AgentDef {
  return { name: '', description: '', pronouns: '', instructions: '', provider_id: null, model: '',
    vision_provider_id: null, vision_model: '', vision_coords: 'relative_1000', vision_zoom: true, sees_images: false,
    fallback_provider_id: null, fallback_model: '', thinking: '', memory_enabled: true, pool: 'default',
    tool_server_ids: [], delegates: [], desktop_url: null, is_default: false, enabled: true,
    subagent_mode: 'off', subagent_limits: defaultSubagentLimits(), learning_mode: 'propose', use_profile: true };
}

export function blankSubscription(agent_id = ''): Subscription {
  return { name: '', topic_filter: 'homelab/alerts/#', agent_id, thread_id: null, prompt_template: '',
    min_interval_seconds: 60, enabled: true };
}

export function blankServer(): ToolServer {
  return { name: '', description: '', transport: 'http', url: '', command: null, args: [],
    env: {}, prefix_tools: true, desktop: false, enabled: true, approval_rules: {} };
}

export interface KnowledgeSettings { recall: boolean; recall_count: number; min_score: number; chunk_chars: number; max_file_mb: number; agent_suggestions: boolean; }

export interface KnowledgeCollection {
  id: string; name: string; description: string; agent_ids: string[]; auto_recall: boolean; enabled: boolean;
  proposals: 'off' | 'all' | 'chosen'; proposer_agent_ids: string[];
  docs?: number; chunks?: number; sources?: number;
}

export interface KnowledgeSource {
  id: string; collection_id: string; kind: 'upload' | 'url' | 'git' | 'agent'; name: string; url: string | null;
  branch: string; username: string; token?: string | null; has_token?: boolean;
  include: string[]; exclude: string[]; obsidian: boolean; vault_name: string; refresh_minutes: number;
  status: 'queued' | 'indexing' | 'ready' | 'error'; error: string | null; last_indexed_at: string | null;
  last_commit: string; docs: number; chunks: number;
}

export interface KnowledgeDoc {
  id: string; source_id: string; collection_id: string; path: string; title: string; link: string;
  tags: string[]; links: string[]; size: number; chunks: number; indexed: boolean; updated_at: string;
  text?: string; sections?: { heading: string; text: string; embedded: boolean }[];
}

export interface KnowledgeHit {
  doc_id: string; title: string; section: string; collection: string; path: string; link: string; tags: string[];
  score: number; sim: number | null; keyword: number; would_recall: boolean; text: string;
}

export interface EvalCheck { type: 'rubric' | 'contains' | 'not_contains' | 'tool_called' | 'tool_not_called' | 'skill_loaded'; value: string; }
export interface EvalStep { tool: string; args: unknown; result: unknown; error?: string | null; }
export interface EvalCase {
  id: string; name: string; agent_id: string; kind: 'turn' | 'task'; prompt: string; context: string;
  checks: EvalCheck[]; recording: EvalStep[]; recording_partial: boolean; skill_names: string[];
  status: 'draft' | 'active' | 'archived'; source: string; source_job_id: string | null; thread_id: string | null;
  last_passed: boolean | null; last_run_at: string | null; created_at: string; updated_at: string;
  results?: EvalResult[];
}
export interface EvalResult {
  id: string; run_id: string; case_id: string; case_name?: string; variant: string; passed: boolean; reply: string;
  checks: { check: EvalCheck; passed: boolean; note: string }[]; calls: { tool: string; args: unknown; answered: string }[];
  model: string; seconds: number; error: string | null; created_at: string;
}
export interface EvalRun {
  id: string; trigger: 'manual' | 'proposal'; label: string; case_ids: string[]; variants: string[];
  proposal_id: string | null; status: 'queued' | 'running' | 'done' | 'failed';
  summary: Record<string, { passed: number; total: number }>; judge: string; error: string | null;
  created_at: string; started_at: string | null; finished_at: string | null; results?: EvalResult[];
}
export interface EvalSummary {
  status?: string; cases?: number; regressed?: boolean; run_id?: string;
  with?: { passed: number; total: number }; without?: { passed: number; total: number };
}
export interface EvalSettings {
  judge_provider_id: string | null; judge_model: string; on_proposals: boolean; gate_auto: boolean;
  draft_from_feedback: boolean; live_read_only: boolean; max_cases: number; keep_traces_days: number;
}

export interface ConversationSettings {
  compaction: boolean; trigger_ratio: number; keep_ratio: number; summary_max_tokens: number;
  reserve_tokens: number; local_window: number; cloud_window: number;
  provider_id: string | null; model: string; warn_overflow: boolean;
}
export type ThinkingLevel = '' | 'off' | 'on' | 'minimal' | 'low' | 'medium' | 'high' | 'xhigh' | 'max';

export interface ThinkingSupport { supported: boolean | null; note: string; }

export interface AgentContextInfo {
  window: number; source: string; detected: number | null; set: number | null;
  instruction_tokens: number; reserve_tokens: number; room_for_conversation: number;
}

export interface InboxCount { requests: number; approvals: number; skills: number; profile: number; knowledge: number; total: number; }

export interface Profile { text: string; enabled: boolean; max_words: number; suggestions: boolean; updated_at: string | null; words: number; }

export interface ProfileProposal {
  id: string; text: string; reason: string; source: string; agent_id: string; agent_name: string;
  thread_id: string | null; job_id: string | null; reflection_id: string | null;
  status: 'pending' | 'approved' | 'rejected'; note: string; created_at: string; decided_at: string | null;
}

/** An agent suggesting a document for a knowledge collection (Requests → Knowledge). */
export interface KnowledgeProposal {
  id: string; collection_id: string; collection: string; title: string; text: string; reason: string;
  attachment_id: string | null; source: 'tool' | 'reflection'; agent_id: string; agent_name: string;
  thread_id: string | null; job_id: string | null; reflection_id: string | null;
  similar_doc_id: string | null; similar_title: string; similar_score: number | null; similar_replaceable: boolean;
  status: 'pending' | 'approved' | 'rejected'; note: string; doc_id: string | null;
  created_at: string; decided_at: string | null;
}

export interface MessageFeedback { rating: 'up' | 'down'; comment: string; }

export interface LearningSettings {
  reflect: 'off' | 'signals' | 'every_job';
  provider_id: string | null;
  model: string;
  on_feedback: boolean;
  on_correction: boolean;
  on_failure: boolean;
  min_tool_calls: number;
  wait_minutes: number;
  feedback_wait_minutes: number;
  max_per_day: number;
  lessons_to_memory: boolean;
}

export interface Reflection {
  id: string; job_id: string; thread_id: string; agent_id: string; agent_name: string;
  reasons: string[]; status: 'waiting' | 'running' | 'done' | 'skipped' | 'failed';
  due_at: string; summary: string; proposal_ids: string[]; memory_ids: string[];
  profile_proposal_ids?: string[]; model: string; error: string | null; created_at: string; started_at: string | null; finished_at: string | null;
  proposals: { id: string; name: string; title: string; status: string; revision: boolean }[];
}

export interface ReflectionLog { reflections: Reflection[]; ran_today: number; max_per_day: number; }

@Injectable({ providedIn: 'root' })
export class Api {
  private http = inject(HttpClient);

  agents() { return this.http.get<AgentDef[]>('/api/agents'); }
  schedules() { return this.http.get<ScheduleInfo[]>('/api/schedules'); }
  createSchedule(s: ScheduleIn) { return this.http.post<ScheduleInfo>('/api/schedules', s); }
  updateSchedule(id: string, s: ScheduleIn) { return this.http.put<ScheduleInfo>(`/api/schedules/${id}`, s); }
  deleteSchedule(id: string) { return this.http.delete(`/api/schedules/${id}`); }
  runSchedule(id: string) { return this.http.post<ScheduleInfo>(`/api/schedules/${id}/run`, {}); }
  previewSchedule(s: Partial<ScheduleIn>) {
    return this.http.post<{ cron: string; summary: string; next: string[] }>('/api/schedules/preview', s);
  }
  saveAgent(a: AgentDef): Observable<AgentDef> {
    return a.id ? this.http.put<AgentDef>(`/api/agents/${a.id}`, a)
                : this.http.post<AgentDef>('/api/agents', a);
  }
  deleteAgent(id: string) { return this.http.delete(`/api/agents/${id}`); }
  uploadAvatar(id: string, file: File) {
    const form = new FormData();
    form.append('file', file);
    return this.http.put<AgentDef>(`/api/agents/${id}/avatar`, form);
  }
  removeAvatar(id: string) { return this.http.delete<AgentDef>(`/api/agents/${id}/avatar`); }
  exportAgents() { return this.http.get<any>('/api/agents/export'); }
  importAgents(data: any) { return this.http.post<any>('/api/agents/import', data); }

  providers() { return this.http.get<ProviderInfo[]>('/api/providers'); }
  saveProvider(p: ProviderIn): Observable<ProviderInfo> {
    const body = { ...p };
    if (!body.api_key) delete body.api_key;   // blank field = keep the stored key
    return p.id ? this.http.put<ProviderInfo>(`/api/providers/${p.id}`, body)
                : this.http.post<ProviderInfo>('/api/providers', body);
  }
  clearProviderKey(p: ProviderInfo) {
    return this.http.put<ProviderInfo>(`/api/providers/${p.id}`,
      { name: p.name, kind: p.kind, base_url: p.base_url, enabled: p.enabled, api_key: '',
        max_concurrent: p.max_concurrent });
  }
  deleteProvider(id: string) { return this.http.delete(`/api/providers/${id}`); }
  providerModels(id: string) { return this.http.get<ModelInfo[]>(`/api/providers/${id}/models`); }
  chatgptStart(id: string) {
    return this.http.post<{ url: string; redirect_uri: string; expires_in: number }>(`/api/providers/${id}/chatgpt/start`, {});
  }
  chatgptFinish(id: string, callback_url: string) {
    return this.http.post<ProviderInfo>(`/api/providers/${id}/chatgpt/finish`, { callback_url });
  }
  chatgptDisconnect(id: string) { return this.http.post<ProviderInfo>(`/api/providers/${id}/chatgpt/disconnect`, {}); }

  desktopState(agentId: string) { return this.http.get<DesktopState>(`/api/agents/${agentId}/desktop`); }
  takeDesktop(agentId: string, threadId: string | null) {
    return this.http.post<DesktopState>(`/api/agents/${agentId}/desktop/take`, { thread_id: threadId });
  }
  releaseDesktop(agentId: string, threadId: string | null, note: string) {
    return this.http.post<DesktopState>(`/api/agents/${agentId}/desktop/release`, { thread_id: threadId, note });
  }

  memories(query: string, opts: { scope?: string; include_retired?: boolean } = {}) {
    const params: Record<string, string> = { query };
    if (opts.scope) params['scope'] = opts.scope;
    if (opts.include_retired) params['include_retired'] = 'true';
    return this.http.get<MemoryItem[]>('/api/memory', { params });
  }
  addMemory(m: { text: string; kind: MemoryKind; scope: MemoryScope; agent_id?: string | null }) {
    return this.http.post<MemoryItem>('/api/memory', m);
  }
  editMemory(id: string, m: { text: string; kind: MemoryKind; scope: MemoryScope; agent_id?: string | null }) {
    return this.http.put<MemoryItem>(`/api/memory/${id}`, m);
  }
  retireMemory(id: string, reason: string) { return this.http.post<MemoryItem>(`/api/memory/${id}/retire`, { reason }); }
  restoreMemory(id: string) { return this.http.post<MemoryItem>(`/api/memory/${id}/restore`, {}); }
  confirmMemory(id: string) { return this.http.post<MemoryItem>(`/api/memory/${id}/confirm`, {}); }
  deleteMemory(id: string) { return this.http.delete(`/api/memory/${id}`); }
  memorySettings() { return this.http.get<MemorySettings>('/api/memory/settings'); }
  saveMemorySettings(s: MemorySettings) { return this.http.put<MemorySettings>('/api/memory/settings', s); }
  memoryStats() { return this.http.get<MemoryStats>('/api/memory/stats'); }
  reembedMemories() { return this.http.post<{ embedded: number; reason?: string }>('/api/memory/reembed', {}); }
  tidyMemories() { return this.http.post<{ groups: number; merged: number; retired: number; model_used: boolean }>('/api/memory/tidy', {}); }

  servers() { return this.http.get<ToolServer[]>('/api/tool-servers'); }
  saveServer(t: ToolServer): Observable<ToolServer> {
    return t.id ? this.http.put<ToolServer>(`/api/tool-servers/${t.id}`, t)
                : this.http.post<ToolServer>('/api/tool-servers', t);
  }
  deleteServer(id: string) { return this.http.delete(`/api/tool-servers/${id}`); }
  probeServer(id: string) {
    return this.http.get<ProbedTool[]>(`/api/tool-servers/${id}/tools`);
  }

  toolApprovals(status?: string) {
    return this.http.get<ToolApproval[]>('/api/tool-approvals', { params: status ? { status } : {} });
  }
  toolApproval(id: string) { return this.http.get<ToolApproval>(`/api/tool-approvals/${id}`); }
  decideApproval(id: string, approve: boolean, note = '', remember_minutes = 0) {
    return this.http.post<ToolApproval>(`/api/tool-approvals/${id}/decide`, { approve, note, remember_minutes });
  }
  inboxCount() { return this.http.get<InboxCount>('/api/inbox/count'); }
  profile() { return this.http.get<Profile>('/api/profile'); }
  saveProfile(x: Partial<Profile>) { return this.http.put<Profile>('/api/profile', x); }
  profileProposals(status?: string) { return this.http.get<ProfileProposal[]>('/api/profile-proposals', { params: status ? { status } : {} }); }
  knowledgeProposals(status?: string) { return this.http.get<KnowledgeProposal[]>('/api/knowledge-proposals', { params: status ? { status } : {} }); }
  knowledgeProposal(id: string) { return this.http.get<KnowledgeProposal>(`/api/knowledge-proposals/${id}`); }
  decideKnowledgeProposal(id: string, d: { approve: boolean; note?: string; title?: string; text?: string;
                                            collection_id?: string; replace_doc_id?: string }) {
    return this.http.post<KnowledgeProposal>(`/api/knowledge-proposals/${id}/decide`, d);
  }
  profileProposal(id: string) { return this.http.get<ProfileProposal>(`/api/profile-proposals/${id}`); }
  decideProfileProposal(id: string, d: { approve: boolean; note?: string; text?: string }) {
    return this.http.post<ProfileProposal>(`/api/profile-proposals/${id}/decide`, d);
  }

  skills(includeRetired = false) { return this.http.get<Skill[]>('/api/skills', { params: { include_retired: includeRetired } }); }
  skill(id: string) { return this.http.get<Skill>(`/api/skills/${id}`); }
  saveSkill(k: Skill) {
    const body = { name: k.name, title: k.title, when_to_use: k.when_to_use, body: k.body, tools: k.tools,
      script: k.script || null, scope: k.scope, agent_id: k.agent_id, note: k.note ?? '' };
    return k.id ? this.http.put<Skill>(`/api/skills/${k.id}`, body) : this.http.post<Skill>('/api/skills', body);
  }
  setSkillStatus(id: string, status: 'active' | 'retired') { return this.http.post<Skill>(`/api/skills/${id}/status`, { status }); }
  rollbackSkill(id: string, version: number) { return this.http.post<Skill>(`/api/skills/${id}/rollback`, { version }); }
  skillProposals(status?: string) {
    return this.http.get<SkillProposal[]>('/api/skill-proposals', { params: status ? { status } : {} });
  }
  skillProposal(id: string) { return this.http.get<SkillProposal>(`/api/skill-proposals/${id}`); }
  decideSkillProposal(id: string, d: { approve: boolean; note?: string; title?: string; when_to_use?: string; body?: string; scope?: string }) {
    return this.http.post<SkillProposal>(`/api/skill-proposals/${id}/decide`, d);
  }

  rateMessage(id: string, rating: 'up' | 'down' | null, comment = '') {
    return this.http.post<{ id: string; feedback: MessageFeedback | null }>(`/api/messages/${id}/feedback`, { rating, comment });
  }
  learningSettings() { return this.http.get<LearningSettings>('/api/learning/settings'); }
  saveLearningSettings(x: LearningSettings) { return this.http.put<LearningSettings>('/api/learning/settings', x); }
  reflections(limit = 50) { return this.http.get<ReflectionLog>('/api/learning/reflections', { params: { limit } }); }
  runReflection(id: string) { return this.http.post(`/api/learning/reflections/${id}/run`, {}); }
  cancelReflection(id: string) { return this.http.delete(`/api/learning/reflections/${id}`); }

  knowledgeSettings() { return this.http.get<KnowledgeSettings>('/api/knowledge/settings'); }
  saveKnowledgeSettings(x: KnowledgeSettings) { return this.http.put<KnowledgeSettings>('/api/knowledge/settings', x); }
  collections() { return this.http.get<KnowledgeCollection[]>('/api/knowledge/collections'); }
  createCollection(c: Partial<KnowledgeCollection>) { return this.http.post<KnowledgeCollection>('/api/knowledge/collections', c); }
  updateCollection(c: KnowledgeCollection) { return this.http.put<KnowledgeCollection>(`/api/knowledge/collections/${c.id}`, c); }
  deleteCollection(id: string) { return this.http.delete(`/api/knowledge/collections/${id}`); }
  sources(collectionId: string) { return this.http.get<KnowledgeSource[]>(`/api/knowledge/collections/${collectionId}/sources`); }
  createSource(collectionId: string, x: Partial<KnowledgeSource>) {
    return this.http.post<KnowledgeSource>(`/api/knowledge/collections/${collectionId}/sources`, x);
  }
  updateSource(x: Partial<KnowledgeSource> & { id: string }) { return this.http.put<KnowledgeSource>(`/api/knowledge/sources/${x.id}`, x); }
  deleteSource(id: string) { return this.http.delete(`/api/knowledge/sources/${id}`); }
  reindexSource(id: string, full = false) { return this.http.post(`/api/knowledge/sources/${id}/reindex`, {}, { params: { full } }); }
  uploadKnowledge(sourceId: string, files: File[]) {
    const form = new FormData();
    for (const f of files) form.append('files', f, f.name);
    return this.http.post<{ added: { id: string; title: string; path: string }[]; errors: string[] }>(
      `/api/knowledge/sources/${sourceId}/files`, form);
  }
  knowledgeDocs(collectionId: string, q = '', limit = 200) {
    return this.http.get<{ total: number; docs: KnowledgeDoc[] }>(`/api/knowledge/collections/${collectionId}/docs`, { params: { q, limit } });
  }
  knowledgeDoc(id: string) { return this.http.get<KnowledgeDoc>(`/api/knowledge/docs/${id}`); }
  deleteKnowledgeDoc(id: string) { return this.http.delete(`/api/knowledge/docs/${id}`); }
  searchKnowledge(q: string, opts: { collection_id?: string; agent_id?: string } = {}) {
    const params: Record<string, string> = { q };
    if (opts.collection_id) params['collection_id'] = opts.collection_id;
    if (opts.agent_id) params['agent_id'] = opts.agent_id;
    return this.http.get<{ results: KnowledgeHit[]; min_score: number }>('/api/knowledge/search', { params });
  }
  proposeSkillsFromDoc(docId: string, agentId: string) {
    return this.http.post<{ summary: string; proposals: { proposal_id: string; name: string; status: string }[]; notes: string[]; model: string }>(
      `/api/knowledge/docs/${docId}/propose-skills`, { agent_id: agentId });
  }

  evalSettings() { return this.http.get<EvalSettings>('/api/evals/settings'); }
  saveEvalSettings(x: EvalSettings) { return this.http.put<EvalSettings>('/api/evals/settings', x); }
  evalCases(status?: string) {
    return this.http.get<{ cases: EvalCase[]; counts: Record<string, number> }>('/api/evals/cases', { params: status ? { status } : {} });
  }
  evalCase(id: string) { return this.http.get<EvalCase>(`/api/evals/cases/${id}`); }
  createEvalCase(c: Partial<EvalCase>) { return this.http.post<EvalCase>('/api/evals/cases', c); }
  saveEvalCase(c: EvalCase) { return this.http.put<EvalCase>(`/api/evals/cases/${c.id}`, c); }
  deleteEvalCase(id: string) { return this.http.delete(`/api/evals/cases/${id}`); }
  caseFromMessage(messageId: string) { return this.http.post<EvalCase>('/api/evals/cases/from-message', { message_id: messageId }); }
  startEvalRun(body: { case_ids?: string[]; agent_id?: string; label?: string }) { return this.http.post<EvalRun>('/api/evals/runs', body); }
  evalRuns(limit = 50) { return this.http.get<EvalRun[]>('/api/evals/runs', { params: { limit } }); }
  evalRun(id: string) { return this.http.get<EvalRun>(`/api/evals/runs/${id}`); }

  conversationSettings() { return this.http.get<ConversationSettings>('/api/conversations/settings'); }
  saveConversationSettings(x: ConversationSettings) { return this.http.put<ConversationSettings>('/api/conversations/settings', x); }
  agentContext(id: string) { return this.http.get<AgentContextInfo>(`/api/agents/${id}/context`); }
  thinkingSupport(providerId: string | null, model: string) {
    const params: Record<string, string> = { model };
    if (providerId) params['provider_id'] = providerId;
    return this.http.get<ThinkingSupport>('/api/agents/thinking-support', { params });
  }

  subscriptions() { return this.http.get<Subscription[]>('/api/subscriptions'); }
  saveSubscription(x: Subscription): Observable<Subscription> {
    return x.id ? this.http.put<Subscription>(`/api/subscriptions/${x.id}`, x)
                : this.http.post<Subscription>('/api/subscriptions', x);
  }
  deleteSubscription(id: string) { return this.http.delete(`/api/subscriptions/${id}`); }
  testEvent(topic: string, payload: string) {
    return this.http.post<{ queued_jobs: string[]; outcomes: EventOutcome[] }>('/api/subscriptions/test', { topic, payload });
  }

  webhooks() { return this.http.get<Webhook[]>('/api/webhooks'); }
  saveWebhook(x: Webhook): Observable<Webhook> {
    return x.id ? this.http.put<Webhook>(`/api/webhooks/${x.id}`, x)
                : this.http.post<Webhook>('/api/webhooks', x);
  }
  deleteWebhook(id: string) { return this.http.delete(`/api/webhooks/${id}`); }
  rotateWebhook(id: string) { return this.http.post<Webhook>(`/api/webhooks/${id}/rotate`, {}); }
  webhookSettings() { return this.http.get<WebhookSettings>('/api/webhooks/settings'); }
  hookDeliveries(id: string, limit = 50) {
    return this.http.get<DeliverySummary[]>(`/api/webhooks/${id}/deliveries`, { params: { limit } });
  }
  recentDeliveries(topic_filter = '', with_body = false, limit = 20) {
    return this.http.get<DeliverySummary[]>('/api/webhooks/deliveries', { params: { topic_filter, with_body, limit } });
  }
  delivery(id: string) { return this.http.get<DeliveryDetail>(`/api/webhooks/deliveries/${id}`); }
  sendHookTest(id: string, event: string, body: string, respect_cooldown: boolean) {
    return this.http.post<DeliveryReply>(`/api/webhooks/${id}/test`, { event, body, respect_cooldown });
  }
  replayDelivery(id: string, respect_cooldown: boolean) {
    return this.http.post<DeliveryReply>(`/api/webhooks/deliveries/${id}/replay`, { respect_cooldown });
  }
  previewTemplate(prompt_template: string, topic: string, payload: string) {
    return this.http.post<{ prompt: string; missing: string[] }>('/api/subscriptions/preview', { prompt_template, topic, payload });
  }

  subagentRequests(status?: string) {
    return this.http.get<SubagentRequest[]>('/api/subagent-requests', { params: status ? { status } : {} });
  }
  subagentRequest(id: string) { return this.http.get<SubagentRequest>(`/api/subagent-requests/${id}`); }
  pendingRequests() { return this.http.get<{ pending: number }>('/api/subagent-requests/pending-count'); }
  decideRequest(id: string, d: Decision) {
    return this.http.post<SubagentRequest>(`/api/subagent-requests/${id}/decide`, d);
  }
  subagentCaps() { return this.http.get<SubagentCaps>('/api/settings/subagents'); }
  saveSubagentCaps(c: SubagentCaps) { return this.http.put<SubagentCaps>('/api/settings/subagents', c); }
  liveSubagents() { return this.http.get<LiveSubagents>('/api/subagents/live'); }
  usage(days: number) { return this.http.get<UsageReport>('/api/usage', { params: { days } }); }

  workers() { return this.http.get<WorkerInfo[]>('/api/system/workers'); }
  queues() { return this.http.get<QueueInfo[]>('/api/system/queues'); }
  status() { return this.http.get<SystemStatus>('/api/system/status'); }

  threads() { return this.http.get<Thread[]>('/api/threads'); }
  createThread(agent_id: string) { return this.http.post<Thread>('/api/threads', { agent_id }); }
  messages(threadId: string) { return this.http.get<Message[]>(`/api/threads/${threadId}/messages`); }
  send(threadId: string, content: string, attachmentIds: string[] = []) {
    return this.http.post(`/api/threads/${threadId}/messages`, { content, attachment_ids: attachmentIds });
  }
  uploadAttachments(threadId: string, files: File[]) {
    const form = new FormData();
    for (const f of files) form.append('files', f, f.name);
    return this.http.post<Attachment[]>(`/api/threads/${threadId}/attachments`, form);
  }
  removeAttachment(id: string) { return this.http.delete(`/api/attachments/${id}`); }
  jobs(threadId: string) { return this.http.get<Job[]>(`/api/threads/${threadId}/jobs`); }

  events(threadId: string): Observable<HqEvent> {
    return new Observable<HqEvent>(sub => {
      const proto = location.protocol === 'https:' ? 'wss' : 'ws';
      let ws: WebSocket | null = null;
      let closed = false;
      const open = () => {
        ws = new WebSocket(`${proto}://${location.host}/ws/threads/${threadId}`);
        ws.onmessage = e => { const ev = JSON.parse(e.data); if (ev.type !== 'ping') sub.next(ev); };
        ws.onclose = () => { if (!closed) setTimeout(open, 2000); };
      };
      open();
      return () => { closed = true; ws?.close(); };
    });
  }
}
