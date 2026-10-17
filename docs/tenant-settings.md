# Reading a tenant setting from Python

Applies to **templated project brains only** — the ones with `projection.yaml` + `tenant.schema.json`.
A flat project has no settings surface: `lib.tenant.get` just returns your fallback, so the same
script runs in both worlds. See [project and tenant brains](brain-model.md#project-and-tenant-brains).

In **any** Python a run or an action executes — a grounding script under `skills/*/scripts/`, or an
action's `script.src.py` / `preflight.py` / `policy.py` (`lib` is baked into the hosted harness too):

```python
from lib import tenant

hours = tenant.get("latecancel_min_hours", 48)   # fallback for an unset key / flat project
hours = tenant.require("latecancel_min_hours")   # raises when the script cannot degrade
```

Rules:

- Resolution is the host-injected `RC_TENANT_PROFILE_JSON` env document (every run and action
  container; `{"values": {}}` on a flat project) → `/brain/tenant_profile.json` only when no host
  document exists (local fixture) → `{}`. Malformed JSON from either source raises `TenantProfileError`.
- Never commit `tenant_profile.json` or `run_context.json` to a brain: both are host-generated, the host
  refuses to write them, and the runtime ignores a committed copy whenever the host injected its own.
- **Never let a playbook tell the model to copy a `{{ key }}` into a script argument or an action
  param.** The model copies wrong; placeholders are for prose the customer reads. A CLI flag may exist
  only as an explicit human/agent *override* of what `tenant.get` already reads.
- The key must exist in `tenant.schema.json`, and carry a default in `projection.yaml`, so `get` sees
  the tenant's *effective* value rather than the fallback.
- Test locally with `RC_TENANT_PROFILE_PATH=<file>`, or fetch a real tenant's document:
  `rp dev brain render --tenant <slug> --path tenant_profile.json` →
  `.replypen/output/brain-render-<slug>/tree/tenant_profile.json`.
