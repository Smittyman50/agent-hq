import { Component, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { ActivatedRoute, Router } from '@angular/router';
import { Icon } from './icon';
import { Session } from './session';

/** Sign in, or (first run) create the account with the setup code from the server log. */
@Component({
  selector: 'hq-login',
  imports: [FormsModule, Icon],
  template: `
  <div class="login-wrap">
    <form class="login-card" (ngSubmit)="submit()">
      <div class="login-brand"><hq-icon name="logo" [size]="30" /><span>Agent HQ</span></div>
      @if (session.setupRequired()) {
        <h1>Create your account</h1>
        <p class="hint">Agent HQ has no account yet. The setup code is in the server log:
          <code>docker compose logs hq | grep "setup code"</code></p>
        <label>Setup code<input name="code" [(ngModel)]="code" autocomplete="off" placeholder="3f9a1c-7b2e40-c19d55" required></label>
        <label>Username<input name="username" [(ngModel)]="username" autocomplete="username" required></label>
        <label>Password <small>at least 10 characters</small>
          <input name="password" type="password" [(ngModel)]="password" autocomplete="new-password" required></label>
        <label>Confirm password<input name="confirm" type="password" [(ngModel)]="confirm" autocomplete="new-password" required></label>
      } @else if (needCode()) {
        <h1>Two-factor sign-in</h1>
        <p class="hint">Enter the 6-digit code from your authenticator app.</p>
        <label>Code<input name="totp" [(ngModel)]="totp" inputmode="numeric" autocomplete="one-time-code"
          maxlength="7" required autofocus></label>
      } @else {
        <h1>Sign in</h1>
        <label>Username<input name="username" [(ngModel)]="username" autocomplete="username" required autofocus></label>
        <label>Password<input name="password" type="password" [(ngModel)]="password" autocomplete="current-password" required></label>
      }
      @if (error()) { <p class="error">{{ error() }}</p> }
      <div class="actions">
        <button type="submit" [disabled]="busy()">{{ session.setupRequired() ? 'Create account' : needCode() ? 'Verify' : 'Sign in' }}</button>
        @if (needCode()) { <button type="button" class="ghost" (click)="back()">Back</button> }
      </div>
    </form>
  </div>`,
})
export class LoginPage {
  session = inject(Session);
  private router = inject(Router);
  private route = inject(ActivatedRoute);
  code = ''; username = ''; password = ''; confirm = ''; totp = '';
  needCode = signal(false);
  error = signal('');
  busy = signal(false);

  constructor() {
    if (this.session.user()) this.done();
  }

  submit() {
    this.error.set('');
    if (this.session.setupRequired()) {
      if (this.password !== this.confirm) { this.error.set("The passwords don't match."); return; }
      this.busy.set(true);
      this.session.setup(this.code, this.username, this.password).subscribe({
        next: r => { this.session.signedIn(r.user); this.done(); },
        error: e => this.fail(e),
      });
      return;
    }
    this.busy.set(true);
    this.session.login(this.username, this.password, this.needCode() ? this.totp : undefined).subscribe({
      next: r => {
        this.busy.set(false);
        if (r.totp_required) { this.needCode.set(true); return; }
        this.session.signedIn(r.user!);
        this.done();
      },
      error: e => this.fail(e),
    });
  }

  back() { this.needCode.set(false); this.totp = ''; this.error.set(''); }

  private fail(e: { error?: { detail?: unknown }; status?: number }) {
    this.busy.set(false);
    const d = e.error?.detail;
    this.error.set(typeof d === 'string' ? d : e.status === 0 ? "Can't reach Agent HQ." : 'Sign-in failed.');
  }

  private done() {
    const next = this.route.snapshot.queryParamMap.get('next');
    this.router.navigateByUrl(next && next.startsWith('/') && !next.startsWith('/login') ? next : '/chat');
  }
}
