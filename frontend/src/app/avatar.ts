import { Component, Injectable, computed, inject, input, signal } from '@angular/core';
import { AgentDef } from './api.service';

export type AnimateMode = 'always' | 'active' | 'never';

/** When animated avatars move. Remembered per browser. */
@Injectable({ providedIn: 'root' })
export class AvatarSettings {
  private reduced = typeof matchMedia !== 'undefined' && matchMedia('(prefers-reduced-motion: reduce)').matches;
  mode = signal<AnimateMode>(this.load());

  private load(): AnimateMode {
    try {
      const v = localStorage.getItem('ahq.avatarAnimate');
      if (v === 'always' || v === 'active' || v === 'never') return v;
    } catch { /* storage unavailable */ }
    return this.reduced ? 'never' : 'always';
  }

  set(mode: AnimateMode) {
    this.mode.set(mode);
    try { localStorage.setItem('ahq.avatarAnimate', mode); } catch { /* ignore */ }
  }

  shouldAnimate(active: boolean) {
    const m = this.mode();
    return m === 'always' || (m === 'active' && active);
  }
}

/** An agent's avatar, or its initials on a colour derived from its name. Hover animates. */
@Component({
  selector: 'hq-avatar',
  template: `
    <span class="avatar" [class.active]="active()" [style.width.px]="size()" [style.height.px]="size()"
          [title]="agent()?.name ?? ''" (mouseenter)="hover.set(true)" (mouseleave)="hover.set(false)">
      @if (src(); as s) {
        <img [src]="s" alt="" draggable="false">
      } @else {
        <span class="initials" [style.background]="color()" [style.font-size.px]="size() * 0.42">{{ initials() }}</span>
      }
    </span>
  `,
})
export class Avatar {
  private settings = inject(AvatarSettings);
  agent = input<AgentDef | undefined | null>(null);
  size = input(32);
  /** The agent is working right now (used by the "while working" animation mode). */
  active = input(false);
  hover = signal(false);

  src = computed(() => {
    const a = this.agent();
    if (!a?.id || !a.avatar_version) return null;
    const animate = !a.avatar_animated || this.hover() || this.settings.shouldAnimate(this.active());
    return `/api/agents/${a.id}/avatar${animate ? '' : '/still'}?v=${a.avatar_version}`;
  });

  initials = computed(() => {
    const words = (this.agent()?.name ?? '?').trim().split(/\s+/);
    return ((words[0]?.[0] ?? '?') + (words.length > 1 ? words[words.length - 1][0] : '')).toUpperCase();
  });

  color = computed(() => {
    let h = 0;
    for (const ch of this.agent()?.name ?? '') h = (h * 31 + ch.charCodeAt(0)) % 360;
    return `hsl(${h} 45% 38%)`;
  });
}
