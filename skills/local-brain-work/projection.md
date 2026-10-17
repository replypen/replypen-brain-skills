# Tenant projection — what `/brain` will look like

The Local Brain Work view of tenant projection, for an agent editing a templated shared brain. Broader
model: [docs/brain-model.md](../../docs/brain-model.md).

## The invariant

For a templated project brain the model does **not** see the committed source tree. The host renders an
ephemeral per-tenant view and mounts *that* read-only at `/brain`. So reviewing the templates is not
reviewing what the tenant reads.

Committed source: `projection.yaml` (placeholders, branch selectors, variants, defaults, gated files),
markdown templates carrying `{{ placeholder }}` / `<!-- rc:branch -->`, and `tenant.schema.json`.
Runtime input: the tenant profile record in the ReplyPen DB (`rp project tenant profile get`).
Output: a throwaway compiled directory — never committed, never pushed, never durable knowledge.

`tenant.schema.json` is the **contract, not the values**: it validates `rp project tenant profile set`
and renders both `rp project tenant profile schema` and the operator Configuration form. A project
without one has no tenant-settings surface at all. Values live in the DB; the schema stays committed.

## Authoring rules

Get three things in view before editing: `projection.yaml`, `rp project tenant profile schema -o json`,
`rp project tenant profile get <slug> -o json`.

- Every placeholder used in markdown must be declared in `projection.yaml` and backed by a tenant value
  or a projection default.
- A script that needs a practice/tenant setting reads it itself via `from lib import tenant`; never let
  the playbook ask the model to copy a `{{ }}` value into a script argument or an action param.
  Placeholders are for prose the customer reads. Full recipe (including actions, which get the profile
  as an env document): [reading a tenant setting from Python](../../docs/tenant-settings.md).
- The run's own situation (plane, ingress surface, simulation, principal-scoped) is a separate
  host-stamped document read with `lib.runctx`, and it lands on flat projects too:
  [what kind of run is this](../../docs/run-context.md).
- A branch selector matches the **exact rendered string** of its value (`true`/`false` for a bool, the
  decimal form for an integral number). Unmatched → the branch `default`; no default → the region is
  dropped.
- Reserved variant `unset` = absent, null, or blank/whitespace-only string. Reserved variant `present` =
  catch-all for "the tenant filled this in", so free-text keys can gate prose without enumerating
  values. `false` and `0` are values, hence present; only emptiness is `unset`. Exact-value variants win
  over `present`.
- Pair `present` with an **empty** `unset` body so an unconfigured section — heading included —
  disappears, instead of reading a "(not configured)" default out loud to the customer:

  ```yaml
  cancellation_policy: { select: cancellation_policy, variants: [present, unset], default: unset }
  ```

## Previewing

`brain_projection.py --tenant <slug>` is a local preview/audit helper, not a second source of truth: it
reads local `projection.yaml`, fetches tenant values via `rp`, and prints what production *would*
compile (settings version, branch choices, defaults used, gated files kept/dropped). `--write-summary`
persists only the summary + settings snapshot under gitignored `.replypen/projection/<tenant>/`. It
must never write compiled files into the brain tree, commit tenant values, or teach the model to pick
variants itself.

`rp dev brain render --project <p> --tenant <slug>` returns the **server-compiled** view,
exactly as `/brain` mounts it, with a header of sha/channel/fill/branch/degradation counts. Pin
`--sha`/`--channel`; `--all` for the whole tree. Every returned file is also written verbatim to
`.replypen/output/brain-render-<tenant>/tree/<brain-relative path>`, so you open the real
`playbooks/annulatie_beleid.md` instead of an opaque `content.txt`. Artifacts are keyed by tenant only
(`.replypen/output/brain-render-<tenant>/`, `tree/` wiped per render), so a second render of another
sha overwrites the first — move it aside to compare. Needs a project-level login.

Template-editing flow: author → `render` for 1–2 representative tenants ("what does this tenant read")
→ `rp dev brain preflight` ("would this commit break anyone", pass/fail across all tenants) → publish
(`brain-publish`). For full production confidence, `rp ask --brain-ref dev/x` with a login bound to the
target tenant, then inspect the dump.

## Reporting a template change (so the reviewer never has to ask)

A change to any templated file (`projection.yaml`, `tenant.schema.json`, a markdown under
`templated_globs`) is reviewed on its **rendered** form, not its source. Before reporting done:

1. Render for the most relevant tenant(s) — the one whose values exercise the new branches, or one per
   variant when a selector has several (`rp dev brain render --tenant <slug> --path <file> …`, `--all`
   when the change spans files). Do it against the SHA you are shipping (`--sha`), not an older channel.
2. In the final report list, as clickable local paths: every source file you changed **and** its
   rendered twin(s) under `.replypen/output/brain-render-<tenant>/…`, naming the tenant and the branch
   values that drove the render (e.g. "de-kies: latecancel_policy=active, free_count=1").
3. Quote branch names that YAML 1.1 reads as booleans (`"off"`, `"on"`, `"yes"`, `"no"`): the Go
   compiler keeps them as strings, PyYAML previews do not.

