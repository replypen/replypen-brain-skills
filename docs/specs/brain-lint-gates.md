# Brain lint gates: which gate runs what, when

Decided 2026-11-02. The rules say what reaches `/brain` must read the way the host reads it. Edits in
between legitimately break them, so the gates fire on the author's own "this is coherent" signals,
commit and push. Git hooks fire the same for Claude, Codex and humans: no editor/agent-turn hooks.

| Gate | Fires | Judges | Rules | Budget |
|---|---|---|---|---|
| managed `pre-commit` (`brain_hygiene.py --staged`) | `git commit` | the index bytes (what the commit records) vs HEAD | hygiene on staged files, placeholders whole tree on a `projection.yaml` edit, changed mermaid blocks, lint FAILs in staged files | ~1 s |
| managed `pre-push` (`brain_hygiene.py --push`) | `git push` | each pushed commit vs the remote tip (new branch: `origin/main`) | the above minus mermaid, plus whole-tree dead links, `reachability` from AGENTS.md, all lint FAILs | < 3 s, no network, no docker |
| `brain_git_sync.py` | before its push | merged HEAD vs the remote tip | same as pre-push, plus mermaid | |
| `brain-publish` | publish | the tree | the above plus action lint, import smoke, `brain_test.py`, server lint | slow |

All three git gates are one engine (`brain_hygiene.gate`): the tree is materialized into a temp
checkout, never read from the working tree, so partial staging and dirty files cannot skew a verdict.

> [!IMPORTANT]
> Regression-only: a finding already present at the base never blocks (legacy debt is fixed by its own
> sweep, `brain_hygiene.py --all`). Exceptions, never baselined: git conflict markers and `/Users/`
> laptop paths.

<details><summary>Install, opt-out, bypass</summary>

- `install.sh` writes both hooks as thin marked wrappers in git's common hooks dir (linked worktrees
  share it). The logic lives in the pinned kit clone, so a release changes behaviour without
  rewriting them; the daily kit refresh rewrites a wrapper only when its text changes, and restores a
  missing one.
- A user-owned hook or `core.hooksPath` is never touched; install prints the line to add.
- `install.sh --no-hooks` removes only the marked wrappers and sticks; `--hooks` opts back in.
- The wrappers fail open (no kit, no Python 3.11+, a crash in the gate) and say so.
- `--no-verify` skips a gate once: a convenience gate, not enforcement.
- The pre-commit hook also runs the once-a-day kit update fallback (`kit_update.maybe_update`); the
  pre-push hook does not (no network).

</details>

## Not built: server-side acceptance lint

The only gate a push cannot bypass would run on the host when a flat project's `main` advances.
Option, not built 2026-11-02: the local gates cover the agents and laptops that carry the kit.
Revisit if local gates demonstrably miss regressions, or agents bypass them (`--no-verify`, pushes
from machines without the kit).
