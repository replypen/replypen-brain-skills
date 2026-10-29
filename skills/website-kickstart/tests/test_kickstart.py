"""kickstart_site.py against an in-memory GitHub (+ Actions) + Cloudflare + rp world."""

from __future__ import annotations

import base64
import contextlib
import html
import importlib.util
import io
import json
import re
import tempfile
import unittest
import unittest.mock
import urllib.parse
from pathlib import Path

import yaml

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "kickstart_site.py"
SPEC = importlib.util.spec_from_file_location("kickstart_site", SCRIPT)
assert SPEC and SPEC.loader
ks = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ks)

ACC = ks.DEFAULT_ACCOUNT
TEMPLATE_FILES = {
    "wrangler.jsonc": '{ "name": "__WORKER_NAME__", "previews": {} }\n',
    "package.json": '{ "name": "__WORKER_NAME__" }\n',
    "_data/site.yml": 'name: "__SITE_NAME__"\ncontact_form_action: "https://app.replypen.com/forms/__PROJECT__/__TENANT__"\n'
                      'turnstile_sitekey: "0x4AAAAtest"\n',
    ".github/workflows/deploy.yml": "name: Website deploy\n",
}
PERSONALISED = ("wrangler.jsonc", "package.json", "_data/site.yml")
TOKENS = {"CF_SITES_USER_TOKEN": "cf", "CF_SITES_DEPLOY_TOKEN": "deploy-token-xyz",
          "GH_SITE_PROVISIONING_PAT": "pat", "CF_BROWSER_RENDERING_TOKEN": "br"}


