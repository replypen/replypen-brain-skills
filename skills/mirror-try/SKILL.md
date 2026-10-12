---
name: mirror-try
description: Try a read-only Python helper change from a mirror checkout against real scoped production data, comparing base and proposed bytes without changing the live mirror ref.
---

Run from a brain checkout with `rc` login. Inspect the helper and its imports for read-only behavior
first: no actions, sends, customer writes or write credentials.

```bash
MT="$PWD/.agents/skills/mirror-try"
uv run "$MT/scripts/mirror_try.py" --repo /path/to/mirror --ref review/fix \
  --project PROJECT --tenant TENANT --read-only-reviewed \
  --cmd 'python skills/topic/scripts/helper.py ARGS'
```

Fetch the mirror first. `--ref` compares against local `origin/main`; `--base COMMIT` overrides it.
`--diff` instead compares tracked working-tree bytes (including staged changes) against HEAD;
add new files to the index first. On a shared checkout add `--only PATH` (repeatable): only those
paths use working-tree bytes, their imports come from HEAD, other agents' edits stay out. `--json` emits reduced stdout/stderr, exit codes, exact revisions,
staged paths and console run IDs. A working-tree result uses a content hash, not a commit claim.
For principal scope, `--as LABEL` resolves an audience from `_internal/scopecheck-<project>.toml` (this brain
or its `.rootcause.toml` `[mirrors]`; a miss lists known labels), or supply `--principal-kind KIND --principal-id ID`.
Omitting `--tenant` explicitly selects project scope.

Changed non-test grounding files (excluding hidden/action paths), the command's Python script and recursively resolved static sibling/root
imports form the closure. Only the helper and the closure files whose bytes differ between base and ref are shipped
into a per-invocation `/tmp/try/<repo>-<pid>-*` dir (parallel runs are safe); unchanged imports resolve from the
live mirror (`/mirrors/<repo dir name>`, override `--mirror PATH`), and the box hashes each of them against the base
bytes first: a mismatch prints one `mirror-try: live mirror differs from base …` stderr line instead of silently
mixing. `--stage-all` ships the whole closure (old behaviour). Use repeated `--path relative/file` for runtime data or
dynamic imports. Both versions run with identical arguments and scope; scratch is removed afterwards. A brand-new
helper: `--no-base` skips the base run (it can only fail). After failure exits nonzero; transport/truncation failures
are not helper evidence.

This proves **the staged helper on real scoped data**. It does not prove an agent run, retrieval,
tree-wide greps, git history, or deployed mirror bytes. Absolute `/mirrors` and `/brain` reads still
see live files; inspect/repoint these dependencies before claiming the changed code was exercised.
Dynamic imports and runtime path manipulation are not inferred. Data may change between reads.

The console accepts at most 256 KiB stdin and 128 KiB per shell argument, so the budget is the compressed payload:
120,000 bytes, reported on stderr as `payload before 61 KB · after 61 KB of 120 KB`. Over budget names every staged
file with the import path that pulled it in (`leader_evaluations_to_md.py 24 KB ← leader_to_md ← lookup_person`): split
the heavy import or narrow the change (`--ref <branch> --base <sha>`, `--diff --only <path>`); never silently omit dependencies.
Explicit/imported hidden files and actions, plus symlinks and submodules, are refused. Output uses the fleet report's heuristic
privacy reducer, not a PII guarantee: prefer aggregate/identifier-only commands and inspect before
sharing. No merge, refresh or deployment is performed.

For review cards, [helper-prototype](../brain-fleet-report/helper-prototype.md) owns branch/capture
and publication. For brain content use [brain-ask](../brain-ask/SKILL.md)'s dev ref with simulation;
mirror refs remain project-wide. See [prod-console](../prod-console/SKILL.md) for scope and access.
