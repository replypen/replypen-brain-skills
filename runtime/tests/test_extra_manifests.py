"""Host-provided manifests (RC_EXTRA_MANIFESTS): custom REST connectors read through the broker."""

import json
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
import responses

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib import api  # noqa: E402

EF = {
    "key": "custom_eenvoudig_factureren",
    "name": "Eenvoudig Factureren",
    "brokered": True,
    "auth": {"strategy": "api_key_header", "name": "X-API-Key"},
    "pagination": {"style": "offset", "offset_param": "skip", "limit_param": "take", "page_size": 2, "items_field": ""},
    "default_headers": {},
}
BROKER = "http://rc-broker.internal/custom_eenvoudig_factureren"


@pytest.fixture(autouse=True)
def clean_registry(monkeypatch):
    api.MANIFESTS.clear()
    api._YAML_LOADED_KEYS.clear()
    api._EXTRA_LOADED_KEYS.clear()
    api._EXTRA_WARNED.clear()
    monkeypatch.delenv("RC_EXTRA_MANIFESTS", raising=False)
    yield
    api.MANIFESTS.clear()
    api._YAML_LOADED_KEYS.clear()
    api._EXTRA_LOADED_KEYS.clear()


def _set(monkeypatch, value):
    monkeypatch.setenv("RC_EXTRA_MANIFESTS", value if isinstance(value, str) else json.dumps(value))


def test_extra_loads_forced_brokered_with_empty_base_url(monkeypatch):
    entry = dict(EF, brokered=False, base_url="https://eenvoudigfactureren.be/api/v1")
    _set(monkeypatch, [entry])
    m = api.load_manifests()["custom_eenvoudig_factureren"]
    assert m.brokered is True
    assert m.base_url == ""
    assert m.auth.name == "X-API-Key"
    assert (m.pagination.style, m.pagination.offset_param, m.pagination.limit_param) == ("offset", "skip", "take")
    assert "stripe" in api.MANIFESTS  # baked catalog still there


def test_writes_and_session_fields_dropped(monkeypatch):
    entry = dict(
        EF,
        write_endpoints={"post": ["/invoices"], "consent": "x"},
        write_paths=["/invoices/**"],
        session_credential="tok",
        read_endpoints={"post": ["/search"]},
    )
    _set(monkeypatch, [entry])
    m = api.load_manifests()["custom_eenvoudig_factureren"]
    assert (m.write_endpoints, m.write_paths, m.session_credential, m.allowed_post_paths) == ({}, (), "", ())
    with pytest.raises(api.MethodPolicyError):
        api.Client(manifest=m)._assert_read_tier("POST", "/invoices")


def test_baked_key_wins(monkeypatch, capsys):
    api.load_manifests()
    baked = api.MANIFESTS["stripe"]
    _set(monkeypatch, [dict(EF, key="stripe"), dict(EF, key="custom_stripe_like")])
    api._EXTRA_WARNED.clear()
    api.load_manifests()
    assert api.MANIFESTS["stripe"] is baked  # 'stripe' is not custom_*: rejected before the clash
    assert "custom_stripe_like" in api.MANIFESTS
    assert "custom_<slug>" in capsys.readouterr().err


def test_registered_custom_key_wins_over_extra(monkeypatch, capsys):
    own = api.register(api.Manifest(key="custom_eenvoudig_factureren", base_url="https://own.test"))
    _set(monkeypatch, [EF])
    api.load_manifests()
    assert api.MANIFESTS["custom_eenvoudig_factureren"] is own
    assert "baked connector owns" in capsys.readouterr().err


@pytest.mark.parametrize("raw", ["{not json", '{"key": "custom_x"}', "42"])
def test_malformed_env_keeps_baked(monkeypatch, capsys, raw):
    _set(monkeypatch, raw)
    m = api.load_manifests()
    assert "stripe" in m and not any(k.startswith("custom_") for k in m)
    assert "RC_EXTRA_MANIFESTS" in capsys.readouterr().err


def test_bad_entry_skipped_good_entry_kept(monkeypatch, capsys):
    _set(monkeypatch, ["nope", {"key": "custom_bad", "auth": "notamapping"}, {"key": "Custom-Bad"}, EF])
    m = api.load_manifests()
    assert "custom_eenvoudig_factureren" in m
    assert "custom_bad" not in m
    err = capsys.readouterr().err
    assert err.count("skipped") == 3


def test_warns_once_across_reloads(monkeypatch, capsys):
    _set(monkeypatch, "{bad")
    api.load_manifests()
    api.load_manifests()
    assert capsys.readouterr().err.count("not valid JSON") == 1


@responses.activate
def test_cli_get_builds_relative_broker_url(monkeypatch, capsys):
    _set(monkeypatch, [EF])
    responses.get(f"{BROKER}/invoices", json=[{"id": 1}])
    assert api._main(["get", "custom_eenvoudig_factureren", "/invoices", "--query", "format=json"]) == 0
    sent = responses.calls[0].request
    parts = urlsplit(sent.url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == f"{BROKER}/invoices"
    assert "__url" not in sent.url
    assert parse_qs(parts.query) == {"format": ["json"]}
    assert "X-API-Key" not in sent.headers  # the broker attaches the credential, never the container
    assert json.loads(capsys.readouterr().out) == [{"id": 1}]


@responses.activate
def test_offset_paging_with_skip_take(monkeypatch):
    _set(monkeypatch, [EF])
    responses.get(f"{BROKER}/invoices", json=[{"id": 1}, {"id": 2}])
    responses.get(f"{BROKER}/invoices", json=[{"id": 3}])
    api.load_manifests()
    out = api.client(api.MANIFESTS["custom_eenvoudig_factureren"]).collect("/invoices", query={"format": "json"})
    assert [it["id"] for it in out["items"]] == [1, 2, 3]
    qs = [parse_qs(urlsplit(c.request.url).query) for c in responses.calls]
    assert [(q["skip"], q["take"]) for q in qs] == [(["0"], ["2"]), (["2"], ["2"])]
