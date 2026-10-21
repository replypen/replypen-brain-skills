"""brain_publish.py end to end against a bare-origin fixture and a fake `rp` (no network, no prod)."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import tempfile
import textwrap
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "brain_publish.py"
SPEC = importlib.util.spec_from_file_location("brain_publish", SCRIPT)
assert SPEC and SPEC.loader
bp = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bp)

FAKE_RC = textwrap.dedent('''\
    #!/usr/bin/env python3
    import fcntl, json, os, sys
    path = os.environ["FAKE_RC_STATE"]
    lock = open(path + ".lock", "w")
    fcntl.flock(lock, fcntl.LOCK_EX)  # brain_publish runs status + preflights in parallel
    state = json.load(open(path))
    argv = sys.argv[1:]
    def opt(name):
        return argv[argv.index(name) + 1] if name in argv else None
    sub, channel, sha = argv[2], opt("--channel"), opt("--sha")
    state["calls"].append([sub, channel])
    def chans():
        return [{"channel": c, "resolved_sha": s, "matches_origin": True} for c, s in state["channels"].items()]
    if "--tenant" in argv:
        if sub == "publish" and not state.get("proof_lie"):
            state["tenant_main"] = sha
        if sub == "publish" and state.get("fail_after_promote"):
            json.dump(state, open(path, "w")); print("verify: connection reset", file=sys.stderr); sys.exit(1)
        out = {"project": "demo", "status": {"local_sha": state.get("tenant_main")}}
    elif sub == "status":
        # A concurrent publisher moves stable after the first read: status runs in parallel with
        # the preflights, so only the publish-recheck (a later status) may see the advance.
        if state.get("advance_before_recheck") and [c[0] for c in state["calls"]].count("status") > 1:
            state["channels"]["stable"] = state.pop("advance_before_recheck")
        out = {"project": "demo", "status": {"channels": chans()}}
    elif sub == "preflight":
        tenants = state["tenants"].get(channel, [])
        ok = all(t["status"] == "ok" or t.get("preexisting") for t in tenants)
        out = {"canary": {"channel": channel, "sha": sha, "ok": ok, "templated": True, "tenants": tenants,
                          "consumers": state["consumers"].get(channel, 0), "checked": len(tenants)}}
    elif sub == "publish":
        if state.get("publish_fail"):
            json.dump(state, open(path, "w")); print("BRAIN_CANARY_FAILED", file=sys.stderr); sys.exit(1)
        old = state["channels"].get(channel)
        if not state.get("proof_lie"):
            state["channels"][channel] = sha
        if state.get("fail_after_promote"):
            json.dump(state, open(path, "w")); print("verify: connection reset", file=sys.stderr); sys.exit(1)
        out = {"project": "demo", "channel": channel, "sha": sha, "old_sha": old, "verified": True}
    json.dump(state, open(path, "w"))
    print(json.dumps(out))
''')


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


class BrainPublishTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        config = self.tmp / "gitconfig"
        config.write_text("[user]\n\tname = T\n\temail = t@example.org\n[init]\n\tdefaultBranch = main\n"
                          "[commit]\n\tgpgSign = false\n")
        self.env = {"GIT_CONFIG_GLOBAL": str(config), "GIT_CONFIG_NOSYSTEM": "1"}
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        (bin_dir / "rc").write_text(FAKE_RC)
        (bin_dir / "rc").chmod(0o755)
        (bin_dir / "rp").symlink_to("rc")  # subprocesses resolve `rp` first
        self.state_path = self.tmp / "rc.json"
        self.env.update(FAKE_RC_STATE=str(self.state_path), PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
        self._saved = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)

        self.origin, self.repo = self.tmp / "origin.git", self.tmp / "brain"
        subprocess.run(["git", "init", "-q", "--bare", str(self.origin)], check=True)
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.repo)], check=True, capture_output=True)
        (self.repo / "skills").mkdir()
        (self.repo / "skills" / "a.md").write_text("one\n")
        (self.repo / ".replypen.toml").write_text('project = "demo"\n')
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "one")
        self.old = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "skills" / "a.md").write_text("two\n")
        git(self.repo, "commit", "-qam", "two")
        self.head = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "push", "-q", "origin", "main")
        self.rc_state(channels={"stable": self.old})
        self._orig_checks, bp.default_checks = bp.default_checks, lambda *_: ["true"]
        self._cwd = os.getcwd()
        os.chdir(self.repo)

    def tearDown(self) -> None:
        os.chdir(self._cwd)
        bp.default_checks = self._orig_checks
        for k, v in self._saved.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        subprocess.run(["rm", "-rf", str(self.tmp)], check=False)

    def rc_state(self, **overrides) -> None:
        state = {"channels": {}, "consumers": {"stable": 3, "edge": 0}, "tenants": {}, "calls": []}
        state.update(overrides)
        self.state_path.write_text(json.dumps(state))

    def publish(self, *args: str) -> tuple[int, dict]:
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = bp.main(["--project", "demo", "--json", *args])
        return code, json.loads(out.getvalue())

    def published(self) -> list[str]:
        return [c for sub, c in json.loads(self.state_path.read_text())["calls"] if sub == "publish"]

    def test_publishes_stable_behind_main(self) -> None:
        code, receipt = self.publish()
        self.assertEqual((code, receipt["status"], receipt["sha"]), (0, "published", self.head))
        self.assertEqual(self.published(), ["stable"])

    def test_noop_when_already_live(self) -> None:
        self.rc_state(channels={"stable": self.head})
        code, receipt = self.publish()
        self.assertEqual((code, receipt["status"]), (0, "noop"))

    def test_dry_run_publishes_nothing(self) -> None:
        code, receipt = self.publish("--dry-run")
        self.assertEqual((code, receipt["status"], receipt["moves"]), (0, "dry_run", ["stable"]))
        self.assertEqual(self.published(), [])

    def test_dry_run_says_already_live(self) -> None:
        self.rc_state(channels={"stable": self.head})
        code, receipt = self.publish("--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("nothing to publish, stable already live", bp.render(receipt, code))

    def test_used_edge_behind_is_aligned_first(self) -> None:
        self.rc_state(channels={"stable": self.old, "edge": self.old}, consumers={"stable": 2, "edge": 1})
        code, receipt = self.publish()
        self.assertEqual((code, self.published()), (0, ["edge", "stable"]))
        self.assertEqual(receipt["after"], {"stable": self.head, "edge": self.head})

    def test_diverged_edge_blocks_before_any_publish(self) -> None:
        side = git(self.repo, "commit-tree", f"{self.old}^{{tree}}", "-p", self.old, "-m", "side")
        self.rc_state(channels={"stable": self.old, "edge": side}, consumers={"stable": 2, "edge": 1})
        code, receipt = self.publish()
        self.assertEqual((code, receipt["stage"]), (2, "edge-gate"))
        self.assertIn("never downgrade", receipt["error"])
        self.assertEqual(self.published(), [])

    def test_unused_edge_cannot_be_published(self) -> None:
        code, receipt = self.publish("--channel", "edge")
        self.assertEqual((code, receipt["stage"]), (2, "edge-gate"))
        self.assertEqual(self.published(), [])

    def test_preflight_offender_blocks_preexisting_fault_warns(self) -> None:
        self.rc_state(channels={"stable": self.old}, tenants={"stable": [
            {"tenant": "t1", "status": "degraded", "degraded": [{"key": "k", "file": "f", "reason": "r"}]}]})
        code, receipt = self.publish()
        self.assertEqual((code, receipt["stage"], self.published()), (2, "preflight", []))
        self.assertIn("t1: degraded k in f: r", receipt["offenders"])
        self.rc_state(channels={"stable": self.old}, tenants={"stable": [
            {"tenant": "t2", "status": "failed", "data_fault": True, "preexisting": True, "error": "no settings"}]})
        code, receipt = self.publish()
        self.assertEqual((code, receipt["status"]), (0, "published"))
        self.assertTrue(any("t2" in w for w in receipt["warnings"]))

    def test_publish_failure_and_proof_mismatch_exit_1(self) -> None:
        self.rc_state(channels={"stable": self.old}, publish_fail=True)
        self.assertEqual(self.publish()[0], 1)
        self.rc_state(channels={"stable": self.old}, proof_lie=True)
        code, receipt = self.publish()
        self.assertEqual((code, receipt["stage"]), (1, "proof"))

    def test_concurrent_advance_during_preflight_is_not_downgraded(self) -> None:
        newer = git(self.repo, "commit-tree", f"{self.head}^{{tree}}", "-p", self.head, "-m", "newer")
        self.rc_state(channels={"stable": self.old}, advance_before_recheck=newer)
        code, receipt = self.publish()
        self.assertEqual((code, receipt["stage"], self.published()), (2, "publish-recheck", []))

    def test_failed_publish_that_moved_the_channel_is_reported_uncertain(self) -> None:
        self.rc_state(channels={"stable": self.old}, fail_after_promote=True)
        code, receipt = self.publish()
        self.assertEqual((code, receipt["stage"]), (1, "publish"))
        self.assertEqual(receipt["published"][0]["now"], self.head)
        self.assertIn("after the failed publish", receipt["uncertain"])

    def test_merge_conflict_exits_3_and_publishes_nothing(self) -> None:
        peer = self.tmp / "peer"
        subprocess.run(["git", "clone", "-q", str(self.origin), str(peer)], check=True, capture_output=True)
        (peer / "skills" / "a.md").write_text("peer\n")
        git(peer, "commit", "-qam", "peer")
        git(peer, "push", "-q", "origin", "main")
        (self.repo / "skills" / "a.md").write_text("local\n")
        git(self.repo, "add", "skills/a.md")
        code, receipt = self.publish("--commit-message", "local")
        self.assertEqual((code, receipt["status"], self.published()), (3, "conflict", []))

    def test_refuses_wrong_project(self) -> None:
        code, receipt = self.publish_with_project("other")
        self.assertEqual((code, receipt["stage"]), (2, "classify"))

    def tenant_brain(self, **state) -> None:
        (self.repo / ".replypen.toml").write_text('project = "demo"\ntenant = "t1"\n')
        git(self.repo, "commit", "-qam", "tenant marker")
        self.rc_state(tenant_main=self.old, **state)

    def test_tenant_brain_syncs_and_proves_served_main(self) -> None:
        self.tenant_brain()
        code, receipt = self.publish()
        sha = git(self.repo, "rev-parse", "HEAD")
        self.assertEqual((code, receipt["status"], receipt["sha"]), (0, "published", sha))
        self.assertEqual(receipt["after"], {"tenant t1 main": sha})
        self.assertIn("LIVE demo tenant t1 main", bp.render(receipt, code))
        self.assertEqual(self.publish()[1]["status"], "noop")

    def test_tenant_brain_unproven_served_main_exits_1(self) -> None:
        self.tenant_brain(proof_lie=True)
        code, receipt = self.publish()
        self.assertEqual((code, receipt["stage"]), (1, "proof"))

    def test_tenant_failed_publish_that_moved_main_is_reported_uncertain(self) -> None:
        self.tenant_brain(fail_after_promote=True)
        code, receipt = self.publish()
        self.assertEqual((code, receipt["stage"]), (1, "publish"))
        self.assertEqual(receipt["published"][0]["now"], git(self.repo, "rev-parse", "HEAD"))
        self.assertIn("UNCERTAIN", bp.render(receipt, code))
        self.assertNotIn("nothing published", bp.render(receipt, code))

    def test_tenant_brain_refuses_channel(self) -> None:
        self.tenant_brain()
        code, receipt = self.publish("--channel", "edge")
        self.assertEqual((code, receipt["stage"]), (2, "classify"))

    def publish_with_project(self, project: str) -> tuple[int, dict]:
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = bp.main(["--project", project, "--json"])
        return code, json.loads(out.getvalue())


if __name__ == "__main__":
    unittest.main()
