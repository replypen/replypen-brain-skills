"""ReplyPen sandbox helper library.

Thin, READ-ONLY helpers the agent's Python grounding code imports inside the disposable
container: `db`, `api`, `stripe`, `cloudwatch`, `fs`, `http`, `html`, `oauth`. Hosted Python actions
also import `action` for the post-confirmation write-plane harness. `api` is the generic read-tier
REST client (the `lib.db` of third-party HTTP integrations); most integrations need only a manifest
row + `python -m lib.api get <key> <path>`, with allowlisted `post` for non-mutating search.
Runtime-owned HTTP attempts share one audited transport; hosted actions use `lib.action.client` or
the action-context-asserted `lib.http.action_request` for non-catalogued providers.
Each is configured from the per-project
secrets injected as env (read-only PG DSN, Stripe restricted key, least-priv AWS creds, GitHub
read token) and the read-only mounts: the brain at /brain and source mirrors at /mirrors/<repo>,
both `:ro` (a write returns EROFS). Only /tmp is writable scratch.

A run rewrites nothing inline — the brain is `:ro` to it. Its only durable output is the
structured `reply.journal` entry, which the HOST appends to the brain as a journal commit;
the curated brain (these helpers included) evolves out of band via the consolidation cron's
operator-merged PRs. They favour being obvious and safe over complete.

`tenant` reads the tenant's effective onboarding-profile values from the host-injected
`RC_TENANT_PROFILE_JSON` document (falling back to `/brain/tenant_profile.json` only when the host
stamped none) — so a script fetches its own setting (`tenant.get("latecancel_min_hours", 24)`) instead
of being handed one.

`runctx` answers "what kind of run is this?" from the host-injected `RC_RUN_CONTEXT_JSON` (file
fallback `/brain/run_context.json`): plane, ingress surface, simulation flag, and
whether the run is scoped to one asserted end-user (`runctx.is_principal_scoped()`). Branch on it
instead of on the prompt's prose — it is descriptive only, never an authorization check.

A project has several databases — pick one with ``db=`` (short name, env-var name, or DSN); see
``db.databases()``. ``db`` and ``cloudwatch`` also have a CLI for one-off queries from bash
(``python -m lib.db --list``, ``python -m lib.cloudwatch --tail <group>``).

Typical use from a `bash` Python script:

    from lib import db, stripe, fs
    rows = db.query("select id, email from accounts where email = %s", ["a@b.com"], db="powertools")
    inv = stripe.latest_invoice("cus_123")
    print(fs.read_file("powertools", "metering/credit.go", 1, 40))
"""

# Submodules are imported on demand (`from lib import db`), not eagerly here: eager imports make
# `python -m lib.db` double-import the module it's running and emit a RuntimeWarning on every call.
__all__ = ["db", "api", "stripe", "cloudwatch", "fs", "http", "html", "oauth", "action", "tenant", "runctx", "rc_client", "telemetry"]

# Auto-wire best-effort PostHog error tracking (no-op unless the host stamps RC_TELEMETRY_POSTHOG_KEY). Swallow any
# failure here — importing `lib` must never fail because telemetry couldn't initialize.
from . import telemetry as telemetry  # noqa: E402

try:
    telemetry.install()
except Exception:  # noqa: BLE001
    pass
