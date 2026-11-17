"""Resources: desktops and workbenches made from the UI, dedicated or shared."""

from __future__ import annotations

import json

import httpx
import pytest

from app import db, resources
from app.models import AgentDef, ToolServer
from app.runtime import agent_tool_servers


class FakeDocker:
    """A Docker Engine API that keeps containers and volumes in dicts."""

    def __init__(self, images=("agent-hq/desktop:latest", "agent-hq/workbench:latest")) -> None:
        self.images = set(images)
        self.containers: dict[str, dict] = {}
        self.volumes: dict[str, dict] = {}
        self.calls: list[tuple[str, str]] = []

    def add_compose(self, service: str, mounts: list[dict], networks=("agent-hq_default",)) -> str:
        name = f"agent-hq-{service}-1"
        self.containers[name] = {
            "Id": name, "Name": "/" + name, "Names": ["/" + name], "State": {"Status": "running"},
            "Config": {"Image": f"agent-hq-{service}"}, "Mounts": mounts,
            "Labels": {"com.docker.compose.project": "agent-hq", "com.docker.compose.service": service},
            "NetworkSettings": {"Networks": {n: {} for n in networks}}}
        return name

    def handler(self, req: httpx.Request) -> httpx.Response:
        path, m = req.url.path, req.method
        self.calls.append((m, path))
        parts = path.strip("/").split("/")
        if parts[0] == "images":
            image = "/".join(parts[1:-1])
            return httpx.Response(200 if image in self.images else 404, json={"message": "no such image"})
        if path == "/containers/create":
            name = req.url.params["name"]
            if name in self.containers:
                return httpx.Response(409, json={"message": f"name {name} in use"})
            body = json.loads(req.content)
            for mnt in body["HostConfig"]["Mounts"]:
                if mnt["Type"] == "volume":
                    self.volumes.setdefault(mnt["Source"], {"Labels": mnt.get("VolumeOptions", {}).get("Labels", {})})
            net = body["HostConfig"]["NetworkMode"]
            self.containers[name] = {"Id": name, "Names": ["/" + name], "State": {"Status": "created"},
                                     "Config": {"Image": body["Image"]}, "body": body,
                                     "Labels": body["Labels"], "NetworkSettings": {"Networks": {net: {}}},
                                     "Mounts": [{"Type": x["Type"], "Name": x["Source"] if x["Type"] == "volume" else None,
                                                 "Source": x["Source"], "Destination": x["Target"], "RW": True}
                                                for x in body["HostConfig"]["Mounts"]]}
            return httpx.Response(201, json={"Id": name})
        if path == "/containers/json":
            want = json.loads(req.url.params["filters"])["label"]
            out = [c for c in self.containers.values()
                   if all(c["Labels"].get(k) == v for k, v in (x.split("=", 1) for x in want))]
            return httpx.Response(200, json=out)
        if parts[0] == "containers":
            c = self.containers.get(parts[1])
            if c is None:
                return httpx.Response(404, json={"message": "no such container"})
            if m == "DELETE":
                del self.containers[parts[1]]
                return httpx.Response(204)
            action = parts[2]
            if action == "json":
                return httpx.Response(200, json=c)
            c["State"]["Status"] = {"start": "running", "restart": "running", "stop": "exited"}[action]
            return httpx.Response(204)
        if parts[0] == "volumes":
            name = parts[1]
            if m == "DELETE":
                self.volumes.pop(name, None)
                return httpx.Response(204)
            v = self.volumes.get(name)
            return httpx.Response(200, json=v) if v is not None else httpx.Response(404, json={"message": "no"})
        if parts[0] == "networks":
            body = json.loads(req.content)
            c = self.containers[body["Container"]]
            if parts[2] == "connect":
                c["NetworkSettings"]["Networks"][parts[1]] = {"Aliases": body["EndpointConfig"]["Aliases"]}
            else:
                c["NetworkSettings"]["Networks"].pop(parts[1], None)
            return httpx.Response(200)
        return httpx.Response(500, json={"message": f"unexpected {m} {path}"})


