import { Component, computed, input } from '@angular/core';
import { DomSanitizer } from '@angular/platform-browser';
import { inject } from '@angular/core';

/**
 * Small line icons (24×24 grid, drawn with the current text color). Path data from
 * Lucide (ISC license, https://lucide.dev), plus Agent HQ's own logo mark.
 */
const ICONS: Record<string, string> = {
  // Agent HQ's mark (also public/favicon.svg): a blue team of three around a red hub.
  logo: '<g style="stroke:#5aa9e6"><circle cx="12" cy="5" r="2.2"/><circle cx="5" cy="18" r="2.2"/>'
      + '<circle cx="19" cy="18" r="2.2"/><path d="M11 7 6 16"/><path d="m13 7 5 9"/><path d="M7.2 18h9.6"/></g>'
      + '<g style="stroke:#e5534b"><path d="M12 7.2v4.4"/><path d="m6.9 16.9 3.6-2.3"/><path d="m17.1 16.9-3.6-2.3"/></g>'
      + '<circle cx="12" cy="13.4" r="1.9" style="stroke:#e5534b;fill:#e5534b"/>',
  chat: '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>',
  plus: '<path d="M5 12h14"/><path d="M12 5v14"/>',
  search: '<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>',
  settings: '<path d="M20 7h-9"/><path d="M14 17H5"/><circle cx="17" cy="17" r="3"/><circle cx="7" cy="7" r="3"/>',
  agents: '<path d="M12 8V4H8"/><rect width="16" height="12" x="4" y="8" rx="2"/><path d="M2 14h2"/>'
        + '<path d="M20 14h2"/><path d="M15 13v2"/><path d="M9 13v2"/>',
  models: '<rect width="16" height="16" x="4" y="4" rx="2"/><rect width="6" height="6" x="9" y="9" rx="1"/>'
        + '<path d="M15 2v2"/><path d="M15 20v2"/><path d="M2 15h2"/><path d="M2 9h2"/><path d="M20 15h2"/>'
        + '<path d="M20 9h2"/><path d="M9 2v2"/><path d="M9 20v2"/>',
  memory: '<ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5V19A9 3 0 0 0 21 19V5"/><path d="M3 12A9 3 0 0 0 21 12"/>',
  tools: '<path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91'
       + 'a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/>',
  events: '<path d="M4.9 19.1C1 15.2 1 8.8 4.9 4.9"/><path d="M7.8 16.2c-2.3-2.3-2.3-6.1 0-8.5"/>'
        + '<circle cx="12" cy="12" r="2"/><path d="M16.2 7.8c2.3 2.3 2.3 6.1 0 8.5"/><path d="M19.1 4.9C23 8.8 23 15.1 19.1 19"/>',
  system: '<rect width="20" height="8" x="2" y="2" rx="2"/><rect width="20" height="8" x="2" y="14" rx="2"/>'
        + '<path d="M6 6h.01"/><path d="M6 18h.01"/>',
  schedule: '<path d="M21 7.5V6a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h3.5"/><path d="M16 2v4"/>'
          + '<path d="M8 2v4"/><path d="M3 10h5"/><path d="M17.5 17.5 16 16.3V14"/><circle cx="16" cy="16" r="6"/>',
  panel: '<rect width="18" height="18" x="3" y="3" rx="2"/><path d="M9 3v18"/>',
  chevron: '<path d="m9 18 6-6-6-6"/>',
  monitor: '<rect width="20" height="14" x="2" y="3" rx="2"/><path d="M8 21h8"/><path d="M12 17v4"/>',
};

@Component({
  selector: 'hq-icon',
  template: `<svg [attr.width]="size()" [attr.height]="size()" viewBox="0 0 24 24" fill="none"
    stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"
    aria-hidden="true" [innerHTML]="svg()"></svg>`,
  styles: [':host { display: inline-flex; flex: none; } svg { display: block; }'],
})
export class Icon {
  private sanitizer = inject(DomSanitizer);
  name = input.required<string>();
  size = input(18);
  svg = computed(() => this.sanitizer.bypassSecurityTrustHtml(ICONS[this.name()] ?? ''));
}
