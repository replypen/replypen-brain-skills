"""Legacy pytest shim for undeclared brains missing the production ``/mirrors`` mount.

Some brain scripts hardcode the prod mount (``sys.path.insert(0, "/mirrors/<repo>/…")`` then
``from ka import …``). On a laptop that path does not exist, so importing them at collection raises
``ModuleNotFoundError`` and the whole ``brain_test.py`` run errors out before a single test runs.
Brains with committed ``.replypen.toml [mirrors]`` declarations fail earlier in import smoke with
``MirrorMissing``; they must not rely on this skip. For undeclared legacy brains, convert such
collection errors into a skip that names the missing mount. Only fires when ``/mirrors`` is
absent and the failing module's skill tree references ``/mirrors/`` literally; other import errors
stay errors. Registered by ``scripts/brain_test.py`` via ``-p lib.mirror_skip``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

PROD_MIRRORS = "/mirrors"


def _skill_root(path: Path) -> Path | None:
    for parent in [path, *path.parents]:
        if parent.parent.name == "skills":
            return parent
    return None


def _references_mirrors(path: Path) -> bool:
    root = _skill_root(path) or path.parent
    for py in root.rglob("*.py"):
        try:
            if f"{PROD_MIRRORS}/" in py.read_text(encoding="utf-8", errors="replace"):
                return True
        except OSError:
            continue
    return False


def mirror_skip_reason(collector_path: Path, excinfo: pytest.ExceptionInfo) -> str | None:
    """Reason string when this collection failure is explained by the missing prod mirror mount."""
    # pytest wraps a test-module import failure in Collector.CollectError (`from e`).
    exc: BaseException | None = excinfo.value
    while exc is not None and not isinstance(exc, ModuleNotFoundError):
        exc = exc.__cause__ or exc.__context__
    if exc is None:
        return None
    if os.path.isdir(PROD_MIRRORS):
        return None
    if not _references_mirrors(collector_path):
        return None
    return (
        f"needs the prod source-mirror mount {PROD_MIRRORS}/ (absent on this machine): "
        f"{exc} — run `brain_test.py --mode docker --mirrors-root …` or rely on prod validation"
    )


@pytest.hookimpl(tryfirst=True)
def pytest_make_collect_report(collector: pytest.Collector) -> pytest.CollectReport | None:
    """Collect ourselves so we can see the exception; on a mirror-explained ModuleNotFoundError
    return a skipped report, on success a normal passed one, otherwise defer to pytest's default
    (which re-collects — only the already-failing path pays that)."""
    path = getattr(collector, "path", None)
    if path is None or not isinstance(collector, pytest.Module):
        return None
    call = pytest.CallInfo.from_call(lambda: list(collector.collect()), "collect")
    if call.excinfo is None:
        return pytest.CollectReport(collector.nodeid, "passed", None, call.result)
    reason = mirror_skip_reason(Path(str(path)), call.excinfo)
    if reason is None:
        return None
    return pytest.CollectReport(collector.nodeid, "skipped", (str(path), 1, f"Skipped: {reason}"), [])
