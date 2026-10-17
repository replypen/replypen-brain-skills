# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml"]
# ///
"""Run a ReplyPen-HOSTED Python action locally — the same way `HostedExecutor` runs it in prod.

A hosted action (`actions/<id>/{manifest.yaml, script.py, preflight.py?}`) is the one state-changing
plane in a brain (see replypen's `actions` / `brain-authoring` skills). In prod the agent only
*proposes* it, a human confirms, and `HostedExecutor` runs `script.py` ONCE in the hardened workspace
container against the sealed `.env.action`. There is otherwise **no dry run of a write body** — so this
runner reproduces that loop on the laptop, faithfully, defaulting to a **dry-run** (the body rolls back).

It gives the same feedback prod would, at the same points:

  1. **Layer-1 manifest validation** — the same `format`/`pattern`/`enum`/`type`/`required` checks the
     host runs at propose time. A mis-shaped param fails here, before anything runs.
  2. **Preflight** (`preflight.py`, if present) — runs read-only in the GROUNDING env (the brain `./.env`
     read DSNs), exactly as the host's in-loop Layer-2. Fail-closed: `ok:false`/crash/unparseable stops
     the run and prints the reason.
  3. **Policy gate** (`policy.py`, if present) — runs read-only in the same GROUNDING env, exactly as the
     host's `PolicyRunner` does for an `autonomy: policy` action. Prints the verdict for THIS invocation:
     `allow` (would auto-execute mid-loop) or `deny` (would escalate to human). Informational for the local
     body test; `--policy-only` turns the verdict into the exit code.
  4. **Write body** (`script.py`) — runs against the sealed **`./.env.action` ONLY** (never the grounding
     `.env`), mirroring the prod container env precisely: a read DSN the body needs but that's missing
     from `.env.action` fails locally the same way it would in prod. Delivered the `RC_ACTION_PARAMS` /
     `RC_ACTION_RESULT` file contract; **dry-run by default** (`RC_ACTION_DRY_RUN=1`).

    uv run brain_action.py --list
    uv run brain_action.py boost_powertools_credits --params '{"user_podio_id":613236,"extra_credits":1000}' --preflight-only
    uv run brain_action.py boost_powertools_credits --params '{"user_podio_id":613236,"extra_credits":1000}'            # dry-run
    uv run brain_action.py boost_powertools_credits --params '{"user_podio_id":613236,"extra_credits":1000}' --commit   # REAL write

⚠️ This is a LOCAL faithful reproduction, not the prod path. Authoring against a real run still goes
through `brain-publish` and public/support-gated publish mechanics. `--commit` here writes
for real against whatever `.env.action` points at — point it at a local/staging DB, never a live customer.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import brain_env as E
import yaml

# ── manifest ──────────────────────────────────────────────────────────────────────────────────────
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_URL_RE = re.compile(r"^https?://[^\s]+$")


def actions_dir(brain_dir: Path) -> Path:
    return brain_dir / "actions"


def list_actions(brain_dir: Path) -> list[str]:
    d = actions_dir(brain_dir)
    if not d.is_dir():
        return []
    return sorted(p.name for p in d.iterdir() if p.is_dir() and (p / "manifest.yaml").is_file())


def load_manifest(action_path: Path) -> dict:
    mf = action_path / "manifest.yaml"
    if not mf.is_file():
        raise FileNotFoundError(f"no manifest.yaml in {action_path}")
    data = yaml.safe_load(mf.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{mf}: manifest must be a mapping")
    return data


def _check_type(name: str, value, typ: str) -> str | None:
    """Mirror the host's ParamType check (internal/action/action.go). Returns an error string or None.
    bool is NOT an int here (Python's bool-is-int footgun would let `true` pass as integer).
    Attachment/generated_file params also accept the delivered local file descriptor shape so body smoke
    tests can run after the host's fetch/rewrite phase."""
    if typ in ("", "string"):
        return None if isinstance(value, str) else f"param {name!r} must be a string, got {type(value).__name__}"
    if typ == "integer":
        ok = (isinstance(value, int) and not isinstance(value, bool)) or (isinstance(value, float) and value.is_integer())
        return None if ok else f"param {name!r} must be an integer, got {value!r}"
    if typ == "number":
        ok = isinstance(value, (int, float)) and not isinstance(value, bool)
        return None if ok else f"param {name!r} must be a number, got {value!r}"
    if typ == "boolean":
        return None if isinstance(value, bool) else f"param {name!r} must be a boolean, got {value!r}"
    if typ == "string[]":
        ok = isinstance(value, list) and all(isinstance(x, str) for x in value)
        return None if ok else f"param {name!r} must be a string[]"
    if typ == "object":
        return None if isinstance(value, dict) else f"param {name!r} must be a JSON object"
    if typ == "attachment":
        if isinstance(value, str):
            trimmed = value.strip()
            return None if _UUID_RE.match(trimmed) else f"param {name!r} must be an attachment_id UUID string, got {value!r}"
        if isinstance(value, dict):
            path = value.get("path") or value.get("local_path") or value.get("tmp_path")
            return None if isinstance(path, str) and path else f"param {name!r} local attachment descriptor must include a path"
        return f"param {name!r} must be an attachment_id UUID string, got {type(value).__name__}"
    if typ == "generated_file":
        if isinstance(value, dict):
            path = value.get("path") or value.get("local_path") or value.get("tmp_path")
            if isinstance(path, str) and path:
                return None
            kind = str(value.get("kind") or "").strip()
            message_id = str(value.get("message_id") or "").strip()
            if not kind:
                return f"param {name!r} generated_file requires kind"
            if not _UUID_RE.match(message_id):
                return f"param {name!r} generated_file requires message_id UUID string"
            return None
        return f"param {name!r} must be a generated_file object"
    return f"param {name!r} declares an unsupported type {typ!r}"


def _check_constraints(name: str, value, p: dict) -> str | None:
    """Layer-1 format/pattern/enum — STRING values only (a non-string passed its type check already)."""
    if p.get("type") == "generated_file" and isinstance(value, dict):
        path = value.get("path") or value.get("local_path") or value.get("tmp_path")
        if path:
            return None
        want = str(p.get("generator") or "").strip()
        got = str(value.get("kind") or "").strip()
        if want and got != want:
            return f"param {name!r} generated_file kind {got!r} does not match generator {want!r}"
        return None
    if not isinstance(value, str):
        return None
    enum = p.get("enum") or []
    if enum and value not in enum:
        return f"param {name!r} must be one of {enum}, got {value!r}"
    fmt = p.get("format") or ""
    if fmt == "email" and not _EMAIL_RE.match(value):
        return f"param {name!r} must be a valid email, got {value!r}"
    if fmt == "uuid" and not _UUID_RE.match(value):
        return f"param {name!r} must be a valid uuid, got {value!r}"
    if fmt == "url" and not _URL_RE.match(value):
        return f"param {name!r} must be a valid url, got {value!r}"
    if fmt and fmt not in ("email", "uuid", "url"):
        return f"param {name!r} declares an unsupported format {fmt!r} (want email|url|uuid)"
    pat = p.get("pattern") or ""
    if pat:
        try:
            rx = re.compile("^(?:" + pat + ")$")
        except re.error as e:
            return f"param {name!r} declares an invalid pattern {pat!r}: {e}"
        if not rx.match(value):
            return f"param {name!r} value {value!r} does not match required pattern {pat!r}"
    return None


def validate_params(manifest: dict, params: dict) -> list[str]:
    """Return a list of Layer-1 validation errors (empty == valid). Mirrors host `ValidateParams`."""
    errors: list[str] = []
    schema = manifest.get("params") or []
    declared = {p["name"] for p in schema}
    for p in schema:
        name = p["name"]
        if name not in params or params[name] is None:
            if p.get("required"):
                errors.append(f"param {name!r} is required")
            continue
        err = _check_type(name, params[name], p.get("type", "string"))
        if err:
            errors.append(err)
            continue
        err = _check_constraints(name, params[name], p)
        if err:
            errors.append(err)
    for extra in sorted(set(params) - declared):
        errors.append(f"unknown param {extra!r} (not in the manifest schema)")
    return errors


# ── running a child python (preflight / body) under uv with `lib` ─────────────────────────────────
def _run_child(child_env: dict, script: Path, *, label: str) -> subprocess.CompletedProcess:
    """Run `script` under `uv run` (providing `lib` + pinned deps), capturing output. Hard-fails up
    front if `import lib.db` won't resolve in the child env — the same footgun guard brain_run uses."""
    if not E.preflight_lib_db(child_env):
        raise SystemExit(1)
    print(f"  → running {label} ({script.name})", file=sys.stderr)
    return subprocess.run(
        [*E.uv_base_cmd(), "python", str(script)],
        env=child_env, capture_output=True, text=True,
    )


def _load_result_file(path: Path, proc: subprocess.CompletedProcess) -> dict | None:
    """Parse the JSON the child wrote to its result file. None when absent/unparseable (the caller
    treats that as a failure, mirroring the host's NoResult / fail-closed handling)."""
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        raw = ""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _emit_child_logs(proc: subprocess.CompletedProcess) -> None:
    if proc.stderr.strip():
        for line in proc.stderr.rstrip().splitlines():
            print(f"    [stderr] {line}", file=sys.stderr)


# ── phases ────────────────────────────────────────────────────────────────────────────────────────
def run_preflight(brain_dir: Path, action_path: Path, params: dict, mirrors_root: str | None) -> bool:
    """Run preflight.py read-only in the grounding env. Returns True to proceed (ok:true or no
    preflight present), False to stop (fail-closed)."""
    pf = action_path / "preflight.py"
    if not pf.is_file():
        print("preflight: none (no preflight.py) — Layer-1 validation only", file=sys.stderr)
        return True

    secrets = E.brain_secrets(brain_dir, required=True)  # preflight needs the grounding read DSNs
    if secrets is None:
        return False
    child = E.uv_child_env(secrets, [pf.parent], mirrors_root)
    with tempfile.TemporaryDirectory() as td:
        result_file = Path(td) / "preflight_result.json"
        child["PREFLIGHT_PARAMS"] = json.dumps(params)
        child["PREFLIGHT_RESULT"] = str(result_file)
        proc = _run_child(child, pf, label="preflight")
        result = _load_result_file(result_file, proc)

    print("\n── preflight (read-only, grounding plane) ─────────────────────────────")
    if result is None:
        _emit_child_logs(proc)
        print("❌ preflight produced no parseable PreflightResult — treating as ok:false (fail-closed).")
        return False
    ok = bool(result.get("ok"))
    print(("✅ " if ok else "❌ ") + (result.get("summary") or ""))
    if not ok and result.get("reason"):
        print(f"   reason: {result['reason']}")
    if result.get("resource_url"):
        print(f"   resource_url: {result['resource_url']}")
    if result.get("observed"):
        print("   observed: " + json.dumps(result["observed"], indent=2, default=str).replace("\n", "\n   "))
    if not ok:
        print("\nPreflight failed — the proposal would be blocked in prod. Fix the params and retry.")
    return ok


def run_policy(brain_dir: Path, action_path: Path, manifest: dict, params: dict, mirrors_root: str | None) -> bool:
    """Run policy.py read-only in the grounding env, exactly as the host's PolicyRunner does for an
    `autonomy: policy` action. Reports whether THIS invocation would auto-execute (allow) or escalate to
    a human (deny). Returns True on allow / no-policy, False on deny/crash/unparseable (fail-closed).

    Unlike preflight this is INFORMATIONAL for the local loop: a deny doesn't block the write-body test
    (in prod a deny just routes the action back through human-confirm, which still runs the body later) —
    it tells you the autonomy verdict the agent would get. `--policy-only` uses the return value as the
    exit code so you can gate CI on it."""
    autonomy = str(manifest.get("autonomy") or "human").strip()
    pol = action_path / "policy.py"
    print("\n── policy gate (read-only, grounding plane) ──────────────────────────")
    if not pol.is_file():
        note = "no policy.py"
        if autonomy == "policy":
            note += " — but manifest says `autonomy: policy`, which REQUIRES one (the host refuses to resolve this action)"
        print(f"policy: {note} (autonomy: {autonomy})")
        return autonomy != "policy"
    if autonomy != "policy":
        print(f"⚠️  policy.py present but manifest autonomy is `{autonomy}` — the gate only runs for `autonomy: policy`; showing the verdict anyway.")

    secrets = E.brain_secrets(brain_dir, required=True)  # policy needs the grounding read DSNs
    if secrets is None:
        return False
    child = E.uv_child_env(secrets, [pol.parent], mirrors_root)
    with tempfile.TemporaryDirectory() as td:
        result_file = Path(td) / "policy_result.json"
        child["RC_POLICY_PARAMS"] = json.dumps(params)
        child["RC_POLICY_RESULT"] = str(result_file)
        proc = _run_child(child, pol, label="policy")
        result = _load_result_file(result_file, proc)

    if result is None:
        _emit_child_logs(proc)
        print("❌ policy produced no parseable verdict — treating as deny (fail-closed → escalate to human).")
        return False
    allow = bool(result.get("allow"))
    print(("✅ allow — would auto-execute mid-loop" if allow else "🔒 deny — would escalate to human confirm"))
    if result.get("reason"):
        print(f"   reason: {result['reason']}")
    if result.get("observed"):
        print("   observed: " + json.dumps(result["observed"], indent=2, default=str).replace("\n", "\n   "))
    return allow


def run_body(brain_dir: Path, action_path: Path, manifest: dict, params: dict, *, commit: bool) -> int:
    """Run script.py against `.env.action` ONLY (faithful to the prod action container), dry-run unless
    --commit. Returns a process exit code."""
    runtime = (manifest.get("runtime") or "python").lower()
    if runtime != "python":
        print(f"error: runtime {runtime!r} — only hosted PYTHON actions run in this local runner "
              f"(gem/ruby actions run through the public/support-gated action plane). See actions/README.md.", file=sys.stderr)
        return 2
    script = action_path / "script.py"
    if not script.is_file():
        print(f"error: no script.py in {action_path}", file=sys.stderr)
        return 1
    for p in manifest.get("params") or []:
        if p.get("type") != "generated_file":
            continue
        raw = params.get(p["name"])
        if not isinstance(raw, dict):
            continue
        path = raw.get("path") or raw.get("local_path") or raw.get("tmp_path")
        if path:
            continue
        print(
            f"error: generated_file param {p['name']!r} is a hosted source object, not a materialized file. "
            "Local body tests need the delivered descriptor shape with a path.",
            file=sys.stderr,
        )
        return 2

    # The action container sees the sealed .env.action ONLY — never the grounding .env. Reproduce that
    # exactly: feed the body just .env.action, so a missing read DSN fails here like it would in prod.
    env_action = brain_dir / ".env.action"
    if not env_action.is_file():
        print(f"error: no {env_action} — the write plane lives in the sealed .env.action (a `*_WRITE_DSN` "
              f"plus any read DSNs the body needs). Create it (gitignored) to run the body locally.",
              file=sys.stderr)
        return 1
    action_secrets = E.parse_env(env_action)
    if not action_secrets:
        print(f"warning: {env_action} parsed to no keys — the body will have no credentials.", file=sys.stderr)

    child = E.uv_child_env(action_secrets, [script.parent], None)
    with tempfile.TemporaryDirectory() as td:
        params_file = Path(td) / "params.json"
        result_file = Path(td) / "result.json"
        params_file.write_text(json.dumps(params), encoding="utf-8")
        result_file.write_text("", encoding="utf-8")
        child["RC_ACTION_PARAMS"] = str(params_file)
        child["RC_ACTION_RESULT"] = str(result_file)
        if not commit:
            child["RC_ACTION_DRY_RUN"] = "1"

        mode = "COMMIT (real write)" if commit else "DRY-RUN (rolls back)"
        print(f"\n── write body — {mode} — .env.action keys: {sorted(action_secrets)} ──")
        if commit:
            print("⚠️  --commit: this writes for real against whatever .env.action points at. Make sure "
                  "that is NOT a live customer on prod.")
        proc = _run_child(child, script, label="script.py")
        result = _load_result_file(result_file, proc)

    if result is None:
        _emit_child_logs(proc)
        print("❌ the action produced no parseable Result (it must write its Result JSON to "
              "$RC_ACTION_RESULT). Recorded as a failure in prod.")
        return 1

    ok = bool(result.get("ok"))
    print(("✅ ok" if ok else "❌ failed") + f"  ({mode})")
    if result.get("error"):
        err = result["error"]
        print(f"   error.class: {err.get('class')}\n   error.message: {err.get('message')}")
        if err.get("backtrace"):
            _emit_child_logs(proc)
    if "return_value" in result:
        print("   return_value: " + json.dumps(result["return_value"], indent=2, default=str).replace("\n", "\n   "))
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="brain_action.py", formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Run a hosted Python action locally (preflight + write body), the way HostedExecutor does.")
    p.add_argument("action_id", nargs="?", help="the action id (a dir under actions/)")
    p.add_argument("--brain", help="brain dir (default: cwd)")
    p.add_argument("--params", help="action params as a JSON object")
    p.add_argument("--params-file", help="read params JSON from a file instead of --params")
    p.add_argument("--preflight-only", action="store_true", help="run Layer-1 + preflight, skip the write body")
    p.add_argument("--no-preflight", action="store_true", help="skip the preflight (Layer-1 still runs)")
    p.add_argument("--policy-only", action="store_true", help="run Layer-1 + preflight + policy gate, skip the write body; exit code reflects the allow/deny verdict")
    p.add_argument("--no-policy", action="store_true", help="skip the policy gate (Layer-1 + preflight still run)")
    p.add_argument("--commit", action="store_true", help="really commit the write (default: dry-run/rollback)")
    p.add_argument("--mirrors-root", help="dir whose immediate subdirs are source mirrors (for the preflight)")
    p.add_argument("--list", action="store_true", help="list the brain's actions and exit")
    args = p.parse_args(argv)

    brain_dir = E.resolve_brain_dir(args.brain)
    if not brain_dir.is_dir():
        print(f"error: brain dir not found: {brain_dir}", file=sys.stderr)
        return 1

    if args.list or not args.action_id:
        acts = list_actions(brain_dir)
        print(f"actions in {brain_dir} ({len(acts)}):" if acts else f"no actions/ in {brain_dir}")
        for a in acts:
            print(f"  {a}")
        return 0 if args.list else (0 if acts else 1)

    action_path = actions_dir(brain_dir) / args.action_id
    if not action_path.is_dir():
        print(f"error: no action {args.action_id!r} (looked in {actions_dir(brain_dir)})", file=sys.stderr)
        return 1
    try:
        manifest = load_manifest(action_path)
    except (FileNotFoundError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    # params
    if args.params_file:
        params = json.loads(Path(args.params_file).read_text(encoding="utf-8"))
    elif args.params:
        params = json.loads(args.params)
    else:
        params = {}
    if not isinstance(params, dict):
        print("error: --params must be a JSON object", file=sys.stderr)
        return 1

    print(f"action: {args.action_id}  (runtime: {manifest.get('runtime', 'python')}, "
          f"risk: {manifest.get('risk', 'n/a')})")
    print(f"params: {json.dumps(params)}")

    # 1. Layer-1 validation (same as the host at propose time)
    errors = validate_params(manifest, params)
    print("\n── Layer-1 manifest validation ───────────────────────────────────────")
    if errors:
        for e in errors:
            print(f"❌ {e}")
        print("\nFix the params to match the manifest schema and retry.")
        return 1
    print("✅ params satisfy the manifest schema")

    # 2. preflight (unless skipped)
    if not args.no_preflight:
        if not run_preflight(brain_dir, action_path, params, args.mirrors_root):
            return 1
    else:
        print("preflight: skipped (--no-preflight)", file=sys.stderr)

    if args.preflight_only:
        print("\n(--preflight-only: stopping before the write body)")
        return 0

    # 3. policy gate (informational unless --policy-only): the autonomy verdict for THIS invocation.
    policy_allow = True
    if not args.no_policy:
        policy_allow = run_policy(brain_dir, action_path, manifest, params, args.mirrors_root)
    else:
        print("policy: skipped (--no-policy)", file=sys.stderr)

    if args.policy_only:
        print("\n(--policy-only: stopping before the write body)")
        return 0 if policy_allow else 1

    # 4. write body
    print(f"\n[brain-action] {E.UV_MODE_CAVEATS}", file=sys.stderr)
    return run_body(brain_dir, action_path, manifest, params, commit=args.commit)


if __name__ == "__main__":
    raise SystemExit(main())
