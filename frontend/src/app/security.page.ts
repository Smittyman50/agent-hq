import { Component, OnInit, inject, signal } from '@angular/core';
import { DatePipe } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { HttpClient } from '@angular/common/http';
import { DomSanitizer, SafeHtml } from '@angular/platform-browser';
import { Session } from './session';

interface SessionRow { id: string; ip: string; user_agent: string; created_at: string; last_seen_at: string; expires_at: string; current: boolean; }
interface TokenRow { id: string; name: string; prefix: string; created_at: string; last_used_at: string | null; expires_at: string | null; }
interface EventRow { id: string; at: string; kind: string; username: string; ip: string; user_agent: string; detail: string; }

/** Your account: password, two-factor sign-in, signed-in browsers, API tokens, sign-in log. */
@Component({
  selector: 'hq-security',
  imports: [FormsModule, DatePipe],
  template: `
  <div class="editor wide">
    <h2>Security</h2>

    <h3>Account</h3>
    <p class="muted small">Signed in as <b>{{ session.user()?.username }}</b> · password changed
      {{ session.user()?.password_changed_at | date:'MMM d, y' }}</p>
    <form class="narrow-form" (ngSubmit)="changePassword()">
      <div class="limit-grid">
        <label>Current password<input type="password" name="cur" [(ngModel)]="pw.current" autocomplete="current-password"></label>
        <label>New password<input type="password" name="new" [(ngModel)]="pw.next" autocomplete="new-password"></label>
        <label>Confirm<input type="password" name="conf" [(ngModel)]="pw.confirm" autocomplete="new-password"></label>
      </div>
      @if (msg['pw']) { <p class="hint">{{ msg['pw'] }}</p> }
      <div class="actions"><button type="submit" [disabled]="!pw.current || !pw.next">Change password</button></div>
    </form>
    <p class="muted small">Changing it signs out your other browsers. Locked out?
      <code>docker compose exec hq python -m app.auth set-password {{ session.user()?.username }}</code></p>

    <h3>Two-factor sign-in</h3>
    @if (session.user()?.totp_enabled) {
      <p><span class="pill done">On</span> Sign-in asks for a code from your authenticator app.</p>
      <div class="limit-grid">
        <label>Password<input type="password" name="dpw" [(ngModel)]="off.password"></label>
        <label>Current code<input name="dcode" [(ngModel)]="off.code" inputmode="numeric"></label>
      </div>
      <div class="actions"><button class="danger" (click)="disable2fa()" [disabled]="!off.password || !off.code">Turn off</button></div>
    } @else if (totp(); as t) {
      <p>Scan this with an authenticator app (Aegis, 2FAS, Google Authenticator, 1Password…), then enter the code it shows.</p>
      <div class="totp-setup">
        <div class="qr" [innerHTML]="t.qr"></div>
        <div>
          <p class="muted small">Can't scan? Enter this key:<br><code class="key">{{ t.secret }}</code></p>
          <label>Code<input name="code" [(ngModel)]="code" inputmode="numeric" autocomplete="one-time-code"></label>
          <div class="actions"><button (click)="enable2fa()" [disabled]="!code">Turn on</button>
            <button class="ghost" (click)="totp.set(null)">Cancel</button></div>
        </div>
      </div>
    } @else {
      <p class="muted">Off. Add a second step so a leaked password isn't enough.</p>
      <div class="actions"><button (click)="setup2fa()">Set up</button></div>
    }
    @if (msg['2fa']) { <p class="hint">{{ msg['2fa'] }}</p> }

    <h3>Signed-in browsers</h3>
    <table class="grid">
      <tr><th>Browser</th><th>Address</th><th>Signed in</th><th>Last active</th><th></th></tr>
      @for (s of sessions(); track s.id) {
        <tr><td>{{ browser(s.user_agent) }} @if (s.current) { <span class="pill done">this one</span> }</td>
          <td>{{ s.ip }}</td><td>{{ s.created_at | date:'MMM d, h:mm a' }}</td><td>{{ s.last_seen_at | date:'MMM d, h:mm a' }}</td>
          <td>@if (!s.current) { <button class="ghost small" (click)="endSession(s.id)">Sign out</button> }</td></tr>
      }
    </table>
    @if (sessions().length > 1) {
      <div class="actions"><button class="ghost" (click)="endOthers()">Sign out all other browsers</button></div>
    }

    <h3>API tokens</h3>
    <p class="hint inline-hint">For scripts, Home Assistant or curl: <code>Authorization: Bearer ahq_…</code>.
      A token can use the API but can't change your password, 2FA or tokens.</p>
    @if (newToken(); as nt) {
      <div class="token-once">
        <p><b>Copy “{{ nt.name }}” now</b>; it won't be shown again.</p>
        <code class="key">{{ nt.token }}</code>
        <div class="actions"><button (click)="copy(nt.token)">{{ copied() ? 'Copied' : 'Copy' }}</button>
          <button class="ghost" (click)="newToken.set(null)">Done</button></div>
      </div>
    }
    <form class="inline token-form" (ngSubmit)="makeToken()">
      <input name="tname" [(ngModel)]="tokenName" placeholder="Name, e.g. Home Assistant">
      <select name="tdays" [(ngModel)]="tokenDays">
        <option [ngValue]="30">30 days</option><option [ngValue]="90">90 days</option>
        <option [ngValue]="365">1 year</option><option [ngValue]="null">Never expires</option>
      </select>
      <button type="submit" [disabled]="!tokenName.trim()">Create token</button>
    </form>
    @if (msg['tok']) { <p class="error">{{ msg['tok'] }}</p> }
    <table class="grid">
      <tr><th>Name</th><th>Token</th><th>Created</th><th>Last used</th><th>Expires</th><th></th></tr>
      @for (t of tokens(); track t.id) {
        <tr><td>{{ t.name }}</td><td><code>{{ t.prefix }}_…</code></td><td>{{ t.created_at | date:'MMM d, y' }}</td>
          <td>{{ t.last_used_at ? (t.last_used_at | date:'MMM d, h:mm a') : 'never' }}</td>
          <td>{{ t.expires_at ? (t.expires_at | date:'MMM d, y') : 'never' }}</td>
          <td><button class="ghost small" (click)="revoke(t)">Revoke</button></td></tr>
      } @empty { <tr><td colspan="6" class="muted">No tokens.</td></tr> }
    </table>

    <h3>Sign-in log</h3>
    <table class="grid log">
      <tr><th>When</th><th>Event</th><th>User</th><th>Address</th><th>Details</th></tr>
      @for (e of events(); track e.id) {
        <tr [class.bad]="bad(e.kind)"><td>{{ e.at | date:'MMM d, h:mm:ss a' }}</td><td>{{ label(e.kind) }}</td>
          <td>{{ e.username }}</td><td>{{ e.ip }}</td><td class="muted">{{ e.detail }}</td></tr>
      }
    </table>
  </div>`,
})
export class SecurityPage implements OnInit {
  private http = inject(HttpClient);
  private sanitizer = inject(DomSanitizer);
  session = inject(Session);
  sessions = signal<SessionRow[]>([]);
  tokens = signal<TokenRow[]>([]);
  events = signal<EventRow[]>([]);
  totp = signal<{ secret: string; qr: SafeHtml } | null>(null);
  newToken = signal<{ name: string; token: string } | null>(null);
  copied = signal(false);
  msg: Record<string, string> = {};
  pw = { current: '', next: '', confirm: '' };
  off = { password: '', code: '' };
  code = '';
  tokenName = '';
  tokenDays: number | null = 90;

