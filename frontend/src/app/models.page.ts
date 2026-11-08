import { Component, OnInit, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { Api, ModelInfo, ProviderIn, ProviderInfo, ProviderKind } from './api.service';

const KINDS: { kind: ProviderKind; label: string; url: string; urlHint: string; keyHint: string }[] = [
  { kind: 'ollama', label: 'Ollama', url: 'http://ollama-host:11434',
    urlHint: 'your Ollama server (with or without /v1)', keyHint: 'usually not needed' },
  { kind: 'openai', label: 'OpenAI', url: '',
    urlHint: 'optional; leave empty for api.openai.com', keyHint: 'or set OPENAI_API_KEY on the workers' },
  { kind: 'openai_compatible', label: 'OpenAI-compatible (vLLM, LM Studio, LiteLLM…)', url: 'http://host:8000/v1',
    urlHint: 'required; the server\'s /v1 URL', keyHint: 'only if the server requires one' },
  { kind: 'anthropic', label: 'Anthropic', url: '',
    urlHint: 'optional; leave empty for api.anthropic.com', keyHint: 'or set ANTHROPIC_API_KEY on the workers' },
  { kind: 'chatgpt', label: 'ChatGPT plan (Plus/Pro, Sign in with ChatGPT)', url: '', urlHint: '', keyHint: '' },
];

@Component({
  selector: 'hq-models',
  imports: [FormsModule],
  template: `
  <div class="split">
    <aside class="list">
      <div class="list-head"><h2>Model providers</h2><button (click)="edit(null)">+ New</button></div>
      @for (p of providers(); track p.id) {
        <button class="row" [class.active]="draft()?.id === p.id" [class.muted]="!p.enabled" (click)="edit(p)">
          <span class="name">{{ p.name }} <span class="pill">{{ kindLabel(p.kind) }}</span></span>
          <span class="sub">@if (p.max_concurrent) { {{ p.max_concurrent }} slot{{ p.max_concurrent === 1 ? '' : 's' }} · }{{ p.kind === 'chatgpt'
            ? (p.chatgpt?.connected ? p.chatgpt?.account || 'signed in' : 'not signed in')
            : (p.base_url || 'default endpoint') }}</span>
        </button>
      } @empty { <p class="hint">Add a provider (e.g. your Ollama server), then pick its models on each agent.</p> }
    </aside>

    @if (draft(); as d) {
      <form class="editor" (ngSubmit)="save()">
        <h2>{{ d.id ? 'Edit ' + d.name : 'New model provider' }}</h2>
        <label>Name<input name="name" [(ngModel)]="d.name" placeholder="Ollama (GPU server)" required></label>
        <label>Kind
          <select name="kind" [(ngModel)]="d.kind">
            @for (k of kinds; track k.kind) { <option [value]="k.kind">{{ k.label }}</option> }
          </select></label>
        @if (d.kind === 'chatgpt') {
          <p class="hint inline-hint">Runs agents on your ChatGPT Plus or Pro plan instead of an API key.
            Usage counts against your plan's limits (on Plus, a five-hour window shared with every app you've
            connected). Give agents on this provider a local fallback model for when the plan runs out.
            @if (!d.id) { Save, then sign in. }</p>
        } @else {
          <label>Base URL <small>{{ kindInfo(d.kind).urlHint }}</small>
            <input name="url" [(ngModel)]="d.base_url" [placeholder]="kindInfo(d.kind).url"></label>
          <label>API key <small>{{ kindInfo(d.kind).keyHint }}</small>
            <input name="key" type="password" autocomplete="new-password" [(ngModel)]="d.api_key"
              [placeholder]="current()?.has_api_key ? 'stored (leave blank to keep)' : ''"></label>
          @if (current()?.has_api_key) {
            <p class="hint inline-hint">A key is stored. <a href="" (click)="$event.preventDefault(); clearKey()">Remove it</a></p>
          }
        }
        <label>Max concurrent requests <small>slots: how many model calls may run at once across all workers (0 = no limit)</small>
          <input name="slots" type="number" min="0" max="64" [(ngModel)]="d.max_concurrent"></label>
        <p class="hint inline-hint">Extra requests wait their turn. Set it to what this server's GPU can serve at once
          (often 1–2 for a single Ollama box) so sub-agents can't swamp it.</p>
        @if (d.id && current()?.kind === 'chatgpt') {
          <div class="signin">
            @if (current()?.chatgpt?.connected) {
              <p><span class="dot ok"></span> Signed in as <b>{{ current()?.chatgpt?.account || 'your ChatGPT account' }}</b></p>
              <div class="actions">
                <button type="button" class="ghost" (click)="startSignIn()">Sign in again</button>
                <button type="button" class="ghost" (click)="disconnect()">Disconnect</button>
              </div>
            } @else if (!signinUrl()) {
              <p><span class="dot"></span> Not signed in.</p>
              <div class="actions"><button type="button" (click)="startSignIn()">Sign in with ChatGPT</button></div>
            }
            @if (signinUrl()) {
              <ol class="steps">
                <li>Approve Agent HQ in the OpenAI tab that just opened
                  (<a [href]="signinUrl()" target="_blank" rel="noopener">open it again</a>).
                  Allow it to use your ChatGPT plan.</li>
                <li>Your browser then goes to <code>127.0.0.1:1455</code> and shows a
                  “can't connect” page. That's expected: Agent HQ runs on a different machine.</li>
                <li>Copy the whole address from that tab's address bar and paste it here.</li>
              </ol>
              <label>Address from the address bar
                <input name="callback" [(ngModel)]="callback" placeholder="http://127.0.0.1:1455/auth/callback?code=…"></label>
              <div class="actions">
                <button type="button" (click)="finishSignIn()" [disabled]="!callback.trim()">Finish sign-in</button>
                <button type="button" class="ghost" (click)="signinUrl.set('')">Cancel</button>
              </div>
            }
          </div>
        }
        <label class="check"><input type="checkbox" name="enabled" [(ngModel)]="d.enabled"> Enabled</label>
        @if (error()) { <p class="error">{{ error() }}</p> }
        <div class="actions">
          <button type="submit">Save</button>
          @if (d.id) {
            <button type="button" class="ghost" (click)="test()">List models</button>
            <button type="button" class="danger" (click)="remove()">Delete</button>
          }
        </div>
        @if (testing()) { <p class="hint">{{ testing() }}</p> }
        @if (models().length) {
          <h3>Available models ({{ models().length }})</h3>
          <table class="grid">
            <tr><th>Model</th><th>Details</th></tr>
            @for (m of models(); track m.id) { <tr><td><code>{{ m.id }}</code></td><td class="muted">{{ m.details }}</td></tr> }
          </table>
        }
      </form>
    } @else { <div class="empty">Select a provider or add one.</div> }
  </div>`,
})
export class ModelsPage implements OnInit {
  private api = inject(Api);
  kinds = KINDS;
  providers = signal<ProviderInfo[]>([]);
  current = signal<ProviderInfo | null>(null);
  draft = signal<ProviderIn | null>(null);
  models = signal<ModelInfo[]>([]);
  testing = signal('');
  error = signal('');
  signinUrl = signal('');
  callback = '';

  ngOnInit() { this.load(); }
  load() { this.api.providers().subscribe(p => this.providers.set(p)); }
  kindInfo(k: ProviderKind) { return KINDS.find(x => x.kind === k)!; }
  kindLabel(k: ProviderKind) { return this.kindInfo(k).label.split(' (')[0]; }

  edit(p: ProviderInfo | null) {
    this.error.set(''); this.testing.set(''); this.models.set([]);
    this.signinUrl.set(''); this.callback = '';
    this.current.set(p);
    this.draft.set(p ? { id: p.id, name: p.name, kind: p.kind, base_url: p.base_url, enabled: p.enabled, api_key: '',
                         max_concurrent: p.max_concurrent ?? 0 }
                     : { name: '', kind: 'ollama', base_url: '', enabled: true, api_key: '', max_concurrent: 0 });
  }

  save() {
    this.api.saveProvider(this.draft()!).subscribe({
      next: p => { this.load(); this.edit(p); if (p.kind !== 'chatgpt' || p.chatgpt?.connected) this.test(); },
      error: e => this.error.set(e.error?.detail ?? 'Save failed'),
    });
  }

  test() {
    const id = this.draft()?.id;
    if (!id) return;
    this.models.set([]); this.testing.set('Asking the provider for its models…');
    this.api.providerModels(id).subscribe({
      next: m => { this.models.set(m); this.testing.set(m.length ? '' : 'Connected, but it offers no models.'); },
      error: e => this.testing.set(e.error?.detail ?? 'Could not reach the provider'),
    });
  }

  startSignIn() {
    const id = this.current()?.id;
    if (!id) return;
    this.error.set('');
    // Open the tab first (inside the click), then point it at OpenAI: avoids popup blockers.
    const tab = window.open('', '_blank');
    this.api.chatgptStart(id).subscribe({
      next: r => {
        this.signinUrl.set(r.url); this.callback = '';
        if (tab) { tab.opener = null; tab.location.href = r.url; }
      },
      error: e => { tab?.close(); this.error.set(e.error?.detail ?? 'Could not start the sign-in'); },
    });
  }

  finishSignIn() {
    const id = this.current()?.id;
    if (!id) return;
    this.error.set(''); this.testing.set('Finishing sign-in…');
    this.api.chatgptFinish(id, this.callback).subscribe({
      next: p => { this.load(); this.edit(p); this.test(); },
      error: e => { this.testing.set(''); this.error.set(e.error?.detail ?? 'Sign-in failed'); },
    });
  }

  disconnect() {
    const p = this.current();
    if (!p || !confirm(`Disconnect ${p.name} from your ChatGPT account? Agents using it will stop working ` +
                       `until you sign in again. (To revoke access entirely, also remove Agent HQ in your ChatGPT settings.)`)) return;
    this.api.chatgptDisconnect(p.id).subscribe(x => { this.load(); this.edit(x); });
  }

  clearKey() {
    const p = this.current();
    if (!p || !confirm(`Remove the stored API key for ${p.name}?`)) return;
    this.api.clearProviderKey(p).subscribe(x => { this.load(); this.edit(x); });
  }

  remove() {
    const d = this.draft()!;
    if (!confirm(`Delete provider ${d.name}?`)) return;
    this.api.deleteProvider(d.id!).subscribe({
      next: () => { this.draft.set(null); this.current.set(null); this.load(); },
      error: e => this.error.set(e.error?.detail ?? 'Delete failed'),
    });
  }
}
