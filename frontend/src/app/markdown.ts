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
  return DOMPurify.sanitize(html, {
    USE_PROFILES: { html: true },
    FORBID_TAGS: ['style', 'form', 'input', 'button', 'textarea', 'select', 'iframe', 'object', 'embed', 'img'],
    ALLOWED_URI_REGEXP: /^(?:https?:|mailto:|#)/i,
  });
}

@Component({
  selector: 'hq-md',
  template: `<div class="md" [innerHTML]="html()"></div>`,
})
export class Markdown {
  private sanitizer = inject(DomSanitizer);
  text = input<string>('');
  // Already sanitized by DOMPurify above; Angular's own sanitizer would also strip the
  // target/rel attributes on links, so it's told to trust this output.
  html = computed<SafeHtml>(() => this.sanitizer.bypassSecurityTrustHtml(renderMarkdown(this.text())));
}
