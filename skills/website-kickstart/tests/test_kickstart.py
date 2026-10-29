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

    A deploy run (push or dispatch) completes on the next check-runs poll: success when the repo has the
    deploy secret + account variable and `deploy_outcomes` (consumed per run) does not say otherwise.
    Success creates/updates the Worker named in wrangler.jsonc and makes it serve that sha's site."""

    def __init__(self):
        self.repos = {}  # full_name -> {"id", "files", "sha", "history", "archived", "secrets", "vars"}
        self.install = (168123503, set())
        self.scripts = {}  # worker name -> tag
        self.live = {}  # worker name -> (repo full_name, sha) it serves
        self.runs = []  # check-runs: {"id", "repo", "sha", "status", "conclusion", "html_url"}
        self.deploy_outcomes = []
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
    def start_run(self, repo):
        self.n += 1
        self.runs.append({"id": self.n, "repo": repo, "sha": self.repos[repo]["sha"], "status": "queued",
                          "conclusion": None, "html_url": f"https://github.com/{repo}/actions/runs/{self.n}"})

    def advance_runs(self):
        for run in self.runs:
            if run["status"] == "completed":
                continue
            repo = self.repos[run["repo"]]
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
                "id": 1000 + self.n, "files": dict(TEMPLATE_FILES), "sha": sha[:40], "history": {sha[:40]},
                "secrets": {}, "vars": {}}
            return js({}, 201)
        if m := re.fullmatch(r"/repos/([^/]+/[^/]+)/contents/(.+)", path):
            text = self.repos[m.group(1)]["files"].get(m.group(2))
            return js({"content": base64.b64encode(text.encode()).decode()}) if text else js({}, 404)
        if m := re.fullmatch(r"/repos/([^/]+/[^/]+)/commits/main", path):
            return js({"sha": self.repos[m.group(1)]["sha"]})
        if m := re.fullmatch(r"/repos/([^/]+/[^/]+)/commits/([0-9a-f]{40})", path):
            repo = self.repos.get(m.group(1))
            return js({"sha": m.group(2)}) if repo and m.group(2) in repo["history"] else js({"message": "No commit"}, 404)
        if m := re.fullmatch(r"/repos/([^/]+/[^/]+)/commits/(\w+)/check-runs", path):
            self.advance_runs()
            assert query.get("check_name") == [ks.DEPLOY_CHECK], query
            return js({"check_runs": [r for r in self.runs if (r["repo"], r["sha"]) == m.groups()]})
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
                repo["history"].add(repo["sha"])
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
                self.start_run(cmd[cmd.index("--repo") + 1])
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
        self.assertIn("Worker site-test exists, deployed from dentai-org/site-test", out)

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
        self.assertIn("not a commit of wrong-org/site-test", out)
        self.assertIn("site-test", self.world.scripts)

    def test_teardown_missing_repo_is_an_error_not_acceptance(self):
        self.world.scripts["site-test"] = "tag0"  # Worker without marker, no record, no repo
        code, out, writes = self.kick("--teardown", "site-test", "--force-orphan")
        self.assertEqual((code, writes), (1, []))
        self.assertIn("no GitHub repo", out)

    def test_worker_without_revision_marker_needs_force_orphan_to_tear_down(self):
        self.kick()
        del self.world.live["site-test"]  # deployed some other way: no marker
        code, out, writes = self.kick("--teardown", "site-test")
        self.assertEqual((code, writes), (1, []))
        self.assertIn("--force-orphan", out)
        code, out, _ = self.kick("--teardown", "site-test", "--force-orphan")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.world.scripts, {})

    def test_worker_without_revision_marker_needs_adopt_to_provision(self):
        self.kick()
        del self.world.live["site-test"]
        code, out, writes = self.kick()
        self.assertEqual((code, writes), (1, []))
        self.assertIn("✗ preflight", out)
        self.assertIn("pass --adopt", out)
        self.forget_record()
        code, out, writes = self.kick("--adopt")
        self.assertEqual(code, 0, out)
        self.assertIn("redeploying", out)  # its old green check-run does not prove what is live now
        self.assertIn("cmd gh workflow run", self.names(writes))

    def test_same_slug_from_another_org_aborts_before_any_write(self):
        self.kick()
        code, out, writes = self.kick("another-org")
        self.assertEqual((code, writes), (1, []))
        self.assertIn("✗ preflight", out)
        self.assertIn("not a commit of another-org/site-test", out)
        self.world.repos["another-org/site-test"] = dict(self.world.repos["dentai-org/site-test"], history={"e" * 40})
        code, out, writes = self.kick("another-org", "--adopt")  # adopt does not override a foreign marker
        self.assertEqual((code, writes), (1, []))
        self.assertEqual(self.world.live["site-test"][0], "dentai-org/site-test")

    def test_existing_repo_without_record_needs_adopt(self):
        self.kick()
        self.forget_record()
        code, out, writes = self.kick()
        self.assertEqual((code, writes), (1, []))
        self.assertIn("--adopt", out)
        code, out, _ = self.kick("--adopt")
        self.assertEqual(code, 0, out)
        self.assertIn("install website-dev", out)  # fake repo has no .agents/skills/website-dev

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
