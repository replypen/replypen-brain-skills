# Secrets

Use public `rp` surfaces only. You need a login with secrets access for grounding env changes and
operator access for the action plane. Do not ask for ReplyPen host shell, SSM, registry DB access, or
private operator scripts.

## Choose The Store

**Anything in the grounding env is readable by the model** (`printenv`, a debug print, a traceback) and
can land in a stored run trace. Pick the most-contained store that works, top to bottom:

1. **Brokered connection** (catalog entry with broker exposure; `rp project connection add/rotate/rm`):
   the token stays on the host, scripts call it through `lib.api`/the central connector, runs only see
   the key name in `RC_API_BROKERED_KEYS`. Default for every catalog API.
2. **Host-only connection** (KB sync: knowledgeowl, helpscout_docs, intercom KB; model keys): never
   injected. Never ALSO seal the same key in the env — the sealed copy reaches every run.
3. **Env connection** (`RC_CONN_<KEY>` JSON, e.g. the `aws` kind): injected raw. Only for a credential
   that is least-privilege at the provider (read-only, one resource).
4. **Sealed grounding env** (`rp project env set`): raw in the container. Only for public tokens (Mapbox
   `pk.`) or a custom credential scoped at the provider to exactly what the scripts use (e.g. an IAM user
   with `s3:GetObject` on one bucket). Document that scope next to the name in `AGENTS.md`.
5. **Database DSN** (`<PROJECT>_<DBKEY>_DSN`): always proxied and projected, never raw in the container —
   see [Register A New Grounding Database](#register-a-new-grounding-database). `*_PROVISION_DSN` and
   `*_SSH_KEY` are host-only.
6. **Hosted action write credential**: `rp project env set --plane action` (operator only) → `.env.action`;
   diagnosis runs never receive it.
7. **Per-user embed-chat token**: not a store you manage. The customer backend mints it into the chat
   token's `credentials` claim; the host injects it as a plain env var (e.g. `PROBACKUP_AGENT_TOKEN`) in
   that session's runs only. Branch on its presence, never print it, treat an auth failure as expiry.
   Names cannot start with `RC_` or collide with a grounding env key.

Secrets inside **customer data** (API keys in a settings table) are a data-scoping problem, not an env
one: hide the rows/columns in the scope manifest so no run can select them.

Once a connection covers an integration, `rp project env rm` the old sealed copy. Audit what a run
actually receives: `rp dev console bash run 'env | cut -d= -f1'` (names only, never values).

## Add Or Rotate A Grounding Secret

For non-DSN secrets, document the env var **name only** in the relevant brain skill and in
`AGENTS.md`'s non-DSN env table. Never commit or paste the value into the brain.

```bash
rp auth status
rp project env keys
printf %s "$SECRET_VALUE" | rp project env set key=FOO_API_TOKEN
rp project env keys
rp project env pull
rp project env diff
```

`rp project env set` reads the value from STDIN by default and never echoes it. Inline `value=...` works, but
puts the secret in shell history and process arguments; avoid it.

After adding the key, update the script to read `os.environ["FOO_API_TOKEN"]` (or the helper that expects
that name), run local checks, then verify production behavior with `rp ask --brain-ref dev/<branch>` when
needed.

## Register A New Grounding Database

There is no `rp project database add`. A database **exists** because a sealed grounding env key
`<PROJECT>_<DBKEY>_DSN` exists; it becomes **visible** in `rp project database ls` once you annotate it.
Those are two different steps — `ls` lists the DSNs the project has *configured* (description, scope
manifest, or PII columns), not the sealed env keys. `rp project database --help` (rp >= 1.18.1) states
the same convention.

Naming is load-bearing: `<PROJECT>_<DBKEY>_DSN`. The short name brain scripts pass to `lib.db`
(`ACME_BILLING_DSN` -> `lib.db.query(..., db="billing")`) and the `name` column of
`rp dev console database list` is the **last underscore segment** before `_DSN`, lowercased
(`ShortDBName` in the host). So `ACME_SCHEDULER_ALL_DSN` is `all`, and two keys sharing a short name
make every run and console query refuse with `DB_AMBIGUOUS` until one is removed. Keep `<DBKEY>`
one segment: `ACME_SCHEDULERALL_DSN`, not `ACME_SCHEDULER_ALL_DSN`.

Prerequisites, both on the customer side:

- the DSN uses a **read-only** role — grounding is read-only by contract, `lib.db` never writes;
- the database host allows connections from the ReplyPen box (network / security-group allowlist). A
  DSN that only works from your laptop fails every production run.

`*_WRITE_DSN` is the action/write plane, not a grounding database: it never joins the roster and never
enters a run container. See [Tenant And Action Planes](#tenant-and-action-planes).

```bash
rp auth status
rp project database ls                                  # what is already configured

# 1. seal the DSN — this is what creates the database
printf %s "$DSN" | rp project env set key=ACME_BILLING_DSN

# 2. verify it physically resolves and holds the tables you expect (authoritative view)
rp dev console database list                            # expect name=billing
rp dev console database schema billing
rp dev console database query billing "select 1 as ok"

# 3. annotate it — this is what surfaces it in `rp project database ls`
rp project database set ACME_BILLING_DSN description="Invoices, subscriptions, payment state."
rp project database ls                                  # now includes ACME_BILLING_DSN
rp project database controls get ACME_BILLING_DSN       # pii + scope_manifest
```

`rp dev console database list` is the authoritative view of which DSNs actually exist and connect —
check there first when a database seems missing, not in `rp project database ls`. Verify from
production, not the laptop: grounding DSNs are usually IP-allowlisted to the box. See
[`prod-console`](../skills/prod-console/SKILL.md).

`description` is the only field `rp project database set` accepts, and it is what the production model
reads in its DB roster: one line naming the tables and purpose. Scope manifest and PII columns go
through `rp project database controls set`; those rules are operator-owned, so propose changes through a
ReplyPen support request ([support-boundary.md](support-boundary.md)).

4. Document the database in the brain's `skills/databases/` map (what it holds, which tables matter,
which script reads it) so the production model knows when to reach for it, and ship with `brain-publish`.

For local live checks, `rp project env pull` after sealing. `rp project env rm ACME_BILLING_DSN` removes
the DSN again.

## Delete Or Inspect

```bash
rp project env rm FOO_API_TOKEN
rp project env reveal FOO_API_TOKEN
```

`keys`, `pull`, `diff`, `set`, and `rm` do not print values. `reveal` intentionally prints one live
secret value for copy/pipe use and is audited by key name. Never reveal into an agent transcript. Piped,
`reveal` switches to JSON (`{"secret": "…"}`): feed it only into `rp project env set` (parse
`["secret"]`) or a hash, and print booleans or hashes, never a masked substring. A regex mask written
for a bare `postgresql://` line missed the JSON wrapper, echoed a whole DSN and forced a rotation.

## Tenant And Action Planes

On tenant-enabled projects, bulk `rp project env keys/pull/diff` can use the active login's tenant or an explicit
`--tenant`. Per-key `set/rm/reveal` target a tenant env only when the OAuth token itself is tenant-bound;
`--tenant` does not retarget those collection writes.

Action-plane credentials are project-level and operator-only:

```bash
printf %s "$WRITE_SECRET" | rp project env set key=FOO_WRITE_TOKEN --plane action
rp project env reveal FOO_WRITE_TOKEN --plane action
```

Use this only for `actions/<id>/` credentials. If an action body also needs a read DSN at execution time,
that read DSN must exist in `.env.action` too, because the hosted action executor loads only the action
plane.
