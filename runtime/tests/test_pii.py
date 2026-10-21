"""`lib.pii`: token shape matches the proxy; fake rows / fake db / fetch functions mask like prod."""

from __future__ import annotations

import datetime as dt
import textwrap
from unittest import mock

import pytest

from lib import pii

pytest_plugins = ["pytester"]


def test_token_shape_and_is_token():
    t = pii.token("participant_birthdate", dt.date(2021, 5, 3))
    assert pii.TOKEN_RE.fullmatch(t)
    assert t.startswith("⟦pii:participant_birthdate:")
    assert pii.is_token(t)
    assert pii.is_token("⟦pii:person_42:email:0123456789⟧")  # host-rendered subject prefix
    assert not pii.is_token("2021-05-03")
    assert not pii.is_token(f"x {t}")  # a token inside text is not a masked column
    assert pii.token("email", " A@b.be ") == pii.token("email", "a@b.be")  # coreference, like prod


def test_mask_row_declared_columns_and_aliases_only():
    row = {"id": 1, "birthdate": "2021-05-03", "participant_birthdate": dt.date(2016, 4, 1),
           "activity_name": "Kamp", "emails": None, "email": ["a@b.be", None]}
    out = pii.mask_row(row, ("birthdate", "email"))
    assert out["id"] == 1 and out["activity_name"] == "Kamp" and out["emails"] is None
    assert pii.is_token(out["birthdate"]) and pii.is_token(out["participant_birthdate"])
    assert pii.is_token(out["email"][0]) and out["email"][1] is None
    assert row["birthdate"] == "2021-05-03"  # input untouched


def test_mask_wraps_fake_db_and_fetch_functions():
    class FakeDB:
        calls: list = []

        def query_one(self, sql, params=None, *, db=None):
            self.calls.append(sql)
            return {"birthdate": "2021-05-03"}

        def query(self, sql, params=None, *, db=None):
            return [{"birthdate": "2021-05-03"}, {"birthdate": None}]

    fake = FakeDB()
    masked = pii.mask(fake)
    assert pii.is_token(masked.query_one("SELECT")["birthdate"])
    assert [pii.is_token(r["birthdate"]) for r in masked.query("SELECT")] == [True, False]
    assert masked.calls == ["SELECT"]  # non-query attributes pass through
    assert pii.is_token(pii.mask(lambda: [{"birthdate": "2021-05-03"}])()[0]["birthdate"])


def test_pii_rows_fixture_runs_opted_in_tests_plain_and_masked(pytester: pytest.Pytester):
    pytester.makeconftest('pytest_plugins = ["lib.pii_pytest"]')
    pytester.makepyfile(textwrap.dedent(
        """
        import datetime as dt
        import pytest

        def age_year(row):  # the 10-21 bug: parses the masked column in Python
            return dt.date.fromisoformat(row["birthdate"]).year

        def test_buggy(pii_rows):
            assert age_year(pii_rows({"birthdate": "2021-05-03"})) == 2021

        def test_sql_derived(pii_rows):
            assert pii_rows({"birthdate": "2021-05-03", "birth_year": 2021})["birth_year"] == 2021

        class TestProjectOverride:  # a brain conftest overrides pii_columns the same way
            @pytest.fixture
            def pii_columns(self):
                return {"firstname"}

            def test_override(self, pii_rows):
                row = pii_rows({"birthdate": "2021-05-03", "firstname": "Noor"})
                assert row["birthdate"] == "2021-05-03"
                assert (row["firstname"] != "Noor") == pii_rows.masked
                assert pii_rows({"firstname": "Noor"}, columns=set()) == {"firstname": "Noor"}

        def test_not_opted_in():
            assert True
        """
    ))
    result = pytester.runpytest("-v")
    result.assert_outcomes(passed=6, failed=1)
    result.stdout.fnmatch_lines(["*test_buggy[[]pii-plain[]] PASSED*", "*test_buggy[[]pii-masked[]] FAILED*"])


def test_alias_reads_the_source_column_token_like_prod():
    # `SELECT birthdate, birthdate AS participant_birthdate`: the proxy labels by source column.
    out = pii.mask_row({"birthdate": "2021-05-03", "participant_birthdate": "2021-05-03"}, ("birthdate",))
    assert out["birthdate"] == out["participant_birthdate"]


def test_mask_dispatch_keeps_the_fakes_interface():
    class CallableFake:
        def __call__(self):
            return [{"birthdate": "2021-05-03"}]

        def query_one(self, sql):
            return {"birthdate": "2021-05-03"}

    masked = pii.mask(CallableFake())
    assert pii.is_token(masked.query_one("x")["birthdate"]) and pii.is_token(masked()[0]["birthdate"])
    fetch = pii.mask(mock.Mock(return_value=[{"birthdate": "2021-05-03"}]))  # Mock synthesizes .query
    assert pii.is_token(fetch()[0]["birthdate"])
