"""``pii_rows``: run an opted-in helper test twice, once with plain fake rows, once masked as prod masks them.

A test opts in by requesting the fixture and passing its fake rows / fake ``db`` / patched fetch
function through it::

    def test_children_age(monkeypatch, pii_rows):
        monkeypatch.setattr(mod, "db", pii_rows(FakeDB(rows=[{"birthdate": "2021-05-03", ...}])))
        assert mod.render(...) ...          # must hold in both [pii-plain] and [pii-masked]

Under ``[pii-masked]`` every declared-PII key (``pii_columns``) holds a ``⟦pii:…⟧`` token, so a
helper that parses ``birthdate`` in Python fails there. ``pii_rows.masked`` tells the two runs apart
when an assertion on a rendered name legitimately differs. Opt-in only: tests that do not request
``pii_rows`` run once, unchanged. Override ``pii_columns`` in the brain's conftest with the project's
declared columns (``rp project database controls get <DSN>``). Registered by ``brain_test.py`` via
``-p lib.pii_pytest``.
"""

from __future__ import annotations

import pytest

from lib import pii


@pytest.fixture
def pii_columns() -> frozenset[str]:
    return pii.DEFAULT_COLUMNS


class _Rows:
    def __init__(self, masked: bool, columns):
        self.masked, self.columns = masked, frozenset(columns)

    def __call__(self, obj, columns=None):
        return pii.mask(obj, self.columns if columns is None else columns) if self.masked else obj


@pytest.fixture(params=[False, True], ids=["pii-plain", "pii-masked"])
def pii_rows(request, pii_columns) -> _Rows:
    return _Rows(request.param, pii_columns)
