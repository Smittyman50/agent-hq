from __future__ import annotations

import httpx
from sqlmodel import select

from app import auth, db
from app.models import AuthEvent, User

PW = "correct horse battery"


def _client(hq, **kw):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=hq.app), base_url="http://test", **kw)


def _events(kind=None):
    with db.session() as s:
        rows = s.exec(select(AuthEvent).order_by(AuthEvent.at)).all()
    return [e for e in rows if kind is None or e.kind == kind]


async def test_everything_needs_sign_in_except_the_basics(hq):
    for path in ("/api/agents", "/api/threads", "/api/usage", "/api/auth/tokens"):
        assert (await hq.anon.get(path)).status_code == 401, path
    assert (await hq.anon.post("/api/agents", json={})).status_code == 401
    assert (await hq.anon.get("/api/health")).status_code == 200
    st = (await hq.anon.get("/api/auth/status")).json()
    assert st == {"setup_required": False, "user": None}
    assert (await hq.http.get("/api/auth/status")).json()["user"]["username"] == "tester"


async def test_unknown_api_paths_are_404_not_the_web_app(hq):
    assert (await hq.http.get("/api/nope")).status_code == 404


async def test_first_account_needs_the_setup_code(hq):
    with db.session() as s:
        for u in s.exec(select(User)).all():
            s.delete(u)
        s.commit()
    code = auth.ensure_setup_code()
    assert (await hq.anon.get("/api/auth/status")).json()["setup_required"] is True
    async with _client(hq) as c:
        bad = await c.post("/api/auth/setup", json={"code": "000000-000000-000000", "username": "mike", "password": PW})
        assert bad.status_code == 403
        weak = await c.post("/api/auth/setup", json={"code": code, "username": "mike", "password": "short"})
        assert weak.status_code == 422
        ok = await c.post("/api/auth/setup", json={"code": code, "username": "mike", "password": PW})
        assert ok.status_code == 200 and ok.json()["user"]["username"] == "mike"
        assert "httponly" in ok.headers["set-cookie"].lower()
        assert (await c.get("/api/agents")).status_code == 200  # signed in by the cookie
        again = await c.post("/api/auth/setup", json={"code": code, "username": "eve", "password": PW})
        assert again.status_code == 409
    assert [e.kind for e in _events()] == ["setup_failed", "setup"]


async def test_login_logout_and_cookie_session(hq):
    async with _client(hq) as c:
        assert (await c.post("/api/auth/login", json={"username": "tester", "password": "nope-nope-nope"})).status_code == 401
        r = await c.post("/api/auth/login", json={"username": "TESTER", "password": PW})
        assert r.status_code == 200
        assert (await c.get("/api/agents")).status_code == 200
        sessions = (await c.get("/api/auth/sessions")).json()
        assert len(sessions) == 1 and sessions[0]["current"] and sessions[0]["ip"] == "127.0.0.1"
        await c.post("/api/auth/logout")
        assert (await c.get("/api/agents")).status_code == 401
    assert [e.kind for e in _events()] == ["login_failed", "login", "logout"]


async def test_cookie_writes_from_another_site_are_refused(hq):
    async with _client(hq) as c:
        await c.post("/api/auth/login", json={"username": "tester", "password": PW})
        evil = await c.post("/api/agents", json={"name": "X", "model": "fake:worker"},
                            headers={"Origin": "http://evil.example"})
        assert evil.status_code == 403
        same = await c.post("/api/agents", json={"name": "X", "model": "fake:worker"},
                            headers={"Origin": "http://test"})
        assert same.status_code == 201
        # Reads aren't affected.
        assert (await c.get("/api/agents", headers={"Origin": "http://evil.example"})).status_code == 200


async def test_lockout_after_repeated_failures(hq):
    async with _client(hq) as c:
        for _ in range(5):
            assert (await c.post("/api/auth/login", json={"username": "tester", "password": "wrong-password"})).status_code == 401
        r = await c.post("/api/auth/login", json={"username": "tester", "password": PW})
        assert r.status_code == 429  # even the right password, until the lock expires
    assert len(_events("lockout")) == 1 and len(_events("login_blocked")) == 1