@pytest.fixture
def docker(monkeypatch):
    fake = FakeDocker()
    monkeypatch.setattr(resources, "docker",
                        lambda: resources.Docker(url="http://docker", transport=httpx.MockTransport(fake.handler)))
    return fake


def _agent(agent_id: str) -> AgentDef:
    with db.session() as s:
        return s.get(AgentDef, agent_id)


async def _put_agent(hq, agent: dict, **changes):
    return await hq.http.put(f"/api/agents/{agent['id']}", json={**agent, **changes})


async def test_dedicated_desktop_is_created_and_given_to_one_agent(hq, docker):
    a = await hq.agent(name="Snake Eyes", description="Browses")
    b = await hq.agent(name="Scarlett", description="Researches")
    r = await hq.http.post("/api/resources", json={"kind": "desktop", "agent_ids": [a["id"]]})
    assert r.status_code == 201, r.text
    res = r.json()
    assert res["name"] == "desktop-1" and res["status"] == "running" and res["managed"]
    assert res["viewer_url"] == "/desktops/desktop-1/"
    assert [x["name"] for x in res["agents"]] == ["Snake Eyes"]

    c = docker.containers["ahq-desktop-1"]["body"]
    assert "SUBFOLDER=/desktops/desktop-1/" in c["Env"]
    assert c["NetworkingConfig"]["EndpointsConfig"]["agent-hq_default"]["Aliases"] == ["desktop-1"]
    assert c["HostConfig"]["Mounts"][0]["Source"] == "ahq-desktop-1"
    assert c["HostConfig"]["ShmSize"] == 1 << 30

    servers = {x["name"]: x for x in res["servers"]}
    assert servers["desktop-1"]["url"] == "http://desktop-1:8765/mcp"
    assert servers["desktop-1-browser"]["url"] == "http://desktop-1:8931/mcp"
    me = _agent(a["id"])
    assert set(me.tool_server_ids) == {x["id"] for x in res["servers"]}
    assert me.desktop_url == "/desktops/desktop-1/"
    assert {t.name for t in agent_tool_servers(me) if t.desktop} == {"desktop-1", "desktop-1-browser"}

    # Someone else can't have a dedicated desktop: not by assignment, not by ticking it.
    r = await hq.http.put(f"/api/resources/{res['id']}/agents", json={"agent_ids": [a["id"], b["id"]]})
    assert r.status_code == 409
    r = await _put_agent(hq, b, tool_server_ids=[res["servers"][0]["id"]])
    assert r.status_code == 409 and "dedicated to Snake Eyes" in r.json()["detail"]

    # A second desktop gets the next name.
    r2 = (await hq.http.post("/api/resources", json={"kind": "desktop"})).json()
    assert r2["name"] == "desktop-2" and r2["agents"] == []


async def test_shared_desktop_goes_to_several_agents_whole(hq, docker):
    a = await hq.agent(name="Snake Eyes", description="Browses")
    b = await hq.agent(name="Scarlett", description="Researches")
    res = (await hq.http.post("/api/resources", json={"kind": "desktop", "scope": "shared"})).json()
    screen = next(x["id"] for x in res["servers"] if x["name"] == "desktop-1")
    # Ticking just one of its servers on the agent page gives the whole desktop.
    r = await _put_agent(hq, b, tool_server_ids=[screen])
    assert r.status_code == 200, r.text
    assert len(r.json()["tool_server_ids"]) == 2 and r.json()["desktop_url"] == "/desktops/desktop-1/"
    r = await hq.http.put(f"/api/resources/{res['id']}/agents", json={"agent_ids": [a["id"], b["id"]]})
    assert {x["name"] for x in r.json()["agents"]} == {"Snake Eyes", "Scarlett"}
    # Can't go back to dedicated while two agents have it.
    assert (await hq.http.put(f"/api/resources/{res['id']}", json={"scope": "dedicated"})).status_code == 409
    # Taking it away clears the viewer it set.
    r = await hq.http.put(f"/api/resources/{res['id']}/agents", json={"agent_ids": [a["id"]]})
    assert [x["name"] for x in r.json()["agents"]] == ["Snake Eyes"]
    assert _agent(b["id"]).tool_server_ids == [] and _agent(b["id"]).desktop_url is None
    assert (await hq.http.put(f"/api/resources/{res['id']}", json={"scope": "dedicated"})).status_code == 200


