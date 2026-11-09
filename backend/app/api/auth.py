"""Sign-in endpoints and the middleware that requires a session or token everywhere else."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlmodel import select

from .. import auth, db
from ..config import settings
from ..models import ApiToken, AuthEvent, AuthSession, User

router = APIRouter(prefix="/api/auth", tags=["auth"])

# Reachable without signing in. Everything else under /api and /ws needs a session or token.
PUBLIC = {"/api/health", "/api/auth/status", "/api/auth/login", "/api/auth/setup", "/api/auth/verify"}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


class AuthMiddleware:
    """Pure ASGI, so it covers WebSockets as well as HTTP."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        path = scope["path"]
        protected = (path.startswith("/api/") or path.startswith("/ws/")) and path not in PUBLIC
        if not protected:
            return await self.app(scope, receive, send)
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        if scope["type"] == "http" and scope["method"] == "OPTIONS":
            return await self.app(scope, receive, send)  # CORS preflight
        who = identify(headers)
        if who is None:
            return await _deny(scope, send, 401, "Sign in required")
        user, via, ref = who
        if via == "session":
            writes = scope["type"] == "websocket" or scope["method"] not in SAFE_METHODS
            if writes and not auth.same_origin(headers, settings.cors_origins):
                return await _deny(scope, send, 403, "Cross-site request refused")
        scope.setdefault("state", {})
        scope["state"].update(user=user, auth_via=via, auth_ref=ref)
        return await self.app(scope, receive, send)


def identify(headers: dict[str, str]) -> tuple[User, str, str] | None:
    """(user, "token"|"session", token id or session id) from the request, or None."""
    bearer = headers.get("authorization", "")
    if bearer.lower().startswith("bearer "):
        hit = auth.token_user(bearer[7:].strip())
        return (hit[0], "token", hit[1].id) if hit else None
    raw = _cookie(headers.get("cookie", ""), auth.COOKIE)
    hit = auth.session_user(raw) if raw else None
    return (hit[0], "session", hit[1]) if hit else None


def _cookie(header: str, name: str) -> str:
    for part in header.split(";"):
        k, _, v = part.strip().partition("=")
        if k == name:
            return v
    return ""


async def _deny(scope, send, status: int, detail: str):
    if scope["type"] == "websocket":
        # Closing before accept makes the handshake fail (the browser sees code 1006/403).
        await send({"type": "websocket.close", "code": 4401 if status == 401 else 4403})
        return
    resp = JSONResponse({"detail": detail}, status_code=status)
    await resp({**scope, "type": "http"}, None, send)


# --------------------------------------------------------------------------- helpers


def _ctx(request: Request) -> dict[str, str]:
    h = {k.lower(): v for k, v in request.headers.items()}
    return {"ip": auth.client_ip(h, request.client.host if request.client else None),
            "user_agent": h.get("user-agent", "")[:300]}


def _secure(request: Request) -> bool:
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "") == "https"


def _set_cookie(request: Request, response: Response, raw: str) -> None:
    response.set_cookie(auth.COOKIE, raw, httponly=True, samesite="lax", secure=_secure(request),
                        max_age=int(auth.max_days() * 86400), path="/")


def _me(request: Request) -> User:
    user = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(401, "Sign in required")
    return auth.get_user(user.id) or user


def _need_session(request: Request) -> None:
    if getattr(request.state, "auth_via", "") != "session":
        raise HTTPException(403, "Account changes need a signed-in browser, not an API token")


# --------------------------------------------------------------------------- sign-in


@router.get("/status")
def status(request: Request) -> dict[str, Any]:
    h = {k.lower(): v for k, v in request.headers.items()}
    who = identify(h)
    return {"setup_required": auth.user_count() == 0,
            "user": auth.public_user(who[0]) if who else None}


class Setup(BaseModel):
    code: str
    username: str
    password: str


@router.post("/setup")
async def setup(body: Setup, request: Request, response: Response):
    ctx = _ctx(request)
    r = request.app.state.broker.r
    if auth.user_count():
        raise HTTPException(409, "An account already exists. Sign in instead.")
    if await auth.locked_out(r, ctx["ip"], "setup"):
        raise HTTPException(429, "Too many attempts. Try again in a few minutes.")
    if not auth.check_setup_code(body.code):
        await auth.note_failure(r, ctx["ip"], "setup")
        auth.record("setup_failed", username=body.username, detail="wrong setup code", **ctx)
        raise HTTPException(403, "That setup code isn't right. It's in the hq container's log.")
    name = body.username.strip()
    if not (2 <= len(name) <= 40) or not name.replace("-", "").replace("_", "").replace(".", "").isalnum():
        raise HTTPException(422, "Usernames are 2–40 letters, digits, dots, dashes or underscores.")
    if (why := auth.password_problem(body.password, name)):
        raise HTTPException(422, f"Choose a stronger password: {why}.")
    user = User(username=name, password_hash=auth.hash_password(body.password), is_admin=True)
    with db.session() as s:
        s.add(user)
        s.commit()
        s.refresh(user)
    auth.clear_setup_code()
    auth.record("setup", username=name, detail="first account created", **ctx)
    _set_cookie(request, response, auth.create_session(user, **ctx))
    return {"user": auth.public_user(user)}


