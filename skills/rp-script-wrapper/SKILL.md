---
name: rp-script-wrapper
description: "Drive the public `rp` CLI from a deterministic local Python or shell script — repeatable console queries, complete exports, typed failures, remote artifact fetches. Use when a console read must be reproducible or feed a program; not for code that runs inside a production brain container (no `rp` there — use the injected `lib.*` helpers)."
---

# rp-script-wrapper — deterministic local wrappers

Developer machine / brain checkout only. Production brain scripts have no `rp`; they use the injected
`lib.db`, `lib.fs`, and friends.

## Prefer the Python client

`lib.rc_client` ([`runtime/lib/rc_client.py`](../../runtime/lib/rc_client.py)) preserves the CLI's
JSON contract — column order, duplicate column names, lossless typing — and maps CLI exit codes to
typed exceptions. Reach for it over parsing `rp` output. Never parse table output, spill manifests, or
shell-quoted JSON.

```python
from lib import rc_client

result = rc_client.query(
    "select id, state from jobs where created_at >= @since order by id",
    {"since": "2026-08-01"}, all=True, database="billing",
)
rc_client.query_to_csv("select id, state from jobs order by id", "./jobs.csv", all=True)  # streams, no buffering
res = rc_client.bash("python /brain/skills/reconcile/scripts/check.py", timeout=120)
rc_client.file_get("/tmp/replypen-out/jobs-abc123.csv", "./jobs.csv")
```

**Install a published, pinned tag — never float `main`, never copy the wrapper into your script.**
The `uv run --with` / PEP 723 form with the current tag is in
[README.md](../../README.md) and [docs/migration-host.md](../../docs/migration-host.md)
(that runbook also covers moving an existing wrapper). Run `uv lock --script script.py` once and
commit the `script.py.lock`: it freezes the tag to a commit sha plus the whole transitive closure, so
later runs resolve from cache instead of re-fetching the tag over the network. Re-run it after a bump.

Database resolution: `RC_CONSOLE_DATABASE` → `RC_DB_DEFAULT` → `prod`; pass `database=` to be explicit.

Catch only the recovery you can actually perform — `AuthenticationError` (login/scope),
`TruncatedError` (use `all=True`, or opt into `allow_truncated=True` on purpose), `RemoteCommandError`
(the guarded workspace command could not start), `TransportError` (server/network). A remote command
exiting non-zero or timing out is **not** an exception: `bash()` still returns a `BashResult` — inspect
`exit_code`, `stderr`, `timed_out`. Omitting `timeout` accepts the server's 120s remote default (plus
~30s local setup/transport); pass it explicitly when a project advertises another limit.

## Non-Python callers

The machine contract — `-o json` envelope, stable exit codes (0/1/2/3/4/5), `--all` snapshot streaming,
`--out`/`--out -`/`--out auto`, `@key` + `--param` binding, SQL from an argument / `-` / `@file` — is
owned by the [replypen-cli README](https://github.com/replypen/replypen-cli#readme) and
[docs/rp-cli.md](../../docs/rp-cli.md). Two rules that scripts get wrong:

- **Branch on the exit code; never consume a partial response as success.** A truncated inline result
  fails closed (exit 3) precisely so a script cannot silently under-report.
- **`--param` is the only supported way to get data into SQL.** Never invent base64 or
  character-substitution quoting to dodge shell escaping — pass `-` or `@file` instead.

## Remote artifacts

`emit_rows` previews in a production run name a fetchable `/tmp/replypen-out/…` path:

```bash
rp dev console file get /tmp/replypen-out/report-abc123.csv --out ./report.csv
```

Preview for shape/orientation; the file for complete machine processing. The console only exposes its
session workspace and `/tmp` — a rejected path is a boundary, not a reason to fall back to base64 over
bash output.
