---
name: website-kickstart
description: Provision a new chat-editable public website for a tenant in one deterministic command — GitHub repo from replypen/site-template, GitHub App access, Cloudflare Worker + Workers Builds triggers (production + branch previews), first build, ReplyPen role=website mirror, Browser Rendering screenshots. Also adopts an existing site repo (--adopt) and tears down a throwaway (--teardown). Use for "maak een website voor deze praktijk", "new practice site", migrating a site repo onto Workers Builds.
---

# website-kickstart — zero-click site provisioning

**Intent:** a new tenant website costs one command and no dashboard clicks; the practice then edits it
through ReplyPen chat (replypen `features/website-editing.md`). Every step reads live state first, so a
rerun after a failure continues where it stopped. Operator laptop only (Cloudflare + GitHub admin creds).

```bash
KS=~/code/replypen/replypen-brain-skills/skills/website-kickstart/scripts/kickstart_site.py
mise -C ~/code/replypen/replypen exec -- uv run -q $KS <github-org> <slug> \
  --project <rp project> (--tenant <rp tenant> | --project-scope) --name "<Naam zoals op de site>" [--dry-run]
```

Scope is explicit: `--tenant` = a tenant's site (mirror on the tenant, form → `/forms/<project>/<tenant>`);
`--project-scope` = the **project's own site** (e.g. `momentum-tools`, no tenants): project mirror (`rp --scope
project`, so an inherited tenant can't capture it), form → `/forms/<project>`, landing in the project's
tenant-less mailbox. Never invent a tenant just to host a site.

Repo + Worker = `site-<slug>`; live `https://site-<slug>.rootcause-sites.workers.dev`, previews
`https://<branch>-site-<slug>.rootcause-sites.workers.dev`. Output: `✓/✗ step — detail` per step, then a
summary (repo, URLs, worker tag, trigger uuids, `--out` files: `<slug>-triggers.json`,
`<slug>-mobile.png`/`-desktop.png`, default `~/Downloads`) and a chat sentence to try. ~2 min.

| Step | What | Notes |
|---|---|---|
| preflight | a same-named Worker in the shared account must already be ours | read-only; refuses before any write |
| repo | `POST /repos/replypen/site-template/generate`, fill `__WORKER_NAME__`/`__SITE_NAME__`/`__PROJECT__`/`__TENANT__` (no tenant ⇒ `__PROJECT__/__TENANT__` → `<project>`), push main | `--adopt`: existing repo, only checks `wrangler.jsonc` name = Worker |
| github apps | add repo to `rootcause-app` + `cloudflare-workers-and-pages` on the org | ids via `GET /orgs/<org>/installations` (gh login); `PUT /user/installations/{id}/repositories/{repo}` with the PAT. A missing install = one-time browser step |
| worker | first `bash build.sh && wrangler@4 deploy` creates the Worker | Builds triggers need its `tag` |
| workers builds | repo connection upsert, the account's build token, 2 triggers | prod `main` → `npx wrangler@4 deploy`; preview `*` minus `main` → `npx wrangler@4 preview`; `bash build.sh`, build cache on. Drift is PATCHed |
| first build | `POST /builds/triggers/{prod}/builds`, poll, GitHub check-run `Workers Builds: <worker>`, live 200 + name | failed build → logs saved to `--out` |
| ReplyPen mirror | `rp project repo add name=site role=website live_url preview_url_template deploy_check="Workers Builds"` + `dev mirror refresh --expect-sha` | `--mirror-name` if the tenant has another `site` |
| screenshots | Browser Rendering, 390 px + 1280 px full page | 429 = free-tier rate limit, retried |

**Ownership (never touch someone else's site).** A Worker counts as ours when its Workers Builds
connection points at `<org>/site-<slug>`, or when it is unbound and this machine's kickstart record
(`~/.local/state/website-kickstart/<account>-<worker>.json`, `--state-dir`) created it. A foreign binding
is always refused; an unbound Worker or an existing repo without a record needs `--adopt` (provision) or
`--force-orphan` (teardown). Subprocesses get a minimal env (PATH/HOME/…; only wrangler gets a Cloudflare
token) and known secret values are redacted from all output. `--name` is written as a JSON-encoded YAML
scalar and parsed back before the push.

**Env** (`~/.config/mise-env/replypen/replypen.env`, hence `mise -C …/replypen exec`):
`CF_SITES_USER_TOKEN` (USER-scoped; the Builds API rejects account tokens with 12006),
`GH_SITE_PROVISIONING_PAT` (classic, `repo` + `read:org`), `CF_BROWSER_RENDERING_TOKEN`; plus a `gh`
login that owns the org, `rp` login, `pnpm`. Account default: Rootcause Sites `60c5feed78b351b48868d3a691184c4b`.

**After:** fill the `[placeholders]` via chat (never invent facts); domain/DNS move → the project's site
runbook (DentAI: `dentai/.agents/skills/practice-websites/SKILL.md`), then
`rp … project repo set site live_url=https://www.<domain>` and `url:` in `_data/site.yml`.

**Throwaway:** `--teardown site-<slug>` removes the rp mirror, Cloudflare triggers + connection + Worker,
App access, then renames the repo to `site-<slug>-teardown-<ts>` and archives it (our tokens lack
`delete_repo`; it prints the delete command). Ownership is proven first (wrong org = refusal, nothing
changed); a missing repo is an error. Rerun resumes a partial teardown: connection uuids live in the
record (Cloudflare has no list-connections endpoint), the renamed repo is found via the record or
GitHub's redirect, deletes retry and treat 404 as done. Tests: `uv run --no-project --with pyyaml python -m unittest
skills/website-kickstart/tests/test_kickstart.py` (fake GitHub/Cloudflare/rc world).
