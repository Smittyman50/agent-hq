import { Component, computed, effect, inject, input, output, signal } from '@angular/core';
import { Api, Attachment, attachmentUrl } from './api.service';
import { Icon } from './icon';
import { copyText } from './markdown';

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
          <span class="muted file-size">{{ size(f.size) }}@if (f.source === 'workbench' || f.source === 'desktop') { · {{ f.source }} }</span>
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

// ------------------------------------------------------------------ outputs

/** Extensions viewable as text in Outputs (code, config, data, notes), with their language label. */
const TEXT_EXT: Record<string, string> = {
  py: 'python', js: 'javascript', mjs: 'javascript', cjs: 'javascript', ts: 'typescript', tsx: 'tsx', jsx: 'jsx',
  sh: 'bash', bash: 'bash', zsh: 'bash', ps1: 'powershell', bat: 'batch', cmd: 'batch',
  json: 'json', jsonl: 'json', yml: 'yaml', yaml: 'yaml', toml: 'toml', ini: 'ini', cfg: 'ini', conf: 'conf', env: 'env',
  xml: 'xml', html: 'html', htm: 'html', css: 'css', scss: 'scss', svg: 'svg',
  sql: 'sql', go: 'go', rs: 'rust', java: 'java', kt: 'kotlin', c: 'c', h: 'c', cpp: 'cpp', hpp: 'cpp', cs: 'csharp',
  rb: 'ruby', php: 'php', lua: 'lua', r: 'r', pl: 'perl', swift: 'swift', dart: 'dart',
  md: 'markdown', markdown: 'markdown', txt: 'text', log: 'log', csv: 'csv', tsv: 'tsv', rst: 'rst',
  ino: 'arduino', kicad_sch: 'kicad', kicad_pcb: 'kicad', gcode: 'gcode', service: 'systemd', tf: 'terraform',
};
const TEXT_NAMES = new Set(['dockerfile', 'makefile', 'caddyfile', 'readme', 'license', '.gitignore', '.env']);
const ARCHIVE_EXT = new Set(['zip', 'tar', 'gz', 'tgz', 'bz2', 'xz', '7z', 'rar', 'zst']);
/** Bigger text files aren't previewed (download them instead). */
export const PREVIEW_LIMIT = 512 * 1024;

function ext(name: string): string {
  const i = name.lastIndexOf('.');
  return i > 0 ? name.slice(i + 1).toLowerCase() : '';
}

/** A file whose contents read as text, so Outputs can show it. */
export function isViewable(f: Attachment): boolean {
  if (f.kind === 'image' || isArchive(f)) return false;
  if (f.kind === 'text') return true;
  const e = ext(f.name);
  if (e in TEXT_EXT || TEXT_NAMES.has(f.name.toLowerCase())) return true;
  const m = (f.mime || '').toLowerCase();
  return m.startsWith('text/') || /\b(json|xml|yaml|javascript|x-sh|x-python|toml|sql)\b/.test(m);
}

export function isArchive(f: Attachment): boolean {
  const n = f.name.toLowerCase();
  return ARCHIVE_EXT.has(ext(n)) || /\.tar\.\w+$/.test(n) || /zip|x-tar|x-7z|x-rar|gzip/.test(f.mime || '');
}

export function languageOf(f: Attachment): string {
  return TEXT_EXT[ext(f.name)] ?? (TEXT_NAMES.has(f.name.toLowerCase()) ? f.name.toLowerCase() : 'text');
}

/** A text file an agent made, opened in the Outputs panel with Copy and Download. */
@Component({
  selector: 'hq-file-view',
  imports: [Icon],
  template: `
  <div class="file-view" [class.expanded]="expanded()">
    <div class="file-view-bar">
      <hq-icon name="file" [size]="14" class="muted" />
      <span class="file-view-name" [title]="file().name">{{ file().name }}</span>
      <span class="muted file-view-meta">{{ lang() }} · {{ size(file().size) }}@if (lines()) { · {{ lines() }} lines }</span>
      <span class="spacer"></span>
      @if (text() !== null) {
        <button type="button" class="code-btn" [class.done]="copied()" (click)="copy()" title="Copy contents">
          <hq-icon name="copy" [size]="13" /><span>{{ copied() ? 'Copied' : 'Copy' }}</span></button>
      }
      <a class="code-btn" [href]="url(file().id, true)" [title]="'Download ' + file().name">
        <hq-icon name="download" [size]="13" /><span>Download</span></a>
    </div>
    @if (tooBig()) {
      <div class="file-view-note muted">Too large to preview here. Download it instead.</div>
    } @else if (error()) {
      <div class="file-view-note err">{{ error() }}</div>
    } @else if (text() === null) {
      <div class="file-view-note muted">Loading…</div>
    } @else {
      <pre><code>{{ text() }}</code></pre>
      @if (long()) {
        <button type="button" class="file-view-more" (click)="expanded.set(!expanded())">
          {{ expanded() ? 'Show less' : 'Show all ' + lines() + ' lines' }}</button>
      }
    }
  </div>`,
})
export class FileView {
  private api = inject(Api);
  file = input.required<Attachment>();
  text = signal<string | null>(null);
  error = signal('');
  copied = signal(false);
  expanded = signal(false);
  url = attachmentUrl;
  size = fileSize;
  tooBig = computed(() => this.file().size > PREVIEW_LIMIT);
  lang = computed(() => languageOf(this.file()));
  lines = computed(() => { const t = this.text(); return t === null ? 0 : t.replace(/\n$/, '').split('\n').length; });
  long = computed(() => this.lines() > 40);

  constructor() {
    effect(onCleanup => {
      const f = this.file();
      this.text.set(null); this.error.set(''); this.expanded.set(false);
      if (f.size > PREVIEW_LIMIT) return;
      const sub = this.api.attachmentText(f.id).subscribe({
        next: t => this.text.set(t),
        error: () => this.error.set('Could not load this file. Use Download instead.'),
      });
      onCleanup(() => sub.unsubscribe());
    });
  }

  copy() {
    copyText(this.text() ?? '').then(ok => {
      if (!ok) return;
      this.copied.set(true);
      setTimeout(() => this.copied.set(false), 1500);
    });
  }
}
