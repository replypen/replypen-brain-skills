"""lib.apidocs: bounded vendor API docs reading. No network: `_fetch` is faked, `responses` covers the transport."""

import json
import sys
from pathlib import Path

import pytest
import responses

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib import apidocs  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures" / "apidocs"
EF_URL = "https://eenvoudigfactureren.be/info/help/api/"
EF_HTML = (FIX / "eenvoudigfactureren_api.html").read_bytes()
SPEC = str(FIX / "invoices_openapi.yaml")


@pytest.fixture(autouse=True)
def tmp_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(apidocs, "CACHE_DIR", tmp_path / "apidocs")


def fake_site(monkeypatch, pages: dict[str, tuple[bytes, str]]):
    calls = []

    def fetch(url):
        calls.append(url)
        if url not in pages:
            raise apidocs.DocsError(f"{url} answered HTTP 404")
        return pages[url]

    monkeypatch.setattr(apidocs, "_fetch", fetch)
    return calls


def test_toc_eenvoudig_factureren_html(monkeypatch):
    calls = fake_site(monkeypatch, {EF_URL: (EF_HTML, "text/html; charset=utf-8")})
    out = apidocs.toc(EF_URL)
    assert "HTML converted" in out
    for heading in ("Authenticatie", "API-sleutel", "Paginering", "Opbouw URL"):
        assert heading in out
    assert calls == ["https://eenvoudigfactureren.be/info/help/api.md", "https://eenvoudigfactureren.be/llms.txt", EF_URL]
    # cached: a second read costs no fetch
    apidocs.section(EF_URL, "paginering")
    assert len(calls) == 3


def test_section_holds_the_integration_facts(monkeypatch):
    fake_site(monkeypatch, {EF_URL: (EF_HTML, "text/html")})
    auth = apidocs.section(EF_URL, "api-sleutel")
    assert "X-API-Key" in auth
    paging = apidocs.section(EF_URL, "Paginering")
    assert "skip" in paging and "take" in paging
    url = apidocs.section(EF_URL, "opbouw url")
    assert "https://eenvoudigfactureren.be/api/v1" in url
    fmt = apidocs.section(EF_URL, "het formaat")
    assert "format=json" in fmt
    assert "<script" not in apidocs.section(EF_URL, "1:5000")


def test_section_line_range_and_unknown_heading(monkeypatch):
    fake_site(monkeypatch, {EF_URL: (EF_HTML, "text/html")})
    out = apidocs.section(EF_URL, "1:3")
    assert "lines 1-3" in out
    with pytest.raises(apidocs.DocsError, match="no heading contains"):
        apidocs.section(EF_URL, "definitely not a heading")


def test_markdown_twin_preferred(monkeypatch):
    twin = b"# Demo API\n\n## Auth\nUse X-Key.\n"
    fake_site(monkeypatch, {"https://docs.demo.test/api.md": (twin, "text/markdown"), "https://docs.demo.test/api": (b"<html></html>", "text/html")})
    out = apidocs.toc("https://docs.demo.test/api")
    assert "markdown twin https://docs.demo.test/api.md" in out
    assert "L3-4" in out and "Auth" in out


def test_twin_that_is_html_is_ignored_and_llms_entry_used(monkeypatch):
    llms = b"# Demo\n\n- [API](https://docs.demo.test/api.md): reference\n"
    calls = fake_site(monkeypatch, {
        "https://docs.demo.test/api.md": (b"<!doctype html><html>spa</html>", "text/plain"),
        "https://docs.demo.test/llms.txt": (llms, "text/plain"),
    })
    # twin is SPA HTML -> rejected; llms.txt points at the same twin -> also rejected; page fetch 404s
    with pytest.raises(apidocs.DocsError):
        apidocs.toc("https://docs.demo.test/api")
    assert calls[-1] == "https://docs.demo.test/api"


def test_llms_entry_for_page(monkeypatch):
    llms = b"# Demo\n\n- [API](https://docs.demo.test/reference/api/index.html.md)\n"
    fake_site(monkeypatch, {
        "https://docs.demo.test/llms.txt": (llms, "text/plain"),
        "https://docs.demo.test/reference/api/index.html.md": (b"# Ref\n## Paging\nskip/take\n", "text/markdown"),
    })
    out = apidocs.section("https://docs.demo.test/reference/api/", "paging")
    assert "llms.txt entry" in out and "skip/take" in out


def test_https_only():
    with pytest.raises(apidocs.DocsError, match="https"):
        apidocs.toc("http://docs.demo.test/api")


@responses.activate
def test_download_cap(monkeypatch):
    monkeypatch.setattr(apidocs, "MAX_DOWNLOAD_BYTES", 1000)
    responses.get("https://big.demo.test/spec.json", body=b"x" * 5000)
    with pytest.raises(apidocs.DocsError, match="larger than"):
        apidocs._fetch("https://big.demo.test/spec.json")


def test_output_capped_with_note(monkeypatch):
    md = "\n".join(f"line {i}" for i in range(1, 2001)).encode()
    fake_site(monkeypatch, {"https://docs.demo.test/big.md": (md, "text/markdown")})
    out = apidocs.section("https://docs.demo.test/big", "1:2000")
    assert "[truncated: 1600 more lines; read on with section <url> 401:800]" in out


def test_openapi_list():
    out = apidocs.openapi_list(SPEC)
    assert "servers: https://api.demo.test/v1" in out
    assert "GET     /invoices  List invoices" in out
    assert "POST    /invoices  createInvoice" in out
    assert "GET     /invoices/{id}  Fetch one invoice." in out


def test_openapi_show_resolves_refs_and_stops_cycles():
    out = apidocs.openapi_show(SPEC, "get", "/invoices/{id}/")
    body = json.loads(out.split("\n", 1)[1])
    schema = body["responses"]["200"]["content"]["application/json"]["schema"]
    assert schema["properties"]["number"] == {"type": "string"}
    parent = schema["properties"]["customer"]["properties"]["parent"]
    assert parent["_note"].startswith("cycle")
    assert body["security_schemes"]["apiKey"]["name"] == "X-API-Key"


def test_openapi_show_path_level_params_and_json(tmp_path):
    import yaml

    spec = tmp_path / "s.json"
    spec.write_text(json.dumps(yaml.safe_load(Path(SPEC).read_text())))
    body = json.loads(apidocs.openapi_show(str(spec), "GET", "/invoices").split("\n", 1)[1])
    assert body["parameters"][0]["name"] == "skip"
    with pytest.raises(apidocs.DocsError, match="no operation"):
        apidocs.openapi_show(str(spec), "DELETE", "/invoices")


def test_cli_exit_codes(monkeypatch, capsys):
    assert apidocs._main(["openapi", SPEC, "list"]) == 0
    assert apidocs._main(["toc", "http://insecure.test/"]) == 1
    assert "https" in capsys.readouterr().err
