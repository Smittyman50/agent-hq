"""Agent HQ egress proxy: the workbenches' only way out, with rules per workbench.

Each workbench sends its traffic here (HTTPS_PROXY / HTTP_PROXY) and signs in with its
own name and token (http://workbench-2:<token>@egress:8888). A request goes through when
one of its rules allows the host and port:

* the default rules, which every workbench gets (PyPI, out of the box), plus
* the workbench's own rules, set on its page under Settings -> Desktops & workbenches.
* A workbench with "Internet and LAN access" ticked is let through to anything.

Rules, one per entry:
    pypi.org                 that host, ports 80 and 443
    *.github.com             github.com and every subdomain
    gitgard.home.arpa:3000   one port ("host:*" for any port)
    192.168.10.0/24:443      any address in a network (host names that resolve into it too)
    192.168.10.5             one address

The rules live in Valkey (written by Agent HQ, key ahq:egress:config) and are re-read
every few seconds, so changes apply without a restart. Refused requests are logged to
the ahq:egress:blocked stream, which the UI shows with an Allow button.

Handles HTTPS (CONNECT tunnels) and plain HTTP (one request per connection). Only tools
that use a proxy (pip, git over HTTPS, curl, requests...) come through here; nothing else
can leave the workbench network.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import socket
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

log = logging.getLogger("egress")

CONFIG_KEY = "ahq:egress:config"
BLOCKED_STREAM = "ahq:egress:blocked"
DEFAULT_RULES = ["pypi.org:443", "files.pythonhosted.org:443"]
DEFAULT_PORTS = frozenset({80, 443})
HEAD_LIMIT = 64 * 1024
DROP_HEADERS = {"proxy-authorization", "proxy-connection", "connection", "keep-alive"}


# --------------------------------------------------------------------------- rules


@dataclass(frozen=True)
class Rule:
    text: str
    kind: str                      # host | wildcard | ip | net | any
    host: str = ""
    net: ipaddress.IPv4Network | ipaddress.IPv6Network | None = None
    ports: frozenset[int] | None = DEFAULT_PORTS   # None = any port

    def allows(self, host: str, port: int, addrs: list[str]) -> list[str] | None:
        """The addresses this rule lets the request reach ([] = all of them), or None."""
        if self.ports is not None and port not in self.ports:
            return None
        h = host.lower().rstrip(".")
        if self.kind == "any":
            return []
        if self.kind == "host":
            return [] if h == self.host else None
        if self.kind == "wildcard":
            return [] if h == self.host or h.endswith("." + self.host) else None
        ok = [a for a in addrs if _in(a, self)]
        return ok or None


def _in(addr: str, rule: Rule) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return ip in rule.net if rule.kind == "net" else ip == rule.net.network_address


def parse_rule(text: str) -> Rule:
    """A rule from its text; ValueError says what's wrong with it."""
    raw = text.strip()
    if not raw:
        raise ValueError("empty rule")
    hostpart, ports = raw, DEFAULT_PORTS
    if raw.startswith("["):  # [IPv6]:port
        end = raw.find("]")
        if end < 0:
            raise ValueError(f"{raw}: missing ]")
        hostpart, rest = raw[1:end], raw[end + 1:]
        if rest:
            if not rest.startswith(":"):
                raise ValueError(f"{raw}: expected :port after ]")
            ports = _ports(rest[1:], raw)
    elif raw.count(":") == 1:
        hostpart, p = raw.split(":")
        ports = _ports(p, raw)
    hostpart = hostpart.strip().lower().rstrip(".")
    if hostpart == "*":
        return Rule(raw, "any", ports=ports)
    if "/" in hostpart:
        try:
            return Rule(raw, "net", net=ipaddress.ip_network(hostpart, strict=False), ports=ports)
        except ValueError:
            raise ValueError(f"{raw}: not a network like 192.168.10.0/24") from None
    try:
        ip = ipaddress.ip_address(hostpart)
        return Rule(raw, "ip", net=ipaddress.ip_network(ip), ports=ports)
    except ValueError:
        pass
    if hostpart.startswith("*."):
        name, kind = hostpart[2:], "wildcard"
    else:
        name, kind = hostpart, "host"
    labels = name.split(".")
    if not name or any(not lab or len(lab) > 63 or not all(c.isalnum() or c == "-" for c in lab)
                       for lab in labels):
        raise ValueError(f"{raw}: not a host name, *.domain, address or network")
    return Rule(raw, kind, host=name, ports=ports)


