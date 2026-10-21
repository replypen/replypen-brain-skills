"""`lib.sibling_modules`: two action dirs with bare `import preflight` each test their own module."""

from __future__ import annotations

import textwrap

import pytest

pytest_plugins = ["pytester"]

TEST = textwrap.dedent(
    """
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent))
    import preflight

    def test_own_module():
        assert preflight.NAME == Path(__file__).parent.name
    """
)


def _actions(pytester: pytest.Pytester) -> None:
    for name in ("alpha", "beta"):
        action = pytester.path / "actions" / name
        action.mkdir(parents=True)
        (action / "preflight.py").write_text(f"NAME = {name!r}\n")
        (action / "test_action.py").write_text(TEST)


def test_without_plugin_the_second_action_tests_the_first_ones_module(pytester: pytest.Pytester) -> None:
    _actions(pytester)
    pytester.runpytest("--import-mode=importlib", "actions").assert_outcomes(passed=1, failed=1)


def test_plugin_gives_each_test_module_its_own_sibling(pytester: pytest.Pytester) -> None:
    _actions(pytester)
    result = pytester.runpytest("-p", "lib.sibling_modules", "--import-mode=importlib", "actions")
    result.assert_outcomes(passed=2)


DEFERRED = textwrap.dedent(
    """
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parents[1]))

    def test_deferred_import_gets_own_module():
        import preflight
        assert preflight.NAME == Path(__file__).parents[1].name
    """
)


def test_deferred_import_inside_a_test_gets_its_own_sibling(pytester: pytest.Pytester) -> None:
    for name in ("alpha", "beta"):
        tests = pytester.path / "actions" / name / "tests"
        tests.mkdir(parents=True)
        (tests.parent / "preflight.py").write_text(f"NAME = {name!r}\n")
        (tests / "test_preflight.py").write_text(DEFERRED)
    pytester.runpytest("--import-mode=importlib", "actions").assert_outcomes(passed=1, failed=1)
    result = pytester.runpytest("-p", "lib.sibling_modules", "--import-mode=importlib", "actions")
    result.assert_outcomes(passed=2)