  ngOnInit() { this.load(); }

  load() {
    this.http.get<SessionRow[]>('/api/auth/sessions').subscribe(s => this.sessions.set(s));
    this.http.get<TokenRow[]>('/api/auth/tokens').subscribe(t => this.tokens.set(t));
    this.http.get<EventRow[]>('/api/auth/events', { params: { limit: 100 } }).subscribe(e => this.events.set(e));
  }

  private err(e: { error?: { detail?: unknown } }) {
    return typeof e.error?.detail === 'string' ? e.error.detail : 'Something went wrong.';
  }

  changePassword() {
    if (this.pw.next !== this.pw.confirm) { this.msg['pw'] = "The new passwords don't match."; return; }
    this.http.post<{ signed_out: number }>('/api/auth/password', { current: this.pw.current, new: this.pw.next }).subscribe({
      next: r => { this.msg['pw'] = `Password changed. ${r.signed_out} other browser(s) signed out.`;
        this.pw = { current: '', next: '', confirm: '' }; this.session.load(); this.load(); },
      error: e => this.msg['pw'] = this.err(e),
    });
  }

  setup2fa() {
    this.http.post<{ secret: string; qr_svg: string }>('/api/auth/totp/setup', {}).subscribe({
      next: r => { this.msg['2fa'] = ''; this.totp.set({ secret: r.secret, qr: this.sanitizer.bypassSecurityTrustHtml(r.qr_svg) }); },
      error: e => this.msg['2fa'] = this.err(e),
    });
  }

