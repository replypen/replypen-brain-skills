"""Delegated writes through lib.api (manifest ``write_endpoints``) and the lib.gsheets wrappers.

No network: the wrappers' one seam (``lib._google.client``) is either a recording fake or a real
brokered ``api.Client`` over ``responses``. The contract manifest below is the shape the host's
googlesheets/googledocs manifests carry.

    cd runtime && uv run --with . --with pytest --with responses --no-project pytest tests/test_google_writes.py -q
"""

import io
import json
import re
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest import mock
from urllib.parse import unquote

import responses

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib import _google, api, gsheets  # noqa: E402

SHEET_ID = "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms"
SHEET_URL = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit#gid=0"
DENIED = {
    "error": {
        "code": "DELEGATED_WRITE_DENIED",
        "message": "This file is not shared with sa@x.iam.gserviceaccount.com as Editor. Ask the user to share it as Editor, then try again.",
    }
}

SHEETS_CONTRACT = {
    "key": "googlesheets",
    "base_url": "https://sheets.googleapis.com/v4",
    "write_endpoints": {
        "consent": "google_drive_share",
        "put": ["/spreadsheets/*/values/*"],
        "post": [
            "/spreadsheets/*/values/*:append",
            "/spreadsheets/*/values/*:clear",
            "/spreadsheets/*/values:batchUpdate",
            "/spreadsheets/*/values:batchClear",
            "/spreadsheets/*:batchUpdate",
        ],
    },
}
DOCS_CONTRACT = {
    "key": "googledocs",
    "base_url": "https://docs.googleapis.com/v1",
    "write_endpoints": {"consent": "google_drive_share", "post": ["/documents/*:batchUpdate"]},
}


def contract_manifest(raw=SHEETS_CONTRACT, *, brokered=True) -> api.Manifest:
    return replace(api._manifest_from_dict(raw), brokered=brokered)


class WriteEndpointPolicy(unittest.TestCase):
    def test_manifest_parses_write_endpoints(self):
        m = api._manifest_from_dict(SHEETS_CONTRACT)
        self.assertEqual(m.write_consent, "google_drive_share")
        self.assertEqual(m.write_endpoints["PUT"], ("/spreadsheets/*/values/*",))
        self.assertIn("/spreadsheets/*:batchUpdate", m.write_endpoints["POST"])

    def test_write_endpoints_need_consent(self):
        with self.assertRaises(ValueError):
            api._manifest_from_dict({**SHEETS_CONTRACT, "write_endpoints": {"put": ["/x/*"]}})

    def test_brokered_write_allowed_with_and_without_version_prefix(self):
        c = api.Client(manifest=contract_manifest())
        for verb, path in [
            ("PUT", f"spreadsheets/{SHEET_ID}/values/Prices!A1:B2"),
            ("PUT", f"/v4/spreadsheets/{SHEET_ID}/values/Prices!A1"),
            ("POST", f"spreadsheets/{SHEET_ID}/values/'My%20Tab':append"),
            ("POST", f"spreadsheets/{SHEET_ID}/values:batchUpdate"),
            ("POST", f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}:batchUpdate"),
        ]:
            self.assertFalse(c._assert_read_tier(verb, path), (verb, path))  # False = write, no auto-retry

    def test_unlisted_write_still_refused(self):
        c = api.Client(manifest=contract_manifest())
        for verb, path in [
            ("DELETE", f"spreadsheets/{SHEET_ID}"),
            ("PATCH", f"spreadsheets/{SHEET_ID}/values/A1"),
            ("POST", f"spreadsheets/{SHEET_ID}/values/A1:B2/extra:append"),
            ("PUT", f"spreadsheets/{SHEET_ID}:batchUpdate"),
        ]:
            with self.assertRaises(api.MethodPolicyError):
                c._assert_read_tier(verb, path)

    def test_write_refused_when_not_brokered(self):
        c = api.Client(manifest=contract_manifest(brokered=False), credential="tok")
        with self.assertRaises(api.MethodPolicyError) as cm:
            c.put(f"spreadsheets/{SHEET_ID}/values/A1", json={"values": [["x"]]})
        self.assertIn("not brokered", str(cm.exception))

    def test_broker_denial_surfaces_code_and_message_verbatim(self):
        e = api.ApiError(403, json.dumps(DENIED), url="http://rc-broker.internal/x")
        self.assertEqual(e.code, "DELEGATED_WRITE_DENIED")
        self.assertEqual(str(e), "HTTP 403 DELEGATED_WRITE_DENIED: " + DENIED["error"]["message"])

    def test_google_numeric_error_code_is_not_a_broker_code(self):
        e = api.ApiError(403, '{"error":{"code":403,"message":"denied","status":"PERMISSION_DENIED"}}')
        self.assertEqual(e.code, "")
        self.assertIn("PERMISSION_DENIED", str(e))

    @responses.activate
    def test_cli_put_through_broker_and_denial_exit(self):
        responses.add(responses.PUT, re.compile(r"http://rc-broker\.internal/googlesheets/.*"), json=DENIED, status=403)
        old = dict(api.MANIFESTS)
        try:
            api.MANIFESTS["googlesheets"] = api._manifest_from_dict(SHEETS_CONTRACT)
            env = {"RC_API_BROKERED_KEYS": "googlesheets"}
            with mock.patch.object(api, "load_manifests"), mock.patch.dict("os.environ", env), \
                    mock.patch.object(api.oauth, "token") as token:
                err = io.StringIO()
                with redirect_stderr(err), self.assertRaises(SystemExit) as cm:
                    api._main(["put", "googlesheets", f"spreadsheets/{SHEET_ID}/values/A1", "--json", '{"values":[["x"]]}'])
            self.assertEqual(cm.exception.code, 1)
            self.assertIn("DELEGATED_WRITE_DENIED", err.getvalue())
            self.assertIn("as Editor", err.getvalue())
            token.assert_not_called()
            self.assertEqual(len(responses.calls), 1)
        finally:
            api.MANIFESTS.clear()
            api.MANIFESTS.update(old)

    def test_cli_put_refused_without_broker(self):
        old = dict(api.MANIFESTS)
        try:
            api.MANIFESTS["googlesheets"] = api._manifest_from_dict(SHEETS_CONTRACT)
            with mock.patch.object(api, "load_manifests"), mock.patch.dict("os.environ", {}, clear=True):
                with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
                    api._main(["put", "googlesheets", f"spreadsheets/{SHEET_ID}/values/A1", "--json", "{}"])
            self.assertEqual(cm.exception.code, 2)
        finally:
            api.MANIFESTS.clear()
            api.MANIFESTS.update(old)


