"""Sign-in for the web UI and API.

* **Accounts** live in Postgres. Passwords are hashed with Argon2id. Two-factor sign-in
  with an authenticator app (TOTP) is optional.
* **Browsers** get an HttpOnly session cookie holding a random token. Only the token's
  SHA-256 is stored, so a database leak doesn't hand out sessions. A session ends after
  AHQ_SESSION_IDLE_DAYS without use (default 7) or AHQ_SESSION_MAX_DAYS in all (30).
* **Scripts** use personal API tokens: `Authorization: Bearer ahq_...`, also stored hashed.
* **First run:** with no account yet, the API process logs a one-time setup code
  (`docker compose logs hq | grep "setup code"`). The first account needs it, so nobody
  else on the network can claim the instance first.
* **Lockout:** 5 failed sign-ins from one address, or 10 for one username, in 15 minutes
  lock further attempts for 15 minutes (counted in Valkey, so across API processes).
* **Audit log:** sign-ins, failures, lockouts, sign-outs and every account, 2FA, session
  and token change are recorded.
* **Forgot the password?** `docker compose exec hq python -m app.auth set-password <name>`.

Agents and workers never sign in: they talk to Valkey and Postgres directly.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import secrets
import struct
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote, urlparse

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlmodel import func, select

from . import db
from .models import ApiToken, AuthEvent, AuthSession, SystemSetting, User

log = logging.getLogger("agent_hq.auth")

COOKIE = "ahq_session"
TOKEN_PREFIX = "ahq_"
MIN_PASSWORD = 10
LOCK_SECONDS = 15 * 60
MAX_FAILS_IP = 5
MAX_FAILS_USER = 10
ISSUER = "Agent HQ"

_ph = PasswordHasher()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def idle_days() -> float:
    return float(os.environ.get("AHQ_SESSION_IDLE_DAYS", "7"))


def max_days() -> float:
    return float(os.environ.get("AHQ_SESSION_MAX_DAYS", "30"))


def sha256(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


# --------------------------------------------------------------------------- passwords


def hash_password(password: str) -> str:
    return _ph.hash(password)


def check_password(user: User, password: str) -> bool:
    try:
        return _ph.verify(user.password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def password_problem(password: str, username: str = "") -> str | None:
    if len(password) < MIN_PASSWORD:
        return f"use at least {MIN_PASSWORD} characters"
    if username and password.lower() == username.lower():
        return "the password can't be the username"
    return None


# --------------------------------------------------------------------------- TOTP (RFC 6238)


def new_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _hotp(secret: str, counter: int) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    o = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[o:o + 4])[0] & 0x7FFFFFFF) % 1_000_000
    return f"{code:06d}"


def totp_now(secret: str, at: float | None = None) -> str:
    return _hotp(secret, int((at or time.time()) // 30))


def check_totp(secret: str, code: str, at: float | None = None) -> bool:
    code = "".join(ch for ch in (code or "") if ch.isdigit())
    if len(code) != 6:
        return False
    step = int((at or time.time()) // 30)
    return any(hmac.compare_digest(_hotp(secret, step + d), code) for d in (-1, 0, 1))


def totp_uri(username: str, secret: str) -> str:
    label = f"{quote(ISSUER)}:{quote(username)}"
    return f"otpauth://totp/{label}?secret={secret}&issuer={quote(ISSUER)}&digits=6&period=30"


def qr_svg(data: str) -> str:
    import io

    import segno

    buf = io.BytesIO()
    segno.make(data, error="m").save(buf, kind="svg", scale=5, border=2, dark="#000", light="#fff")
    return buf.getvalue().decode()


# --------------------------------------------------------------------------- users


def user_count() -> int:
    with db.session() as s:
        return s.exec(select(func.count()).select_from(User)).one()


def find_user(username: str) -> User | None:
    with db.session() as s:
        return s.exec(select(User).where(func.lower(User.username) == username.strip().lower())).first()


def get_user(user_id: str) -> User | None:
    with db.session() as s:
        return s.get(User, user_id)


def public_user(u: User) -> dict[str, Any]:
    return {"id": u.id, "username": u.username, "is_admin": u.is_admin,
            "totp_enabled": bool(u.totp_secret), "created_at": u.created_at,
            "last_login_at": u.last_login_at, "password_changed_at": u.password_changed_at}


# --------------------------------------------------------------------------- first-run setup code

_SETUP_KEY = "auth_setup"


def ensure_setup_code() -> str | None:
    """With no accounts yet, make a fresh one-time setup code, log it and return it."""
    if user_count():
        return None
    code = "-".join(secrets.token_hex(3) for _ in range(3))  # e.g. 3f9a1c-7b2e40-c19d55
    with db.session() as s:
        row = s.get(SystemSetting, _SETUP_KEY) or SystemSetting(key=_SETUP_KEY)
        row.value = {"code_hash": sha256(code), "at": _now().isoformat()}
        s.add(row)
        s.commit()
    log.warning("No accounts yet. Open the web UI and create yours with this setup code: %s", code)
    return code


def check_setup_code(code: str) -> bool:
    with db.session() as s:
        row = s.get(SystemSetting, _SETUP_KEY)
    want = (row.value or {}).get("code_hash") if row else None
    return bool(want) and hmac.compare_digest(want, sha256(code.strip().lower()))


def clear_setup_code() -> None:
    with db.session() as s:
        row = s.get(SystemSetting, _SETUP_KEY)
        if row:
            s.delete(row)
            s.commit()


# --------------------------------------------------------------------------- sessions

_cache: dict[str, tuple[float, str, str]] = {}  # token hash -> (cached at, user id, session id)
CACHE_SECONDS = 30


def create_session(user: User, ip: str, user_agent: str) -> str:
    raw = secrets.token_urlsafe(32)
    now = _now()
    with db.session() as s:
        s.add(AuthSession(token_hash=sha256(raw), user_id=user.id, ip=ip, user_agent=user_agent[:300],
                          expires_at=now + timedelta(days=max_days())))
        u = s.get(User, user.id)
        u.last_login_at = now
        s.add(u)
        s.commit()
    return raw


def session_user(raw: str) -> tuple[User, str] | None:
    """The user and session id for a session cookie, or None if it isn't valid."""
    if not raw:
        return None
    h = sha256(raw)
    hit = _cache.get(h)
    if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
        u = get_user(hit[1])
        return (u, hit[2]) if u else None
    now = _now()
    with db.session() as s:
        sess = s.exec(select(AuthSession).where(AuthSession.token_hash == h)).first()
        if sess is None:
            return None
        if aware(sess.expires_at) < now or aware(sess.last_seen_at) < now - timedelta(days=idle_days()):
            s.delete(sess)
            s.commit()
            return None
        sess.last_seen_at = now
        s.add(sess)
        s.commit()
        user = s.get(User, sess.user_id)
        sid = sess.id
    if user is None:
        return None
    _cache[h] = (time.monotonic(), user.id, sid)
    return user, sid


