import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable } from 'rxjs';

export interface AgentDef {
  id?: string;
  name: string;
  description: string;
  instructions: string;
  model: string;
  tool_server_ids: string[];
  delegates: string[];
  desktop_url: string | null;
  is_default: boolean;
  enabled: boolean;
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

export interface Job {
  id: string; status: string; task: string; background: boolean;
  requester_agent_id: string; assignee_agent_id: string;
  result: { status: string; summary: string; details: string } | null;
  created_at: string; finished_at: string | null;
}

export interface HqEvent { type: string; [k: string]: any; }

export function blankAgent(): AgentDef {
  return { name: '', description: '', instructions: '', model: 'ollama:gpt-oss:20b',
    tool_server_ids: [], delegates: [], desktop_url: null, is_default: false, enabled: true };
}

export function blankServer(): ToolServer {
  return { name: '', description: '', transport: 'http', url: '', command: null, args: [],
    env: {}, prefix_tools: true, enabled: true };
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
  exportAgents() { return this.http.get<any>('/api/agents/export'); }
  importAgents(data: any) { return this.http.post<any>('/api/agents/import', data); }

  servers() { return this.http.get<ToolServer[]>('/api/tool-servers'); }
  saveServer(t: ToolServer): Observable<ToolServer> {
    return t.id ? this.http.put<ToolServer>(`/api/tool-servers/${t.id}`, t)
                : this.http.post<ToolServer>('/api/tool-servers', t);
  }
  deleteServer(id: string) { return this.http.delete(`/api/tool-servers/${id}`); }
  probeServer(id: string) {
    return this.http.get<{ name: string; description: string }[]>(`/api/tool-servers/${id}/tools`);
  }

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