class World:
    """GitHub + Actions + Cloudflare + rp state; every write is appended to `writes`.

    A workflow run (push or dispatch) of deploy.yml advances one tick per Actions read (check-runs, runs):
    it builds for `push_delay`/`dispatch_delay` ticks with no `Website deploy` check-run yet, then shows
    that check in progress, then completes: success when the repo has the deploy secret + account variable
    and `deploy_outcomes` (consumed in completion order) does not say otherwise. Success creates/updates the
    Worker named in wrangler.jsonc and makes it serve that sha's site. A cancelled run stops on the next
    tick unless `stuck`; a disabled workflow starts no runs."""

    def __init__(self):
        self.repos = {}  # full_name -> {"id", "files", "sha", "history", "archived", "secrets", "vars"}
        self.install = (168123503, set())
        self.scripts = {}  # worker name -> tag
        self.live = {}  # worker name -> (repo full_name, sha) it serves
        self.runs = []  # workflow runs; each one's deploy job is check-run id == run id
        self.deploy_outcomes = []
        self.push_delay = self.dispatch_delay = 0
        self.rc_repos = []
        self.redirects = {}  # old full_name -> new (GitHub keeps answering renamed repos)
        self.fail_once = set()  # (method, path prefix) answered 503 once
        self.writes = []
        self.rp_scopes = []
        self.perms = None  # ReplyPen installation permissions override
        self.token = None  # bearer of the current GitHub request
        self.no_classic_pat = False  # the org's policy refuses the classic PAT (403)
        self.no_user_installations = False  # …and /user/installations refuses gh's OAuth token too
        self.argv = []  # every command line, to prove no secret rides in argv
        self.n = 0

    # ---- Actions ----
    def start_run(self, repo, event="push", delay=None):
        if self.repos[repo].get("workflow_disabled"):
            if event == "workflow_dispatch":
                raise ks.StepFailed("could not create workflow dispatch event: HTTP 422: Workflow is disabled")
            return None
        self.n += 1
        delay = (self.push_delay if event == "push" else self.dispatch_delay) if delay is None else delay
        run = {"id": self.n, "repo": repo, "sha": self.repos[repo]["sha"], "event": event, "status": "queued",
               "conclusion": None, "ticks": delay, "cancel": False, "stuck": False,
               "html_url": f"https://github.com/{repo}/actions/runs/{self.n}"}
        self.runs.append(run)
        return run

    def advance_runs(self):
        for run in self.runs:
            if run["status"] == "completed":
                continue
            if run["cancel"] and not run["stuck"]:
                run |= {"status": "completed", "conclusion": "cancelled"}
                continue
            if run["ticks"] > 0:
                run |= {"ticks": run["ticks"] - 1, "status": "in_progress"}
                continue
            repo = self.repos[self.redirects.get(run["repo"], run["repo"])]
            armed = ks.DEPLOY_SECRET in repo["secrets"] and repo["vars"].get(ks.ACCOUNT_VAR) == ACC
            outcome = self.deploy_outcomes.pop(0) if self.deploy_outcomes else "success"
            run |= {"status": "completed", "conclusion": outcome if armed else "failure"}
            if run["conclusion"] == "success":
                worker = re.search(r'"name": "([^"]+)"', repo["files"]["wrangler.jsonc"]).group(1)
                self.scripts.setdefault(worker, f"tag-{worker}")
                self.live[worker] = (run["repo"], run["sha"])

    # ---- HTTP ----
    def request(self, method, url, *, headers=None, body=None):
        u = urllib.parse.urlsplit(url)
        path, query = u.path, urllib.parse.parse_qs(u.query)
        short = re.sub(r"^/client/v4/accounts/\w+", "", path)
        if method != "GET":
            self.writes.append((method, short, body))
        for key in list(self.fail_once):
            if method == key[0] and short.startswith(key[1]):
                self.fail_once.discard(key)
                return 503, b'{"success": false, "errors": [{"message": "flaky"}]}'
        if u.netloc == "api.github.com":
            self.token = (headers or {}).get("Authorization", "").removeprefix("Bearer ")
            return self.github(method, path, query, body)
        if u.netloc == "api.cloudflare.com":
            return self.cloudflare(method, path.removeprefix(f"/client/v4/accounts/{ACC}"), body)
        worker = u.netloc.split(".")[0]
        if worker not in self.live or worker not in self.scripts:
            return 404, b"not found"
        repo, sha = self.live[worker]
        if path == ks.REVISION_PATH:
            return 200, json.dumps({"revision": sha}).encode()
        files = self.repos[self.redirects.get(repo, repo)]["files"]
        site = yaml.safe_load(files.get("_data/site.yml") or "name: Praktijk Test")  # adopted non-template site
        return 200, f"<h1>{html.escape(site['name'])}</h1>".encode()

    def ok(self, result, status=200):
        return status, json.dumps({"success": True, "errors": [], "messages": [], "result": result}).encode()

    def github(self, method, path, query, body):
        js = lambda d, s=200: (s, json.dumps(d).encode())
        if self.token == "pat" and self.no_classic_pat:
            return js({"message": "`dentai-org` forbids access via a personal access token (classic)."}, 403)
        if m := re.fullmatch(r"/repos/([^/]+/[^/]+)", path):
            name = self.redirects.get(m.group(1), m.group(1))
            if method == "PATCH":
                repo = self.repos.pop(name)
                repo.update(body)
                new = name.split("/")[0] + "/" + body.get("name", name.split("/")[1])
                self.repos[new] = repo
                if new != name:
                    self.redirects[name] = new
                return js({})
            repo = self.repos.get(name)
            if not repo:
                return js({"message": "Not Found"}, 404)
            return js({"id": repo["id"], "full_name": name, "owner": {"id": 277291192},
                       "html_url": f"https://github.com/{name}", "archived": repo.get("archived", False)})
        if path == "/repos/replypen/site-template/generate":
            self.redirects.pop(f"{body['owner']}/{body['name']}", None)
            sha = f"{len(self.repos) + 1:x}" * 40  # every generated repo starts its own history
            self.repos[f"{body['owner']}/{body['name']}"] = {
                "id": 1000 + self.n, "files": dict(TEMPLATE_FILES), "sha": sha[:40],
                "secrets": {}, "vars": {}}
            return js({}, 201)
        if m := re.fullmatch(r"/repos/([^/]+/[^/]+)/contents/(.+)", path):
            text = self.repos[m.group(1)]["files"].get(m.group(2))
            return js({"content": base64.b64encode(text.encode()).decode()}) if text else js({}, 404)
        if m := re.fullmatch(r"/repos/([^/]+/[^/]+)/commits/main", path):
            return js({"sha": self.repos[m.group(1)]["sha"]})
        check = lambda r: {k: r[k] for k in ("id", "status", "conclusion", "html_url")}
        if m := re.fullmatch(r"/repos/([^/]+/[^/]+)/commits/(\w+)/check-runs", path):
            self.advance_runs()
            assert query.get("check_name") == [ks.DEPLOY_CHECK], query
            return js({"check_runs": [check(r) for r in self.runs
                                      if (r["repo"], r["sha"]) == m.groups() and r["ticks"] == 0]})
        if m := re.fullmatch(r"/repos/([^/]+/[^/]+)/check-runs/(\d+)", path):
            self.advance_runs()
            return js(check(next(r for r in self.runs if r["id"] == int(m.group(2)))))
        if m := re.fullmatch(r"/repos/([^/]+/[^/]+)/actions/workflows/deploy\.yml/(runs|disable)", path):
            name = self.redirects.get(m.group(1), m.group(1))
            if m.group(2) == "disable":
                assert method == "PUT"
                self.repos[name]["workflow_disabled"] = True
                return 204, b""
            self.advance_runs()
            ev, sha = query.get("event", [None])[0], query.get("head_sha", [None])[0]
            status, per_page = query.get("status", [None])[0], int(query.get("per_page", ["30"])[0])
            runs = [r for r in self.runs if self.redirects.get(r["repo"], r["repo"]) == name
                    and ev in (None, r["event"]) and sha in (None, r["sha"]) and status in (None, r["status"])]
            return js({"total_count": len(runs), "workflow_runs": [  # newest first, first page only, like GitHub
                {"head_sha": r["sha"], "event": r["event"]} | check(r) for r in list(reversed(runs))[:per_page]]})
        if m := re.fullmatch(r"/repos/([^/]+/[^/]+)/actions/runs/(\d+)(/cancel)?", path):
            run = next(r for r in self.runs if r["id"] == int(m.group(2)))
            if m.group(3):
                if run["status"] == "completed":
                    return js({"message": "Cannot cancel a workflow run that is completed."}, 409)
                run["cancel"] = True
                return js({}, 202)
            self.advance_runs()
            return js({"head_sha": run["sha"], "event": run["event"]} | check(run))
        if m := re.fullmatch(r"/repos/([^/]+/[^/]+)/actions/secrets/(\w+)", path):
            secrets = self.repos[m.group(1)]["secrets"]
            return (204, b"") if secrets.pop(m.group(2), None) else js({"message": "Not Found"}, 404)
        if re.fullmatch(r"/orgs/[^/]+/installations", path):
            return js({"installations": [
                {"id": 99, "app_slug": "other-app", "app_id": 1, "repository_selection": "all"},
                {"id": self.install[0], "app_slug": "replypen", "app_id": 4006321, "repository_selection": "selected",
                 "permissions": self.perms or {"checks": "read", "statuses": "read"}}]})
        if m := re.fullmatch(r"/user/installations/(\d+)/repositories(?:/(\d+))?", path):
            if self.no_user_installations:
                return js({"message": "You must authenticate with an access token authorized to a GitHub App, "
                                      "a personal access token, or basic auth in order to list repositories"}, 403)
            assert int(m.group(1)) == self.install[0]
            members = self.install[1]
            if method == "PUT":
                members.add(int(m.group(2)))
                return 204, b""
            if method == "DELETE":
                members.discard(int(m.group(2)))
                return 204, b""
            return js({"repositories": [{"id": i} for i in sorted(members)]})
        raise AssertionError(f"unrouted GitHub {method} {path}")

    def cloudflare(self, method, path, body):
        if path.endswith("/browser-rendering/screenshot"):
            return 200, b"\x89PNG fake"
        if path == "/workers/scripts":
            return self.ok([{"id": n, "tag": t} for n, t in self.scripts.items()])
        if m := re.fullmatch(r"/workers/scripts/([\w-]+)", path):
            self.scripts.pop(m.group(1))
            self.live.pop(m.group(1), None)
            return self.ok(None)
        raise AssertionError(f"unrouted Cloudflare {method} {path}")

    # ---- commands ----
    def run(self, cmd, *, cwd=None, env=None, stdin=None):
        self.argv.append(list(cmd))
        if cmd[:3] == ["gh", "auth", "token"]:
            return "gho_x\n"
        if cmd[:3] == ["gh", "repo", "clone"]:
            target = Path(cmd[4])
            for rel, text in self.repos[cmd[3]]["files"].items():
                (target / rel).parent.mkdir(parents=True, exist_ok=True)
                (target / rel).write_text(text)
            target.joinpath(".origin").write_text(cmd[3])
            return ""
        if cmd[0] == "git":
            root = Path(cmd[2])
            if "push" in cmd:
                name = root.joinpath(".origin").read_text()
                repo = self.repos[name]
                repo["files"] |= {rel: (root / rel).read_text() for rel in PERSONALISED}
                repo["sha"] = "b" + repo["sha"][1:]
                self.writes.append(("cmd", "git push", None))
                self.start_run(name)  # the push itself starts the deploy workflow
            return ""
        if cmd[:3] in (["gh", "secret", "set"], ["gh", "variable", "set"], ["gh", "workflow", "run"]):
            repo = self.repos[cmd[cmd.index("--repo") + 1]]
            self.writes.append(("cmd", " ".join(cmd[:3]), stdin))
            if cmd[1] == "secret":
                repo["secrets"][cmd[3]] = stdin
            elif cmd[1] == "variable":
                repo["vars"][cmd[3]] = cmd[cmd.index("--body") + 1]
            else:
                assert cmd[3:6] == ["deploy.yml", "--ref", "main"], cmd
                self.start_run(cmd[cmd.index("--repo") + 1], "workflow_dispatch")
            return ""
        if cmd[0] == "rp":
            n = 5  # rp --project P (--tenant T | --scope project) …
            self.rp_scopes.append(cmd[1:n])
            args = cmd[n:]
            args = args[1:] if args[0] == "project" else args
            verb, params = args[:2], args[2:]
            if verb == ["repo", "ls"]:
                return json.dumps(self.rc_repos)
            self.writes.append(("cmd", "rp " + " ".join(verb), params))
            kv = dict(a.split("=", 1) for a in params if "=" in a)
            if verb == ["repo", "add"]:
                self.rc_repos.append(kv)
            elif verb == ["repo", "set"]:
                next(r for r in self.rc_repos if r["name"] == params[0]).update(kv)
            elif verb == ["repo", "rm"]:
                self.rc_repos = [r for r in self.rc_repos if r["name"] != params[0]]
            return ""
        raise AssertionError(f"unrouted command {cmd}")