class Login(BaseModel):
    username: str
    password: str
    totp: str | None = None


@router.post("/login")
async def login(body: Login, request: Request, response: Response):
    ctx = _ctx(request)
    r = request.app.state.broker.r
    wait = await auth.locked_out(r, ctx["ip"], body.username)
    if wait:
        auth.record("login_blocked", username=body.username, detail="locked out", **ctx)
        raise HTTPException(429, f"Too many failed sign-ins. Try again in {max(1, wait // 60)} min.")
    user = auth.find_user(body.username)
    ok = user is not None and auth.check_password(user, body.password)
    if not ok:
        if user is None:
            auth.hash_password(body.password)  # same time either way: don't reveal usernames
        locked = await auth.note_failure(r, ctx["ip"], body.username)
        auth.record("login_failed", username=body.username, detail="wrong username or password", **ctx)
        if locked:
            auth.record("lockout", username=body.username, detail="too many failures; locked 15 min", **ctx)
        raise HTTPException(401, "Wrong username or password.")
    if user.totp_secret:
        if not body.totp:
            return {"totp_required": True}
        if not auth.check_totp(user.totp_secret, body.totp):
            locked = await auth.note_failure(r, ctx["ip"], body.username)
            auth.record("login_failed", username=user.username, detail="wrong 2FA code", **ctx)
            if locked:
                auth.record("lockout", username=user.username, detail="too many failures; locked 15 min", **ctx)
            raise HTTPException(401, "That code isn't right. Use the current one from your authenticator app.")
    await auth.clear_failures(r, ctx["ip"], body.username)
    auth.record("login", username=user.username, detail="2FA" if user.totp_secret else "password", **ctx)
    _set_cookie(request, response, auth.create_session(user, **ctx))
    return {"user": auth.public_user(auth.get_user(user.id))}


@router.post("/logout")
def logout(request: Request, response: Response):
    raw = request.cookies.get(auth.COOKIE)
    user = getattr(request.state, "user", None)
    if raw:
        auth.end_session(raw)
    auth.record("logout", username=user.username if user else "", **_ctx(request))
    response.delete_cookie(auth.COOKIE, path="/")
    return {"ok": True}


@router.get("/verify")
def verify(request: Request) -> Response:
    """For the HTTPS proxy's forward_auth: 200 if signed in (the desktop viewers use this)."""
    h = {k.lower(): v for k, v in request.headers.items()}
    who = identify(h)
    if who is None:
        return Response(status_code=401)
    if who[1] == "session" and not auth.same_origin(h, settings.cors_origins):
        return Response(status_code=403)  # another site trying to use your desktop session
    return Response(status_code=200, headers={"X-Ahq-User": who[0].username})


# --------------------------------------------------------------------------- account


class PasswordChange(BaseModel):
    current: str
    new: str


@router.post("/password")
def change_password(body: PasswordChange, request: Request):
    _need_session(request)
    me = _me(request)
    ctx = _ctx(request)
    if not auth.check_password(me, body.current):
        auth.record("password_change_failed", username=me.username, detail="wrong current password", **ctx)
        raise HTTPException(403, "Your current password isn't right.")
    if (why := auth.password_problem(body.new, me.username)):
        raise HTTPException(422, f"Choose a stronger password: {why}.")
    with db.session() as s:
        u = s.get(User, me.id)
        u.password_hash, u.password_changed_at = auth.hash_password(body.new), auth._now()
        s.add(u)
        s.commit()
    ended = auth.end_other_sessions(me.id, request.state.auth_ref)
    auth.record("password_changed", username=me.username, detail=f"{ended} other session(s) signed out", **ctx)
    return {"ok": True, "signed_out": ended}


@router.post("/totp/setup")
def totp_setup(request: Request):
    _need_session(request)
    me = _me(request)
    secret = auth.new_totp_secret()
    with db.session() as s:
        u = s.get(User, me.id)
        u.pending_totp = secret
        s.add(u)
        s.commit()
    uri = auth.totp_uri(me.username, secret)
    return {"secret": secret, "uri": uri, "qr_svg": auth.qr_svg(uri)}


class Code(BaseModel):
    code: str
    password: str | None = None


