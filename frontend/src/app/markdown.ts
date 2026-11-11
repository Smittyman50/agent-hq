import { Component, computed, inject, input } from '@angular/core';
import { DomSanitizer, SafeHtml } from '@angular/platform-browser';
import DOMPurify from 'dompurify';
import { marked } from 'marked';

// Links in agent replies open in a new tab, without giving that page access to this one.
DOMPurify.addHook('afterSanitizeAttributes', node => {
  if (node.tagName === 'A' && node.getAttribute('href')) {
    node.setAttribute('target', '_blank');
    node.setAttribute('rel', 'noopener noreferrer nofollow');
  }
});

/**
 * Markdown as HTML (GitHub-flavoured: tables, lists, code, links), always sanitized:
 * agent replies can contain text copied from web pages, so no scripts, styles, forms or
 * event handlers ever get through, only plain formatting and http(s)/mailto links.
 */
export function renderMarkdown(text: string): string {
  const html = marked.parse(text ?? '', { gfm: true, breaks: true, async: false }) as string;
  const clean = DOMPurify.sanitize(html, {
    USE_PROFILES: { html: true },
    FORBID_TAGS: ['style', 'form', 'input', 'button', 'textarea', 'select', 'iframe', 'object', 'embed', 'img'],
    ALLOWED_URI_REGEXP: /^(?:https?:|mailto:|#)/i,
  });
  return addCodeBars(clean);
}

// ------------------------------------------------------------------ code blocks

/** File extensions for the languages agents tag their code blocks with. */
const EXT: Record<string, string> = {
  python: 'py', py: 'py', javascript: 'js', js: 'js', typescript: 'ts', ts: 'ts', tsx: 'tsx', jsx: 'jsx',
  bash: 'sh', sh: 'sh', shell: 'sh', zsh: 'sh', powershell: 'ps1', ps1: 'ps1', pwsh: 'ps1', bat: 'bat', cmd: 'bat',
  json: 'json', yaml: 'yml', yml: 'yml', toml: 'toml', ini: 'ini', xml: 'xml', html: 'html', css: 'css', scss: 'scss',
  sql: 'sql', go: 'go', rust: 'rs', rs: 'rs', java: 'java', kotlin: 'kt', c: 'c', cpp: 'cpp', 'c++': 'cpp', csharp: 'cs',
  cs: 'cs', ruby: 'rb', php: 'php', lua: 'lua', markdown: 'md', md: 'md', csv: 'csv', nginx: 'conf', conf: 'conf',
  caddyfile: 'Caddyfile', dockerfile: 'Dockerfile', makefile: 'Makefile', angular: 'ts', text: 'txt', plaintext: 'txt',
};
const SPECIAL = new Set(['Caddyfile', 'Dockerfile', 'Makefile']);
const FILE_RE = /^[\w.\-/]+\.[A-Za-z0-9]{1,8}$|^(?:Dockerfile|Makefile|Caddyfile)$/;

const COPY_ICON = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>';
const SAVE_ICON = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m7 10 5 5 5-5"/><path d="M12 15V3"/></svg>';

/**
 * Give each code block a bar with its language and Copy / Save buttons. Runs on the
 * already-sanitized HTML; the bar is our own markup. The file name for Save comes from
 * the reply itself when it names one (`dungeon_rpg.py` just before the block), or from
 * a first-line comment like "# file: x.py", else "snippet.<ext>".
 */
function addCodeBars(html: string): string {
  if (!html.includes('<pre')) return html;
  const root = document.createElement('div');
  root.innerHTML = html;
  const named: string[] = [];  // file names mentioned so far, in order
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT);
  const blocks: { pre: HTMLElement; mentioned: string[] }[] = [];
  for (let n = walker.nextNode() as HTMLElement | null; n; n = walker.nextNode() as HTMLElement | null) {
    if (n.tagName === 'PRE') { blocks.push({ pre: n, mentioned: [...named] }); continue; }
    if (n.tagName === 'CODE' && n.parentElement?.tagName !== 'PRE') {
      const t = (n.textContent ?? '').trim();
      if (FILE_RE.test(t)) named.push(t.split('/').pop()!);
    }
  }
  blocks.forEach(({ pre, mentioned }, i) => {
    const code = pre.querySelector('code');
    const lang = ((code?.className ?? '').match(/language-([\w+#-]+)/)?.[1] ?? '').toLowerCase();
    const name = fileName(lang, code?.textContent ?? '', mentioned, i);
    const wrap = document.createElement('div');
    wrap.className = 'code-block';
    const bar = document.createElement('div');
    bar.className = 'code-bar';
    const label = document.createElement('span');
    label.className = 'code-lang';
    label.textContent = name.startsWith('snippet') ? (lang || 'code') : name;
    bar.appendChild(label);
    for (const [act, icon, text] of [['copy', COPY_ICON, 'Copy'], ['save', SAVE_ICON, 'Save']]) {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'code-btn';
      b.dataset['codeAction'] = act;
      if (act === 'save') { b.dataset['fileName'] = name; b.title = `Save as ${name}`; }
      else b.title = 'Copy code';
      b.innerHTML = `${icon}<span>${text}</span>`;
      bar.appendChild(b);
    }
    pre.replaceWith(wrap);
    wrap.append(bar, pre);
  });
  return root.innerHTML;
}

function fileName(lang: string, code: string, mentioned: string[], index: number): string {
  const ext = EXT[lang] ?? (lang && /^[a-z0-9]{1,6}$/.test(lang) ? lang : 'txt');
  const first = code.split('\n').slice(0, 2).join('\n');
  const header = first.match(/(?:#|\/\/|--|;|<!--)\s*(?:file(?:name)?|path)\s*[:=]\s*([\w.\-/]+\.\w+)/i);
  if (header) return header[1].split('/').pop()!;
  const fits = (n: string) => SPECIAL.has(ext) ? n === ext : n.toLowerCase().endsWith('.' + ext.toLowerCase());
  const match = [...mentioned].reverse().find(fits);
  if (match) return match;
  if (SPECIAL.has(ext)) return ext;
  return `snippet${index ? '-' + (index + 1) : ''}.${ext}`;
}

@Component({
  selector: 'hq-md',
  template: `<div class="md" [innerHTML]="html()" (click)="onClick($event)"></div>`,
})
export class Markdown {
  private sanitizer = inject(DomSanitizer);
  text = input<string>('');
  // Already sanitized by DOMPurify above; Angular's own sanitizer would also strip the
  // target/rel attributes on links (and the code bars' buttons), so it's told to trust it.
  html = computed<SafeHtml>(() => this.sanitizer.bypassSecurityTrustHtml(renderMarkdown(this.text())));

  onClick(e: MouseEvent) {
    const btn = (e.target as HTMLElement).closest<HTMLButtonElement>('button[data-code-action]');
    if (!btn) return;
    const code = btn.closest('.code-block')?.querySelector('pre code, pre')?.textContent ?? '';
    if (btn.dataset['codeAction'] === 'copy') {
      copyText(code).then(ok => flash(btn, ok ? 'Copied' : 'Copy failed'));
    } else {
      const name = btn.dataset['fileName'] || 'snippet.txt';
      const url = URL.createObjectURL(new Blob([code], { type: 'text/plain;charset=utf-8' }));
      const a = document.createElement('a');
      a.href = url; a.download = name;
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      flash(btn, 'Saved');
    }
  }
}

/** Clipboard API where allowed (HTTPS), else the old execCommand route. */
async function copyText(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard && window.isSecureContext) { await navigator.clipboard.writeText(text); return true; }
  } catch { /* fall through */ }
  const ta = document.createElement('textarea');
  ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
  document.body.appendChild(ta); ta.select();
  let ok = false;
  try { ok = document.execCommand('copy'); } catch { ok = false; }
  ta.remove();
  return ok;
}

function flash(btn: HTMLElement, text: string) {
  const span = btn.querySelector('span');
  if (!span) return;
  const was = btn.dataset['codeAction'] === 'copy' ? 'Copy' : 'Save';
  span.textContent = text;
  btn.classList.add('done');
  setTimeout(() => { span.textContent = was; btn.classList.remove('done'); }, 1500);
}