async def test_workbench_is_locked_down_and_network_access_can_change(hq, docker):
    a = await hq.agent(name="Mainframe", description="Codes")
    res = (await hq.http.post("/api/resources", json={"kind": "workbench", "agent_ids": [a["id"]]})).json()
    assert res["name"] == "workbench-1"
    c = docker.containers["ahq-workbench-1"]
    hc = c["body"]["HostConfig"]
    assert hc["ReadonlyRootfs"] and hc["CapDrop"] == ["ALL"] and hc["NetworkMode"] == "agent-hq_workbench"
    assert any(e.startswith("HTTPS_PROXY=http://workbench-1:") for e in c["body"]["Env"])
    assert "agent-hq_default" not in c["NetworkSettings"]["Networks"]
    assert _agent(a["id"]).desktop_url is None

    r = await hq.http.put(f"/api/resources/{res['id']}", json={"network_access": True})
    assert r.json()["network_access"] is True
    assert "agent-hq_default" in c["NetworkSettings"]["Networks"]
    await hq.http.put(f"/api/resources/{res['id']}", json={"network_access": False})
    assert "agent-hq_default" not in c["NetworkSettings"]["Networks"]


async def test_stop_start_recreate_and_delete(hq, docker):
    a = await hq.agent(name="Snake Eyes", description="Browses")
    res = (await hq.http.post("/api/resources", json={"kind": "desktop", "agent_ids": [a["id"]]})).json()
    rid = res["id"]
    assert (await hq.http.post(f"/api/resources/{rid}/stop")).json()["status"] == "stopped"
    listed = (await hq.http.get("/api/resources")).json()
    assert listed["docker"] is True and listed["resources"][0]["status"] == "stopped"
    assert (await hq.http.post(f"/api/resources/{rid}/start")).json()["status"] == "running"
    assert (await hq.http.post(f"/api/resources/{rid}/recreate")).json()["status"] == "running"
    assert "ahq-desktop-1" in docker.containers and "ahq-desktop-1" in docker.volumes

    # The tool servers belong to the resource: delete it, not them.
    srv = res["servers"][0]["id"]
    assert (await hq.http.delete(f"/api/tool-servers/{srv}")).status_code == 409

    assert (await hq.http.delete(f"/api/resources/{rid}")).status_code == 204
    assert "ahq-desktop-1" not in docker.containers and "ahq-desktop-1" in docker.volumes  # data kept
    me = _agent(a["id"])
    assert me.tool_server_ids == [] and me.desktop_url is None
    with db.session() as s:
        assert s.get(ToolServer, srv) is None

    res = (await hq.http.post("/api/resources", json={"kind": "desktop"})).json()
    assert res["name"] == "desktop-1"  # the name is free again; its old data is reused
    assert (await hq.http.delete(f"/api/resources/{res['id']}?remove_data=true")).status_code == 204
    assert "ahq-desktop-1" not in docker.volumes