@router.post("/totp/enable")
def totp_enable(body: Code, request: Request):
    _need_session(request)
    me = _me(request)
    if not me.pending_totp:
        raise HTTPException(409, "Start the setup again.")
    if not auth.check_totp(me.pending_totp, body.code):
        raise HTTPException(422, "That code doesn't match. Check the time on your phone and try the current code.")
    with db.session() as s:
        u = s.get(User, me.id)
        u.totp_secret, u.pending_totp = u.pending_totp, None
        s.add(u)
        s.commit()
    auth.record("totp_enabled", username=me.username, **_ctx(request))
    return {"ok": True}


@router.post("/totp/disable")
def totp_disable(body: Code, request: Request):
    _need_session(request)
    me = _me(request)
    if not me.totp_secret:
        return {"ok": True}
    if not (body.password and auth.check_password(me, body.password)) or not auth.check_totp(me.totp_secret, body.code):
        auth.record("totp_disable_failed", username=me.username, **_ctx(request))
        raise HTTPException(403, "Enter your password and a current code to turn 2FA off.")
    with db.session() as s:
        u = s.get(User, me.id)
        u.totp_secret = u.pending_totp = None
        s.add(u)
        s.commit()
    auth.record("totp_disabled", username=me.username, **_ctx(request))
    return {"ok": True}


# --------------------------------------------------------------------------- sessions


@router.get("/sessions")
def sessions(request: Request):
    me = _me(request)
    current = request.state.auth_ref if request.state.auth_via == "session" else None
    with db.session() as s:
        rows = s.exec(select(AuthSession).where(AuthSession.user_id == me.id)
                      .order_by(AuthSession.last_seen_at.desc())).all()
    return [{"id": r.id, "ip": r.ip, "user_agent": r.user_agent, "created_at": r.created_at,
             "last_seen_at": r.last_seen_at, "expires_at": r.expires_at, "current": r.id == current}
            for r in rows]


@router.delete("/sessions/{session_id}")
def end_session(session_id: str, request: Request):
    me = _me(request)
    with db.session() as s:
        row = s.get(AuthSession, session_id)
        if row is None or row.user_id != me.id:
            raise HTTPException(404)
    auth.end_session(session_id=session_id)
    auth.record("session_ended", username=me.username, detail=f"session {session_id}", **_ctx(request))
    return {"ok": True}


@router.post("/sessions/end-others")
def end_others(request: Request):
    _need_session(request)
    me = _me(request)
    n = auth.end_other_sessions(me.id, request.state.auth_ref)
    auth.record("sessions_ended", username=me.username, detail=f"{n} other session(s)", **_ctx(request))
    return {"ended": n}


# --------------------------------------------------------------------------- API tokens


def _token_out(t: ApiToken) -> dict[str, Any]:
    return {"id": t.id, "name": t.name, "prefix": t.prefix, "created_at": t.created_at,
            "last_used_at": t.last_used_at, "expires_at": t.expires_at}


@router.get("/tokens")
def tokens(request: Request):
    me = _me(request)
    with db.session() as s:
        rows = s.exec(select(ApiToken).where(ApiToken.user_id == me.id)
                      .order_by(ApiToken.created_at.desc())).all()
    return [_token_out(t) for t in rows]


class NewToken(BaseModel):
    name: str
    days: int | None = 90  # None = never expires


@router.post("/tokens", status_code=201)
def new_token(body: NewToken, request: Request):
    _need_session(request)  # a token can't mint more tokens
    me = _me(request)
    if body.days is not None and not 1 <= body.days <= 3650:
        raise HTTPException(422, "Expiry must be between 1 and 3650 days (or never).")
    if not body.name.strip():
        raise HTTPException(422, "Give the token a name, e.g. what will use it.")
    row, raw = auth.create_token(me, body.name, body.days)
    auth.record("token_created", username=me.username,
                detail=f"{row.name} ({row.prefix}), expires "
                f"{row.expires_at.date().isoformat() if row.expires_at else 'never'}", **_ctx(request))
    return {**_token_out(row), "token": raw}


@router.delete("/tokens/{token_id}")
def revoke_token(token_id: str, request: Request):
    me = _me(request)
    with db.session() as s:
        row = s.get(ApiToken, token_id)
        if row is None or row.user_id != me.id:
            raise HTTPException(404)
        name, prefix = row.name, row.prefix
        s.delete(row)
        s.commit()
    auth.record("token_revoked", username=me.username, detail=f"{name} ({prefix})", **_ctx(request))
    return {"ok": True}


# --------------------------------------------------------------------------- audit log


@router.get("/events")
def events(request: Request, limit: int = 200, kind: str | None = None):
    _me(request)
    q = select(AuthEvent).order_by(AuthEvent.at.desc()).limit(max(1, min(limit, 1000)))
    if kind:
        q = q.where(AuthEvent.kind == kind)
    with db.session() as s:
        return s.exec(q).all()
