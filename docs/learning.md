# Learning

How agents get better with use: skills, the learning loop, your profile and evals. Back to the [overview](../README.md).

## Skills: know-how agents write down and improve

A **skill** is a short, named procedure. It has a title, a line saying *when to use it*, the steps in markdown (including what to check and the pitfalls), the tools it relies on, and optionally the name of a saved workbench script that does the mechanical part. A skill is either shared by every agent or belongs to one agent. You can write and edit skills under **Settings → Skills**. Every change is a new version, with who made it and why, and you can restore any earlier version.

**How agents use them:**
- **Listing:** on each run, an agent's instructions list up to 8 skills that fit the task, each with its name and *when to use* line.
  - **How they're matched:** by meaning when an embedding model is set (Settings → Memory), and by keywords otherwise.
  - **Small collections:** with 12 or fewer skills, all of them are listed.
- **Loading:** the agent calls `load_skill` to read a skill's full procedure and follows it. `list_skills` searches the rest.
- **Files:** a skill can come with files (scripts, reference docs, templates), as imported skills often do. `load_skill` lists them and the agent reads one with `read_skill_file` when the procedure points to it.
- **Tracking:** each time a skill is loaded, the outcome of that job (done, partial or failed) is recorded, along with your 👍/👎 on the reply. **Settings → Skills** shows each skill's uses, when it was last used, its success rate and its ratings.

**How agents learn.** When an agent works out how to do something that will come up again, or finds that a skill it used was wrong or incomplete, it calls `propose_skill`. A proposal is either a new skill, or a revision to an existing one with a diff against the current version. What happens next depends on the agent's **Learning** setting (Settings → Agents):
- **Propose to me** (the default for every agent):
  - **Where it shows up:** in the conversation, in the **Requests** inbox (with the sidebar badge), and on your [notification channels](notifications.md#notifications-telegram-discord-and-mqtt) as a `skill_proposal` notice.
  - **Your options:** approve it, edit it first, or reject it, with an optional note.
  - **Revisions** show a diff against the current version, with a warning if the skill changed after the agent wrote its proposal.
- **Save automatically:** the change is saved at once and you're told. You can review it or roll it back on the Skills page. If there are test cases for that agent or skill (see Evals), the change is tested first. It's applied only if it doesn't lower the pass rate; otherwise it waits for you.
- **Off:** the agent uses skills but can't propose them.

Skills can come from five places besides your own writing: an agent's `propose_skill` call, a reflection after a job (see the learning loop), a document in the knowledge library ("Propose skills" turns a runbook's procedures into proposals), an import (below), or the skill review (below). Each proposal records where it came from. Sub-agents use their parent's skills but don't propose any. Skills never grant tools: every call a skill leads to still goes through your tool approvals.

### Importing and exporting skills (SKILL.md)