async def test_missing_image_or_no_docker(hq, docker, monkeypatch):
    docker.images.clear()
    res = (await hq.http.post("/api/resources", json={"kind": "desktop"})).json()
    assert res["status"] == "error" and "docker compose --profile images build" in res["error"]
    assert len(res["servers"]) == 2  # registered anyway; Recreate once the image is built
    docker.images.add("agent-hq/desktop:latest")
    assert (await hq.http.post(f"/api/resources/{res['id']}/recreate")).json()["status"] == "running"

    monkeypatch.setattr(resources, "docker", lambda: resources.Docker(url=""))
    res = (await hq.http.post("/api/resources", json={"kind": "workbench"})).json()
    assert res["status"] == "unknown" and "no Docker access" in res["error"]
    assert (await hq.http.get("/api/resources")).json()["docker"] is False


async def test_global_tools_reach_every_agent(hq, docker):
    a = await hq.agent(name="Duke", description="Lead")
    r = await hq.http.post("/api/tool-servers", json={"name": "mqtt", "url": "http://mqtt-publisher:8810/mcp",
                                                      "all_agents": True, "prefix_tools": False})
    assert r.status_code == 201, r.text
    assert [t.name for t in agent_tool_servers(_agent(a["id"]))] == ["mqtt"]
    r = await hq.http.post("/api/tool-servers", json={"name": "screen", "url": "http://x:1/mcp",
                                                      "desktop": True, "all_agents": True})
    assert r.status_code == 422
    res = (await hq.http.post("/api/resources", json={"kind": "workbench"})).json()
    srv = res["servers"][0]
    r = await hq.http.put(f"/api/tool-servers/{srv['id']}", json={"name": srv["name"], "url": srv["url"],
                                                                 "all_agents": True})
    assert r.status_code == 422


async def test_compose_desktops_are_adopted_then_taken_over(hq, docker):
    a = await hq.agent(name="Snake Eyes", description="Browses")
    ids = []
    for name, url, desk in (("desktop", "http://desktop-1:8765/mcp", True),
                            ("browser", "http://desktop-1:8931/mcp", True),
                            ("workbench-1", "http://workbench-1:8770/mcp", False)):
        r = await hq.http.post("/api/tool-servers", json={"name": name, "url": url, "desktop": desk})
        ids.append(r.json()["id"])
    await _put_agent(hq, a, tool_server_ids=ids[:2], desktop_url="/desktops/desktop-1/")
    docker.add_compose("desktop-1", [{"Type": "bind", "Source": "/srv/agent-hq/desktops/desktop-1",
                                      "Destination": "/config", "RW": True}])
    docker.add_compose("workbench-2", [{"Type": "volume", "Name": "agent-hq_workbench-2",
                                        "Source": "/var/lib/docker/volumes/x", "Destination": "/work", "RW": True}],
                       networks=("agent-hq_workbench",))

    listed = (await hq.http.get("/api/resources")).json()
    assert [x["name"] for x in listed["adoptable"]] == ["desktop-1", "workbench-1", "workbench-2"]
    made = {x["name"]: x for x in (await hq.http.post("/api/resources/adopt")).json()}
    d1 = made["desktop-1"]
    assert not d1["managed"] and d1["container"] == "agent-hq-desktop-1-1" and d1["status"] == "running"
    assert {x["name"] for x in d1["servers"]} == {"desktop", "browser"}
    assert [x["name"] for x in d1["agents"]] == ["Snake Eyes"] and d1["scope"] == "dedicated"
    # Registered nowhere yet: the workbench in compose gets its tool server made for it.
    assert [x["name"] for x in made["workbench-2"]["servers"]] == ["workbench-2"]
    assert made["workbench-1"]["status"] == "unknown"  # tool server only, no container found
    assert (await hq.http.get("/api/resources")).json()["adoptable"] == []

    # Compose runs it: Agent HQ won't recreate it until you take it over.
    assert (await hq.http.post(f"/api/resources/{d1['id']}/recreate")).status_code == 409
    r = await hq.http.post(f"/api/resources/{d1['id']}/take-over")
    assert r.status_code == 200, r.text
    assert r.json()["managed"] and r.json()["container"] == "ahq-desktop-1"
    assert "agent-hq-desktop-1-1" not in docker.containers
    mounts = docker.containers["ahq-desktop-1"]["body"]["HostConfig"]["Mounts"]
    assert mounts == [{"Type": "bind", "Source": "/srv/agent-hq/desktops/desktop-1", "Target": "/config",
                       "ReadOnly": False}]
    assert docker.containers["ahq-desktop-1"]["body"]["Image"] == "agent-hq/desktop:latest"

    # Detaching an adopted one leaves its tool servers as they were.
    w1 = made["workbench-1"]
    assert (await hq.http.post(f"/api/resources/{w1['id']}/detach")).status_code == 204
    with db.session() as s:
        assert s.get(ToolServer, ids[2]).resource_id is None


