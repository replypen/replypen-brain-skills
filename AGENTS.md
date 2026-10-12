# rootcause-brain-skills

Reusable brain-development kit for **external project developers and their AI agents**.

This repo is the kit, not a brain:

- shipped agent skills in `skills/*/SKILL.md`;
- the Local Brain Work engine in `skills/local-brain-work/scripts/`;
- the `rootcause-runtime` Python package in `runtime/` (`lib` helpers imported by brain scripts) —
  **edits here are release-gated; see [Version Coherence](#version-coherence)**;
- the workspace image in `docker/`;
- installer/plugin/release plumbing.

Brains consume this kit from a brain checkout (`rootcause-brain-<project>`). Skills install once and
run from inside any brain; never copy this kit into a brain's committed `skills/`.

## Domain Taxonomy

Use the shared RootCause vocabulary in [Brain Model](docs/brain-model.md) and
[Run Trace Model](docs/run-trace-model.md). The distinctions that matter in this kit:

| Term | Meaning / distinction |
|---|---|
| **Project / tenant** | Project is the SaaS world being grounded; tenant is a scoped organization within it. Tenant exceptions do not belong in shared project rules. |
| **Brain / kit** | Brain is project-owned knowledge and tooling mounted read-only for runs. This kit supplies developer tooling and runtime helpers; it is not a project brain or KB. |
| **Skill** | Kit `skills/` teaches local developers; a brain's `skills/` holds production domain knowledge/scripts; mounted `/skills` holds shared file-format skills. These are separate sources, not interchangeable install paths. |
| **Tenant overlay / projection** | Overlay adds tenant-specific knowledge; projection compiles shared templates plus tenant profile values into an ephemeral `/brain` view. The compiled view is not another maintained brain. See [project and tenant brains](docs/brain-model.md#project-and-tenant-brains). |
| **Mirror / knowledge base (KB)** | Mirror is a read-only customer source checkout (UI: “Code bases”); KB is synced support material. Both are external context, refreshed independently of brain changes. See [mirrors](docs/mirrors.md). |
| **Run / workspace** | Run is one end-to-end handling of an inbound trigger; workspace is its execution container. A local script check is not a production run. |
| **Grounding / grounding pre-step** | Grounding gathers evidence for an answer; the pre-step selects files for the main agent. Selection is not a guarantee that required reference material reaches the answer-writer. See [`include_in`](docs/brain-model.md#the-include_in-contract--hard-loading-a-doc-into-a-host-prompt). |
| **Triage / persona** | Triage decides process versus skip; persona controls voice, language and signature. Neither replaces business playbooks. See [prompt ownership](docs/brain-model.md#production-prompt-boundary). |
| **Action / preflight** | Action is a vetted write intent, not a loop tool. Preflight checks it read-only; a proposal or passing preflight does not prove execution. See [side effects](docs/side-effects.md). |
| **Brain channel / kit release** | A brain channel selects a shared brain commit for runs; a kit release pins developer tooling and runtime versions. Publishing one does not update the other. See [channels and refs](docs/brain-model.md#channels-and-refs) and [releases](RELEASING.md). |

## Audience

One audience only: project developers and their agents.

They have:

- a brain checkout;
- `rc auth login`;
- maybe Docker and local `.env` via `rc project env pull`.

They do **not** have:

- private RootCause app source;
- host shells, SSM, registry DB access, `accounts.yml`;
- private operator scripts.

If a workflow is not exposed through public `rc`/API, produce a RootCause support request. Do not leak
private mechanics into shipped docs or skills.

Exception: `brain-fleet-report` queue persistence is a laptop operator job. Its DB adapter imports
the operator checkout’s existing support connection via `RC_HOST_CHECKOUT`; external developers
without that access can test with `--dsn`, but cannot publish production queues.

## Two Execution Contexts

Keep these planes explicit in every skill, template, and brain edit:

- **Local brain development:** a developer or coding agent runs outside production from a brain
  checkout. After OAuth login, it may use the public `rc` CLI for runs, settings, diagnostics, and
  guarded production primitives. The CLI skills in this kit serve only this context and install
  locally into the ignored Codex-native `.agents/skills/` tree; `.claude/skills` is a **committed**
  relative symlink to that tree (Claude Code discovers only `.claude/skills`, and git worktrees carry
  tracked files only). The alias is tracked, the skills behind it are not, so the link dangles in a
  fresh checkout — prod skips dangling relative in-repo symlinks and brain lint only WARNs.
  `.worktreeinclude` carries the ignored kit paths into worktrees. They never mount into `/brain`.
- **Production main loop:** the model has `bash` plus its scenario terminal tool (`reply` for email),
  not an `rc` binary. The committed brain is mounted read-only at `/brain`. Ground through `/brain`
  scripts and the injected `lib.db`, `lib.cloudwatch`, `lib.http`, `lib.fs`, `lib.connectors`,
  `lib.api`, `lib.mcp`, `lib.tenant`, `lib.runctx`, `lib.image`, and `lib.transcribe` capabilities available for that project/run.
  (`lib.tenant` — a script that needs a practice/tenant setting reads it itself; never let the
  playbook ask the model to copy a `{{ }}` value into an argument. Recipe, and the action-container
  env fallback: [docs/tenant-settings.md](docs/tenant-settings.md).)
  (`lib.runctx` — what KIND of run this is (plane, ingress surface, simulation, principal-scoped):
  branch on the host-stamped document, never on the prompt's prose. Descriptive, never authorization:
  [docs/run-context.md](docs/run-context.md).)
  (`lib.image` — cheap preview → refine-from-preview image generation over the broker's `image`
  mount, saving to `/tmp/outbox`; absent mount ⇒ one clear "not enabled" sentence. Cost model + ladder decisions: `docs/image-generation.md`.)
  (`lib.transcribe` — on-demand transcript of a chat recording over the broker's `transcribe` mount,
  with the run's own `--instructions`/`--keywords` added to the project hints: [docs/transcription.md](docs/transcription.md).)

Never put `rc ...` command guidance in committed project-brain content. Describe the project-specific
evidence or decision; keep laptop-side control-plane steps in this kit's local skills/docs.

Do not repeat RootCause's generic production prompt in brain docs. `emailPreamble` and capability-gated
sections in `rootcause/internal/agent/prompt.go` already own the generic support-engineer role,
draft/note split, read-only workspace, actions/preflight, PII, DB scoping, mirrors, and grounding
mandate. Persona settings own brand voice, language, formality, and signature at project, tenant, and
mailbox scope. Brain docs should add project business context, terminology, playbooks, source/KB
pointers, and action rules.

## Core Model

Read these before changing product-facing docs/skills:

- [docs/brain-model.md](docs/brain-model.md) — audience, brain-vs-external context, prompt boundary,
  `include_in` frontmatter (triage/grounding/agent), layout, refs, mounts, tenant/project model.
- [docs/run-trace-model.md](docs/run-trace-model.md) — how to read `rc run debug`.
- [docs/side-effects.md](docs/side-effects.md) — read-only vs explicit side effects.
- [docs/support-boundary.md](docs/support-boundary.md) — brain fix vs support escalation.
- [docs/mirrors.md](docs/mirrors.md) — source mirror freshness and local/prod gaps.
- [docs/secrets.md](docs/secrets.md) — public `rc` flow for adding/rotating grounding env and action
  credentials, and for [registering a new grounding
  database](docs/secrets.md#register-a-new-grounding-database) (there is no `rc project database add`:
  sealing the `<PROJECT>_<DBKEY>_DSN` key creates it, `rc project database set … description=…` surfaces
  it in `ls`).

## Canonical Skills

Only these are first-class:

- `local-brain-work` — local scripts/tests/projection/action checks; broad router.
- `brain-dream-cycle` — local consolidation from run feedback/sent deltas/patterns using public `rc`,
  including persona and triage setting updates.
- `brain-harvest` — local synthesis from a mailbox's harvested sent-history corpus using public `rc`:
  trigger export, download, per-topic subagent distillation, privacy/contract lint, brain/persona/triage
  homes.
- `brain-website-scout` — local broad public-site mapping and Firecrawl capture, then per-topic
  synthesis into a progressive-disclosure brain; raw pages stay gitignored.
- `website-kickstart` — operator-laptop provisioning of a chat-editable tenant website (template repo,
  GitHub App access, Workers Builds triggers, first build, `role=website` mirror, screenshots); `--teardown`.
- `brain-kb-mirror` — local monthly re-pull of a third-party public help center into a committed
  Markdown + image snapshot under `knowledge/` (source profile in `_internal/kb-sources/`), sub-agent
  image alt text, candidate-tree validation, byte-identical no-op reruns.
- `source-routing` — question digest, trace-backed document usage, provider-neutral routing proposals,
  and reviewed prefer/avoid imports for large document sources.
- `brain-ask` — last-mile prod/test run validation with `rc ask`.
- `rc-debug` — one run/thread/session trace; inspect/propose/stop before edits.
- `rc-health` — stale mirrors and dead-lettered runs.
- `rc-fleet` — recent fleet and recurring failure patterns.
- `brain-fleet-report` — the daily two-audience fleet report: Python collects a day of evidence;
  the model judges it into persisted owner/technical review queues.
- `brain-simulate` — replay representative real inbound cases through `rc ask --simulation`, grade
  each draft against the human's historical reply, render an HTML report with run links and
  copy-paste steering prompts; re-run with `--ref dev/<branch>` and diff.
- `brain-helpcenter-suggestions` — one window of real customer questions (email runs, in-app chat
  runs, Help Scout) held against the public help-centre inventory: Python collects the corpus and
  every article body, the model classifies in two passes and writes one small markdown file per
  suggestion, Python validates every quote and anchor and renders one `report.html` of ranked,
  anchored KB edits for the help-centre owner, with a bot-readable block per card.
- `brain-grounding-intake` — a customer's code (source mirror or local clone) and grounding
  database, taken in together from the production run's chair: `context.py` writes what a run
  receives (database descriptions, mirrors, brain files), a bounded repo scan and a bounded schema
  probe on the box, a benchmark of the last window of real customer questions into grounded /
  ambiguous / missing / knowledge / human, safe data drills through a guarded `query.py`, a
  self-contained `report.html` questionnaire for the customer's developer (impact line per
  question, clickable cluster chips showing the real tickets, one copy-as-markdown button), and
  the answers written back as `skills/codebase/` and `skills/databases/<db>.md` plus routing rows
  and the database description line.
- `brain-helper-devloop` — the helper feedback loop: tickets → read-only verdict-first helper →
  `mirror-try`/`scope-check` → `rc ask` synthetic questions on a dev ref → `rc run debug` for
  unprompted pickup → DX fixes, never steering → `brain-publish` → live re-ask.
- `mirror-try` — compare staged mirror helpers and sibling imports on real scoped production data.
- `prod-console` — direct guarded production primitives through `rc dev console capabilities`,
  `rc dev console database`, `rc dev console bash`, and `rc dev console action`.
- `scope-check` — principal-scoped projects: the per-audience table/column visibility matrix
  (`scope_matrix.py`) and the per-audience helper smoke matrix run in real production sandboxes
  (`scope_smoke.py`). The closest-to-production tier for "for whom does this helper work".
- `rc-script-wrapper` — deterministic local Python/shell wrappers around `rc`, including complete
  exports, typed failures, parameters, and remote artifact fetches.
- `brain-dev-upgrade` — update kit and `rc`.
- `brain-git-sync` — safely reconcile local brain work with cross-computer `origin/main` and push.
- `brain-publish` — post-edit publish/support-request step.

Do not reintroduce aliases such as `brain-debug`, `observability`, `rc-inspect`, `rc-thread`,
`brain-source-intake`, or `brain-schema-intake`.

## Boundaries

Ships here:

- local brain engine: `brain_run.py`, `brain_test.py`, `brain_projection.py`, `brain_action.py`,
  `brain_dump.py`;
- the fleet-report engine in `skills/brain-fleet-report/scripts/` (read-only collection over public
  `rc`, plus schema/validate and operator queue publication);
- the simulate engine in `skills/brain-simulate/scripts/` (case normalization, `rc ask --simulation`
  replay, judge bundles, HTML/markdown report);
- the help-centre engine in `skills/brain-helpcenter-suggestions/scripts/` (corpus normalisers,
  read-only collection over public `rc`, strict suggestion schema, HTML report);
- the grounding-intake engine in `skills/brain-grounding-intake/scripts/` (run context, bounded
  repo scan, remote schema probe and reduction, guarded read-only drill, question benchmark,
  validator over paths and tables and columns, HTML questionnaire);
- the scope-check scripts in `skills/scope-check/scripts/` (per-audience `rc project database
  preview` matrix, per-audience `rc dev console bash run` smoke matrix);
- public-API skills over `rc`;
- `rootcause-runtime` (`runtime/lib`);
- workspace Dockerfile/image.

Stays out:

- RootCause host/debug/operator mechanics;
- registry/River/raw SQL/debug scripts;
- secrets, credentials, private repo paths;
- fake publish/promote wrappers for capabilities not public yet.

Diagnosis is read-only by default. Exceptions must be explicit: `rc ask` creates runs, action confirm
executes writes, and `brain_action.py --commit` writes to whatever `.env.action` targets.

## Version Coherence

**Did you touch `runtime/lib/**`, `runtime/pyproject.toml`, or `runtime/requirements.lock`? Then you
owe a release — a bare merge/push does NOT reach prod.** The box installs the *pinned tag*, not `main`,
so unreleased lib bytes never ship. Close it out with `./refresh-brains.sh --release <patch|minor>`
(bumps, publishes main, tags/pushes the whole line below, then bumps + commits the sibling
**rootcause** pin for you; `--relock` if deps changed) and **promote rootcause** afterwards. Full
steps: [RELEASING.md](RELEASING.md). Three gates back this up: the release exits non-zero if the prod
bump did not land, rootcause CI (`scripts/check_workspace_pin.py`) goes red on unconsumed drift, and
rootcause's `promote.py` preflight **FAILs** while lib commits sit past the pin (even unpushed local
ones) — but don't rely on the gates; release as you go.

One version line moves together:

- plugin manifests;
- `skills/local-brain-work/scripts/brain_env.py`;
- `runtime/pyproject.toml`;
- install docs;
- workspace image tag;
- `rootcause/runtime/Dockerfile` pin.

Run `./check-release-coherence.sh` before trusting a release. Runtime dependency changes require
`runtime/requirements.lock` and the sibling `rootcause/runtime/requirements.lock` to match.

## Default Close-Out

After focused checks pass, release and push by default.

Use `./refresh-brains.sh --release patch` for docs/skill changes too; local brains fetch tags, not
floating `main`. Releases must run on `main`; the publisher pushes `HEAD:main`, verifies
`origin/main == HEAD`, and only then pushes the version tag. `--no-push` changes neither remote ref.

The release refuses a dirty working tree and its commit carries only the files the bump writes:
commit (or, from a shared checkout, land with `skills/brain-git-sync/scripts/git_land.py`) your work
first. Hold instead only for concrete blockers: failing checks, someone else's uncommitted work in the
tree, missing image/runtime access, secrets/irreversible effects, or explicit user instruction.

## Verification

Use the smallest checks that cover the change:

```bash
uv run --no-project python -m py_compile skills/local-brain-work/scripts/*.py
# PYTHONPATH is load-bearing here and in uv-mode children: `--with .` can serve a CACHED wheel of an
# unchanged version, so local runtime source must precede site-packages while iterating on runtime/lib.
cd runtime && PYTHONPATH=$(pwd) uv run --with '.[test]' --no-project pytest tests -q
./check-release-coherence.sh
```

For skill edits, validate each `skills/*/SKILL.md` with the skill-creator quick validator. For markdown
changes, scan relative links and stale private references.

## Tooling

- Python: `uv` only.
- Node/JS: `pnpm` only.
- Versions/env: `mise`.
- Shell search: prefer `rg`.
- Skill authoring: use the `skill-creator` skill.