def end_session(raw: str | None = None, *, session_id: str | None = None) -> None:
    with db.session() as s:
        q = select(AuthSession)
        q = q.where(AuthSession.token_hash == sha256(raw)) if raw else q.where(AuthSession.id == session_id)
        for row in s.exec(q).all():
            s.delete(row)
        s.commit()
    _cache.clear()


def end_other_sessions(user_id: str, keep_session_id: str | None) -> int:
    with db.session() as s:
        rows = [r for r in s.exec(select(AuthSession).where(AuthSession.user_id == user_id)).all()
                if r.id != keep_session_id]
        for r in rows:
            s.delete(r)
        s.commit()
    _cache.clear()
    return len(rows)


# --------------------------------------------------------------------------- API tokens


def create_token(user: User, name: str, days: int | None) -> tuple[ApiToken, str]:
    prefix = TOKEN_PREFIX + secrets.token_hex(4)
    raw = f"{prefix}_{secrets.token_urlsafe(32)}"
    row = ApiToken(user_id=user.id, name=name.strip()[:80] or "token", prefix=prefix,
                   token_hash=sha256(raw),
                   expires_at=_now() + timedelta(days=days) if days else None)
    with db.session() as s:
        s.add(row)
        s.commit()
        s.refresh(row)
    return row, raw


def token_user(raw: str) -> tuple[User, ApiToken] | None:
    if not raw.startswith(TOKEN_PREFIX):
        return None
    now = _now()
    with db.session() as s:
        tok = s.exec(select(ApiToken).where(ApiToken.token_hash == sha256(raw))).first()
        if tok is None or (aware(tok.expires_at) and aware(tok.expires_at) < now):
            return None
        if tok.last_used_at is None or aware(tok.last_used_at) < now - timedelta(minutes=1):
            tok.last_used_at = now
            s.add(tok)
            s.commit()
            s.refresh(tok)
        user = s.get(User, tok.user_id)
    return (user, tok) if user else None


# --------------------------------------------------------------------------- audit and lockout


def record(kind: str, *, username: str = "", ip: str = "", user_agent: str = "", detail: str = "") -> None:
    try:
        with db.session() as s:
            s.add(AuthEvent(kind=kind, username=username[:80], ip=ip[:64],
                            user_agent=user_agent[:300], detail=detail[:500]))
            s.commit()
    except Exception as e:  # the log must never block a sign-in
        log.warning("could not record auth event: %s", e)
    log.info("auth: %s user=%s ip=%s %s", kind, username, ip, detail)


def _fail_keys(ip: str, username: str) -> tuple[str, str]:
    return f"ahq:auth:fail:ip:{ip}", f"ahq:auth:fail:user:{username.strip().lower()}"


