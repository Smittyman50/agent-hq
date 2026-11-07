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
        <input [ngModel]="modelName()" (ngModelChange)="modelName.set($event)" [attr.list]="listId"
          [disabled]="optional() && !providerId() && !modelName()"
          [placeholder]="providerId() ? 'pick or type a model name' : (optional() ? '' : 'provider:model')"></label>
    </div>
    @if (details(); as d) { <p class="hint inline-hint">{{ d }}</p> }
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

  constructor() {
    effect(() => { this.providerId(); this.load(); });
  }

  setProvider(id: string | null) {
    if (id !== this.providerId()) this.modelName.set('');
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
