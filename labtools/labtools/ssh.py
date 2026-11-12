"""Restarting allow-listed systemd services and Docker containers over SSH.

Only (host, service) pairs listed in the inventory's `ssh_hosts` can be touched, and only
these commands are ever run (unit and container names are checked against the list, never
interpolated from free text):

    sudo -n systemctl restart <unit>   /   systemctl is-active <unit>
    sudo -n docker restart <name>      /   docker inspect -f '{{.State.Status}}' <name>

On each host, give the SSH user a sudoers entry for exactly those commands, e.g.
    agent-hq ALL=(root) NOPASSWD: /usr/bin/systemctl restart isc-kea-dhcp4-server
SSH key: LABTOOLS_SSH_KEY (path to a private key file). Host keys are checked against
LABTOOLS_SSH_KNOWN_HOSTS (a known_hosts file); unknown hosts are refused. The user is the
host's `user` in the inventory, else LABTOOLS_SSH_USER (default agent-hq).
"""

from __future__ import annotations

import re
import shlex
from typing import Any

from .common import LabError, env, section

_NAME = re.compile(r"^[A-Za-z0-9@._:-]{1,128}$")


def host_entry(host: str) -> dict[str, Any]:
    for h in section("ssh_hosts") or []:
        if h.get("name", "").lower() == host.strip().lower():
            return h
    known = ", ".join(h.get("name", "?") for h in section("ssh_hosts") or []) or "none"
    raise LabError(f"no SSH host named '{host}' in the inventory. Known: {known}")


def allowed(host: str, name: str) -> tuple[dict[str, Any], str]:
    """(host entry, kind) if `name` is an allow-listed service or container on `host`."""
    h = host_entry(host)
    if name in (h.get("services") or []):
        kind = "service"
    elif name in (h.get("containers") or []):
        kind = "container"
    else:
        listed = [*(h.get("services") or []), *(h.get("containers") or [])]
        raise LabError(f"'{name}' isn't on {h['name']}'s allow-list ({', '.join(listed) or 'empty'}); "
                       "add it to the inventory to allow restarting it")
    if not _NAME.match(name):
        raise LabError(f"'{name}' isn't a valid unit or container name")
    return h, kind


def commands(kind: str, name: str) -> tuple[str, str]:
    """(restart command, status command)."""
    q = shlex.quote(name)
    if kind == "service":
        return f"sudo -n systemctl restart {q}", f"systemctl is-active {q}"
    return f"sudo -n docker restart {q}", f"sudo -n docker inspect -f '{{{{.State.Status}}}}' {q}"


async def run(h: dict[str, Any], command: str, timeout: float = 60) -> tuple[int, str]:
    import asyncssh

    key = env("LABTOOLS_SSH_KEY")
    known = env("LABTOOLS_SSH_KNOWN_HOSTS")
    if not key or not known:
        raise LabError("LABTOOLS_SSH_KEY and LABTOOLS_SSH_KNOWN_HOSTS must be set")
    async with asyncssh.connect(h["host"], port=int(h.get("port", 22)), username=h.get("user") or env("LABTOOLS_SSH_USER", "agent-hq"),
                                client_keys=[key], known_hosts=known, connect_timeout=10) as conn:
        res = await conn.run(command, check=False, timeout=timeout)
    out = (str(res.stdout or "") + str(res.stderr or "")).strip()
    return int(res.exit_status or 0), out[-1500:]


async def status(host: str, name: str) -> dict[str, Any]:
    h, kind = allowed(host, name)
    _, status_cmd = commands(kind, name)
    code, out = await run(h, status_cmd)
    return {"host": h["name"], kind: name, "state": out.splitlines()[-1] if out else "", "exit": code}


async def restart(host: str, name: str) -> dict[str, Any]:
    h, kind = allowed(host, name)
    restart_cmd, status_cmd = commands(kind, name)
    _, before = await run(h, status_cmd)
    code, out = await run(h, restart_cmd, timeout=120)
    _, after = await run(h, status_cmd)
    return {"host": h["name"], kind: name, "before": before.splitlines()[-1] if before else "",
            "restart_exit": code, "restart_output": out[-500:], "after": after.splitlines()[-1] if after else ""}
