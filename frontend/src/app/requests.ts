import { Component, OnInit, computed, inject } from '@angular/core';
import { toSignal } from '@angular/core/rxjs-interop';
import { ActivatedRoute, NavigationEnd, Router, RouterLink, RouterLinkActive, RouterOutlet } from '@angular/router';
import { filter, map } from 'rxjs';
import { Api } from './api.service';
import { Icon } from './icon';
import { Workspace } from './workspace';

type Kind = 'approvals' | 'skills' | 'profile' | 'requests';

export const REQUEST_SECTIONS: { path: string; label: string; icon: string; count: Kind; param: string }[] = [
  { path: 'approvals', label: 'Tool approvals', icon: 'tools', count: 'approvals', param: 'approval' },
  { path: 'skills', label: 'Skill proposals', icon: 'skills', count: 'skills', param: 'skill' },
  { path: 'profile', label: 'Profile updates', icon: 'profile', count: 'profile', param: 'suggestion' },
  { path: 'subagents', label: 'Sub-agent requests', icon: 'subagents', count: 'requests', param: 'id' },
];

/** Requests: like Settings, a sub-menu of request types (with what's waiting) and the selected one's page. */
@Component({
  selector: 'hq-requests-layout',
  imports: [RouterOutlet, RouterLink, RouterLinkActive, Icon],
  template: `
    <div class="settings">
      <header class="crumbs">
        <a routerLink="/requests">Requests</a>
        @if (current(); as c) { <hq-icon name="chevron" [size]="15" /><strong>{{ c.label }}</strong> }
      </header>
      <div class="settings-body">
        <nav class="settings-nav">
          @for (s of sections; track s.path) {
            <a class="nav-item" [routerLink]="['/requests', s.path]" routerLinkActive="on">
              <hq-icon [name]="s.icon" [size]="17" /><span>{{ s.label }}</span>
              @if (ws.inbox()[s.count]) { <span class="count-badge">{{ ws.inbox()[s.count] }}</span> }</a>
          }
        </nav>
        <div class="settings-content"><router-outlet /></div>
      </div>
    </div>
  `,
})
export class RequestsLayout {
  private router = inject(Router);
  ws = inject(Workspace);
  sections = REQUEST_SECTIONS;
  private url = toSignal(this.router.events.pipe(
    filter(e => e instanceof NavigationEnd), map(() => this.router.url)), { initialValue: this.router.url });
  current = computed(() => this.sections.find(s => this.url().startsWith(`/requests/${s.path}`)));
}

/**
 * /requests on its own: go to the first kind with something waiting (tool approvals
 * first). Old links (/requests?approval=…, ?skill=…, ?id=…, as sent on MQTT) go to the
 * right page.
 */
@Component({ selector: 'hq-requests-home', template: '' })
export class RequestsHome implements OnInit {
  private api = inject(Api);
  private route = inject(ActivatedRoute);
  private router = inject(Router);

  ngOnInit() {
    const q = this.route.snapshot.queryParamMap;
    const linked = REQUEST_SECTIONS.find(s => q.get(s.param));
    if (linked) {
      this.go(linked.path, { [linked.param]: q.get(linked.param) });
      return;
    }
    this.api.inboxCount().subscribe({
      next: c => this.go((REQUEST_SECTIONS.find(s => c[s.count] > 0) ?? REQUEST_SECTIONS[0]).path),
      error: () => this.go(REQUEST_SECTIONS[0].path),
    });
  }

  private go(path: string, queryParams: Record<string, string | null> = {}) {
    this.router.navigate(['/requests', path], { queryParams, replaceUrl: true });
  }
}
