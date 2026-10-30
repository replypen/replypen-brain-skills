"""Unit tests for lib.transcribe — sidecar → broker → transcript file. The broker is faked with the
`responses` library, like tests/test_image.py.

    cd runtime && PYTHONPATH=$(pwd) uv run --with '.[test]' --no-project pytest tests/test_transcribe.py -q
"""

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import requests
import responses

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make `lib` importable

from lib import transcribe  # noqa: E402

ATTACHMENT_ID = "4b9c0f1e-9a52-4c4e-8f3e-1d2a3b4c5d6e"


class TranscribeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        # The media itself is absent (a large recording is never staged) — only the sidecar exists.
        self.media = self.tmp / "2-schermopname.webm"
        Path(str(self.media) + ".attachment.json").write_text(
            json.dumps({"attachment_id": ATTACHMENT_ID, "media_mode": "screen", "media_staged": False}),
            encoding="utf-8",
        )

    def _broker(self, http_status=200, **payload):
        responses.add(responses.POST, transcribe.BROKER_URL, json=payload, status=http_status)

    @responses.activate
    def test_posts_extras_and_writes_transcript(self):
        self._broker(status="done", transcript="[00:01] SCREEN: opened \"Kamp 17\"", mode="screen",
                     duration_seconds=754.2, cached=False)

        res = transcribe.transcribe(self.media, instructions="  10:02 opened kamp 17 ", keywords=["Kampweek", " "])

        sent = json.loads(responses.calls[0].request.body)
        self.assertEqual(sent["attachment_id"], ATTACHMENT_ID)
        self.assertEqual(sent["instructions"], "10:02 opened kamp 17")
        self.assertEqual(sent["keywords"], ["Kampweek"])
        self.assertEqual(res.path, str(self.media) + ".transcript.md")
        self.assertEqual(Path(res.path).read_text(encoding="utf-8"), "[00:01] SCREEN: opened \"Kamp 17\"\n")
        self.assertEqual((res.mode, res.duration_seconds, res.cached), ("screen", 754.2, False))
        self.assertIn("duration=12:34", transcribe.render(res))

    @responses.activate
    def test_accepts_sidecar_or_transcript_path_and_overwrites(self):
        Path(str(self.media) + ".transcript.md").write_text("old\n", encoding="utf-8")
        self._broker(status="done", transcript="new", mode="meeting", duration_seconds=None, cached=True)

        res = transcribe.transcribe(str(self.media) + ".transcript.md")

        self.assertTrue(res.cached)
        self.assertEqual(Path(res.path).read_text(encoding="utf-8"), "new\n")

    def test_missing_sidecar_is_a_clear_error(self):
        with self.assertRaisesRegex(transcribe.TranscribeError, "no recording sidecar"):
            transcribe.transcribe(self.tmp / "1-other.webm")

    @responses.activate
    def test_foreign_attachment_404_surfaces_host_sentence(self):
        self._broker(http_status=404, message="no such attachment in this conversation")
        with self.assertRaisesRegex(transcribe.TranscribeError, "no such attachment"):
            transcribe.transcribe(self.media)

    @responses.activate
    def test_running_exits_3_and_writes_nothing(self):
        self._broker(http_status=202, message="still transcribing; run the same command again")
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            code = transcribe.main([str(self.media)])
        self.assertEqual(code, 3)
        self.assertIn("still transcribing", err.getvalue())
        self.assertFalse(Path(str(self.media) + ".transcript.md").exists())

    @responses.activate
    def test_timeout_is_pending_not_failure(self):
        responses.add(responses.POST, transcribe.BROKER_URL, body=requests.exceptions.ReadTimeout())
        with self.assertRaises(transcribe.TranscribePending):
            transcribe.transcribe(self.media)

    def test_no_broker_mount_is_unavailable(self):
        with responses.RequestsMock() as mock:
            mock.add(responses.POST, transcribe.BROKER_URL, body=requests.exceptions.ConnectionError())
            with self.assertRaises(transcribe.TranscribeUnavailable):
                transcribe.transcribe(self.media)

    @responses.activate
    def test_cli_prints_head_for_long_transcript(self):
        long = "\n".join(f"[{i // 60:02d}:{i % 60:02d}] Speaker 1: regel {i}" for i in range(2000))
        self._broker(status="done", transcript=long, mode="meeting", duration_seconds=2000, cached=False)
        instructions = self.tmp / "timeline.txt"
        instructions.write_text("10:02 opened kamp 17", encoding="utf-8")
        out = io.StringIO()
        with redirect_stdout(out):
            code = transcribe.main([str(self.media), "--instructions-file", str(instructions), "--keywords", "a,b"])
        self.assertEqual(code, 0)
        self.assertIn("more lines", out.getvalue())
        self.assertNotIn("regel 1999", out.getvalue())
        sent = json.loads(responses.calls[0].request.body)
        self.assertEqual((sent["instructions"], sent["keywords"]), ("10:02 opened kamp 17", ["a", "b"]))


    @responses.activate
    def test_frame_read_posts_moments_and_leaves_the_transcript_alone(self):
        transcript = Path(str(self.media) + ".transcript.md")
        transcript.write_text("[03:12] SCREEN: order id UNREADABLE\n", encoding="utf-8")
        self._broker(status="done", transcript="[03:12]\nOrder 4417-AB", mode="screen")

        out = io.StringIO()
        with redirect_stdout(out):
            code = transcribe.main([str(transcript), "--at", "03:12", "--at", "241.5", "--instructions", "the order id"])

        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue(), "[03:12]\nOrder 4417-AB\n")
        sent = json.loads(responses.calls[0].request.body)
        self.assertEqual(sent["at_seconds"], [192.0, 241.5])
        self.assertEqual(sent["instructions"], "the order id")
        self.assertEqual(transcript.read_text(encoding="utf-8"), "[03:12] SCREEN: order id UNREADABLE\n")

    def test_frame_read_refuses_bad_moments_before_calling_the_host(self):
        self.assertEqual(transcribe.parse_moment("1:03:12"), 3792.0)
        self.assertEqual(transcribe.parse_moment("[00:07]"), 7.0)
        for bad in (["x"], ["-3"], ["1"] * 7):
            err = io.StringIO()
            with redirect_stderr(err):
                self.assertEqual(transcribe.main([str(self.media), *sum((["--at", b] for b in bad), [])]), 2)


if __name__ == "__main__":
    unittest.main()
