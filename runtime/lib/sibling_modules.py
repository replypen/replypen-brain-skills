"""Each test module imports its OWN bare-named siblings (`preflight`, `script`, `policy`, …).

Action tests do ``sys.path.insert(0, here); import preflight``. Collected one dir at a time that
works; collected together (``brain_test.py`` runs ``actions/`` in one session) the second
``import preflight`` returns the first action's cached module and the test exercises the wrong code
(pro-backup 2026-10-21: 28 false failures). Two seams, both touching only names that clash with a
``*.py`` next to the test file (or next to its ``tests/`` parent) and were loaded from another file
under the rootdir (never stdlib/site-packages, never shared helpers without a clash):

* before a test module is imported, the foreign cached modules are evicted;
* before each of its tests runs, the objects its module-level import bound go back into
  ``sys.modules`` and its own dirs move to the front of ``sys.path``, so an ``import preflight``
  inside a test or fixture gets the right module too.

Registered by ``brain_test.py`` via ``-p lib.sibling_modules``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType

import pytest

_OWN = pytest.StashKey[dict[Path, dict[str, ModuleType]]]()


def _homes(test_file: Path) -> list[Path]:
    """The test's own dirs, nearest first: its dir, plus the action dir above a `tests/` dir."""
    here = test_file.resolve().parent
    return [here, here.parent] if here.name == "tests" else [here]


def _siblings(test_file: Path) -> dict[str, Path]:
    # reversed: the nearest dir's file wins a stem clash between `tests/` and its parent
    return {py.stem: py.resolve() for home in _homes(test_file)[::-1] for py in home.glob("*.py")
            if py.stem != "conftest" and not py.stem.startswith(("test_", "__"))}


def _origin(mod: ModuleType | None) -> Path | None:
    origin = getattr(mod, "__file__", None)
    return Path(origin).resolve() if origin else None


def evict_foreign_siblings(test_file: Path, root: Path) -> list[str]:
    """Drop cached top-level modules named like a sibling of `test_file` but loaded from another file
    under `root` (the brain)."""
    root, evicted = root.resolve(), []
    for stem, own in _siblings(test_file).items():
        origin = _origin(sys.modules.get(stem))
        if origin and origin != own and origin.is_relative_to(root):
            del sys.modules[stem]
            evicted.append(stem)
    return evicted


def own_siblings(test_file: Path) -> dict[str, ModuleType]:
    """The sibling modules currently cached that were loaded from `test_file`'s own dirs."""
    return {stem: sys.modules[stem] for stem, own in _siblings(test_file).items()
            if _origin(sys.modules.get(stem)) == own}


def restore_siblings(test_file: Path, root: Path, own: dict[str, ModuleType]) -> None:
    """Make `test_file`'s siblings win again: evict foreign ones, put back the objects its module-level
    import bound, and move its own dirs to the front of sys.path if they are on it, so a deferred
    `import preflight` resolves to its own file instead of whichever action dir was inserted last."""
    evict_foreign_siblings(test_file, root)
    sys.modules.update(own)
    for home in _homes(test_file)[::-1]:
        hits = [p for p in sys.path if p and Path(p).resolve() == home]
        if hits:
            sys.path[:] = [hits[0], *(p for p in sys.path if p not in hits)]


@pytest.hookimpl(wrapper=True)
def pytest_make_collect_report(collector: pytest.Collector):
    # Wraps the module's collect (= its import): evict before it, record what it bound after it.
    if not isinstance(collector, pytest.Module):
        return (yield)
    path = Path(str(collector.path))
    evict_foreign_siblings(path, collector.config.rootpath)
    report = yield
    collector.config.stash.setdefault(_OWN, {})[path] = own_siblings(path)
    return report


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item: pytest.Item) -> None:
    path = Path(str(item.path))
    own = item.config.stash.setdefault(_OWN, {}).setdefault(path, {})
    restore_siblings(path, item.config.rootpath, own)