  enable2fa() {
    this.http.post('/api/auth/totp/enable', { code: this.code }).subscribe({
      next: () => { this.totp.set(null); this.code = ''; this.msg['2fa'] = 'Two-factor sign-in is on.'; this.session.load(); this.load(); },
      error: e => this.msg['2fa'] = this.err(e),
    });
  }

  disable2fa() {
    this.http.post('/api/auth/totp/disable', this.off).subscribe({
      next: () => { this.off = { password: '', code: '' }; this.msg['2fa'] = 'Two-factor sign-in is off.'; this.session.load(); this.load(); },
      error: e => this.msg['2fa'] = this.err(e),
    });
  }

  endSession(id: string) { this.http.delete(`/api/auth/sessions/${id}`).subscribe(() => this.load()); }
  endOthers() { this.http.post('/api/auth/sessions/end-others', {}).subscribe(() => this.load()); }

  makeToken() {
    this.msg['tok'] = '';
    this.http.post<{ name: string; token: string }>('/api/auth/tokens', { name: this.tokenName, days: this.tokenDays }).subscribe({
      next: r => { this.newToken.set({ name: r.name, token: r.token }); this.copied.set(false); this.tokenName = ''; this.load(); },
      error: e => this.msg['tok'] = this.err(e),
    });
  }

  revoke(t: TokenRow) {
    if (!confirm(`Revoke “${t.name}”? Anything using it stops working.`)) return;
    this.http.delete(`/api/auth/tokens/${t.id}`).subscribe(() => this.load());
  }

  copy(text: string) {
    navigator.clipboard?.writeText(text).then(() => this.copied.set(true), () => {});
  }

  browser(ua: string) {
    const b = /Edg\//.test(ua) ? 'Edge' : /Chrome\//.test(ua) ? 'Chrome' : /Firefox\//.test(ua) ? 'Firefox'
      : /Safari\//.test(ua) ? 'Safari' : ua ? ua.split(' ')[0] : 'Unknown';
    const os = /Windows/.test(ua) ? 'Windows' : /Mac OS X/.test(ua) ? 'macOS' : /Android/.test(ua) ? 'Android'
      : /iPhone|iPad/.test(ua) ? 'iOS' : /Linux/.test(ua) ? 'Linux' : '';
    return os ? `${b} on ${os}` : b;
  }

  bad(kind: string) { return /failed|lockout|blocked/.test(kind); }

  label(kind: string) {
    const names: Record<string, string> = {
      login: 'Signed in', logout: 'Signed out', login_failed: 'Failed sign-in', lockout: 'Locked out',
      login_blocked: 'Blocked (locked out)', setup: 'Account created', setup_failed: 'Failed setup',
      password_changed: 'Password changed', password_change_failed: 'Password change failed',
      password_reset_cli: 'Password reset (CLI)', totp_enabled: '2FA turned on', totp_disabled: '2FA turned off',
      totp_disable_failed: '2FA turn-off failed', totp_reset_cli: '2FA reset (CLI)', token_created: 'Token created',
      token_revoked: 'Token revoked', session_ended: 'Browser signed out', sessions_ended: 'Other browsers signed out',
      sign_out_all_cli: 'Everyone signed out (CLI)',
    };
    return names[kind] ?? kind;
  }
}
