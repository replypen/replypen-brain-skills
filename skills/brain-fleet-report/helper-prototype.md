# Prototype Python helpers during judging

A helper error, wrong output, usage trap or missing capability earns working code and genuine
before/after output before a technical card is decided. This includes new helpers in brains and
source mirrors. The human decides whether to merge; never merge or promote the prototype yourself.

For mirror repos use [mirror-try](../mirror-try/SKILL.md) to resolve sibling imports and compare
a ref or tracked working-tree changes. `scripts/helper_prototype.py` uses its shared console staging
engine for review-card capture. For brain content, validate the dev channel with
[brain-ask simulation](../brain-ask/SKILL.md); scratch helper proof alone does not validate a brain.

Use `scripts/helper_prototype.py`: **stage committed helper files in scratch inside the guarded
production console**. Both revisions use identical arguments, tenant/principal and current production
DB/mirror mounts. No local DSN, live ref changes or LLM run needed. This proves the staged helper,
not a deployed brain, retrieval or the full reply loop. Data can change between the two reads.

1. Read the source checkout's instructions and cited failure. Inspect the helper and imports for
   read-only behavior: no actions, sends, customer writes or write credentials. Select the original
   run's project, tenant and principal, and a minimal output that avoids personal data.
2. Prepare one branch/worktree per card (base is freshly fetched `origin/main`):
   `uv run "$FR/scripts/helper_prototype.py" prepare --repo /path/to/repo --slug signature-slug --state /tmp/prototype-state.json`
3. Edit/create the helper in the printed worktree, add a focused regression test and update its
   skill contract. Run relevant checks. Commit only these files. Keep captured output outside Git.
4. Capture and push the committed branch:
   `uv run "$FR/scripts/helper_prototype.py" capture --state /tmp/prototype-state.json --project PROJECT --tenant TENANT --out /tmp/prototype.json --read-only-reviewed -- python skills/topic/scripts/helper.py ARGS`
   Staging is [mirror-try](../mirror-try/SKILL.md)'s: the helper's static import closure is resolved,
   only the helper and files whose bytes changed since base are shipped, and unchanged imports
   (e.g. a mirror's 42 KB `ka.py`) are borrowed from the live checkout (`/mirrors/<repo dir>`, a brain
   `/brain`; override `--live PATH`) after the box verifies their sha256 against base. A mismatch
   stages those files from git and reruns both sides; `--stage-all` ships the whole closure. Add
   `--path` for runtime data or dynamic imports; changed production Python outside the closure is
   refused. Absolute `/brain` or `/mirrors` imports still read live bytes: inspect them and
   include/repoint imports when testing changes there. No hidden files, symlinks or actions. The
   limit is **120 KiB (122,880 bytes) per console command** after the host's shell quoting (CLI stdin
   and API body allow 256 KiB, the workspace exec argument does not); over it, capture fails before
   any production call, naming the size, the limit and every staged file with its importer. stderr
   reports `payload before N KiB · after N KiB of 120 KiB; staged S, borrowed B`. New helper: Before
   truthfully reports the missing script.
   Supply both `--principal-kind` and `--principal-id` when reproducing an end-user scope.
5. Inspect the reduced output: first-name/email heuristics are not a PII guarantee. Use aggregate
   or identifier-only output, remove residual contact/secret material before publication. AFTER
   must succeed, and you must establish the intended improvement; an exit zero alone is insufficient.
   CLI/transport failures are not helper evidence. A failing AFTER never pushes or emits a merge card.
6. Put the JSON object in the finding's `prototype` field. Publication appends Before/After fenced
   blocks, command, exact commits and console run IDs, plus `Branch: repo@review/slug (N files)`.
   “Merge branch” becomes the first technical option. Owner cards never expose prototypes.
   For an existing card: `uv run "$FR/scripts/body_update.py" --project PROJECT --item UUID --prototype /tmp/prototype.json`
   Inspect the preview, then repeat with `--write`. Only an open technical card can change; decisions,
   evidence and receipts are preserved. Repeating replaces the previous prototype block.

Leave the branch/worktree for the implement job. It fetches and merges the reviewed commit, tests,
ships using the repo's normal flow, verifies production, then deletes the remote branch. If staging
cannot faithfully run the helper (e.g. absolute imports of changed code), report that limitation and
fix the staging recipe; never substitute mocked output or claim local tests proved production.
