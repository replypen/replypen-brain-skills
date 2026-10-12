# Migration runbook — cut over `rootcause` to the kit

These steps are **outward-facing / sequencing-sensitive** and are intentionally NOT applied
automatically: they push a public tag + image and edit the production repo. Do them **in order** —
repointing prod before the tag exists breaks prod image builds.

## Order of operations

1. **Publish this repo** at `v0.11.12` with `./refresh-brains.sh --release patch` (or an explicit
   version). The release must be cut on `main`; the publisher pushes and verifies `origin/main` at
   the release commit before it pushes the tag. Never publish the tag first.
   - Bump the whole single version line together first — see [../RELEASING.md](../RELEASING.md)
     (`skills/local-brain-work/scripts/brain_env.py` `VERSION`/`DEFAULT_IMAGE`, `runtime/pyproject.toml`, both
     plugin manifests + marketplaces, the image tag).
   - Prove the package resolves by tag (no `rootcause` source):
     ```bash
     uv run --no-project \
       --with "rootcause-runtime @ git+https://github.com/rootcause-org/rootcause-brain-skills@v0.11.12#subdirectory=runtime" \
       python -c "import lib.db; print('ok')"
     ```

2. **Publish the workspace image** to ghcr, pinned to the same tag:
   ```bash
   docker build -f docker/Dockerfile -t ghcr.io/rootcause-org/workspace:v0.11.12 .
   docker push ghcr.io/rootcause-org/workspace:v0.11.12
   ```
   (Already builds + runs locally — see verification in the session that produced this repo.)

3. **Repoint prod** — `rootcause/runtime/Dockerfile`. Replace the inline client-dep install +
   `COPY lib/` + `ENV PYTHONPATH=/opt/rootcause` with a single package install (deps now come from
   `rootcause-runtime`'s `pyproject.toml`; the import name stays `lib`):

   ```diff
   -RUN uv pip install --system --no-cache \
   -        "psycopg[binary]==3.2.3" \
   -        "stripe==11.4.1" \
   -        "boto3==1.35.92" \
   -        "requests==2.32.3" \
   -        "markdownify==1.2.2"
   ...
   -COPY lib/ /opt/rootcause/lib/
   -ENV PYTHONPATH=/opt/rootcause
   +# lib now ships as the pinned rootcause-runtime package (ONE source of truth).
   +# subdirectory=runtime is where pyproject.toml lives; the import name stays `lib`.
   +# Needs build-time network + read auth to the (private) repo; pin the tag, never float main.
   +RUN uv pip install --system --no-cache \
   +        "rootcause-runtime @ git+https://github.com/rootcause-org/rootcause-brain-skills@v0.11.12#subdirectory=runtime"
   ```
   **Confirm a real prod run still grounds** before deleting anything (the make-or-break check).

4. **Delete the now-redundant `lib` source in `rootcause`** once step 3 is confirmed:
   `rootcause/runtime/lib/` and `runtime/tests/` (the package + its tests are canonical here
   now). Optionally publish the same image from `rootcause/runtime/Dockerfile` instead of this
   repo's `docker/Dockerfile` — pick ONE builder to avoid drift; this repo's is recommended since
   `runtime/` lives here.

5. **Delete the bucket-A copies + point the support skill at the kit:** *Already done* — an earlier
   `rootcause` cleanup removed the engine copies (`brain_run.py`, `brain_test.py`) and the old
   `local-brain-scripts.md`. No action.

## Already done (no action)

- **`.env` standardization.** `rootcause-brain-momentum-tools` already uses a single gitignored `.env`
  at its root (mode 600). No rename needed. (`rootcause/.env.momentum-tools` is the operator's
  own copy — bucket C — leave it.)
