import { Component, input, output } from '@angular/core';
import { Attachment, attachmentUrl } from './api.service';
import { Icon } from './icon';

export function fileSize(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(n < 10240 ? 1 : 0)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

/** Files on a message (or waiting in the composer): image thumbnails and file chips. */
@Component({
  selector: 'hq-files',
  imports: [Icon],
  template: `
  <div class="files" [class.compact]="removable()">
    @for (f of files(); track f.id) {
      @if (f.kind === 'image') {
        <a class="file-thumb" [href]="url(f.id)" target="_blank" rel="noopener" [title]="f.name + ' · ' + size(f.size)">
          <img [src]="url(f.id)" [alt]="f.name" loading="lazy">
          @if (removable()) { <button type="button" class="file-x" title="Remove" (click)="drop($event, f)"><hq-icon name="x" [size]="12" /></button> }
        </a>
      } @else {
        <a class="file-chip" [href]="url(f.id, true)" [title]="'Download ' + f.name">
          <hq-icon name="file" [size]="15" />
          <span class="file-name">{{ f.name }}</span>
          <span class="muted file-size">{{ size(f.size) }}@if (f.source === 'workbench') { · workbench }</span>
          @if (removable()) {
            <button type="button" class="file-x" title="Remove" (click)="drop($event, f)"><hq-icon name="x" [size]="12" /></button>
          } @else { <hq-icon name="download" [size]="13" class="muted" /> }
        </a>
      }
    }
  </div>`,
})
export class Files {
  files = input.required<Attachment[]>();
  removable = input(false);
  remove = output<Attachment>();
  url = attachmentUrl;
  size = fileSize;
  drop(e: Event, f: Attachment) { e.preventDefault(); e.stopPropagation(); this.remove.emit(f); }
}
