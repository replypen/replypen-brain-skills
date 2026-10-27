---
name: brain-chat-debug
description: "Debug one bad in-app chat answer from a share link, run link or session id: gather the case, decide where the fix belongs, replay it on a dev ref. Use for a pasted chat share link (/s/…) or run link (/runs/…?t=…), 'why did the chat say X', 'this is what I expected', a bad chat answer, replaying a chat turn, or rerunning it after a brain or mirror edit."
---

# brain-chat-debug — bad chat answer → fix → replay

One script does the mechanics; you do the judgement. Public `rp` ≥ 1.41 only.

## Required Context

- [rp-debug](../rp-debug/SKILL.md): reading one trace, and its discipline: a single bad run is
  evidence, not permission to oversteer ([run-trace-model](../../docs/run-trace-model.md#debug-discipline)).
- [brain-helper-devloop](../brain-helper-devloop/SKILL.md) when the fix is a helper;
  [mirror-try](../mirror-try/SKILL.md) / [scope-check](../scope-check/SKILL.md) to prove it on real data.
- [brain-publish](../brain-publish/SKILL.md), [brain-git-sync § git_land](../brain-git-sync/SKILL.md) to ship.

```bash
CC="$PWD/.agents/skills/brain-chat-debug/scripts/chat_case.py"   # run from the brain checkout
```

## 1. Open

```bash
uv run --no-project python "$CC" open '<share link | run link | session id | run id>' --expected "<what the user expected>"
```

Writes `.replypen/chatdebug/<session8>/case.md` + `case.json`: header (project, tenant, surface,
principal, feedback), Expected verbatim, every turn's Q/A, and per run the brain/mirror refs it ran on,
relevant prompt sections that were off, commands with exit + stdout head, files read, flags. Traces
land in `.replypen/debug/` (`rp run debug`, skipped when present; `--force` refetches).
A share/run link needs no login but withholds feedback and principal; logged in, `open` adds both.

- **0 runs** ("No answer exists in this shared chat…"): a spawned child chat nobody asked anything in.
  Say so, find the real chat with `rp run sessions search --contains "<words>" --since <date>`, never
  guess a failure from the seed message.
- The scratch dir holds customer data and capability links (`share_url`, `?t=`); it is gitignored
  with `.replypen/`. In tickets and chat reports cite run ids, never those links.

## 2. Read

`case.md` first. Then the turn's trace md (path in case.md), then `jq` on its JSONL exactly as in
[rp-debug § Drill](../rp-debug/SKILL.md#drill) (header drift first: `brain_resolved`, mirrors,
`tenant_settings_drift`). Compare the answer with Expected and say which claim is wrong and why.

## 3. Diagnose + own

Fix at the highest layer that generalizes; nothing lower papers over a higher-layer bug.

| Symptom | Layer | Edit |
|---|---|---|
| Rendering/privacy of the chat itself (raw `⟦pii:…⟧` tokens, broken record links, output contract) | ReplyPen host | support request ([support-boundary](../../docs/support-boundary.md)); never a brain rule |
| Wrong fact/query shared by all of the product's projects, helper missing or misleading | shared source mirror | its `skills/**` helper or doc ([brain-helper-devloop](../brain-helper-devloop/SKILL.md)) |
| Audience-specific routing, case runbook, missing pointer for this brain | project brain | `AGENTS.md` routing line / `skills/cases/*` |
| One tenant's genuinely local policy | tenant overlay / tenant settings | overlay repo or settings, not shared prose |
| Tone, language, signature; process vs skip | persona / triage settings | `rp` settings, not brain prose. Settings have no dev ref: when a persona line is read too broadly, scope it with a brain rule you can replay and propose the settings edit separately |
| The customer's app computes or shows the wrong thing | the customer's app | report to its developers; the brain explains, never hides it |

When the brain keeps a repository/settings map (e.g. under `_internal/`), route through it. Propose the smallest change, then stop unless the user asked to fix it. Name every
file the change touches as a clickable absolute-path link: an existing file as
`[skills/cases/x.md](/abs/path/skills/cases/x.md:42)` (line of the anchor when known), a new file as
its proposed name in its absolute target directory, followed by the added text quoted (or a small
`diff` block for a replaced line). The user sees the delta without opening git.

## 4. Edit on a dev ref

Brain: `git push origin HEAD:refs/heads/dev/<branch>`. Tenant brain (the fix belongs in `/tenant`):
the same in the tenant brain repo. Mirror: the same in the mirror repo (a worktree on a shared
checkout). Nothing moves `main`/`stable`.

A rule that states a count or carries a query: run that exact SQL on production first
([prod-console](../prod-console/SKILL.md): `database schema` before `query`, tenant-scoped) and keep
the numbers; the candidate replay must reproduce them.

## 5. Replay

```bash
uv run --no-project python "$CC" replay <session8> --turn N                    # baseline: today's live brain
uv run --no-project python "$CC" replay <session8> --turn N --brain-ref dev/<branch> \
  --mirror-ref <repo>=dev/<branch>                                              # candidate
uv run --no-project python "$CC" replay <session8> --turn N --tenant-brain-ref dev/<branch>  # tenant brain fix
```

Replays may run in parallel (each claims its own file); a failed run exits 1 with its error.

- **Email sibling:** a bad inbound-email draft replays with `rp ask --replay-run <run id>` (same
  simulation, real thread + PII seed, diffed against the original); not this skill.
- **Chat-faithful first.** When `rp ask --help` lists `--replay-chat-run`, `replay` uses it: the
  stored chat turn with chat framing, prior turns (text only, no attachments) and the session's own
  principal, as a simulation test run. `--prompt-override` swaps the question text.
- **Raw fallback = grounding-only probe** (`--mode raw|email`, or an older rp): `rp ask --scenario raw
  --simulation` with prior turns embedded in the question and the case principal. Not
  chat-equivalent (no persona, confidentiality floor, chat output contract, record links): judge
  facts, grounding and helper pickup, not prose; it can never close a privacy/rendering regression.
  `--mode raw --dry-scope` previews the principal scope without LLM spend.
- **Tenant brain fix:** `--tenant-brain-ref dev/<branch>` (rp ≥ 1.70) mounts the candidate at
  `/tenant` (`--brain-ref` is always the project brain). The replay md's Refs show
  `tenant brain: <ref> @ <sha>`; check it names your ref. Never copy tenant text into a project-brain branch to
  test it: different mount, different precedence.
- **Prior turns are text only**: a turn that builds on an earlier file asks for it again. Prove file
  output (columns, rows) with a fresh `--prompt-override` question and read the code that wrote it.
- Baseline first when unsure whether the bad answer is flaky. Each replay costs a real LLM run.

`replays/<ts>-<label>.md`: refs actually mounted (`override` = your mirror ref), **Expected check**
with an empty `verdict:` you fill (pass / still wrong / partial; the script never judges), then
historical vs latest baseline vs this replay: answers, commands, files read. `compare <session8> <a>
<b>` sets any two replays side by side.

## 6. Widen

When the fix touches a shared mirror or the project brain, check 5–10 other tenants:

```bash
uv run --no-project python "$CC" widen <session8> --tenants a,b,c [--as <roster label>] [--brain-ref dev/<branch>]
```

It writes `questions-<tag>.tsv` and prints the [devloop](../brain-helper-devloop/SKILL.md) `ask` +
`report` commands (no spend itself). Edit rows whose record ids or names do not exist in that tenant.

## 7. Ship

[brain-publish](../brain-publish/SKILL.md) for the brain; the mirror via `git_land.py … --also stable
--refresh-mirror <mirror> --project <each consuming project>`. Then one replay without refs (or one
live question) and confirm the published SHAs in its trace. Done means live.
No second ask: once the user agreed with the change you showed (path + quoted text/diff) and the
candidate replays pass, publish. Ask first only for scope beyond what they agreed on.
