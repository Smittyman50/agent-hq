"""Agent Skills (SKILL.md) import and export, and skill upkeep: staleness, archiving and
restoring, and the scheduled skill review."""

from __future__ import annotations

import base64
import io
import subprocess
import zipfile
from datetime import datetime, timedelta, timezone

from sqlmodel import select

from app import db, evals, skill_upkeep, skills, skills_io
from app.models import Skill, SkillProposal, SkillUse

BACKUPS = {"name": "check_pve_backups", "title": "Check Proxmox backups",
           "when_to_use": "When asked whether last night's Proxmox backups worked.",
           "body": "1. Open PBS on rewind.\n2. Check the TrueNAS datastore's last job.\n3. Report failures first.",
           "tools": ["proxmox_list_tasks", "pbs_status"], "script": "pbs_report"}

PDF_MD = """---
name: pdf-processing
description: Extract PDF text, fill forms, merge files. Use when handling PDFs.
license: Apache-2.0
compatibility: Requires Python 3.12+ and pypdf
metadata:
  author: example-org
  version: "1.0"
allowed-tools: Bash(python:*) Read
---

# PDF processing

## Extract text
Run `scripts/extract.py <file>`.

See [the reference](references/REFERENCE.md).
"""
PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + bytes(range(40))


def _zip(entries: dict[str, bytes | str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for path, data in entries.items():
            z.writestr(path, data)
    return buf.getvalue()


async def _upload(hq, files: list[tuple[str, bytes]], **form):
    r = await hq.http.post("/api/skills/import", files=[("files", (n, d)) for n, d in files],
                           data={"paths": __import__("json").dumps([n for n, _ in files]), **form})
    assert r.status_code == 200, r.text
    return r.json()


async def _approve(hq, proposal_id, **edits):
    r = await hq.http.post(f"/api/skill-proposals/{proposal_id}/decide", json={"approve": True, **edits})
    assert r.status_code == 200, r.text
    return r.json()


def _fields(k: dict) -> dict:
    return {f: k[f] for f in ("name", "title", "when_to_use", "body", "tools", "script", "frontmatter")}


# --------------------------------------------------------------------------- the format


def test_skill_md_maps_onto_agent_hq_fields():
    p = skills_io.parse(PDF_MD, folder="pdf-processing")
    assert p.name == "pdf_processing" and p.title == "PDF processing"
    assert p.when_to_use.startswith("Extract PDF text") and p.body.startswith("## Extract text")
    assert p.tools == ["Bash(python:*)", "Read"]
    assert p.frontmatter == {"license": "Apache-2.0", "compatibility": "Requires Python 3.12+ and pypdf",
                             "metadata": {"author": "example-org", "version": "1.0"}}
    # No heading: the title comes from the name.
    bare = skills_io.parse("---\nname: tidy-dns\ndescription: When DNS records pile up.\n---\nDelete stale records.")
    assert bare.title == "Tidy dns" and bare.body == "Delete stale records."
    for bad, why in (("no frontmatter", "starts with YAML"), ("---\nname: x\n---\nbody", "no description"),
                     ("---\n: [\n---\n", "valid YAML")):
        try:
            skills_io.parse(bad)
            raise AssertionError("should not parse")
        except ValueError as e:
            assert why in str(e)


def test_render_then_parse_gives_back_the_same_skill():
    k = Skill(name="check_pve_backups", title="Check Proxmox backups: PBS & TrueNAS",
              when_to_use="When asked: did the backups work?\nAlso after a restore.",
              body="# Heading inside\n\n1. Open PBS.\n   - indented: yes\n2. Done #2",
              tools=["a", "b"], script="pbs_report",
              frontmatter={"license": "MIT", "metadata": {"author": "Mike"}})
    text = skills_io.render(k)
    assert text.startswith("---\nname: check-pve-backups\ndescription: |")
    assert "agent-hq-script: pbs_report" in text and "allowed-tools: a b" in text
    p = skills_io.parse(text)
    assert (p.name, p.title, p.when_to_use, p.body, p.tools, p.script, p.frontmatter) == (
        k.name, k.title, k.when_to_use, k.body, k.tools, k.script, k.frontmatter)


def test_git_links_to_folders_become_clone_url_branch_and_path():
    assert skills_io.parse_git_url("https://github.com/anthropics/skills/tree/main/skills/pdf") == (
        "https://github.com/anthropics/skills", "main", "skills/pdf")
    assert skills_io.parse_git_url("https://gitgard.home.arpa/smittyman/skills/src/branch/dev/ops/SKILL.md") == (
        "https://gitgard.home.arpa/smittyman/skills", "dev", "ops")
    assert skills_io.parse_git_url("https://gitlab.com/g/r/-/tree/main/x") == ("https://gitlab.com/g/r", "main", "x")
    assert skills_io.parse_git_url("https://github.com/o/r.git", "main", "/a/") == ("https://github.com/o/r.git", "main", "a")


# --------------------------------------------------------------------------- round trip


async def test_a_skill_exported_and_imported_back_is_unchanged(hq):
    made = (await hq.http.post("/api/skills", json=BACKUPS)).json()
    md = await hq.http.get(f"/api/skills/{made['id']}/export")
    assert md.headers["content-type"].startswith("text/markdown") and "SKILL.md" in md.headers["content-disposition"]
    assert md.text.startswith("---\nname: check-pve-backups\n")

    # Into the same Agent HQ: nothing to do.
    again = await _upload(hq, [("SKILL.md", md.content)])
    assert again["counts"] == {"unchanged": 1}
    assert (await hq.http.get("/api/skill-proposals")).json() == []

    # Into one without it (here: after renaming the original away): the same skill comes back.
    await hq.http.put(f"/api/skills/{made['id']}", json={**BACKUPS, "name": "old_backups"})
    fresh = await _upload(hq, [("SKILL.md", md.content)])
    [res] = fresh["results"]
    assert res["status"] == "proposed" and not res["revision"]
    await _approve(hq, res["proposal_id"])
    [back] = [k for k in (await hq.http.get("/api/skills")).json() if k["name"] == "check_pve_backups"]
    assert _fields(back) == {**_fields({**made, "name": "check_pve_backups"})}
    assert back["origin"] == "SKILL.md"


async def test_an_imported_folder_with_files_round_trips_through_a_zip(hq):
    folder = [("pdf-processing/SKILL.md", PDF_MD.encode()),
              ("pdf-processing/scripts/extract.py", b"import sys\nprint(open(sys.argv[1]).read())\n"),
              ("pdf-processing/references/REFERENCE.md", b"# Reference\nForms live in AcroForm.\n"),
              ("pdf-processing/assets/logo.png", PNG),
              ("pdf-processing/.git/config", b"ignored")]
    got = await _upload(hq, folder)
    [res] = got["results"]
    assert res["status"] == "proposed" and got["problems"] == []
    p = (await hq.http.get(f"/api/skill-proposals/{res['proposal_id']}")).json()
    assert p["source"] == "import" and p["origin"] == "pdf-processing/" and p["status"] == "pending"
    assert sorted(p["files"]) == ["assets/logo.png", "references/REFERENCE.md", "scripts/extract.py"]
    await _approve(hq, p["id"])
    [k] = (await hq.http.get("/api/skills")).json()
    assert {f["path"]: f["text"] for f in k["files"]} == {
        "assets/logo.png": False, "references/REFERENCE.md": True, "scripts/extract.py": True}
    ref = (await hq.http.get(f"/api/skills/{k['id']}/files", params={"path": "references/REFERENCE.md"})).json()
    assert ref["content"].startswith("# Reference")

    z = await hq.http.get(f"/api/skills/{k['id']}/export")  # it has files: a zip
    assert z.headers["content-type"] == "application/zip"
    names = sorted(zipfile.ZipFile(io.BytesIO(z.content)).namelist())
    assert names == ["pdf-processing/SKILL.md", "pdf-processing/assets/logo.png",
                     "pdf-processing/references/REFERENCE.md", "pdf-processing/scripts/extract.py"]
    assert zipfile.ZipFile(io.BytesIO(z.content)).read("pdf-processing/assets/logo.png") == PNG
    exported = zipfile.ZipFile(io.BytesIO(z.content)).read("pdf-processing/SKILL.md").decode()
    assert "license: Apache-2.0" in exported and "author: example-org" in exported
    assert (await _upload(hq, [("pdf-processing.zip", z.content)]))["counts"] == {"unchanged": 1}

    # Agents see the files and read them.
    with db.session() as s:
        row = s.get(Skill, k["id"])
    assert skills.read_file(row, "scripts/extract.py")["content"].startswith("import sys")
    assert skills.read_file(row, "assets/logo.png")["note"].startswith("a binary file")
    assert "error" in skills.read_file(row, "../etc/passwd")


async def test_a_zip_of_many_skills_imports_each_and_reports_the_bad_ones(hq):
    data = _zip({"skills/a-one/SKILL.md": "---\nname: a-one\ndescription: When doing the first thing.\n---\n# One\n\nDo the first thing carefully.",
                 "skills/b-two/SKILL.md": "---\nname: b-two\ndescription: When doing the second thing.\n---\n# Two\n\nDo the second thing carefully.",
                 "skills/b-two/notes.txt": "extra",
                 "skills/broken/SKILL.md": "no frontmatter here",
                 "README.md": "a hub"})
    got = await _upload(hq, [("hub.zip", data)])
    assert sorted(r["name"] for r in got["results"]) == ["a_one", "b_two"]
    assert got["counts"] == {"proposed": 2}
    assert any("skills/broken/SKILL.md" in p for p in got["problems"])
    b = next(r for r in got["results"] if r["name"] == "b_two")
    assert (await hq.http.get(f"/api/skill-proposals/{b['proposal_id']}")).json()["files"] == ["notes.txt"]
    # Importing the same thing while it waits: refused, not duplicated.
    dup = await _upload(hq, [("hub.zip", data)])
    assert dup["counts"] == {"refused": 2}
    none = await _upload(hq, [("readme.txt", b"hello")])
    assert none["found"] == 0 and none["problems"] == ["no SKILL.md found"]


async def test_skills_import_from_a_git_repository(hq, tmp_path):
    repo = tmp_path / "hub"
    for name in ("dns-tidy", "cert-renew"):
        (repo / "skills" / name).mkdir(parents=True)
        (repo / "skills" / name / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: When the {name} job comes up.\n---\n# {name}\n\nThe {name} steps, in order.")
    (repo / "skills" / "cert-renew" / "renew.sh").write_text("certbot renew\n")
    git = ["git", "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(git + ["-C", str(repo), "add", "."], check=True)
    subprocess.run(git + ["-C", str(repo), "commit", "-qm", "skills"], check=True)

    r = await hq.http.post("/api/skills/import/git", json={"url": f"file://{repo}", "path": "skills/cert-renew"})
    assert r.status_code == 200, r.text
    [one] = r.json()["results"]
    assert one["name"] == "cert_renew" and one["where"] == "skills/cert-renew/SKILL.md"
    p = (await hq.http.get(f"/api/skill-proposals/{one['proposal_id']}")).json()
    assert p["files"] == ["renew.sh"] and p["origin"] == f"file://{repo}:skills/cert-renew"
    everything = (await hq.http.post("/api/skills/import/git", json={"url": f"file://{repo}"})).json()
    assert everything["counts"] == {"proposed": 1, "refused": 1}  # cert_renew is already waiting
    bad = await hq.http.post("/api/skills/import/git", json={"url": f"file://{tmp_path}/nope"})
    assert bad.status_code == 422 and "Couldn't read the repository" in bad.json()["detail"]


# --------------------------------------------------------------------------- imports and evals


async def test_imports_are_proposals_that_run_the_evals_even_in_auto_mode(hq):
    await hq.http.put("/api/evals/settings", json={"judge_model": "fake:judge"})
    a = await hq.agent(name="Duke", model="fake:skilled", learning_mode="auto")
    await hq.http.post("/api/evals/cases", json={
        "agent_id": a["id"], "prompt": "write the report", "skill_names": ["house_style"],
        "checks": [{"type": "contains", "value": "TABLES FIRST"}]})
    text = ("---\nname: house-style\ndescription: When writing any report or answer.\n---\n"
            "# House style\n\nTABLES FIRST, then a short summary.")
    r = await hq.http.post("/api/skills/import/text", json={"text": text})
    [res] = r.json()["results"]
    assert res["status"] == "proposed" and res["tests"] == 1
    assert (await hq.http.get("/api/skills")).json() == []  # never applied straight away
    [run] = await evals.run_due(hq.runtime)
    assert run.summary == {"without": {"passed": 0, "total": 1}, "with": {"passed": 1, "total": 1}}
    p = (await hq.http.get(f"/api/skill-proposals/{res['proposal_id']}")).json()
    assert p["status"] == "pending" and p["eval_summary"]["regressed"] is False
    assert p["agent_name"] == "Import" and (await hq.http.get("/api/inbox/count")).json()["skills"] == 1
    await _approve(hq, p["id"])
    [k] = (await hq.http.get("/api/skills")).json()
    assert k["name"] == "house_style" and k["origin"] == "pasted SKILL.md"

    # An updated copy is a revision with a diff; approving it makes version 2.
    newer = text.replace("TABLES FIRST, then a short summary.", "TABLES FIRST, then a short summary and sources.")
    [res2] = (await hq.http.post("/api/skills/import/text", json={"text": newer})).json()["results"]
    assert res2["revision"]
    p2 = (await hq.http.get(f"/api/skill-proposals/{res2['proposal_id']}")).json()
    assert p2["skill_id"] == k["id"] and "+TABLES FIRST, then a short summary and sources." in p2["diff"]
    await evals.run_due(hq.runtime)
    await _approve(hq, p2["id"])
    k2 = (await hq.http.get(f"/api/skills/{k['id']}")).json()
    assert k2["version"] == 2 and k2["versions"][0]["note"].startswith("imported from pasted SKILL.md")


async def test_importing_an_archived_skill_again_restores_it(hq):
    made = (await hq.http.post("/api/skills", json=BACKUPS)).json()
    md = (await hq.http.get(f"/api/skills/{made['id']}/export")).content
    await hq.http.post(f"/api/skills/{made['id']}/archive", json={"reason": "not needed"})
    assert (await _upload(hq, [("SKILL.md", md)]))["results"][0]["summary"].endswith("(archived)")
    changed = md.replace(b"Report failures first.", b"Report failures first, then warnings.")
    [res] = (await _upload(hq, [("SKILL.md", changed)]))["results"]
    await _approve(hq, res["proposal_id"])
    k = (await hq.http.get(f"/api/skills/{made['id']}")).json()
    assert k["status"] == "active" and k["version"] == 2 and k["archived_reason"] == ""


async def test_imports_for_one_agent_and_bad_scopes(hq):
    a = await hq.agent(name="Scarlett", model="fake:worker")
    text = "---\nname: mine-only\ndescription: When Scarlett does her thing.\n---\n# Mine\n\nScarlett's own steps."
    r = await hq.http.post("/api/skills/import/text", json={"text": text, "scope": "agent", "agent_id": a["id"]})
    pid = r.json()["results"][0]["proposal_id"]
    await _approve(hq, pid)
    [k] = (await hq.http.get("/api/skills")).json()
    assert k["scope"] == "agent" and k["agent_id"] == a["id"]
    assert (await hq.http.post("/api/skills/import/text", json={"text": text, "scope": "agent"})).status_code == 422
    assert (await hq.http.post("/api/skills/import/text", json={"text": "nope"})).status_code == 422


async def test_export_all_is_a_zip_of_folders(hq):
    await hq.http.post("/api/skills", json=BACKUPS)
    await hq.http.post("/api/skills", json={**BACKUPS, "name": "tidy_dns", "title": "Tidy DNS"})
    z = await hq.http.get("/api/skills/export")
    assert sorted(zipfile.ZipFile(io.BytesIO(z.content)).namelist()) == [
        "check-pve-backups/SKILL.md", "tidy-dns/SKILL.md"]
    assert (await _upload(hq, [("all.zip", z.content)]))["counts"] == {"unchanged": 2}


# --------------------------------------------------------------------------- upkeep


def _age(skill_id: str, days: int) -> None:
    with db.session() as s:
        k = s.get(Skill, skill_id)
        k.created_at = datetime.now(timezone.utc) - timedelta(days=days)
        s.add(k)
        s.commit()


def _use(skill_id: str, days_ago: int, rating: str | None = None) -> None:
    with db.session() as s:
        s.add(SkillUse(skill_id=skill_id, agent_id="a", outcome="done", rating=rating,
                       at=datetime.now(timezone.utc) - timedelta(days=days_ago)))
        s.commit()


async def test_the_skills_list_shows_usage_and_staleness(hq):
    old = (await hq.http.post("/api/skills", json=BACKUPS)).json()
    idle = (await hq.http.post("/api/skills", json={**BACKUPS, "name": "idle_one", "title": "Idle"})).json()
    loved = (await hq.http.post("/api/skills", json={**BACKUPS, "name": "loved_one", "title": "Loved"})).json()
    disliked = (await hq.http.post("/api/skills", json={**BACKUPS, "name": "disliked", "title": "Disliked"})).json()
    _age(old["id"], 90)
    _age(idle["id"], 200)
    _use(idle["id"], 75)
    _use(loved["id"], 1, "up")
    for r in ("down", "down", "up"):
        _use(disliked["id"], 2, r)
    rows = {k["name"]: k for k in (await hq.http.get("/api/skills")).json()}
    assert rows["check_pve_backups"]["stale"] == ["never used (added 90 days ago)"]
    assert rows["idle_one"]["stale"] == ["unused for 75 days"] and rows["idle_one"]["stats"]["uses"] == 1
    assert rows["loved_one"]["stale"] == [] and rows["loved_one"]["stats"]["up"] == 1
    assert rows["loved_one"]["stats"]["last_used_at"]
    assert rows["disliked"]["stale"] == ["poorly rated (👎 2 of 3)"]
    stale = [k["name"] for k in (await hq.http.get("/api/skills?stale=true")).json()]
    assert stale == ["check_pve_backups", "disliked", "idle_one"]
    await hq.http.put("/api/skills/upkeep", json={"stale_after_days": 0, "min_ratings": 5})
    assert (await hq.http.get("/api/skills?stale=true")).json() == []


async def test_archive_and_restore(hq):
    k = (await hq.http.post("/api/skills", json=BACKUPS)).json()
    got = (await hq.http.post(f"/api/skills/{k['id']}/archive", json={"reason": "replaced by PBS alerts"})).json()
    assert got["status"] == "archived" and got["archived_reason"] == "replaced by PBS alerts" and got["archived_at"]
    assert (await hq.http.get("/api/skills")).json() == []
    assert [x["name"] for x in (await hq.http.get("/api/skills?status=archived")).json()] == ["check_pve_backups"]
    a = await hq.agent(name="Duke", model="fake:worker")
    from app.runtime import load_agent
    assert skills.visible_to(load_agent(a["id"])) == []
    back = (await hq.http.post(f"/api/skills/{k['id']}/restore")).json()
    assert back["status"] == "active" and back["archived_at"] is None and back["version"] == 1
    # Rows from before archiving had "retired": they read as archived.
    with db.session() as s:
        row = s.get(Skill, k["id"])
        row.status = "retired"
        s.add(row)
        s.commit()
    [old] = (await hq.http.get("/api/skills?status=archived")).json()
    assert old["status"] == "archived"
    assert (await hq.http.post(f"/api/skills/{k['id']}/status", json={"status": "active"})).json()["status"] == "active"


async def test_the_skill_review_proposes_merges_and_archiving_you_approve(hq):
    await hq.http.put("/api/skills/upkeep", json={"model": "fake:curator"})
    keep = (await hq.http.post("/api/skills", json=BACKUPS)).json()
    twin = (await hq.http.post("/api/skills", json={**BACKUPS, "name": "pve_backup_check"})).json()
    old = (await hq.http.post("/api/skills", json={**BACKUPS, "name": "old_thing", "title": "Old thing"})).json()
    _age(old["id"], 120)
    a = await hq.agent(name="Duke", model="fake:skilled")
    await hq.http.post("/api/evals/cases", json={
        "agent_id": a["id"], "prompt": "backups?", "skill_names": ["pve_backup_check"],
        "checks": [{"type": "contains", "value": "Following"}]})

    r = await skill_upkeep.review(hq.runtime, trigger="manual")
    assert r.status == "done", r.error
    assert "no_such_skill" in r.summary and r.model.startswith("fake:curator")
    props = {p["action"]: p for p in (await hq.http.get("/api/skill-proposals?status=pending")).json()}
    assert set(props) == {"archive", "merge"}
    merge, arch = props["merge"], props["archive"]
    assert merge["agent_name"] == "Skill review" and merge["source"] == "curator"
    assert merge["skill_id"] == keep["id"] and [m["name"] for m in merge["merging"]] == ["pve_backup_check"]
    assert "+1. One merged procedure" in merge["diff"]
    assert arch["skill_id"] == old["id"]
    [listed] = (await hq.http.get("/api/skills/reviews")).json()
    assert {p["action"] for p in listed["proposals"]} == {"archive", "merge"}

    # The merge is tested against the case that names the skill it folds in.
    [run] = await evals.run_due(hq.runtime)
    assert run.proposal_id == merge["id"] and set(run.summary) == {"with", "without"}

    await _approve(hq, merge["id"])
    await _approve(hq, arch["id"])
    rows = {k["name"]: k for k in (await hq.http.get("/api/skills?status=all")).json()}
    assert rows["check_pve_backups"]["version"] == 2 and rows["check_pve_backups"]["status"] == "active"
    assert rows["pve_backup_check"]["status"] == "archived"
    assert rows["pve_backup_check"]["archived_reason"] == "merged into check_pve_backups"
    assert rows["old_thing"]["status"] == "archived"
    # A second review leaves alone what's already decided and proposes nothing new for them.
    with db.session() as s:
        pending = s.exec(select(SkillProposal).where(SkillProposal.status == "pending")).all()
    assert pending == []


async def test_the_review_runs_on_its_schedule(hq):
    await hq.http.post("/api/skills", json=BACKUPS)
    assert await skill_upkeep.run_if_due(hq.runtime) is None  # off by default
    got = (await hq.http.put("/api/skills/upkeep", json={"review": "weekly", "review_weekday": 0,
                                                          "review_hour": 3, "model": "fake:curator"})).json()
    nxt = datetime.fromisoformat(got["next_review_at"])
    assert nxt > datetime.now(timezone.utc)
    assert await skill_upkeep.run_if_due(hq.runtime) is None  # not yet
    r = await skill_upkeep.run_if_due(hq.runtime, now=nxt + timedelta(minutes=1))
    assert r is not None and r.trigger == "schedule" and r.status == "done"
    after = (await hq.http.get("/api/skills/upkeep")).json()
    assert datetime.fromisoformat(after["next_review_at"]) > nxt and after["last_review_at"]
    bad = await hq.http.put("/api/skills/upkeep", json={"review": "hourly"})
    assert bad.status_code == 422


async def test_run_review_now_from_the_api(hq):
    await hq.http.put("/api/skills/upkeep", json={"model": "fake:curator"})
    await hq.http.post("/api/skills", json=BACKUPS)
    r = await hq.http.post("/api/skills/reviews")
    assert r.status_code == 202 and r.json()["status"] == "running"
    import asyncio
    for _ in range(50):
        [row] = (await hq.http.get("/api/skills/reviews")).json()
        if row["status"] != "running":
            break
        await asyncio.sleep(0.05)
    assert row["status"] == "done" and row["trigger"] == "manual"