def _ports(p: str, raw: str) -> frozenset[int] | None:
    p = p.strip()
    if p == "*":
        return None
    if not p.isdigit() or not 0 < int(p) < 65536:
        raise ValueError(f"{raw}: port must be 1-65535 or *")
    return frozenset({int(p)})


def parse_rules(texts: list[str]) -> list[Rule]:
    out = []
    for t in texts or []:
        try:
            out.append(parse_rule(t))
        except ValueError as e:
            log.warning("ignoring rule %s", e)
    return out


# --------------------------------------------------------------------------- config


@dataclass
class Identity:
    name: str                 # the workbench, or "(no sign-in)"
    rules: list[Rule]
    open: bool = False        # Internet and LAN access: everything goes


@dataclass
class Config:
    defaults: list[Rule] = field(default_factory=lambda: parse_rules(DEFAULT_RULES))
    workbenches: dict[str, dict] = field(default_factory=dict)

    @classmethod
    def from_json(cls, data: dict) -> "Config":
        wbs = {}
        for name, w in (data.get("workbenches") or {}).items():
            wbs[name] = {"token_sha256": w.get("token_sha256", ""), "open": bool(w.get("open")),
                         "rules": parse_rules(w.get("rules") or [])}
        return cls(defaults=parse_rules(data.get("defaults", DEFAULT_RULES)), workbenches=wbs)

    def identify(self, auth: str | None) -> Identity | None:
        """Who's asking. No sign-in: the defaults only. Wrong sign-in: None."""
        if not auth:
            return Identity("(no sign-in)", self.defaults)
        scheme, _, value = auth.partition(" ")
        if scheme.lower() != "basic":
            return None
        try:
            name, _, token = base64.b64decode(value.strip()).decode().partition(":")
        except (ValueError, UnicodeDecodeError):
            return None
        w = self.workbenches.get(name)
        if not w or not w["token_sha256"]:
            return None
        if not hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(), w["token_sha256"]):
            return None
        return Identity(name, self.defaults + w["rules"], w["open"])


class ConfigSource:
    """Reads the config from Valkey every few seconds; keeps the last good one."""

    def __init__(self, redis_url: str, every: float = 3.0) -> None:
        self.config = Config()
        self.every = every
        self.r = None
        if redis_url:
            import redis.asyncio as aioredis

            self.r = aioredis.from_url(redis_url, decode_responses=True)

    async def load(self) -> None:
        if self.r is None:
            return
        try:
            raw = await self.r.get(CONFIG_KEY)
            if raw:
                self.config = Config.from_json(json.loads(raw))
        except Exception as e:  # keep the last good config
            log.warning("can't read the egress config: %s", e)

    async def loop(self) -> None:
        while True:
            await self.load()
            await asyncio.sleep(self.every)

    async def blocked(self, who: str, host: str, port: int, reason: str) -> None:
        log.info("blocked %s -> %s:%s (%s)", who, host, port, reason)
        if self.r is None:
            return
        try:
            await self.r.xadd(BLOCKED_STREAM, {"workbench": who, "host": host, "port": str(port),
                                               "reason": reason, "at": str(time.time())},
                              maxlen=2000, approximate=True)
        except Exception as e:
            log.warning("can't log a blocked request: %s", e)


# --------------------------------------------------------------------------- proxy


async def resolve(host: str, port: int) -> list[str]:
    try:
        ipaddress.ip_address(host)
        return [host]
    except ValueError:
        pass
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(i[4][0] for i in infos))


def decide(who: Identity, host: str, port: int, addrs: list[str]) -> list[str] | None:
    """The addresses the request may connect to, or None if no rule allows it."""
    if who.open:
        return addrs
    for rule in who.rules:
        ok = rule.allows(host, port, addrs)
        if ok is not None:
            return ok or addrs
    return None


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.IncompleteReadError):
        pass
    finally:
        try:
            if writer.can_write_eof():
                writer.write_eof()
        except (OSError, RuntimeError):
            pass


async def _relay(reader, writer, up_r, up_w, *, tunnel: bool) -> None:
    """Copy both ways. Plain HTTP ends when the server's response does; a tunnel when
    both sides are done (or a minute after one side is, so half-open ones don't linger)."""
    to_up = asyncio.create_task(_pipe(reader, up_w))
    to_client = asyncio.create_task(_pipe(up_r, writer))
    if not tunnel:
        await to_client
        to_up.cancel()
        return
    done, pending = await asyncio.wait({to_up, to_client}, return_when=asyncio.FIRST_COMPLETED)
    if pending:
        _, still = await asyncio.wait(pending, timeout=60)
        for t in still:
            t.cancel()