async def test_two_factor(hq):
    async with _client(hq) as c:
        await c.post("/api/auth/login", json={"username": "tester", "password": PW})
        setup = (await c.post("/api/auth/totp/setup")).json()
        assert setup["uri"].startswith("otpauth://totp/Agent%20HQ:tester?") and "<svg" in setup["qr_svg"]
        assert (await c.post("/api/auth/totp/enable", json={"code": "000000"})).status_code == 422
        r = await c.post("/api/auth/totp/enable", json={"code": auth.totp_now(setup["secret"])})
        assert r.status_code == 200
        await c.post("/api/auth/logout")
        first = (await c.post("/api/auth/login", json={"username": "tester", "password": PW})).json()
        assert first == {"totp_required": True}
        assert (await c.get("/api/agents")).status_code == 401
        bad = await c.post("/api/auth/login", json={"username": "tester", "password": PW, "totp": "123456"})
        assert bad.status_code == 401
        ok = await c.post("/api/auth/login", json={"username": "tester", "password": PW,
                                                   "totp": auth.totp_now(setup["secret"])})
        assert ok.status_code == 200 and ok.json()["user"]["totp_enabled"]
    assert auth.check_totp(setup["secret"], auth.totp_now(setup["secret"], 1_000_000), at=1_000_030)


async def test_api_tokens(hq):
    async with _client(hq) as c:
        await c.post("/api/auth/login", json={"username": "tester", "password": PW})
        made = (await c.post("/api/auth/tokens", json={"name": "Home Assistant", "days": 30})).json()
        raw = made["token"]
        assert raw.startswith(made["prefix"] + "_")
        listed = (await c.get("/api/auth/tokens")).json()
        assert "token" not in listed[0] and {t["name"] for t in listed} == {"Home Assistant", "tests"}
    async with _client(hq, headers={"Authorization": f"Bearer {raw}"}) as t:
        assert (await t.get("/api/agents")).status_code == 200
        # A token can't make more tokens or change the account.
        assert (await t.post("/api/auth/tokens", json={"name": "x"})).status_code == 403
        assert (await t.post("/api/auth/password", json={"current": PW, "new": "another long one"})).status_code == 403
        await hq.http.delete(f"/api/auth/tokens/{made['id']}")
        assert (await t.get("/api/agents")).status_code == 401
    assert (await hq.anon.get("/api/agents", headers={"Authorization": "Bearer ahq_bogus_x"})).status_code == 401
    kinds = [e.kind for e in _events()]
    assert "token_created" in kinds and "token_revoked" in kinds


async def test_password_change_signs_out_other_browsers(hq):
    async with _client(hq) as a, _client(hq) as b:
        await a.post("/api/auth/login", json={"username": "tester", "password": PW})
        await b.post("/api/auth/login", json={"username": "tester", "password": PW})
        bad = await a.post("/api/auth/password", json={"current": "wrong", "new": "brand new password"})
        assert bad.status_code == 403
        r = await a.post("/api/auth/password", json={"current": PW, "new": "brand new password"})
        assert r.json() == {"ok": True, "signed_out": 1}
        assert (await a.get("/api/agents")).status_code == 200
        assert (await b.get("/api/agents")).status_code == 401


async def test_verify_for_the_https_proxy(hq):
    assert (await hq.anon.get("/api/auth/verify")).status_code == 401
    r = await hq.http.get("/api/auth/verify")
    assert r.status_code == 200 and r.headers["x-ahq-user"] == "tester"
    async with _client(hq) as c:
        await c.post("/api/auth/login", json={"username": "tester", "password": PW})
        assert (await c.get("/api/auth/verify")).status_code == 200
        # Another site opening your desktop (e.g. its WebSocket) with your cookie is refused.
        assert (await c.get("/api/auth/verify", headers={"Origin": "https://evil.example"})).status_code == 403


async def test_websocket_needs_sign_in(hq):
    from starlette.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    t = await hq.thread((await hq.agent(name="A", model="fake:worker"))["id"])
    with TestClient(hq.app) as tc:
        try:
            with tc.websocket_connect(f"/ws/threads/{t['id']}"):
                raise AssertionError("should be refused")
        except WebSocketDisconnect as e:
            assert e.code == 4401
        with tc.websocket_connect(f"/ws/threads/{t['id']}",
                                  headers={"Authorization": f"Bearer {hq.token}"}):
            pass


def test_cli_set_password(hq, capsys):
    assert auth._cli(["set-password", "tester", "--password", "a fresh password"]) == 0
    assert auth.check_password(auth.find_user("tester"), "a fresh password")
    assert auth._cli(["set-password", "nobody", "--password", "a fresh password"]) == 1
