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
          <span class="sub">{{ p.base_url || 'default endpoint' }}</span>
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
        <label>Base URL <small>{{ kindInfo(d.kind).urlHint }}</small>
          <input name="url" [(ngModel)]="d.base_url" [placeholder]="kindInfo(d.kind).url"></label>
        <label>API key <small>{{ kindInfo(d.kind).keyHint }}</small>
          <input name="key" type="password" autocomplete="new-password" [(ngModel)]="d.api_key"
            [placeholder]="current()?.has_api_key ? 'stored (leave blank to keep)' : ''"></label>
        @if (current()?.has_api_key) {
          <p class="hint inline-hint">A key is stored. <a href="" (click)="$event.preventDefault(); clearKey()">Remove it</a></p>
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

  ngOnInit() { this.load(); }
  load() { this.api.providers().subscribe(p => this.providers.set(p)); }
  kindInfo(k: ProviderKind) { return KINDS.find(x => x.kind === k)!; }
  kindLabel(k: ProviderKind) { return this.kindInfo(k).label.split(' (')[0]; }

  edit(p: ProviderInfo | null) {
    this.error.set(''); this.testing.set(''); this.models.set([]);
    this.current.set(p);
    this.draft.set(p ? { id: p.id, name: p.name, kind: p.kind, base_url: p.base_url, enabled: p.enabled, api_key: '' }
                     : { name: '', kind: 'ollama', base_url: '', enabled: true, api_key: '' });
  }

  save() {
    this.api.saveProvider(this.draft()!).subscribe({
      next: p => { this.load(); this.edit(p); this.test(); },
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