async def test_shared_desktop_lease_is_one_conversation_at_a_time(hq):
    r = hq.broker.r
    assert await resources.acquire_lease(r, "res1", thread_id="t1", job_id="j1", agent="Snake Eyes") is None
    # Same conversation (e.g. an agent it delegated to): no waiting.
    assert await resources.acquire_lease(r, "res1", thread_id="t1", job_id="j2", agent="Scarlett") is None
    waited = []

    async def on_wait(cur):
        waited.append(cur["agent"])

    busy = await resources.acquire_lease(r, "res1", thread_id="t2", job_id="j3", agent="Duke",
                                         on_wait=on_wait, wait=0.2)
    assert busy["agent"] == "Scarlett" and waited == ["Scarlett"]
    await resources.release_lease(r, "res1", "j1")  # not the holder any more: no effect
    assert (await resources.lease_holder(r, "res1"))["job_id"] == "j2"
    await resources.release_lease(r, "res1", "j2")
    assert await resources.acquire_lease(r, "res1", thread_id="t2", job_id="j3", agent="Duke", wait=0) is None


async def test_shared_desktop_tools_take_the_lease(hq, docker):
    from app.runtime import ObservedToolset

    from .conftest import SCRIPTS, worker_fn

    SCRIPTS["res-worker"] = worker_fn
    a = await hq.agent(name="Snake Eyes", description="Browses", model="fake:res-worker")
    b = await hq.agent(name="Scarlett", description="Researches", model="fake:res-worker")
    shared = (await hq.http.post("/api/resources", json={"kind": "desktop", "scope": "shared",
                                                         "agent_ids": [a["id"], b["id"]]})).json()
    own = (await hq.http.post("/api/resources", json={"kind": "desktop", "agent_ids": []})).json()
    built = hq.runtime.build_agent(_agent(a["id"]), as_worker=False)
    leases = {t.lease_resource_id for t in built.toolsets if isinstance(t, ObservedToolset) and t.desktop_server_id}
    assert leases == {shared["id"]}
    await hq.http.put(f"/api/resources/{own['id']}/agents", json={"agent_ids": [b["id"]]})
    built = hq.runtime.build_agent(_agent(b["id"]), as_worker=False)
    by_server = {t.desktop_server_id: t.lease_resource_id for t in built.toolsets
                 if isinstance(t, ObservedToolset) and t.desktop_server_id}
    assert sorted(by_server.values(), key=str) == sorted([shared["id"]] * 2 + [None] * 2, key=str)


async def _egress(hq) -> dict:
    return json.loads(await hq.broker.r.get(resources.EGRESS_KEY))


