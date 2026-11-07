import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable } from 'rxjs';

export interface AgentDef {
  id?: string;
  name: string;
  description: string;
  instructions: string;
  provider_id: string | null;
  model: string;
  vision_provider_id: string | null;
  vision_model: string;
  vision_coords: 'relative_1000' | 'pixels';
  vision_zoom: boolean;
  fallback_provider_id: string | null;
  fallback_model: string;
  pool: string;
  tool_server_ids: string[];
  delegates: string[];
  desktop_url: string | null;
  is_default: boolean;
  enabled: boolean;
  avatar_version?: number;
  avatar_animated?: boolean;
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

export type ProviderKind = 'ollama' | 'openai' | 'openai_compatible' | 'anthropic';

export interface ProviderInfo {
  id: string; name: string; kind: ProviderKind; base_url: string | null;
  has_api_key: boolean; enabled: boolean; created_at: string;
}

/** What the editor sends. api_key: undefined = keep, '' = clear, value = replace. */
export interface ProviderIn {
  id?: string; name: string; kind: ProviderKind; base_url: string | null;
  api_key?: string; enabled: boolean;
}

export interface ModelInfo { id: string; details: string; }

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
  return { name: '', description: '', instructions: '', provider_id: null, model: '',
    vision_provider_id: null, vision_model: '', vision_coords: 'relative_1000', vision_zoom: true,
    fallback_provider_id: null, fallback_model: '', pool: 'default',
    tool_server_ids: [], delegates: [], desktop_url: null, is_default: false, enabled: true };
}

export function blankSubscription(agent_id = ''): Subscription {
  return { name: '', topic_filter: 'homelab/alerts/#', agent_id, thread_id: null, prompt_template: '',
    min_interval_seconds: 60, enabled: true };
}

export function blankServer(): ToolServer {
  return { name: '', description: '', transport: 'http', url: '', command: null, args: [],
    env: {}, prefix_tools: true, desktop: false, enabled: true };
}

@Injectable({ providedIn: 'root' })
export class Api {
  private http = inject(HttpClient);

  agents() { return this.http.get<AgentDef[]>('/api/agents'); }
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
      { name: p.name, kind: p.kind, base_url: p.base_url, enabled: p.enabled, api_key: '' });
  }
  deleteProvider(id: string) { return this.http.delete(`/api/providers/${id}`); }
  providerModels(id: string) { return this.http.get<ModelInfo[]>(`/api/providers/${id}/models`); }

  desktopState(agentId: string) { return this.http.get<DesktopState>(`/api/agents/${agentId}/desktop`); }
  takeDesktop(agentId: string, threadId: string | null) {
    return this.http.post<DesktopState>(`/api/agents/${agentId}/desktop/take`, { thread_id: threadId });
  }
  releaseDesktop(agentId: string, threadId: string | null, note: string) {
    return this.http.post<DesktopState>(`/api/agents/${agentId}/desktop/release`, { thread_id: threadId, note });
  }

  servers() { return this.http.get<ToolServer[]>('/api/tool-servers'); }
  saveServer(t: ToolServer): Observable<ToolServer> {
    return t.id ? this.http.put<ToolServer>(`/api/tool-servers/${t.id}`, t)
                : this.http.post<ToolServer>('/api/tool-servers', t);
  }
  deleteServer(id: string) { return this.http.delete(`/api/tool-servers/${id}`); }
  probeServer(id: string) {
    return this.http.get<{ name: string; description: string }[]>(`/api/tool-servers/${id}/tools`);
  }

  subscriptions() { return this.http.get<Subscription[]>('/api/subscriptions'); }
  saveSubscription(x: Subscription): Observable<Subscription> {
    return x.id ? this.http.put<Subscription>(`/api/subscriptions/${x.id}`, x)
                : this.http.post<Subscription>('/api/subscriptions', x);
  }
  deleteSubscription(id: string) { return this.http.delete(`/api/subscriptions/${id}`); }
  testEvent(topic: string, payload: string) {
    return this.http.post<{ queued_jobs: string[] }>('/api/subscriptions/test', { topic, payload });
  }

  workers() { return this.http.get<WorkerInfo[]>('/api/system/workers'); }
  queues() { return this.http.get<QueueInfo[]>('/api/system/queues'); }
  status() { return this.http.get<SystemStatus>('/api/system/status'); }

  threads() { return this.http.get<Thread[]>('/api/threads'); }
  createThread(agent_id: string) { return this.http.post<Thread>('/api/threads', { agent_id }); }
  messages(threadId: string) { return this.http.get<Message[]>(`/api/threads/${threadId}/messages`); }
  send(threadId: string, content: string) {
    return this.http.post(`/api/threads/${threadId}/messages`, { content });
  }
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
