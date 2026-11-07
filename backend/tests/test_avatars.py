from __future__ import annotations

import io

from PIL import Image

from app.config import settings


def animated_gif() -> bytes:
    frames = [Image.new("RGB", (64, 64), c) for c in ((220, 30, 30), (30, 30, 220))]
    buf = io.BytesIO()
    frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:], duration=200, loop=0)
    return buf.getvalue()


def png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", (512, 300), (0, 160, 90, 255)).save(buf, format="PNG")
    return buf.getvalue()


async def _upload(hq, agent_id, data, name="a.gif", ctype="image/gif"):
    return await hq.http.put(f"/api/agents/{agent_id}/avatar", files={"file": (name, data, ctype)})


async def test_animated_gif_avatar_and_still(hq):
    a = await hq.agent(name="Duke-ish", model="fake:worker")
    gif = animated_gif()
    r = await _upload(hq, a["id"], gif)
    assert r.status_code == 200
    assert r.json()["avatar_version"] == 1 and r.json()["avatar_animated"] is True

    got = await hq.http.get(f"/api/agents/{a['id']}/avatar")
    assert got.headers["content-type"] == "image/gif" and got.content == gif  # served untouched
    assert "immutable" in got.headers["cache-control"]
    with Image.open(io.BytesIO(got.content)) as im:
        assert im.n_frames == 2  # still animated

    still = await hq.http.get(f"/api/agents/{a['id']}/avatar/still")
    assert still.headers["content-type"] == "image/png"
    with Image.open(io.BytesIO(still.content)) as im:
        assert im.getpixel((5, 5))[:3] == (220, 30, 30)  # the first frame


async def test_replace_with_static_image_bumps_version(hq):
    a = await hq.agent(name="Pic", model="fake:worker")
    await _upload(hq, a["id"], animated_gif())
    r = await _upload(hq, a["id"], png(), "p.png", "image/png")
    assert r.json()["avatar_version"] == 2 and r.json()["avatar_animated"] is False
    still = await hq.http.get(f"/api/agents/{a['id']}/avatar/still")
    with Image.open(io.BytesIO(still.content)) as im:
        assert max(im.size) == 256  # thumbnailed


async def test_rejects_non_images_and_oversize(hq, monkeypatch):
    a = await hq.agent(name="Strict", model="fake:worker")
    r = await _upload(hq, a["id"], b"not an image at all", "x.txt", "text/plain")
    assert r.status_code == 415
    monkeypatch.setattr(settings, "avatar_max_bytes", 100)
    r = await _upload(hq, a["id"], animated_gif())
    assert r.status_code == 413


async def test_saving_agent_keeps_avatar_and_delete_removes_it(hq):
    a = await hq.agent(name="Keep", model="fake:worker")
    a = (await _upload(hq, a["id"], animated_gif())).json()
    a["description"] = "edited"
    a = (await hq.http.put(f"/api/agents/{a['id']}", json=a)).json()
    assert a["avatar_version"] == 1
    a = (await hq.http.delete(f"/api/agents/{a['id']}/avatar")).json()
    assert a["avatar_version"] == 0
    assert (await hq.http.get(f"/api/agents/{a['id']}/avatar")).status_code == 404


async def test_avatars_travel_with_export_import(hq):
    a = await hq.agent(name="Traveler", model="fake:worker")
    gif = animated_gif()
    await _upload(hq, a["id"], gif)
    exported = (await hq.http.get("/api/agents/export")).json()
    assert exported["agents"][0]["avatar"]["content_type"] == "image/gif"
    await hq.http.delete(f"/api/agents/{a['id']}/avatar")
    await hq.http.post("/api/agents/import", json=exported)
    assert (await hq.http.get(f"/api/agents/{a['id']}/avatar")).content == gif