async def test_workbench_signs_in_to_the_egress_proxy_with_its_own_rules(hq, docker):
    import hashlib

    res = (await hq.http.post("/api/resources", json={"kind": "workbench"})).json()
    assert "egress_token" not in res and res["egress_rules"] == [] and res["proxy_current"] is True
    env = docker.containers["ahq-workbench-1"]["body"]["Env"]
    proxy = next(e.split("=", 1)[1] for e in env if e.startswith("HTTPS_PROXY="))
    assert proxy.startswith("http://workbench-1:") and proxy.endswith("@egress:8888")
    assert f"http_proxy={proxy}" in env and f"https_proxy={proxy}" in env
    token = proxy.split(":")[2].split("@")[0]
    cfg = await _egress(hq)
    assert cfg["defaults"] == ["pypi.org:443", "files.pythonhosted.org:443"]
    assert cfg["workbenches"]["workbench-1"] == {"token_sha256": hashlib.sha256(token.encode()).hexdigest(),
                                                 "rules": [], "open": False}

    r = await hq.http.put(f"/api/resources/{res['id']}", json={"egress_rules": [
        "GitGard.home.arpa:3000", " *.github.com ", "192.168.10.0/24:443", "", "*.github.com"]})
    assert r.status_code == 200, r.text
    assert r.json()["egress_rules"] == ["gitgard.home.arpa:3000", "*.github.com", "192.168.10.0/24:443"]
    assert (await _egress(hq))["workbenches"]["workbench-1"]["rules"] == r.json()["egress_rules"]
    for bad in ("bad host!", "pypi.org:99999", "10.0.0.0/33"):
        r = await hq.http.put(f"/api/resources/{res['id']}", json={"egress_rules": [bad]})
        assert r.status_code == 422, bad
    await hq.http.put(f"/api/resources/{res['id']}", json={"network_access": True})
    assert (await _egress(hq))["workbenches"]["workbench-1"]["open"] is True

    # Recreate keeps the token: same proxy sign-in.
    await hq.http.post(f"/api/resources/{res['id']}/recreate")
    assert f"HTTPS_PROXY={proxy}" in docker.containers["ahq-workbench-1"]["body"]["Env"]

    desk = (await hq.http.post("/api/resources", json={"kind": "desktop"})).json()
    assert (await hq.http.put(f"/api/resources/{desk['id']}", json={"egress_rules": ["x.org"]})).status_code == 422
    assert desk["proxy_current"] is None

    await hq.http.delete(f"/api/resources/{res['id']}")
    assert (await _egress(hq))["workbenches"] == {}


async def test_egress_defaults_and_blocked_requests(hq, docker):
    res = (await hq.http.post("/api/resources", json={"kind": "workbench"})).json()
    r = await hq.http.put("/api/resources/network", json={"defaults": ["pypi.org:443", "files.pythonhosted.org:443",
                                                                        "deb.debian.org"]})
    assert r.status_code == 200 and r.json()["defaults"][-1] == "deb.debian.org"
    assert (await _egress(hq))["defaults"][-1] == "deb.debian.org"
    assert (await hq.http.get("/api/resources")).json()["egress_defaults"][-1] == "deb.debian.org"
    assert (await hq.http.put("/api/resources/network", json={"defaults": ["no way"]})).status_code == 422

    for who, host, port in (("workbench-1", "api.github.com", 443), ("workbench-1", "api.github.com", 443),
                            ("workbench-9", "example.com", 443), ("workbench-1", "gitgard.home.arpa", 3000)):
        await hq.broker.r.xadd(resources.EGRESS_BLOCKED, {"workbench": who, "host": host, "port": str(port),
                                                          "reason": "not allowed", "at": "1"})
    mine = (await hq.http.get(f"/api/resources/{res['id']}/blocked")).json()
    assert [(b["host"], b["port"]) for b in mine] == [("gitgard.home.arpa", 3000), ("api.github.com", 443)]
    everyone = (await hq.http.get("/api/resources/network")).json()["blocked"]
    assert {b["workbench"] for b in everyone} == {"workbench-1", "workbench-9"}

    r = await hq.http.post(f"/api/resources/{res['id']}/allow", json={"rule": "gitgard.home.arpa:3000"})
    assert r.json()["egress_rules"] == ["gitgard.home.arpa:3000"]
    assert (await _egress(hq))["workbenches"]["workbench-1"]["rules"] == ["gitgard.home.arpa:3000"]
