# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml"]
# ///
"""Inspect the tenant projection inputs for a templated project brain.

Read-only by default: fetches one tenant's profile values through `rc project tenant profile get`, reads
the local `projection.yaml`, and prints the choices prod will make before mounting the ephemeral compiled
view at `/brain`. It never writes compiled files into the brain tree. With `--write-summary`, it writes
only debug artifacts under `.rootcause/projection/<tenant>/`.

    uv run brain_projection.py --tenant de-kies
    uv run brain_projection.py --tenant de-kies --write-summary
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import brain_env as E
import yaml

OUT_ROOT = Path(".rootcause") / "projection"
BRANCH_KEYS = (
    "newpatient_method",
    "existingpatient_method",
    "reschedule_method",
    "booking_hygienist_dentist_interaction",
)


def _lookup(root: dict[str, Any], dotted: str) -> tuple[Any, bool]:
    cur: Any = root
    for seg in dotted.split("."):
        if not isinstance(cur, dict) or seg not in cur:
            return None, False
        cur = cur[seg]
    return cur, True


def _as_inline(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _load_spec(brain_dir: Path) -> dict[str, Any]:
    path = brain_dir / "projection.yaml"
    if not path.is_file():
        raise FileNotFoundError(f"{path} not found (this brain is not templated)")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a YAML mapping")
    return data


def _fetch_settings(brain_dir: Path, tenant: str) -> dict[str, Any]:
    proc = subprocess.run(
        ["rc", "project", "tenant", "profile", "get", tenant, "-o", "json", "--raw-output"],
        cwd=brain_dir,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}"
        raise RuntimeError(f"`rc project tenant profile get {tenant} -o json` failed: {detail}")
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"`rc project tenant profile get` did not return JSON ({exc}): {proc.stdout[:200]!r}") from exc
    if not isinstance(data, dict):
        raise RuntimeError("`rc project tenant profile get` returned non-object JSON")
    settings = data.get("settings") or {}
    if not isinstance(settings, dict):
        raise RuntimeError("tenant profile response has non-object `settings`")
    return data


# Two reserved variant names, mirroring rootcause `internal/projection`: `unset` (absent, null or a
# blank string) and `present` (any other value — the catch-all a free-text key branches on).
SELECTOR_UNSET = "unset"
VARIANT_PRESENT = "present"


def _render_selector(raw: Any) -> str | None:
    """The exact string a value is matched against `when=`, or None if it has no such form.

    Mirrors `resolveSelector`: absent/null/blank -> `unset`, bool -> true/false, integral number ->
    its decimal form. A fractional number, list or dict is a data fault with no writable variant name.
    """
    if raw is None:
        return SELECTOR_UNSET
    if isinstance(raw, str):
        return raw if raw.strip() else SELECTOR_UNSET
    if isinstance(raw, bool):
        return "true" if raw else "false"
    if isinstance(raw, int):
        return str(raw)
    if isinstance(raw, float) and raw.is_integer():
        return str(int(raw))
    return None


def _yaml_token(v: Any) -> str:
    """A variant/default name as the Go compiler (yaml.v3, YAML 1.2) reads it.

    PyYAML is YAML 1.1: bare `off`/`on`/`yes`/`no` parse as booleans here but stay strings in Go, so a
    real name would vanish into "" and the preview would wrongly report a dropped region. Warn, and
    recover the token the author wrote. Quote such names in projection.yaml to keep both readers honest.
    """
    if v is None:
        return ""
    if isinstance(v, bool):
        print(f"warn: projection.yaml uses a bare YAML-1.1 boolean lookalike as a variant/default "
              f"({'true' if v else 'false'} after parsing) — quote it, e.g. \"off\"", file=sys.stderr)
        return "true" if v else "false"
    return str(v)


def _selected_variant(settings: dict[str, Any], branch: dict[str, Any]) -> tuple[str, str, str, str | None]:
    """(selector field, raw value as displayed, variant prod collapses to, note) — "" = region dropped."""
    field = str(branch.get("select") or "")
    raw, found = _lookup(settings, field)
    if not found:
        raw = None
    rendered = _render_selector(raw)
    variants = [_yaml_token(v) for v in branch.get("variants") or []]
    default = _yaml_token(branch.get("default"))
    shown = _as_inline(raw) if rendered is None else rendered
    if rendered is None:
        note = f"type-error: selector is not a string, bool or int -> {default or 'region dropped'}"
        return field, shown, default, note
    if rendered in variants:
        return field, shown, rendered, None
    if rendered != SELECTOR_UNSET and VARIANT_PRESENT in variants:
        return field, shown, VARIANT_PRESENT, f"value is set -> {VARIANT_PRESENT}"
    if default:
        return field, shown, default, f"default -> {default}"
    return field, shown, "", "error: no default for unmatched selector (region dropped)"


def _parse_literal(s: str) -> Any:
    s = s.strip()
    if s == "true":
        return True
    if s == "false":
        return False
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        return s[1:-1]
    try:
        return float(s)
    except ValueError:
        return s


def _loose_equal(got: Any, want: Any) -> bool:
    if isinstance(want, bool):
        return isinstance(got, bool) and got == want
    if isinstance(want, float):
        return isinstance(got, (int, float)) and not isinstance(got, bool) and float(got) == want
    if isinstance(want, str):
        return isinstance(got, str) and got == want
    return False


def _eval_keep_when(expr: str, settings: dict[str, Any]) -> tuple[bool | None, str]:
    expr = expr.strip()
    if "==" in expr:
        lhs, rhs = expr.split("==", 1)
        op = "=="
    elif "!=" in expr:
        lhs, rhs = expr.split("!=", 1)
        op = "!="
    else:
        return None, "expected == or !="
    got, present = _lookup(settings, lhs.strip())
    eq = present and _loose_equal(got, _parse_literal(rhs))
    return (not eq if op == "!=" else eq), ""


def projection_summary(brain_dir: Path, tenant: str, spec: dict[str, Any], record: dict[str, Any]) -> str:
    settings = record.get("settings") or {}
    placeholders = spec.get("placeholders") or {}
    branches = spec.get("branches") or {}
    sections = spec.get("sections") or {}
    globs = spec.get("templated_globs") or ["**/*.md"]

    lines = [
        f"# Projection preview — {tenant}",
        "",
        f"- **Brain:** `{brain_dir}`",
        f"- **Runtime values:** `rc project tenant profile get {tenant} -o json`",
        f"- **Tenant ID:** `{record.get('tenant_id') or '?'}`",
        f"- **Version:** `{record.get('version') or '?'}`",
        f"- **Applied at:** `{record.get('applied_at') or '?'}`",
        f"- **Templated globs:** {', '.join(f'`{g}`' for g in globs)}",
        "",
        "## Branch choices",
        "",
    ]

    if branches:
        for name in sorted(branches):
            branch = branches[name] or {}
            field, raw, selected, note = _selected_variant(settings, branch)
            suffix = f" ({note})" if note else ""
            lines.append(f"- `{name}` via `{field}`: raw `{raw}` -> `{selected or '(region dropped)'}`{suffix}")
    else:
        lines.append("_(no branches declared)_")

    tracked = [(k, settings.get(k)) for k in BRANCH_KEYS if k in settings]
    if tracked:
        lines += ["", "## Known DentAI selectors", ""]
        lines += [f"- `{k}` = `{_as_inline(v)}`" for k, v in tracked]

    defaults, missing_required = [], []
    for name in sorted(placeholders):
        ph = placeholders[name] or {}
        _, present = _lookup(settings, name)
        if not present:
            if ph.get("required"):
                missing_required.append(name)
            elif "default" in ph:
                defaults.append((name, ph.get("default")))

    lines += ["", "## Placeholder defaults", ""]
    if defaults:
        lines.append(f"{len(defaults)} default(s) would be used:")
        lines += [f"- `{name}` = `{_as_inline(default)}`" for name, default in defaults]
    else:
        lines.append("_(none)_")
    if missing_required:
        lines += ["", "## Missing required placeholders", ""]
        lines += [f"- `{name}`" for name in missing_required]

    lines += ["", "## Section gates", ""]
    if sections:
        for rel in sorted(sections):
            expr = str((sections[rel] or {}).get("keep_when") or "")
            keep, err = _eval_keep_when(expr, settings)
            status = "ERROR" if keep is None else ("keep" if keep else "drop")
            detail = f" ({err})" if err else ""
            lines.append(f"- `{rel}`: {status} via `{expr}`{detail}")
    else:
        lines.append("_(no section gates declared)_")

    lines += [
        "",
        "## Notes",
        "",
        "- This is a preview/audit summary, not the prod compiler.",
        "- Prod mounts the compiled view ephemerally at `/brain`; do not commit compiled files or values.",
    ]
    return "\n".join(lines) + "\n"


def _safe_out_dir(brain_dir: Path, tenant: str) -> Path:
    root = (brain_dir / OUT_ROOT).resolve()
    out = (root / tenant).resolve()
    if not (out == root or out.is_relative_to(root)):
        raise ValueError(f"tenant {tenant!r} escapes {root}")
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="brain_projection.py", description=__doc__.split("\n")[0])
    p.add_argument("--brain", help="project brain dir (default: cwd)")
    p.add_argument("--tenant", required=True, help="tenant slug passed to `rc project tenant profile get`")
    p.add_argument("--write-summary", action="store_true",
                   help="write summary.md + settings.json under .rootcause/projection/<tenant>/")
    args = p.parse_args(sys.argv[1:] if argv is None else argv)

    brain_dir = E.resolve_brain_dir(args.brain)
    try:
        spec = _load_spec(brain_dir)
        record = _fetch_settings(brain_dir, args.tenant)
        summary = projection_summary(brain_dir, args.tenant, spec, record)
    except Exception as exc:  # noqa: BLE001 — CLI surface
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(summary, end="")
    if args.write_summary:
        try:
            out = _safe_out_dir(brain_dir, args.tenant)
            out.mkdir(parents=True, exist_ok=True)
            (out / "summary.md").write_text(summary, encoding="utf-8")
            (out / "settings.json").write_text(json.dumps(record, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
                                                encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            print(f"error: could not write .rootcause projection summary: {exc}", file=sys.stderr)
            return 1
        print(f"\nwrote {out / 'summary.md'}")
        print(f"wrote {out / 'settings.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