class FakeClient:
    """Records calls; answers from a list of ``(verb, path_regex, body)`` in order of declaration."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def _call(self, verb, path, query=None, json=None):
        self.calls.append({"verb": verb, "path": path, "query": query, "json": json})
        for i, (v, rx, body) in enumerate(self.answers):
            if v == verb and re.search(rx, path):
                self.answers.pop(i)
                if isinstance(body, Exception):
                    raise body
                return body
        raise AssertionError(f"unexpected {verb} {path}")

    def get(self, path, *, query=None):
        return self._call("GET", path, query)

    def post(self, path, *, query=None, json=None):
        return self._call("POST", path, query, json)

    def put(self, path, *, query=None, json=None):
        return self._call("PUT", path, query, json)


def fake(*answers):
    f = FakeClient(answers)
    return f, mock.patch.object(_google, "client", lambda key: f)


GRID = [["Email", "Plan", "Seats"], ["a@x.be", "pro", "5"], ["B@X.be ", "starter", "1"], ["c@x.be", "pro"]]


class Sheets(unittest.TestCase):
    def test_file_id_accepts_url_or_id_and_refuses_doc_links(self):
        self.assertEqual(_google.file_id(SHEET_URL, "googlesheets"), SHEET_ID)
        self.assertEqual(_google.file_id(SHEET_ID, "googlesheets"), SHEET_ID)
        with self.assertRaises(ValueError):
            _google.file_id(f"https://docs.google.com/document/d/{SHEET_ID}/edit", "googlesheets")

    def test_values_formulas_flag(self):
        f, patch = fake(("GET", "values/", {"values": [["=SUM(A1:A2)"]]}))
        with patch:
            self.assertEqual(gsheets.values(SHEET_URL, "Tab 1!A1", formulas=True), [["=SUM(A1:A2)"]])
        self.assertEqual(f.calls[0]["path"], f"spreadsheets/{SHEET_ID}/values/Tab%201!A1")
        self.assertEqual(f.calls[0]["query"], {"valueRenderOption": "FORMULA"})

    def test_find_rows_by_header_and_letter(self):
        f, patch = fake(("GET", "values/", {"values": GRID}), ("GET", "values/", {"values": GRID}))
        with patch:
            got = gsheets.find_rows(SHEET_ID, "Customers", "email", "b@x.be")
            by_letter = gsheets.find_rows(SHEET_ID, "Customers", "B", "PRO")
        self.assertEqual(f.calls[0]["path"], f"spreadsheets/{SHEET_ID}/values/'Customers'")
        self.assertEqual([m["row"] for m in got["matches"]], [3])
        self.assertEqual(got["matches"][0]["record"], {"Email": "B@X.be ", "Plan": "starter", "Seats": "1"})
        self.assertEqual([m["row"] for m in by_letter["matches"]], [2, 4])
        self.assertEqual(by_letter["matches"][1]["record"]["Seats"], "")

    def test_find_rows_unknown_column(self):
        for column in ("Phone number", "SKU"):  # "SKU" also parses as a column letter past the grid
            _, patch = fake(("GET", "values/", {"values": GRID}))
            with self.subTest(column), patch, self.assertRaises(LookupError):
                gsheets.find_rows(SHEET_ID, "Customers", column, "x")

    def test_update_is_raw_by_default_and_returns_before_after(self):
        f, patch = fake(
            ("GET", "values/", {"values": [["old"]]}),
            ("PUT", "values/", {"updatedRange": "Tab!B2", "updatedData": {"values": [["=1+1"]]}}),
        )
        with patch:
            got = gsheets.update(SHEET_ID, "Tab!B2", [["=1+1"]])
        self.assertEqual(got, {"range": "Tab!B2", "before": [["old"]], "after": [["=1+1"]]})
        put = f.calls[1]
        self.assertEqual(put["query"]["valueInputOption"], "RAW")
        self.assertEqual(put["json"], {"majorDimension": "ROWS", "values": [["=1+1"]]})

    def test_update_user_entered_is_explicit(self):
        f, patch = fake(("GET", "values/", {}), ("PUT", "values/", {}))
        with patch:
            gsheets.update(SHEET_ID, "Tab!B2", [["=1+1"]], input_option="user_entered")
        self.assertEqual(f.calls[1]["query"]["valueInputOption"], "USER_ENTERED")
        with self.assertRaises(ValueError):
            gsheets._input_option("FORMULA")

    def test_append_raw_returns_landing_range(self):
        f, patch = fake(("POST", r":append$", {"updates": {"updatedRange": "'Log'!A7:B7", "updatedRows": 1, "updatedData": {"values": [["x", "1"]]}}}))
        with patch:
            got = gsheets.append(SHEET_ID, "Log", [["x", 1]])
        self.assertEqual(got, {"range": "'Log'!A7:B7", "rows": 1, "after": [["x", "1"]]})
        self.assertEqual(f.calls[0]["path"], f"spreadsheets/{SHEET_ID}/values/'Log':append")
        self.assertEqual(f.calls[0]["query"]["valueInputOption"], "RAW")
        self.assertEqual(f.calls[0]["query"]["insertDataOption"], "INSERT_ROWS")

    def test_replace_range_blanks_the_stale_tail_in_one_batch_update(self):
        before = [["h1", "h2", "h3"], ["a", "b", "c"], ["d", "e"], ["f"]]
        f, patch = fake(
            ("GET", "values/", {"values": before}),
            ("POST", r"values:batchUpdate$", {"responses": [{"updatedRange": "T!A1:C4", "updatedData": {"values": [["h1", "h2"], ["z", "y"]]}}]}),
        )
        with patch:
            got = gsheets.replace_range(SHEET_ID, "T!A1:C", [["h1", "h2"], ["z", "y"]])
        self.assertEqual([c["verb"] for c in f.calls], ["GET", "POST"])
        body = f.calls[1]["json"]
        self.assertEqual(body["valueInputOption"], "RAW")
        self.assertEqual(body["data"], [{
            "range": "T!A1:C",
            "majorDimension": "ROWS",
            "values": [["h1", "h2", ""], ["z", "y", ""], ["", "", ""], ["", "", ""]],
        }])
        self.assertEqual(got["before"], before)
        self.assertEqual(got["range"], "T!A1:C4")

    def test_none_cells_clear_instead_of_being_skipped(self):
        f, patch = fake(
            ("GET", "values/", {"values": [["old", "x"], ["stale", "tail"]]}),
            ("POST", r"values:batchUpdate$", {"responses": [{}]}),
            ("GET", "values/", {"values": [["old"]]}),
            ("PUT", "values/", {}),
        )
        with patch:
            gsheets.replace_range(SHEET_ID, "T!A1:B", [[None, "new"]])
            gsheets.update(SHEET_ID, "T!A1:B1", [[None, "new"]])
        self.assertEqual(f.calls[1]["json"]["data"][0]["values"], [["", "new"], ["", ""]])
        self.assertEqual(f.calls[3]["json"]["values"], [["", "new"]])

    def test_add_tab_returns_sheet_id(self):
        f, patch = fake(("POST", r":batchUpdate$", {"replies": [{"addSheet": {"properties": {"sheetId": 42, "title": "Q4", "index": 3, "gridProperties": {}}}}]}))
        with patch:
            self.assertEqual(gsheets.add_tab(SHEET_URL, "Q4"), {"sheetId": 42, "title": "Q4", "index": 3})
        self.assertEqual(f.calls[0]["path"], f"spreadsheets/{SHEET_ID}:batchUpdate")
        self.assertEqual(f.calls[0]["json"], {"requests": [{"addSheet": {"properties": {"title": "Q4"}}}]})

    def test_every_write_path_matches_the_contract(self):
        f, patch = fake(
            ("GET", "", {"values": [["a"]]}), ("PUT", "", {}),
            ("POST", ":append", {}),
            ("GET", "", {"values": [["a"]]}), ("POST", "values:batchUpdate", {}),
            ("POST", ":batchUpdate", {}),
        )
        with patch:
            gsheets.update(SHEET_ID, "My Tab/2026!A1", [["x"]])
            gsheets.append(SHEET_ID, "My Tab/2026", [["x"]])
            gsheets.replace_range(SHEET_ID, "Tab!A1:B", [["x"]])
            gsheets.add_tab(SHEET_ID, "New")
        shipped = api._parse_manifest_file(Path(api.__file__).parent / "connectors/googlesheets/manifest.yaml")
        for manifest in (contract_manifest(), replace(shipped, brokered=True)):
            c = api.Client(manifest=manifest)
            for call in f.calls:
                if call["verb"] != "GET":
                    self.assertFalse(c._assert_read_tier(call["verb"], call["path"]), call["path"])

    def test_shipped_docs_manifest_allows_batch_update(self):
        shipped = api._parse_manifest_file(Path(api.__file__).parent / "connectors/googledocs/manifest.yaml")
        c = api.Client(manifest=replace(shipped, brokered=True))
        self.assertFalse(c._assert_read_tier("POST", f"documents/{SHEET_ID}:batchUpdate"))

    @responses.activate
    def test_update_end_to_end_through_brokered_client(self):
        url = re.compile(r"http://rc-broker\.internal/googlesheets/__url/.*")
        responses.add(responses.GET, url, json={"values": [["old"]]})
        responses.add(responses.PUT, url, json={"updatedRange": "Tab!A1", "updatedData": {"values": [["new"]]}})
        client = api.Client(manifest=contract_manifest())
        with mock.patch.object(_google, "client", lambda key: client):
            got = gsheets.update(SHEET_ID, "Tab!A1", [["new"]])
        self.assertEqual(got["after"], [["new"]])
        upstream = unquote(responses.calls[1].request.url.split("/__url/", 1)[1])
        self.assertTrue(upstream.startswith(f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values/Tab!A1?"))
        self.assertIn("valueInputOption=RAW", upstream)
        self.assertEqual(json.loads(responses.calls[1].request.body), {"majorDimension": "ROWS", "values": [["new"]]})

    @responses.activate
    def test_cli_denial_prints_the_share_ask(self):
        url = re.compile(r"http://rc-broker\.internal/googlesheets/__url/.*")
        responses.add(responses.GET, url, json={"values": []})
        responses.add(responses.POST, url, json=DENIED, status=403)
        client = api.Client(manifest=contract_manifest())
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(_google, "client", lambda key: client), redirect_stdout(out), redirect_stderr(err):
            code = gsheets._main(["append", SHEET_ID, "Log", "--rows", '[["x"]]'])
        self.assertEqual(code, 1)
        self.assertEqual(err.getvalue().strip().splitlines()[-1], "error: HTTP 403 DELEGATED_WRITE_DENIED: " + DENIED["error"]["message"])

    def test_cli_values_prints_json(self):
        _, patch = fake(("GET", "", {"values": [["a", "b"]]}))
        out = io.StringIO()
        with patch, redirect_stdout(out):
            self.assertEqual(gsheets._main(["values", SHEET_ID, "A1:B1"]), 0)
        self.assertEqual(json.loads(out.getvalue()), [["a", "b"]])

    def test_cli_rejects_non_grid_rows(self):
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(gsheets._main(["append", SHEET_ID, "Log", "--rows", '["x"]']), 2)


if __name__ == "__main__":
    unittest.main()
