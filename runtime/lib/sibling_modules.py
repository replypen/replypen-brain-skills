"""Each test module imports its OWN bare-named siblings (`preflight`, `script`, `policy`, …).

Action tests do ``sys.path.insert(0, here); import preflight``. Collected one dir at a time that
works; collected together (``brain_test.py`` runs ``actions/`` in one session) the second
``import preflight`` returns the first action's cached module and the test exercises the wrong code
(pro-backup 2026-10-21: 28 false failures). Before each test module is imported, evict only the
cached bare modules that collide with a ``*.py`` next to it (or next to its ``tests/`` parent) but
come from another file. Modules without a name clash are untouched, so shared helpers keep their
identity across conftest and tests. Registered by ``brain_test.py`` via ``-p lib.sibling_modules``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


def evict_foreign_siblings(test_file: Path, root: Path) -> list[str]:
    """Drop cached top-level modules named like a sibling of `test_file` but loaded from another file
    under `root` (the brain); stdlib/site-packages modules are never touched."""
    root, here = root.resolve(), test_file.resolve().parent
    homes = [here, here.parent] if here.name == "tests" else [here]
    evicted = []
    for home in homes:
        for py in home.glob("*.py"):
            mod = sys.modules.get(py.stem)
            origin = getattr(mod, "__file__", None)
            if (mod is not None and origin and Path(origin).resolve() != py.resolve()
                    and Path(origin).resolve().is_relative_to(root)):
                del sys.modules[py.stem]
                evicted.append(py.stem)
    return evicted


@pytest.hookimpl(wrapper=True)
def pytest_make_collect_report(collector: pytest.Collector):
    # Wraps the module's collect (= its import), so eviction lands right before that import.
    if isinstance(collector, pytest.Module):
        evict_foreign_siblings(Path(str(collector.path)), collector.config.rootpath)
    return (yield)