def _reply(writer: asyncio.StreamWriter, status: str, text: str, extra: str = "") -> None:
    body = text.encode()
    writer.write(f"HTTP/1.1 {status}\r\nContent-Type: text/plain; charset=utf-8\r\n"
                 f"Content-Length: {len(body)}\r\nConnection: close\r\n{extra}\r\n".encode() + body)


class Proxy:
    def __init__(self, source: ConfigSource, connect_timeout: float = 15.0, max_clients: int = 200) -> None:
        self.source = source
        self.connect_timeout = connect_timeout
        self.slots = asyncio.Semaphore(max_clients)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        async with self.slots:
            try:
                await self._handle(reader, writer)
            except Exception as e:  # one bad client never takes the proxy down
                log.debug("connection error: %s", e)
            finally:
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 30)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            return
        lines = head.decode("latin-1").split("\r\n")
        try:
            method, target, version = lines[0].split(" ", 2)
        except ValueError:
            _reply(writer, "400 Bad Request", "Bad request line.\n")
            return
        headers = [(k.strip(), v.strip()) for k, _, v in (ln.partition(":") for ln in lines[1:] if ln)]
        auth = next((v for k, v in headers if k.lower() == "proxy-authorization"), None)
        who = self.source.config.identify(auth)
        if who is None:
            _reply(writer, "407 Proxy Authentication Required", "Wrong workbench sign-in.\n",
                   'Proxy-Authenticate: Basic realm="agent-hq egress"\r\n')
            return

        if method.upper() == "CONNECT":
            host, _, p = target.rpartition(":")
            host = host.strip("[]")
            port = int(p) if p.isdigit() else 443
        else:
            url = urlsplit(target)
            if url.scheme != "http" or not url.hostname:
                _reply(writer, "400 Bad Request", "Only http:// URLs and CONNECT go through this proxy.\n")
                return
            host, port = url.hostname, url.port or 80
        try:
            addrs = await resolve(host, port)
        except OSError:
            await self.source.blocked(who.name, host, port, "unknown host")
            _reply(writer, "502 Bad Gateway", f"Can't resolve {host}.\n")
            return
        allowed = decide(who, host, port, addrs)
        if not allowed:
            await self.source.blocked(who.name, host, port, "not allowed")
            _reply(writer, "403 Forbidden",
                   f"Blocked by the Agent HQ egress proxy: {host}:{port} isn't allowed for {who.name}. "
                   "The user can allow it on the workbench's page (Settings -> Desktops & workbenches).\n")
            return

        upstream, err = None, None
        for addr in allowed:
            try:
                upstream = await asyncio.wait_for(asyncio.open_connection(addr, port), self.connect_timeout)
                break
            except (OSError, asyncio.TimeoutError) as e:
                err = e
        if upstream is None:
            _reply(writer, "502 Bad Gateway", f"Can't connect to {host}:{port}: {err}\n")
            return
        up_r, up_w = upstream
        try:
            if method.upper() == "CONNECT":
                writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                await writer.drain()
            else:
                url = urlsplit(target)
                path = (url.path or "/") + (f"?{url.query}" if url.query else "")
                out = [f"{method} {path} {version}"]
                out += [f"{k}: {v}" for k, v in headers if k.lower() not in DROP_HEADERS]
                if not any(k.lower() == "host" for k, _ in headers):
                    out.append(f"Host: {url.netloc.rpartition('@')[2]}")
                out.append("Connection: close")  # one request per connection: the next may be elsewhere
                up_w.write(("\r\n".join(out) + "\r\n\r\n").encode("latin-1"))
                await up_w.drain()
            await _relay(reader, writer, up_r, up_w, tunnel=method.upper() == "CONNECT")
        finally:
            up_w.close()


async def main() -> None:
    logging.basicConfig(level=os.environ.get("EGRESS_LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(message)s")
    source = ConfigSource(os.environ.get("AHQ_REDIS_URL", ""))
    await source.load()
    asyncio.create_task(source.loop())
    proxy = Proxy(source)
    port = int(os.environ.get("EGRESS_PORT", "8888"))
    server = await asyncio.start_server(proxy.handle, "0.0.0.0", port, limit=HEAD_LIMIT)
    log.info("egress proxy on :%s (%s)", port,
             "rules from Valkey" if source.r is not None else "no AHQ_REDIS_URL: default rules only")
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
