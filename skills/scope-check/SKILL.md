---
name: scope-check
description: "Replicate a real production per-principal session from a brain checkout: generate the per-audience table/column visibility matrix (scope_matrix.py) and run the brain's helpers as each identity in a real sandbox (scope_smoke.py). Use when a project scopes runs to an asserted end-user (principal), when a helper works for the operator but fails for a parent/leader/admin, or before shipping a helper that touches a policy-scoped database."
---

# scope-check — does this brain work for every requester?

On a principal-scoped project, a run's database views already encode WHO is asking. A table the
requester's access policy removed simply is not there, and a column can be absent for one audience
and present for another. A helper written against the operator's own wide view therefore passes every
local test and then dies in production with `relation "form_fields" does not exist`.

This skill makes that visible and testable with two artifacts:

| Script | Answers |
|---|---|
| `scripts/scope_matrix.py` | *What does each audience see?* — a committed markdown matrix of tables and stripped columns per identity. |
| `scripts/scope_smoke.py` | *Do our helpers still work for each audience?* — every helper run in a real sandbox as each identity, as a pass/fail matrix. |

Both are read-only and go through the public `rp` CLI only. They never touch a DSN.

## Fidelity Ladder Position

This is the **closest-to-production tier** for scope questions — above
[`local-brain-work`](../local-brain-work/SKILL.md)'s uv/docker rungs and its
`brain_projection.py` preview, because the command actually executes in a production sandbox bound to
a real principal, with exactly the projection a hosted run for that person gets. Reach for it when the
question is "for whom does this work", not "does this code run".

The cheaper neighbours: `brain_projection.py` (tenant projection, no principal),
[`prod-console`](../prod-console/SKILL.md) (one primitive, one identity, ad hoc). `rp ask` proves the
LLM loop but tells you nothing extra about scope.

## Picking Audiences

Pick the smallest set that spans the project's real access shapes, and always keep the implicit
`no-principal` baseline (the tenant-wide view — the reference column). Rules of thumb:

- one audience per **principal kind** the project asserts;
- inside a kind, one **wide** and one **narrow** instance (a super admin vs a brand-new regular
  admin) — the narrow one is where helpers break;
- the identities with the *least* data, not the demo account with everything.

KampAdmin, for example: `no-principal`, a regular admin, a super admin, a KA staff admin, a parent,
a leader. Use real production external ids of low-risk accounts; the ids go in the committed config,
so pick accounts whose existence is not itself sensitive.

## 1. The Visibility Matrix

```bash
SKILL=<path to this directory>
uv run "$SKILL/scripts/scope_matrix.py" \
  --project kampadmin --dsn KAMPADMIN_APP_DSN --tenant acme \
  --as regular-admin=kampadmin_person:<uuid> \
  --as parent=kampadmin_parent:<uuid> \
  --out _internal/scope-matrix-app.md
```

Reading a row:

| cell | meaning |
|---|---|
| `✓` | fully queryable for that audience |
| `rows:<n>` | in the projection but row-constrained by a compiled predicate; `<n>` rows visible |
| `hidden` | removed by the access policy — querying it raises `lib.db.HiddenTableError` |
| `—` | not in that projection at all |

A second section lists, per audience, the columns stripped relative to `no-principal` (only when the
installed `rp` can scope a schema read by principal; otherwise it says so and the table matrix still
stands). Ordering is deterministic, so a regenerated matrix diffs cleanly — that diff is the review
artifact when an access policy changes.

**Commit the matrix** into the project's shared-grounding repo (the brain checkout's `_internal/`, or
wherever that project keeps operator-facing grounding). Regenerate it after any policy change; the
header carries the exact command.

## 2. The Smoke Matrix

```bash
uv run "$SKILL/scripts/scope_smoke.py" --config _internal/scopecheck.toml
uv run "$SKILL/scripts/scope_smoke.py" \
  --project kampadmin --tenant acme \
  --as parent=kampadmin_parent:<uuid> \
  --cmd "python skills/camps/scripts/camp_overview.py --camp-id 42"
```

Each cell runs `rp dev console bash run --principal-kind … --principal-id …`. A cell FAILS on a
non-zero exit, a timeout, a missing `expect` substring, or a failure marker in the output
(`does not exist`, `Traceback`, `QueryError` by default; extend with `--fail-on` or `fail_on`). The
script exits non-zero if any cell failed; `--json` for CI. Config shape:
[`scripts/scopecheck.example.toml`](scripts/scopecheck.example.toml).

## The Rule

> **A helper must exit 0 for every audience that can read its primary table. Hidden enrichment tables
> degrade, never fail.**

So the fix for a red cell is almost never "widen the policy". It is:

- ask first — `lib.db.is_visible("form_fields")` returns `True` / `False` / `None` (unknown);
- catch `lib.db.HiddenTableError` around the enrichment query and drop that section of the output;
- never name a hidden table back to the requester — the name is itself the leak. Say the information
  is not available to them.

`lib.db.visible_tables()` / `hidden_tables()` / `tables()` report the projection at runtime; the env
vars behind them are documented in [docs/rp-cli.md](../../docs/rp-cli.md#principal-scoping).

## Close-Out

Report which audiences ran, the matrix path, the smoke result per audience, and — for every red cell
— whether the fix is a helper degrading gracefully or a genuine access-policy question for the
operator (the policy itself is not editable through public `rp`; that is a ReplyPen support
request).
