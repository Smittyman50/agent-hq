import { Component, effect, inject, input, model, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { Api, ModelInfo, ProviderInfo } from './api.service';

let nextId = 0;

/** Provider dropdown + model field filled from that provider's live model list. */
@Component({
  selector: 'hq-model-picker',
  imports: [FormsModule],
  template: `
    <div class="inline model-row">
      <label>{{ label() }} provider
        <select [ngModel]="providerId()" (ngModelChange)="setProvider($event)">
          @if (optional()) { <option [ngValue]="null">{{ noneLabel() }}</option> }
          @for (p of providers(); track p.id) {
            <option [ngValue]="p.id">{{ p.name }}{{ p.enabled ? '' : ' (disabled)' }}</option>
          }
          @if (!optional()) { <option [ngValue]="null">Custom model spec (advanced)</option> }
        </select></label>
      <label>{{ label() }}
        @if (providerId()) {
          <small>{{ status() }} <a href="" (click)="$event.preventDefault(); load()">refresh</a></small>
        } @else if (!optional()) { <small>a PydanticAI spec, e.g. openai:gpt-4.1</small> }
        @if (useList()) {
          <select [ngModel]="modelName()" (ngModelChange)="pick($event)" [class.needs]="!modelName()">
            <option value="" disabled>Choose a model…</option>
            @for (m of models(); track m.id) { <option [value]="m.id">{{ m.id }}</option> }
            <option [value]="OTHER">Other (type a name)…</option>
          </select>
        } @else {
          <input [ngModel]="modelName()" (ngModelChange)="modelName.set($event)" [attr.list]="listId"
            [disabled]="optional() && !providerId() && !modelName()" [class.needs]="providerId() && !modelName()"
            [placeholder]="providerId() ? 'type a model name' : (optional() ? '' : 'provider:model')">
        }</label>
    </div>
    @if (providerId() && !modelName()) {
      <p class="hint inline-hint needs-hint">Choose a {{ label().toLowerCase() }} for this provider before saving.</p>
    } @else if (details(); as d) { <p class="hint inline-hint">{{ d }}</p> }
    <datalist [id]="listId">
      @for (m of models(); track m.id) { <option [value]="m.id">{{ m.details }}</option> }
    </datalist>
  `,
})
export class ModelPicker {
  private api = inject(Api);
  label = input('Model');
  providers = input<ProviderInfo[]>([]);
  /** Optional pickers offer a "none" choice (e.g. no vision model). */
  optional = input(false);
  noneLabel = input('None');
  providerId = model<string | null>(null);
  modelName = model<string>('');

  listId = `models-${nextId++}`;
  models = signal<ModelInfo[]>([]);
  status = signal('');
  readonly OTHER = '__other__';
  /** Typing a name instead of choosing from the list (e.g. a model the server doesn't list). */
  typing = signal(false);

  /** A dropdown when the provider listed its models and the current name is one of them. */
  useList() {
    const name = this.modelName();
    return !!this.providerId() && !this.typing() && this.models().length > 0 &&
      (!name || this.models().some(m => m.id === name));
  }

  pick(value: string) {
    if (value === this.OTHER) { this.typing.set(true); return; }
    this.modelName.set(value);
  }

  constructor() {
    effect(() => { this.providerId(); this.load(); });
  }

  setProvider(id: string | null) {
    if (id !== this.providerId()) { this.modelName.set(''); this.typing.set(false); }
    this.providerId.set(id);
  }

  details() {
    return this.providerId() ? this.models().find(m => m.id === this.modelName())?.details ?? '' : '';
  }

  load() {
    const id = this.providerId();
    this.models.set([]);
    if (!id) { this.status.set(''); return; }
    this.status.set('loading models…');
    this.api.providerModels(id).subscribe({
      next: m => { this.models.set(m); this.status.set(`${m.length} available`); },
      error: e => this.status.set(e.error?.detail ?? 'could not reach provider'),
    });
  }
}
