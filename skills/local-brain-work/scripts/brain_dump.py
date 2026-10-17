# /// script
# requires-python = ">=3.11"
# ///
"""Dump ONE brain run to two local files — a concise markdown index + a jq-queryable JSONL — over the
PUBLIC API. It needs only `rp auth login` plus the `rp` CLI on PATH — never SSM, a registry DB shell, or a
private ReplyPen checkout.

    uv run brain_dump.py <run_id>                 # writes .replypen/dump/<run8>-<proj>.{md,jsonl}
    uv run brain_dump.py <run_id> --out-dir /tmp

`fetch_via_api()` shells `rp run trace <id> -o json --stream` → the run-dump **bundle**
(`{run, events}`) → the SHARED `run_dump` renderer in `replypen-runtime` → both files. Get a <run_id> from default
email-simulation `rp ask "<q>"`, or from `rp ask "<q>" --scenario raw` for a direct investigation.
Add `--brain-ref dev/x` to either scenario to test a pushed dev branch without moving `main`.

Then drill into any step with jq (the index prints ready-made queries):

    jq -r 'select(.disp=="3").command' .replypen/dump/<run8>-<proj>.jsonl
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import brain_env as E


def _cli() -> str:
    """The ReplyPen CLI on PATH: `rp`, else `rc` (its pre-rebrand name, same binary)."""
    return "rp" if shutil.which("rp") else "rc"

# Brain-dir-relative, under the wholesale-gitignored `.replypen/` dir (one `/.replypen/` rule covers
# every rc/kit subfolder — debug dumps, run dumps, …). It's run output (real run data, PII), not brain
# content, so it never gets committed. Written under the cwd you invoke from.
OUT_DIR = Path(".replypen") / "dump"


def _load_renderer():
    """Import the shared run_dump renderer from `replypen-runtime`. The renderer is pure stdlib, so
    the canonical sibling `runtime/` dir (present in every full-kit install: checkout, CC/Codex plugin
    bundle, local symlink) needs no package install — just put it on the path. Returns the three entry
    points, or None when neither the local dir nor an installed package is importable (→ uv re-exec)."""
    if E.RUNTIME.is_dir():
        sys.path.insert(0, str(E.RUNTIME))
    try:
        from lib.run_dump import decorate, emit_jsonl, render_index  # noqa: F401 (decorate re-exported)
        return render_index, emit_jsonl
    except ModuleNotFoundError:
        return None


def fetch_via_api(run_id: str) -> dict:
    """The run-dump trace bundle (`{run, events}`) = `rp run trace <id> -o json --stream`. `rp` carries
    the auth from `rp auth login` and the current brain checkout; we only parse its output. Raises on a
    CLI/parse failure.

    `rp run trace <id> -o json --stream` emits the bundle as **NDJSON** — one
    `{"type":"run",…}` header line, then one `{"type":"event",…}` line per tool call (the same shape
    `emit_jsonl` writes). We reassemble that back into the `{run, events}` dict the renderer consumes.
    A single JSON object is also accepted, so the function survives either CLI emit style."""
    proc = subprocess.run(
        [_cli(), "run", "trace", run_id, "-o", "json", "--stream"], capture_output=True, text=True
    )
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}"
        raise RuntimeError(f"`rp run trace {run_id} -o json --stream` failed: {detail}")

    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    if not lines:
        raise RuntimeError("`rp run trace` returned no output — is the API version recent enough?")
    try:
        objs = [json.loads(ln) for ln in lines]
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"`rp run trace` did not return JSON ({exc}); got: {proc.stdout[:200]!r}") from exc

    # Single bundle object (forward-compat) vs the NDJSON header+events stream.
    if len(objs) == 1 and "run" in objs[0] and "events" in objs[0]:
        return objs[0]

    strip = lambda o: {k: v for k, v in o.items() if k != "type"}  # noqa: E731
    run = next((strip(o) for o in objs if o.get("type") == "run"), None)
    events = [strip(o) for o in objs if o.get("type") == "event"]
    if run is None:
        raise RuntimeError("`rp run trace` output has no run-header line ({type:run}) — "
                           "is the `rp` / API version recent enough? (needs the trace bundle endpoint)")
    return {"run": run, "events": events}


def _reexec_under_uv() -> int:
    """Standalone-skill fallback: the kit's `runtime/` dir isn't alongside, so re-run self under
    `uv run --with replypen-runtime` (the tag-pinned package) where `lib.run_dump` resolves."""
    if os.environ.get("_BRAIN_DUMP_REEXEC"):
        print("error: could not import lib.run_dump even under the pinned replypen-runtime — "
              "check RC_RUNTIME_SPEC / network access to the repo.", file=sys.stderr)
        return 1
    cmd = [*E.uv_base_cmd(), "python", str(Path(__file__).resolve()), *sys.argv[1:]]
    return subprocess.run(cmd, env={**os.environ, "_BRAIN_DUMP_REEXEC": "1"}).returncode


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="brain_dump.py", description=__doc__.split("\n")[0])
    p.add_argument("run_id", help="the run UUID (from `rp ask`, optionally `--scenario raw`).")
    p.add_argument("--out-dir", help=f"output directory (default: {OUT_DIR}).")
    args = p.parse_args(sys.argv[1:] if argv is None else argv)

    renderer = _load_renderer()
    if renderer is None:
        return _reexec_under_uv()
    render_index, emit_jsonl = renderer

    try:
        bundle = fetch_via_api(args.run_id)
    except Exception as exc:  # noqa: BLE001 — surface the message and exit non-zero
        print(f"error: {exc}", file=sys.stderr)
        return 1

    run = bundle["run"]
    base = f"{str(run['run_id'])[:8]}-{run['project']}"
    out_dir = Path(args.out_dir) if args.out_dir else OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    index_path = out_dir / f"{base}.md"
    jsonl_path = out_dir / f"{base}.jsonl"
    index_path.write_text(render_index(bundle), encoding="utf-8")
    jsonl_path.write_text("\n".join(emit_jsonl(bundle)) + "\n", encoding="utf-8")

    print(f"wrote {index_path}")
    print(f"wrote {jsonl_path}")
    ref = run.get("brain_ref")
    print(f"run {run['run_id']} · status={run['status']}"
          + (f" · brain_ref={ref}" if ref else "")
          + f" · {len(bundle['events'])} tool calls")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
