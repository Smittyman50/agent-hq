import { Component, computed, inject } from '@angular/core';
import { toSignal } from '@angular/core/rxjs-interop';
import { NavigationEnd, Router, RouterLink, RouterLinkActive, RouterOutlet } from '@angular/router';
import { filter, map } from 'rxjs';
import { Icon } from './icon';

export const SETTINGS_SECTIONS = [
  { path: 'agents', label: 'Agents', icon: 'agents' },
  { path: 'models', label: 'Model providers', icon: 'models' },
  { path: 'subagents', label: 'Sub-agents', icon: 'subagents' },
  { path: 'usage', label: 'Usage', icon: 'usage' },
  { path: 'memory', label: 'Memory', icon: 'memory' },
  { path: 'tools', label: 'Tools', icon: 'tools' },
  { path: 'events', label: 'Events', icon: 'events' },
  { path: 'system', label: 'System', icon: 'system' },
];

/** Settings: a breadcrumb, the list of sections, and the selected section's page. */
@Component({
  selector: 'hq-settings',
  imports: [RouterOutlet, RouterLink, RouterLinkActive, Icon],
  template: `
    <div class="settings">
      <header class="crumbs">
        <a routerLink="/settings">Settings</a>
        <hq-icon name="chevron" [size]="15" />
        <strong>{{ current()?.label }}</strong>
      </header>
      <div class="settings-body">
        <nav class="settings-nav">
          @for (s of sections; track s.path) {
            <a class="nav-item" [routerLink]="['/settings', s.path]" routerLinkActive="on">
              <hq-icon [name]="s.icon" [size]="17" /><span>{{ s.label }}</span></a>
          }
        </nav>
        <div class="settings-content"><router-outlet /></div>
      </div>
    </div>
  `,
})
export class SettingsLayout {
  private router = inject(Router);
  sections = SETTINGS_SECTIONS;
  private url = toSignal(this.router.events.pipe(
    filter(e => e instanceof NavigationEnd), map(() => this.router.url)), { initialValue: this.router.url });
  current = computed(() => this.sections.find(s => this.url().startsWith(`/settings/${s.path}`)));
}