Agent HQ reads and writes the [Agent Skills](https://agentskills.io) format, the one Hermes skills and the Skills Hub use. A skill is a folder with a `SKILL.md` (YAML frontmatter, then markdown instructions) and any other files it needs, such as `scripts/`, `references/` or `assets/`.

| SKILL.md | Agent HQ |
|---|---|
| `name` (hyphens) | name (underscores): `check-pve-backups` is `check_pve_backups` |
| `description` | *When to use* |
| the first `# Heading` of the body | title (the rest of the body is the procedure) |
| `allowed-tools` | tools it relies on |
| `metadata.agent-hq-script` | workbench script |
| `license`, `compatibility`, other `metadata` | kept as they are, and written back on export |
| other files in the folder | the skill's files, which agents read with `read_skill_file` |

**Export** (Settings → Skills): **Export** on a skill downloads its `SKILL.md`, or a zip of its folder when it has files. **Export all** downloads every active skill (or every archived one, from the Archived tab) as a zip with one folder per skill. A skill exported from Agent HQ and imported back is unchanged.

**Import** (Settings → Skills → **Import**) takes:
- **a file:** a `SKILL.md`, or a zip with one skill or many;
- **a folder:** a skill's folder, or a folder of skills, with all their files;
- **pasted text:** a `SKILL.md`, frontmatter included;
- **a Git repository:** every `SKILL.md` in it, or only those under a path. A link to a folder works too (GitHub `/tree/<branch>/<path>`, Gitea `/src/branch/<branch>/<path>`, GitLab `/-/tree/...`). For a private repo, give a username and token; they're used for that one shallow clone and not kept.

Choose whether new skills are for every agent or for one. Every skill found becomes a **proposal** in Requests → Skill proposals, marked *import*, with where it came from. It is never applied straight away, not even for agents in Auto learning mode:
- **Tests:** it runs through your test cases without and with it, like any other skill change. A new skill for every agent is tested against all active cases, since any agent might pick it up. A skill for one agent is tested against that agent's cases, plus any that name it.
- **Updates:** a skill that's already here comes in as a revision with a diff (files included). One that's identical is reported as *unchanged* and skipped. Approving an update to an archived skill restores it.
- **Limits:** up to 50 skills per import, 200 files and 5 MB per skill, and 50 MB unpacked. Folders named `.git`, `node_modules` and `__MACOSX` are skipped. Problems, such as a `SKILL.md` without frontmatter, are listed after the import.

### Upkeep: stale skills, the skill review and archiving

- **Stale skills.** A skill is flagged *stale* when nobody has used it for 60 days, or, if it was never used, when it was added more than 60 days ago. It's also flagged when its ratings are poor: at least 3 👍/👎, with half or more of them 👎. The **Stale** tab lists them, and each stale skill says why. Nothing happens to them by itself. All three thresholds are under **Upkeep & review**.
- **The skill review** (optional). A model you choose looks over every active skill: what it says, how much it's used, how it's rated, and whether it's stale. It then proposes up to 5 changes:
  - **Merge:** skills that do the same job become one. The kept skill gets a new version, and the others are archived.
  - **Rewrite:** a new version of a poorly rated, failing or unclear skill.
  - **Archive:** a skill that's no longer used or no longer worth keeping.
- **How the review runs.** Each change is a proposal in Requests → Skill proposals, marked *review*, and is tested by the evals first: against the cases that name the skills involved and the cases of agents that have used them. Nothing changes until you approve it. The review leaves alone any skill that already has a proposal waiting.
- **When it runs:** daily, weekly or monthly at an hour you pick (in `AHQ_TIMEZONE`), or only when you press **Run review now**. Recent reviews and what came of them are listed under Upkeep & review.
- **Its model:** any provider and model, like the evals judge. Left empty, it uses the judge, or else your first agent's model.
- **Archiving.** **Archive** on a skill, with an optional reason, hides it from agents but keeps its history and usage. The **Archived** tab lists archived skills, and **Restore** brings one back as it was.

## The learning loop: ratings, corrections and reflections

**Rate replies.** Every agent reply has 👍 and 👎 buttons. A 👎 opens a box for *what it should have done*, which is the most useful thing an agent can learn from. Ratings show on each skill's stats.

**Corrections count too.** A reply such as "No, use the TrueNAS datastore" or "from now on, failures first" is recognised as a correction of the job the agent just did.

**Reflection.** After a job that's worth a look, the agent rereads the conversation, its tool calls, how the job ended and what you said about it. A job is worth a look when:
- you rated it 👎, or 👍 on work that took several steps;
- you corrected it;
- it failed or only partly worked;
- or it made 4+ tool calls.

It then may do three things:
- **propose skills,** new or improved, through the same path as `propose_skill`, so your learning mode applies;
- **save lessons** to its memory, and post "noted for next time: …" in the conversation;
- **suggest a line for your profile** when you stated a standing rule.

**Settings → Learning** controls all of this and keeps a log of every reflection: why it ran, what it concluded, and links to what it proposed. Each reflection has **Run now** and **Cancel**.
- **Reasons to reflect:** you choose which ones count, or reflect after every job, or never.
- **Waiting:** a reflection waits 10 minutes after a job so your follow-ups are included, and 2 minutes after a rating.
- **Daily limit:** 30 by default.
- **Model:** each agent's own, unless you pick a reflection model; a stronger one writes better skills for small local agents.
- **Who never reflects:** agents whose learning is Off, and sub-agents.

**Requests** has a sub-menu like Settings: **Tool approvals**, **Goals** (see [Standing goals](agents.md#standing-goals-the-board)), **Skill proposals**, **Profile updates**, **Knowledge** (documents agents suggest) and **Sub-agent requests**. Each has its own count, and the sidebar badge shows the total.

## Your profile

**Settings → Profile** holds what every agent should always know about you: who you are, how you like things done, your environment. Memory comes up when it matches a task; the profile goes into every agent's instructions, every time.
- **Size:** keep it short. Anything past the word limit (600 by default) is cut, so it doesn't crowd small models.
- **Opting out:** an agent can opt out under Settings → Agents → Learning ("Knows your profile").
- **Suggestions:** reflections suggest additions when you state a standing preference. Suggestions show up in the conversation, in **Requests → Profile updates**, and on your notification channels as a `profile_proposal` notice. Nothing changes your profile without your approval, and you can reword a suggestion first.

## Evals

**Settings → Evals** shows whether agents, and the skills they learn, actually get better. A **test case** is three things:
- a request for an agent;
- the checks its answer must pass: a rubric graded by the judge, the reply contains or doesn't contain some text, calls or doesn't call a tool, loads a skill;
- the tool results recorded from the job it came from.

**Replay, so tests never touch your lab:**
- **Recorded calls:** a call that matches the recording gets the recorded result.
- **Calls with no recording:** they run for real only if the tool is set to Allow *and* ticked **Safe in tests** (Settings → Tools). Anything else is told "not available in a test run", and the result shows it.
- **Ask and Block tools** never run in a test, and no approval requests are raised.
- **Agent HQ's own tools:** the read-only ones (skills, knowledge search, memory recall) run normally. Ones that would change something (memories, delegation, proposals) are stubbed.

Full tool results are kept for 14 days so you can turn any recent reply into a test.

**Where cases come from:**
- a 👎 with a note, or a correction, drafts one, and you click **Keep**;
- the flask button under any reply ("Make this a test");
- writing one yourself.

Feedback never changes a test you've kept: a later note starts a fresh draft.

**When they run:**
- **On skill changes:** when an agent proposes one, the cases for that agent and skill run without and with the change, and the proposal card shows both pass rates. Imported skills and the skill review's merges, rewrites and archiving are tested the same way (see [Upkeep](#upkeep-stale-skills-the-skill-review-and-archiving)).
- **In Auto learning mode:** a change that lowers the pass rate isn't applied; it waits in Requests for you.
- **Anytime:** **Run now** on a case, or **Run all**.

**The judge** is any provider and model you pick, switchable any time: a strong local model, your ChatGPT plan, or any OpenAI-compatible service. Pick one that isn't grading its own work.