async def locked_out(r, ip: str, username: str) -> int:
    """Seconds left on a lockout for this address or username, or 0."""
    kip, kuser = _fail_keys(ip, username)
    nip, nuser = await r.get(kip), await r.get(kuser)
    if (nip and int(nip) >= MAX_FAILS_IP) or (nuser and int(nuser) >= MAX_FAILS_USER):
        ttl = max(await r.ttl(kip), await r.ttl(kuser))
        return max(ttl, 1)
    return 0


async def note_failure(r, ip: str, username: str) -> bool:
    """Count a failed sign-in. True if this one triggered a lockout."""
    kip, kuser = _fail_keys(ip, username)
    pipe = r.pipeline()
    pipe.incr(kip)
    pipe.expire(kip, LOCK_SECONDS, nx=True)
    pipe.incr(kuser)
    pipe.expire(kuser, LOCK_SECONDS, nx=True)
    nip, _, nuser, _ = await pipe.execute()
    return nip == MAX_FAILS_IP or nuser == MAX_FAILS_USER


async def clear_failures(r, ip: str, username: str) -> None:
    await r.delete(*_fail_keys(ip, username))


# --------------------------------------------------------------------------- request helpers


def client_ip(headers: dict[str, str], peer: str | None) -> str:
    """The browser's address: the first X-Forwarded-For hop (set by the HTTPS proxy), else
    the TCP peer."""
    fwd = headers.get("x-forwarded-for", "")
    return (fwd.split(",")[0].strip() if fwd else "") or (peer or "")


def same_origin(headers: dict[str, str], allowed: list[str]) -> bool:
    """For cookie-authenticated writes and WebSockets: the Origin, if sent, must be this
    site (or a configured dev origin). Stops other web pages acting with your cookie."""
    origin = headers.get("origin")
    if not origin:
        return True  # not a cross-site browser request (curl, scripts, same-origin GETs)
    host = headers.get("x-forwarded-host") or headers.get("host", "")
    if urlparse(origin).netloc == host.split(",")[0].strip():
        return True
    return origin.rstrip("/") in [o.rstrip("/") for o in allowed]


# --------------------------------------------------------------------------- CLI


def _cli(argv: list[str] | None = None) -> int:
    import argparse
    import getpass

    p = argparse.ArgumentParser(prog="python -m app.auth", description="Agent HQ account tools")
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("set-password", help="set a password (creates the account if there is none)")
    sp.add_argument("username")
    sp.add_argument("--password", help="omit to be prompted")
    sub.add_parser("reset-2fa", help="turn off two-factor sign-in").add_argument("username")
    sub.add_parser("sign-out-all", help="end every session (all browsers)")
    sub.add_parser("unlock", help="clear sign-in lockouts")
    sub.add_parser("list", help="list accounts")
    a = p.parse_args(argv)
    db.init_db()

    if a.cmd == "set-password":
        pw = a.password or getpass.getpass("New password: ")
        if (why := password_problem(pw, a.username)):
            print(f"Not set: {why}.")
            return 1
        with db.session() as s:
            u = s.exec(select(User).where(func.lower(User.username) == a.username.lower())).first()
            if u is None:
                if s.exec(select(func.count()).select_from(User)).one():
                    print(f"No account named {a.username}.")
                    return 1
                u = User(username=a.username, password_hash="")
            u.password_hash, u.password_changed_at = hash_password(pw), _now()
            s.add(u)
            s.commit()
        clear_setup_code()
        end_other_sessions(u.id, None)
        record("password_reset_cli", username=u.username, detail="set from the command line; sessions ended")
        print(f"Password set for {u.username}; all their sessions were signed out.")
    elif a.cmd == "reset-2fa":
        with db.session() as s:
            u = s.exec(select(User).where(func.lower(User.username) == a.username.lower())).first()
            if u is None:
                print(f"No account named {a.username}.")
                return 1
            u.totp_secret = u.pending_totp = None
            s.add(u)
            s.commit()
        record("totp_reset_cli", username=u.username)
        print(f"Two-factor sign-in turned off for {u.username}.")
    elif a.cmd == "sign-out-all":
        with db.session() as s:
            for r in s.exec(select(AuthSession)).all():
                s.delete(r)
            s.commit()
        record("sign_out_all_cli")
        print("All sessions ended.")
    elif a.cmd == "unlock":
        import redis

        from .config import settings

        r = redis.from_url(settings.redis_url)
        keys = list(r.scan_iter("ahq:auth:fail:*"))
        if keys:
            r.delete(*keys)
        print(f"Cleared {len(keys)} lockout counter(s).")
    elif a.cmd == "list":
        with db.session() as s:
            for u in s.exec(select(User)).all():
                print(f"{u.username}  2FA={'on' if u.totp_secret else 'off'}  last sign-in={u.last_login_at}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
