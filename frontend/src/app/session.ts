import { HttpClient, HttpErrorResponse, HttpInterceptorFn } from '@angular/common/http';
import { Injectable, inject, signal } from '@angular/core';
import { CanActivateFn, Router } from '@angular/router';
import { catchError, firstValueFrom, throwError } from 'rxjs';

export interface Me {
  id: string; username: string; is_admin: boolean; totp_enabled: boolean;
  created_at: string; last_login_at: string | null; password_changed_at: string;
}

/** Who's signed in. Loaded once at startup; the guard and the 401 handler keep it honest. */
@Injectable({ providedIn: 'root' })
export class Session {
  private http = inject(HttpClient);
  private router = inject(Router);
  user = signal<Me | null>(null);
  setupRequired = signal(false);

  async load() {
    try {
      const s = await firstValueFrom(this.http.get<{ setup_required: boolean; user: Me | null }>('/api/auth/status'));
      this.user.set(s.user);
      this.setupRequired.set(s.setup_required);
    } catch {
      this.user.set(null);
    }
  }

  login(username: string, password: string, totp?: string) {
    return this.http.post<{ user?: Me; totp_required?: boolean }>('/api/auth/login', { username, password, totp });
  }

  setup(code: string, username: string, password: string) {
    return this.http.post<{ user: Me }>('/api/auth/setup', { code, username, password });
  }

  signedIn(user: Me) {
    this.user.set(user);
    this.setupRequired.set(false);
  }

  logout() {
    this.http.post('/api/auth/logout', {}).subscribe({ complete: () => this.signedOut(), error: () => this.signedOut() });
  }

  /** The server says we're not signed in (session expired, signed out elsewhere…). */
  signedOut(next?: string) {
    this.user.set(null);
    if (!this.router.url.startsWith('/login')) {
      this.router.navigate(['/login'], { queryParams: next && next !== '/' ? { next } : {} });
    }
  }
}

export const authGuard: CanActivateFn = (_route, state) => {
  const s = inject(Session);
  if (s.user()) return true;
  return inject(Router).createUrlTree(['/login'], { queryParams: state.url !== '/' ? { next: state.url } : {} });
};

export const authInterceptor: HttpInterceptorFn = (req, next) => {
  const session = inject(Session);
  const router = inject(Router);
  return next(req).pipe(catchError((e: unknown) => {
    if (e instanceof HttpErrorResponse && e.status === 401 && !req.url.includes('/api/auth/')) {
      session.signedOut(router.url);
    }
    return throwError(() => e);
  }));
};
