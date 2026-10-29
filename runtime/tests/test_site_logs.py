"""Unit tests for lib.site_logs — the host's site_logs mount, faked with `responses`.

    cd runtime && PYTHONPATH=$(pwd) uv run --with '.[test]' --no-project pytest tests/test_site_logs.py -q
"""

import sys
import unittest
from pathlib import Path

import requests
import responses

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import site_logs  # noqa: E402

ANSWER = {"site": "https://example.org", "window_days": 7, "events_scanned": 3, "truncated": False, "noise_filtered": 1,
          "rows": [{"path": "/old", "count": 2, "first_seen": "2026-10-22T10:00:00Z", "last_seen": "2026-10-23T10:00:00Z"}],
          "note": "untrusted"}


class SiteLogsTest(unittest.TestCase):
    @responses.activate
    def test_misses_passes_bounds_and_returns_the_answer(self):
        responses.add(responses.GET, site_logs.BROKER_URL, json=ANSWER, status=200)
        self.assertEqual(site_logs.misses(days=3, limit=10, include_noise=True), ANSWER)
        self.assertIn("days=3", responses.calls[0].request.url)
        self.assertIn("include_noise=1", responses.calls[0].request.url)

    @responses.activate
    def test_no_logs_for_this_scope_is_unavailable_with_the_host_sentence(self):
        responses.add(responses.GET, site_logs.BROKER_URL, json={"error": "no_site_logs", "message": "no website logs are connected"}, status=404)
        with self.assertRaisesRegex(site_logs.SiteLogsUnavailable, "no website logs"):
            site_logs.misses()

    @responses.activate
    def test_cap_is_an_error(self):
        responses.add(responses.GET, site_logs.BROKER_URL, json={"error": "too_many_calls", "message": "at most 10"}, status=429)
        with self.assertRaisesRegex(site_logs.SiteLogsError, "at most 10"):
            site_logs.misses()

    @responses.activate
    def test_no_mount_is_unavailable(self):
        responses.add(responses.GET, site_logs.BROKER_URL, body=requests.exceptions.ConnectionError("no broker"))
        with self.assertRaises(site_logs.SiteLogsUnavailable):
            site_logs.misses()


if __name__ == "__main__":
    unittest.main()