class KickstartTest(unittest.TestCase):
    def setUp(self):
        self.world = World()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def kick(self, *extra, env=TOKENS, tenant="test"):
        org = extra[0] if extra and not extra[0].startswith("-") else "dentai-org"
        extra = extra[1:] if extra and not extra[0].startswith("-") else extra
        scope = ["--tenant", tenant] if tenant else ["--project-scope"]
        args = ks.parse_args([org, "test", "--project", "dentai", *scope, "--name", "Praktijk Test",
                              "--out", self.tmp.name, "--state-dir", self.tmp.name + "/state", *extra])
        k = ks.Kickstart(args, http=self.world, run=self.world.run, sleep=lambda s: None, env=env)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = k.teardown() if args.teardown else k.main()
        self.world.writes, writes = [], self.world.writes
        return code, out.getvalue(), writes

    def forget_record(self):
        Path(self.tmp.name, "state").joinpath(f"{ACC}-site-test.json").unlink()

    @staticmethod
    def names(writes):
        return [f"{m} {p}" for m, p, _ in writes]

    def test_fresh_run_provisions_everything_in_order(self):
        code, out, writes = self.kick()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.names(writes), [
            "POST /repos/replypen/site-template/generate",
            "cmd git push",
            "PUT /user/installations/168123503/repositories/1000",
            "cmd gh secret set",
            "cmd gh variable set",
            # no dispatch: the push-triggered run picked up the secret before its deploy job
            "cmd rp repo add",
            "cmd rp dev mirror",
            "POST /browser-rendering/screenshot",
            "POST /browser-rendering/screenshot",
        ])
        repo = self.world.repos["dentai-org/site-test"]
        self.assertIn('"name": "site-test"', repo["files"]["wrangler.jsonc"])
        self.assertIn("forms/dentai/test", repo["files"]["_data/site.yml"])
        self.assertIn('name: "Praktijk Test"', repo["files"]["_data/site.yml"])
        self.assertEqual(repo["secrets"], {"CLOUDFLARE_API_TOKEN": "deploy-token-xyz"})
        self.assertEqual(repo["vars"], {"CLOUDFLARE_ACCOUNT_ID": ACC})
        self.assertFalse([a for a in self.world.argv if any("deploy-token-xyz" in x for x in a)])  # stdin only
        self.assertEqual(self.world.live["site-test"], ("dentai-org/site-test", repo["sha"]))  # workflow made it
        rc_add = next(w[2] for w in writes if w[1] == "rp repo add")
        self.assertIn("preview_url_template=https://{branch}-site-test.rootcause-sites.workers.dev", rc_add)
        self.assertIn("deploy_check=Website deploy", rc_add)
        self.assertIn("revision_path=/replypen-revision.json", rc_add)
        self.assertIn("form_challenge=turnstile", rc_add)  # bot check on: the template renders the widget
        self.assertIn(f"--expect-sha {repo['sha']}", " ".join(next(w[2] for w in writes if w[1] == "rp dev mirror")))
        self.assertTrue((Path(self.tmp.name) / "test-mobile.png").exists())
        self.assertIn(f"/replypen-revision.json = {repo['sha'][:8]}", out)
        self.assertIn("deploy run  https://github.com/dentai-org/site-test/actions/runs/", out)
        self.assertIn("Pas op de website", out)

    def test_push_run_without_secret_is_replaced_by_a_dispatched_run(self):
        self.world.deploy_outcomes = ["failure"]  # the push run lost the race with the secret
        code, out, writes = self.kick()
        self.assertEqual(code, 0, out)
        self.assertIn("cmd gh workflow run", self.names(writes))
        self.assertEqual([r["conclusion"] for r in self.world.runs], ["failure", "success"])

    def test_stale_push_failure_landing_after_the_dispatch_does_not_abort_the_wait(self):
        """No check yet → dispatch; the push run's failed check lands while the dispatched run still builds.
        The waiter follows the dispatched run, so it ends green instead of reporting the stale failure."""
        self.world.push_delay, self.world.dispatch_delay = 2, 3
        self.world.deploy_outcomes = ["failure"]  # the push run completes first, and fails
        code, out, writes = self.kick()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.names(writes).count("cmd gh workflow run"), 1)
        push, dispatched = self.world.runs
        self.assertEqual((push["event"], push["conclusion"]), ("push", "failure"))
        self.assertEqual((dispatched["event"], dispatched["conclusion"]), ("workflow_dispatch", "success"))
        self.assertIn(f"deploy run  {dispatched['html_url']}", out)

    def test_in_progress_push_check_is_awaited_not_redispatched(self):
        self.world.push_delay = 1  # first look: no check; ReplyPen polls it via check-runs/<id> once visible
        self.world.deploy_outcomes = []
        orig = self.world.github
        def first_look_sees_it_running(method, path, query, body):
            if path.endswith("/check-runs") and self.world.runs and self.world.runs[0]["ticks"]:
                self.world.runs[0]["ticks"] = 0  # build done, deploy job running
                return 200, json.dumps({"check_runs": [{"id": 1, "status": "in_progress", "conclusion": None,
                                                        "html_url": "x"}]}).encode()
            return orig(method, path, query, body)
        self.world.github = first_look_sees_it_running
        code, out, writes = self.kick()
        self.assertEqual(code, 0, out)
        self.assertNotIn("cmd gh workflow run", self.names(writes))
        self.assertIn("already succeeded", out)

    def test_failed_deploy_stops_then_rerun_redispatches(self):
        self.world.deploy_outcomes = ["failure", "failure"]
        code, out, writes = self.kick()
        self.assertEqual(code, 1)
        self.assertIn("✗ deploy", out)
        self.assertIn("concluded failure: https://github.com/dentai-org/site-test/actions/runs/", out)
        self.assertNotIn("cmd rp repo add", self.names(writes))
        code, out, writes = self.kick()
        self.assertEqual(code, 0, out)
        self.assertIn("cmd gh workflow run", self.names(writes))

    def test_missing_deploy_token_stops_before_touching_secrets(self):
        env = {k: v for k, v in TOKENS.items() if k != "CF_SITES_DEPLOY_TOKEN"}
        code, out, writes = self.kick(env=env)
        self.assertEqual(code, 1)
        self.assertIn("✗ deploy — CF_SITES_DEPLOY_TOKEN is not set", out)
        self.assertNotIn("cmd gh secret set", self.names(writes))

    def test_no_sitekey_no_bot_check(self):
        """A template without a filled sitekey shows no widget, so the mirror must not demand a token."""
        TEMPLATE_FILES["_data/site.yml"], orig = TEMPLATE_FILES["_data/site.yml"].replace('"0x4AAAAtest"', '""'), TEMPLATE_FILES["_data/site.yml"]
        self.addCleanup(TEMPLATE_FILES.__setitem__, "_data/site.yml", orig)
        code, out, writes = self.kick()
        self.assertEqual(code, 0, out)
        self.assertNotIn("form_challenge=turnstile", next(w[2] for w in writes if w[1] == "rp repo add"))

    def test_sitekey_read_as_yaml(self):
        """Unquoted or single-quoted keys render the widget too, so they must switch the check on."""
        for line in ("turnstile_sitekey: 0x4AAAAtest\n", "turnstile_sitekey: '0x4AAAAtest'\n"):
            with self.subTest(line=line):
                self.setUp()
                orig = TEMPLATE_FILES["_data/site.yml"]
                TEMPLATE_FILES["_data/site.yml"] = orig.replace('turnstile_sitekey: "0x4AAAAtest"\n', line)
                self.addCleanup(TEMPLATE_FILES.__setitem__, "_data/site.yml", orig)
                code, out, writes = self.kick()
                TEMPLATE_FILES["_data/site.yml"] = orig
                self.assertEqual(code, 0, out)
                self.assertIn("form_challenge=turnstile", next(w[2] for w in writes if w[1] == "rp repo add"))

    def test_adopted_site_without_template_data_file(self):
        """An adopted non-template site (Astro, no _data/site.yml) re-runs cleanly and keeps its bot check."""
        self.kick()
        del self.world.repos["dentai-org/site-test"]["files"]["_data/site.yml"]
        self.forget_record()
        code, out, writes = self.kick("--adopt")
        self.assertEqual(code, 0, out)
        self.assertFalse([w for w in writes if "form_challenge" in str(w)])

    def test_adopted_repo_without_workflow_is_refused(self):
        self.kick()
        del self.world.repos["dentai-org/site-test"]["files"][".github/workflows/deploy.yml"]
        code, out, writes = self.kick("--adopt")
        self.assertEqual((code, writes), (1, []))
        self.assertIn("has no .github/workflows/deploy.yml: copy it from replypen/site-template", out)

    def test_project_scope_site_has_no_tenant(self):
        """No --tenant ⇒ the project's own site: form posts to /forms/<project>, mirror at project scope."""
        code, out, _ = self.kick(tenant=None)
        self.assertEqual(code, 0, out)
        site = yaml.safe_load(self.world.repos["dentai-org/site-test"]["files"]["_data/site.yml"])
        self.assertEqual(site["contact_form_action"], "https://app.replypen.com/forms/dentai")
        self.assertEqual({tuple(s) for s in self.world.rp_scopes}, {("--project", "dentai", "--scope", "project")})
        self.assertIn("rp --project dentai --scope project project chat send", out)

    def test_org_forbidding_classic_pats_falls_back_to_the_gh_login(self):
        self.world.no_classic_pat = True
        code, out, _ = self.kick()
        self.assertEqual(code, 0, out)
        self.assertIn(self.world.repos["dentai-org/site-test"]["id"], self.world.install[1])

    def test_org_where_no_token_may_edit_app_repos_continues_with_a_hint(self):
        self.world.no_classic_pat = self.world.no_user_installations = True
        code, out, writes = self.kick()
        self.assertEqual(code, 0, out)
        self.assertIn("unverifiable, add site-test by hand", out)
        self.assertFalse([p for m, p, _ in writes if m == "PUT" and p.startswith("/user/installations/")])

    def test_replypen_app_without_check_permissions_stops_with_the_accept_url(self):
        self.world.perms = {"contents": "write", "pull_requests": "write"}
        code, out, writes = self.kick()
        self.assertEqual(code, 1)
        self.assertIn("lacks checks, statuses: read", out)
        self.assertIn("/settings/installations/168123503/permissions/update", out)
        self.assertNotIn("cmd gh secret set", self.names(writes))

    def test_scope_is_explicit(self):
        for argv in ([], ["--tenant", "t", "--project-scope"]):
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                ks.parse_args(["dentai-org", "test", "--project", "dentai", *argv])

    def test_rerun_skips_done_steps(self):
        self.kick()
        code, out, writes = self.kick()
        self.assertEqual(code, 0, out)
        # only the re-set deploy secret/variable, the mirror refresh and fresh screenshots remain
        self.assertEqual(self.names(writes), [
            "cmd gh secret set", "cmd gh variable set", "cmd rp dev mirror",
            "POST /browser-rendering/screenshot", "POST /browser-rendering/screenshot"])
        self.assertIn("already succeeded", out)
        self.assertIn("Worker site-test exists, created by this machine for dentai-org/site-test", out)

    def test_dry_run_without_credentials_writes_nothing(self):
        world_run = self.world.run
        self.world.run = lambda cmd, **kw: "" if cmd[:3] == ["gh", "auth", "token"] else world_run(cmd, **kw)
        code, out, writes = self.kick("--dry-run", env={})
        self.assertEqual(code, 0, out)
        self.assertEqual(writes, [])
        self.assertIn("(dry-run) would POST /repos/replypen/site-template/generate", out)
        self.assertIn("(dry-run) would $ gh secret set CLOUDFLARE_API_TOKEN --repo dentai-org/site-test "
                      "< $CF_SITES_DEPLOY_TOKEN", out)
        self.assertIn("(dry-run) would $ rp --project dentai --tenant test project repo add name=site", out)
        self.assertIn("result  dry-run", out)

    def test_dry_run_with_credentials_reads_but_never_writes(self):
        self.kick()
        self.world.deploy_outcomes = ["failure"]
        self.world.repos["dentai-org/site-test"]["sha"] = "c" * 40  # new head, not yet deployed
        self.world.start_run("dentai-org/site-test")
        code, out, writes = self.kick("--dry-run")
        self.assertEqual((code, writes), (0, []))
        self.assertIn("(dry-run) would $ gh workflow run deploy.yml --ref main", out)
        self.assertFalse([a for a in self.world.argv if a[:2] in (["gh", "secret"], ["gh", "workflow"])][1:])

    def test_mirror_name_collision_is_refused(self):
        self.world.rc_repos = [{"name": "site", "git_url": "https://github.com/x/other.git"}]
        code, out, _ = self.kick()
        self.assertEqual(code, 1)
        self.assertIn("--mirror-name", out)

    def test_teardown_undoes_and_frees_the_name(self):
        self.kick()
        code, out, _ = self.kick("--teardown", "wrong-name")
        self.assertEqual(code, 1)
        code, out, _ = self.kick("--teardown", "site-test")
        self.assertEqual(code, 0, out)
        self.assertEqual((self.world.scripts, self.world.rc_repos, self.world.install[1]), ({}, [], set()))
        parked = [n for n in self.world.repos if n.startswith("dentai-org/site-test-teardown-")]
        self.assertEqual(len(parked), 1)
        self.assertTrue(self.world.repos[parked[0]]["archived"])
        self.assertEqual(self.world.repos[parked[0]]["secrets"], {})  # no deploy token left behind
        self.assertIn("gh repo delete", out)
        code, out, _ = self.kick()  # the slug is free again
        self.assertEqual(code, 0, out)

    # ---- ownership, secrets, YAML, resumable teardown ----

    def test_teardown_from_wrong_org_refuses_before_any_write(self):
        self.kick()
        code, out, writes = self.kick("wrong-org", "--teardown", "site-test")
        self.assertEqual((code, writes), (1, []))
        self.assertIn("this machine created Worker site-test for dentai-org/site-test, not wrong-org/site-test", out)
        self.assertIn("site-test", self.world.scripts)

    def test_fork_with_the_same_history_cannot_tear_down_or_adopt_the_worker(self):
        """A fork/copy keeps every commit and the Worker serves its revision marker; neither proves anything."""
        self.kick()
        self.world.repos["another-org/site-test"] = dict(self.world.repos["dentai-org/site-test"], id=4242)
        for extra in (("--teardown", "site-test"), ("--teardown", "site-test", "--force-orphan"), ("--adopt",)):
            with self.subTest(extra=extra):
                code, out, writes = self.kick("another-org", *extra)
                self.assertEqual((code, writes), (1, []))
                self.assertIn("for dentai-org/site-test, not another-org/site-test", out)
        self.assertEqual(self.world.live["site-test"][0], "dentai-org/site-test")

    def test_teardown_missing_repo_is_an_error_not_acceptance(self):
        self.world.scripts["site-test"] = "tag0"  # Worker without record, no repo
        code, out, writes = self.kick("--teardown", "site-test", "--force-orphan")
        self.assertEqual((code, writes), (1, []))
        self.assertIn("no GitHub repo", out)

    def test_teardown_without_record_needs_force_orphan(self):
        self.kick()
        self.forget_record()
        code, out, writes = self.kick("--teardown", "site-test")
        self.assertEqual((code, writes), (1, []))
        self.assertIn("Worker site-test exists, and this machine has no record of creating it", out)
        self.assertIn("pass --force-orphan", out)
        code, out, _ = self.kick("--teardown", "site-test", "--force-orphan")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.world.scripts, {})

    def test_existing_worker_without_record_needs_adopt(self):
        self.kick()
        self.forget_record()
        del self.world.live["site-test"]  # what it serves now is unknown
        code, out, writes = self.kick()
        self.assertEqual((code, writes), (1, []))
        self.assertIn("✗ preflight — Worker site-test already exists in account", out)
        self.assertIn("pass --adopt", out)
        code, out, writes = self.kick("--adopt")
        self.assertEqual(code, 0, out)
        self.assertIn("no record on this machine, --adopt", out)
        self.assertIn("redeploying", out)  # its old green check-run does not prove what is live now
        self.assertIn("cmd gh workflow run", self.names(writes))
        self.assertIn("install website-dev", out)  # fake repo has no .agents/skills/website-dev

    def test_same_slug_from_another_org_aborts_before_any_write(self):
        self.kick()
        code, out, writes = self.kick("another-org")
        self.assertEqual((code, writes), (1, []))
        self.assertIn("✗ preflight", out)
        self.assertIn("for dentai-org/site-test, not another-org/site-test", out)

    def test_existing_repo_without_record_needs_adopt(self):
        self.kick()
        Path(self.tmp.name, "state", f"{ACC}-site-test.json").write_text(json.dumps(
            {"account": ACC, "worker": "site-test", "repo": "dentai-org/site-test"}))  # Worker claim, no repo_id
        code, out, writes = self.kick()
        self.assertEqual((code, writes), (1, []))
        self.assertIn("is not a kickstart from this machine; pass --adopt", out)

    def test_teardown_stops_deploys_before_deleting_anything(self):
        self.kick()
        queued = self.world.start_run("dentai-org/site-test", delay=5)  # would recreate the Worker later
        code, out, writes = self.kick("--teardown", "site-test")
        self.assertEqual(code, 0, out)
        names = self.names(writes)
        disable, cancel = (names.index("PUT /repos/dentai-org/site-test/actions/workflows/deploy.yml/disable"),
                           names.index(f"POST /repos/dentai-org/site-test/actions/runs/{queued['id']}/cancel"))
        first_delete = min(i for i, n in enumerate(names) if n.startswith(("DELETE", "cmd rp repo rm")))
        self.assertLess(disable, cancel)
        self.assertLess(cancel, first_delete)
        self.assertEqual(queued["conclusion"], "cancelled")
        self.world.advance_runs()
        self.assertEqual(self.world.scripts, {})  # nothing came back

    def test_teardown_finds_an_active_run_behind_a_page_of_finished_ones(self):
        self.kick()
        old = self.world.start_run("dentai-org/site-test", delay=99)
        for _ in range(120):  # newer, finished history pushes the active run off the first page
            done = self.world.start_run("dentai-org/site-test")
            done |= {"status": "completed", "conclusion": "success"}
        code, out, writes = self.kick("--teardown", "site-test")
        self.assertEqual(code, 0, out)
        self.assertIn(f"POST /repos/dentai-org/site-test/actions/runs/{old['id']}/cancel", self.names(writes))
        self.assertEqual(old["conclusion"], "cancelled")

    def test_refused_provisioning_leaves_no_record_a_teardown_would_trust(self):
        self.kick()
        self.forget_record()
        self.world.scripts.pop("site-test")  # Worker free, repo exists but is not ours on this machine
        code, out, _ = self.kick()
        self.assertEqual(code, 1)
        self.assertIn("is not a kickstart from this machine; pass --adopt", out)
        self.assertFalse(Path(self.tmp.name, "state", f"{ACC}-site-test.json").exists())
        code, out, writes = self.kick("--teardown", "site-test")
        self.assertEqual((code, writes), (1, []))
        self.assertIn("pass --force-orphan", out)

    def test_record_for_another_repo_refuses_even_when_the_worker_is_gone(self):
        self.kick()
        self.world.scripts.pop("site-test")
        record = Path(self.tmp.name, "state", f"{ACC}-site-test.json")
        before = record.read_text()
        code, out, writes = self.kick("another-org")
        self.assertEqual((code, writes), (1, []))
        self.assertIn("for dentai-org/site-test, not another-org/site-test", out)
        self.assertEqual(record.read_text(), before)

    def test_teardown_aborts_while_a_run_will_not_stop(self):
        self.kick()
        self.world.start_run("dentai-org/site-test", delay=99)["stuck"] = True
        code, out, writes = self.kick("--teardown", "site-test")
        self.assertEqual(code, 1)
        self.assertIn("✗ deploys — deploy runs still active", out)
        self.assertFalse([n for n in self.names(writes) if n.startswith("DELETE") or "rp repo rm" in n])
        self.assertIn("site-test", self.world.scripts)

    def test_subprocess_env_carries_no_operator_secrets(self):
        import os
        import sys
        probe = "import os, sys; print(sorted(k for k in os.environ if k.endswith(('TOKEN', '_PAT'))), sys.stdin.read())"
        with unittest.mock.patch.dict(os.environ, {"GH_SITE_PROVISIONING_PAT": "SENTINEL_PAT_123",
                                                   "CF_SITES_DEPLOY_TOKEN": "SENTINEL_CF_456"}):
            self.assertEqual(ks.run_command([sys.executable, "-c", probe]).strip(), "[]")
            out = ks.run_command([sys.executable, "-c", probe], stdin="piped").strip()
            self.assertEqual(out, "[] piped")

    def test_secrets_in_failing_command_output_are_redacted(self):
        env = {k: v * 8 for k, v in TOKENS.items()}  # only values >= 8 chars count as secrets
        world_run = self.world.run

        def leaky(cmd, **kw):
            if cmd[:3] == ["gh", "secret", "set"]:
                raise ks.StepFailed(f"env: {env['GH_SITE_PROVISIONING_PAT']} {env['CF_SITES_DEPLOY_TOKEN']}")
            return world_run(cmd, **kw)
        args = ks.parse_args(["dentai-org", "test", "--project", "dentai", "--tenant", "test",
                              "--out", self.tmp.name, "--state-dir", self.tmp.name + "/state"])
        k = ks.Kickstart(args, http=self.world, run=leaky, sleep=lambda s: None, env=env)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(k.main(), 1)
        self.assertIn("env: *** ***", out.getvalue())
        self.assertNotIn(env["CF_SITES_DEPLOY_TOKEN"], out.getvalue())

    def test_names_with_quotes_backslashes_newlines_stay_valid_yaml(self):
        for name in ['Praktijk "De Brug"', "Back\\slash: yes", "Twee\nregels", "Tandarts Ève & Zoon #1"]:
            with self.subTest(name=name):
                self.world = World()
                code, out, _ = self.kick("--name", name)
                self.assertEqual(code, 0, out)
                site = yaml.safe_load(self.world.repos["dentai-org/site-test"]["files"]["_data/site.yml"])
                self.assertEqual(site["name"], name)
                self.forget_record()

    def test_bad_slugs_are_rejected(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            ks.parse_args(["dentai-org", "Bad Slug", "--project", "dentai", "--tenant", "t"])

    def test_transient_503_is_retried(self):
        self.kick()
        self.world.fail_once = {("DELETE", "/workers/scripts/")}
        code, out, _ = self.kick("--teardown", "site-test")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.world.scripts, {})

    def test_teardown_resumes_after_archive_failure(self):
        self.kick()
        orig = self.world.github
        def archive_fails(method, path, query, body):
            if method == "PATCH" and body == {"archived": True}:
                return 500, b'{"message": "boom"}'
            return orig(method, path, query, body)
        self.world.github = archive_fails
        code, out, _ = self.kick("--teardown", "site-test")
        self.assertEqual(code, 1, out)
        parked = [n for n in self.world.repos if "-teardown-" in n]
        self.assertEqual(len(parked), 1)
        self.assertFalse(self.world.repos[parked[0]].get("archived"))
        self.world.github = orig
        code, out, _ = self.kick("--teardown", "site-test")
        self.assertEqual(code, 0, out)
        self.assertIn("renamed by an earlier teardown", out)
        self.assertIn("Worker site-test already deleted", out)
        self.assertTrue(self.world.repos[parked[0]]["archived"])
        self.assertEqual(len([n for n in self.world.repos if "-teardown-" in n]), 1)  # no second rename


if __name__ == "__main__":
    unittest.main()
