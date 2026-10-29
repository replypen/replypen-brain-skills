#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml"]
# ///
"""Provision a chat-editable public website deployed by GitHub Actions to Cloudflare Workers.

repo (from replypen/site-template, with .github/workflows/deploy.yml) → ReplyPen App access →
deploy secret + variable → first `Website deploy` on main (creates the Worker) → ReplyPen
`role=website` mirror → Browser Rendering screenshots.

Every step reads live state first and only writes what is missing (the repo secret is re-set each
run: GitHub never returns its value, and re-setting converges a rotated token), so a rerun after a
failure continues where it stopped. `--dry-run` never writes: reads run when their credential is
present, writes are printed. urllib + PyYAML; shells out to git, gh, rp with a minimal environment.

Ownership: an existing Worker (and, for teardown, the whole site) is only touched when this machine's
state record names that exact account + Worker + repo. Everything else needs an explicit --adopt
(provision) or --force-orphan (teardown).
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import yaml


def _cli() -> str:
    """The ReplyPen CLI on PATH: `rp`, else `rc` (its pre-rebrand name, same binary)."""
    return "rp" if shutil.which("rp") else "rc"

CF_API = "https://api.cloudflare.com/client/v4"
GH_API = "https://api.github.com"
# GitHub's non-terminal workflow-run statuses (the list endpoint's `status` filter takes one at a time).
ACTIVE_RUN_STATUSES = ("queued", "in_progress", "waiting", "pending", "requested")
DEFAULT_ACCOUNT = "60c5feed78b351b48868d3a691184c4b"  # Cloudflare "Rootcause Sites"
WORKERS_SUBDOMAIN = "rootcause-sites"  # <worker>.rootcause-sites.workers.dev
TEMPLATE = "replypen/site-template"
REPLYPEN_APP = "replypen"
REPLYPEN_APP_ID = 4006321  # matched by id: a GitHub App rename changes its slug (was rootcause-app)
MIRROR_NAME = "site"
WORKFLOW = ".github/workflows/deploy.yml"
DEPLOY_CHECK = "Website deploy"  # the workflow's deploy job; ReplyPen waits for the same check-run
REVISION_PATH = "/replypen-revision.json"  # {"revision": "<sha>"}, written by the deploy job
DEPLOY_SECRET, ACCOUNT_VAR = "CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID"
POLL_SECONDS = 10
DEPLOY_POLLS = 72  # 12 min: build + deploy jobs, plus queueing behind a push-triggered run
LIVE_POLLS = 12  # 2 min
DISPATCH_POLLS = 6  # 1 min for a dispatched run to be listed
STOP_POLLS = 12  # teardown: 2 min for cancelled runs to stop
VIEWPORTS = {"mobile": (390, 844), "desktop": (1280, 800)}


class StepFailed(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


SLUG = re.compile(r"[a-z0-9][a-z0-9-]*")
# What a child process may see: tool plumbing, never the operator's tokens. Per-command extras are
# added explicitly (gh/git: their own config/keyring; rc: RC_*); the deploy token reaches gh on stdin only.
SAFE_ENV = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TMPDIR", "SHELL",
            "SSH_AUTH_SOCK", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "PNPM_HOME",
            "MISE_DATA_DIR", "MISE_CONFIG_DIR", "MISE_CACHE_DIR")
TOOL_ENV = {"gh": ("GH_CONFIG_DIR", "GH_HOST"), "git": ("GIT_SSH_COMMAND", "GH_CONFIG_DIR"), "rp": ("RC_",), "rc": ("RC_",)}


def child_env(cmd, extra=None):
    allowed = SAFE_ENV + TOOL_ENV.get(Path(cmd[0]).name, ())
    env = {k: v for k, v in os.environ.items()
           if k in allowed or any(a.endswith("_") and k.startswith(a) for a in allowed)}
    return env | (extra or {})


class Http:
    """Tiny urllib wrapper; returns (status, bytes). The test suite swaps it for a fake."""

    def request(self, method, url, *, headers=None, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, method=method, headers=dict(headers or {}))
        if data is not None:
            req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", "replypen-website-kickstart")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as err:
            return err.code, err.read()
        except urllib.error.URLError as err:
            return 0, str(err.reason).encode()


def run_command(cmd, *, cwd=None, env=None, stdin=None):
    # No input ⇒ /dev/null: a child (a gh prompt) must fail, never wait on the operator's terminal.
    feed = {"input": stdin} if stdin is not None else {"stdin": subprocess.DEVNULL}
    proc = subprocess.run(cmd, cwd=cwd, env=child_env(cmd, env), text=True, capture_output=True,
                          check=False, **feed)
    if proc.returncode:
        tail = (proc.stderr or proc.stdout).strip().splitlines()[-15:]
        raise StepFailed(f"`{' '.join(cmd[:4])}…` exited {proc.returncode}: " + " | ".join(tail))
    return proc.stdout


class Kickstart:
    def __init__(self, args, *, http=None, run=run_command, sleep=time.sleep, env=None):
        self.a = args
        self.http = http or Http()
        self.run = run
        self.sleep = sleep
        env = os.environ if env is None else env
        self.cf_token = env.get("CF_SITES_USER_TOKEN", "")
        self.deploy_token = env.get("CF_SITES_DEPLOY_TOKEN", "")
        self.gh_pat = env.get("GH_SITE_PROVISIONING_PAT", "")
        self.br_token = env.get("CF_BROWSER_RENDERING_TOKEN", "")
        self.br_account = env.get("CF_BROWSER_RENDERING_ACCOUNT_ID", "") or args.account
        self._gh_token = None
        self.repo_name = f"site-{args.slug}"
        self.full_name = f"{args.org}/{self.repo_name}"
        self.worker = args.worker_name or self.repo_name
        self.workers_dev = f"https://{self.worker}.{WORKERS_SUBDOMAIN}.workers.dev"
        self.live_url = args.live_url or self.workers_dev
        self.preview_template = f"https://{{branch}}-{self.worker}.{WORKERS_SUBDOMAIN}.workers.dev"
        self.out = Path(args.out).expanduser()
        self.s = {}  # facts gathered along the way, printed in the summary
        self._checkout = None
        self.secrets = [t for t in (self.cf_token, self.deploy_token, self.gh_pat, self.br_token) if len(t) >= 8]
        # This machine's record: the Worker it claimed for which repo (ownership), the repo it personalised
        # (an existing repo without it needs --adopt) and where a partial teardown parked the repo.
        self.state_path = Path(args.state_dir).expanduser() / f"{args.account}-{self.worker}.json"
        self.state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}

    # ---- plumbing -------------------------------------------------------------------------------

    def say(self, mark, step, detail):
        print(f"{mark} {step} — {self.redact(detail)}", flush=True)

    def redact(self, text):
        for secret in self.secrets:
            text = text.replace(secret, "***")
        return text

    def save_state(self, **facts):
        if self.a.dry_run:
            return
        self.state |= facts | {"account": self.a.account, "worker": self.worker, "repo": self.full_name,
                               "project": self.a.project, "tenant": self.a.tenant}
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(self.state, indent=2, sort_keys=True) + "\n")

    @property
    def form_scope(self):
        """Form endpoint path after /forms/: <project>/<tenant>, or <project> for a project-scope site."""
        return f"{self.a.project}/{self.a.tenant}" if self.a.tenant else self.a.project

    @property
    def scope_label(self):
        return f"tenant {self.a.tenant}" if self.a.tenant else f"project {self.a.project}"

    def rp_scope(self):
        """rp CLI + scope flags: the tenant's mirrors, or the project's own. `--scope project` clears a tenant
        rp would otherwise inherit (RC_TENANT, a tenant brain checkout, a tenant-bound login)."""
        return [_cli(), "--project", self.a.project] + (
            ["--tenant", self.a.tenant] if self.a.tenant else ["--scope", "project"])

    def plan(self, what):
        print(f"    (dry-run) would {what}", flush=True)

    def gh_token(self):
        return self.gh_pat or self.gh_login_token()

    def gh_login_token(self):
        if self._gh_token is None:
            try:
                token = self.run(["gh", "auth", "token"]).strip()
                if len(token) >= 8:
                    self.secrets.append(token)
            except (StepFailed, OSError):
                token = ""
            self._gh_token = token
        return self._gh_token

    def _api(self, base, token, token_env, method, path, body=None, ok=(200, 201, 204)):
        """JSON API call. Returns parsed JSON, None for 404 on GET, or None for a dry-run write/read
        without credentials (callers treat None as "unknown / not there")."""
        if method != "GET" and self.a.dry_run:
            self.plan(f"{method} {path}" + (f" {json.dumps(body, sort_keys=True)}" if body else ""))
            return None
        if not token:
            if self.a.dry_run:
                self.plan(f"{method} {path} (no {token_env}: read skipped)")
                return None
            raise StepFailed(f"{token_env} is not set (mise-env replypen/replypen.env)")
        headers = {"Authorization": f"Bearer {token}"}
        if base == GH_API:
            headers |= {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        status, raw = self.http.request(method, base + path, headers=headers, body=body)
        if status == 404 and method == "GET":
            return None
        data = json.loads(raw) if raw and raw.strip()[:1] in (b"{", b"[") else {}
        if status not in ok or (isinstance(data, dict) and data.get("success") is False):
            errors = data.get("errors") if isinstance(data, dict) else None
            msg = errors or (data.get("message") if isinstance(data, dict) else None) or raw[:300]
            raise StepFailed(f"{method} {path} → HTTP {status}: {msg}", status)
        return data

    def cf(self, method, path, body=None):
        data = self._api(CF_API, self.cf_token, "CF_SITES_USER_TOKEN", method,
                         f"/accounts/{self.a.account}{path}", body)
        return None if data is None else data.get("result")

    def gh(self, method, path, body=None, *, pat=False, ok=(200, 201, 204)):
        token = self.gh_pat if pat else self.gh_token()
        try:
            return self._api(GH_API, token, "GH_SITE_PROVISIONING_PAT" if pat else "a GitHub token",
                             method, path, body, ok)
        except StepFailed as e:
            # An org may forbid classic PATs (pro-backup does): the gh login's OAuth token still works there.
            if e.status != 403 or "personal access token (classic)" not in str(e):
                raise
            login = self.gh_login_token()
            if not login or login == token:
                raise
            return self._api(GH_API, login, "the gh login", method, path, body, ok)

    def act(self, cmd, *, stdin=None, shown=None):
        """Run a writing command, or print it on a dry run. `shown` replaces argv in the plan line."""
        if self.a.dry_run:
            self.plan("$ " + (shown or " ".join(_quote(x) for x in cmd)))
        else:
            self.run(cmd, stdin=stdin)

    def checkout(self):
        """Clone the repo once per invocation (only placeholder filling needs a working tree)."""
        if self._checkout:
            return self._checkout
        root = Path(tempfile.mkdtemp(prefix="kickstart-"))
        target = root / self.repo_name
        for _ in range(6):  # a repo generated from a template fills asynchronously
            shutil.rmtree(target, ignore_errors=True)
            self.run(["gh", "repo", "clone", self.full_name, str(target), "--", "--quiet"])
            if (target / "wrangler.jsonc").exists():
                break
            self.sleep(5)
        else:
            raise StepFailed(f"{self.full_name} has no wrangler.jsonc after cloning")
        self._checkout = target
        return target

    def cleanup(self):
        if self._checkout:
            shutil.rmtree(self._checkout.parent, ignore_errors=True)

    # ---- 0. ownership ---------------------------------------------------------------------------

    def worker_exists(self):
        """True/False, or None on a dry run without CF_SITES_USER_TOKEN."""
        scripts = self.cf("GET", "/workers/scripts")
        if scripts is None:
            return None
        return any(s.get("id") == self.worker for s in scripts)

    def record_conflict(self):
        """The repo this machine's record names for this account + Worker, when it is not ours."""
        other = self.state.get("repo")
        return other if other and other.lower() != self.full_name.lower() else None

    def owns_worker(self):
        """This machine's record (written by the run that saw the Worker free and created it) names exactly
        this account + Worker + repo. Nothing served by the Worker counts: a fork keeps history, a marker
        is content anyone can serve."""
        st = self.state
        return (st.get("account"), st.get("worker"), (st.get("repo") or "").lower()) == (
            self.a.account, self.worker, self.full_name.lower())

    def check_owner(self, override, flag, what):
        """Raise unless this machine's record proves `what` belongs to this repo, or the operator overrides.
        A record naming another repo is refused even with the override: that is this machine's own site."""
        if other := self.record_conflict():
            raise StepFailed(f"this machine created Worker {self.worker} for {other}, not {self.full_name} "
                             f"({self.state_path}); pick another slug/--worker-name, tear {other} down first, "
                             "or delete that record if it is wrong")
        if self.owns_worker():
            return f"created by this machine for {self.full_name}"
        if override:
            return f"no record on this machine, {flag}"
        raise StepFailed(f"{what}, and this machine has no record of creating it for {self.full_name} "
                         f"({self.state_path}); run from the machine that created it, or pass {flag} "
                         f"if it really is {self.full_name}'s")

    def step_preflight(self):
        """Before any write: a same-named Worker in the shared account must already be ours, since the
        first deploy would overwrite it, and this machine's record must not name another repo. The record
        is written only once step_repo has proven the repo is ours (created, adopted or our own kickstart),
        so a refused run never leaves a record a later teardown would trust."""
        if other := self.record_conflict():
            raise StepFailed(f"this machine created Worker {self.worker} for {other}, not {self.full_name} "
                             f"({self.state_path}); pick another slug/--worker-name, tear {other} down first, "
                             "or delete that record if it is wrong")
        exists = self.worker_exists()
        if exists is None:
            return f"would check Worker {self.worker} is free or ours"
        if exists:
            return f"Worker {self.worker} exists, " + self.check_owner(
                self.a.adopt, "--adopt", f"Worker {self.worker} already exists in account {self.a.account}")
        return f"Worker {self.worker} is free"

    # ---- 1. repository --------------------------------------------------------------------------

    def step_repo(self):
        repo = self.get_repo()
        if repo is None and self.a.adopt:
            if self.a.dry_run and not self.gh_token():
                return "would adopt (no GitHub token to check it exists)"
            raise StepFailed(f"--adopt: {self.full_name} does not exist")
        created = repo is None
        if created:
            owner, name = TEMPLATE.split("/")
            self.gh("POST", f"/repos/{owner}/{name}/generate",
                    {"owner": self.a.org, "name": self.repo_name, "private": True,
                     "description": f"Website {self.a.name} (Eleventy → GitHub Actions → Cloudflare Workers)"})
            if self.a.dry_run:
                self.plan(f"clone, fill placeholders (__WORKER_NAME__={self.worker}, "
                          f"__SITE_NAME__={self.a.name}, form → {self.form_scope}), push main")
                return f"would create {self.full_name} from {TEMPLATE}"
            for _ in range(6):
                repo = self.get_repo()
                if repo:
                    break
                self.sleep(5)
            else:
                raise StepFailed(f"{self.full_name} not visible after creation")
        self.s |= {"repo_id": repo["id"], "repo_url": repo["html_url"]}
        for _ in range(12):  # a repo generated from a template is empty for a few seconds
            wrangler = self.file_text("wrangler.jsonc")
            if wrangler is not None:
                break
            self.sleep(5)
        else:
            raise StepFailed(f"{self.full_name} has no wrangler.jsonc on main")
        if self.file_text(WORKFLOW) is None:
            raise StepFailed(f"{self.full_name} has no {WORKFLOW}: copy it from {TEMPLATE} onto main")
        if self.a.adopt:
            m = re.search(r'"name"\s*:\s*"([^"]+)"', wrangler)
            if not m or m.group(1) != self.worker:
                raise StepFailed(f"wrangler.jsonc name is {m.group(1) if m else 'missing'!r}, "
                                 f"expected {self.worker!r} (pass --worker-name)")
            note = "" if '"previews"' in wrangler else ' (add "previews": {} to wrangler.jsonc for branch previews)'
            if self.file_text(".agents/skills/website-dev/SKILL.md") is None:
                note += " (no coding-agent skill: install website-dev, see docs/website-context.md)"
            detail = f"adopted {self.full_name}{note}"
        elif "__WORKER_NAME__" in wrangler or "__TENANT__" in (self.file_text("_data/site.yml") or ""):
            if self.a.dry_run:
                self.plan("clone, fill placeholders, push main")
                detail = f"{self.full_name} exists, placeholders still to fill"
            else:
                self.personalise()
                detail = f"{'created' if created else 'filled'} {self.full_name} ({self.worker})"
        elif self.state.get("repo_id") == repo["id"]:
            detail = f"{self.full_name} exists, already personalised"
        else:
            raise StepFailed(f"{self.full_name} exists and is not a kickstart from this machine; "
                             "pass --adopt to take it over")
        self.save_state(repo_id=repo["id"])  # the repo is proven ours: claim the Worker before the deploy
        head = self.gh("GET", f"/repos/{self.full_name}/commits/main")
        self.s["head_sha"] = head["sha"] if head else None
        return detail + (f" @ {self.s['head_sha'][:8]}" if self.s["head_sha"] else "")

    def get_repo(self):
        """The repo, or None. A renamed (torn-down) repo still answers on its old name via a redirect;
        that is not ours, the name is free."""
        repo = self.gh("GET", f"/repos/{self.full_name}")
        return repo if repo and repo["full_name"].lower() == self.full_name.lower() else None

    def find_repo(self):
        """(repo, parked) for teardown: the live repo, or the renamed `-teardown-` one a previous
        teardown left (found via the record, or via GitHub's redirect from the old name)."""
        repo = self.gh("GET", f"/repos/{self.full_name}")
        if repo and repo["full_name"].lower() == self.full_name.lower():
            return repo, False
        parked = re.compile(re.escape(self.full_name) + r"-teardown-\d+", re.IGNORECASE)
        if repo and parked.fullmatch(repo["full_name"]):
            return repo, True
        if self.state.get("parked_repo") and not self.record_conflict():
            repo = self.gh("GET", f"/repos/{self.state['parked_repo']}")
            if repo:
                return repo, True
        return None, False

    def file_text(self, path):
        try:
            data = self.gh("GET", f"/repos/{self.full_name}/contents/{path}?ref=main")
        except StepFailed as err:  # 409 "Git Repository is empty" right after generate
            if "409" in str(err):
                return None
            raise
        return base64.b64decode(data["content"]).decode() if data and data.get("content") else None

    def personalise(self):
        root = self.checkout()
        # Slugs are validated by parse_args; the free-text name becomes a JSON-encoded (= valid YAML
        # double-quoted) scalar, replacing the template's quoted placeholder whole.
        name = json.dumps(self.a.name, ensure_ascii=False)
        # "__PROJECT__/__TENANT__" first: a project-scope site (no tenant) posts to /forms/<project>.
        values = {'"__SITE_NAME__"': name, "__SITE_NAME__": name, "__WORKER_NAME__": self.worker,
                  "__PROJECT__/__TENANT__": self.form_scope,
                  "__PROJECT__": self.a.project, "__TENANT__": self.a.tenant or ""}
        for rel in ("wrangler.jsonc", "package.json", "_data/site.yml"):
            path = root / rel
            text = path.read_text()
            for key, value in values.items():
                text = text.replace(key, value)
            path.write_text(text)
        try:
            site = yaml.safe_load((root / "_data/site.yml").read_text())
        except yaml.YAMLError as err:
            raise StepFailed(f"_data/site.yml no longer parses after filling: {err}") from err
        if site.get("name") != self.a.name:
            raise StepFailed(f"_data/site.yml name reads back as {site.get('name')!r}, not {self.a.name!r}")
        self.run(["git", "-C", str(root), "add", "-A"])
        self.run(["git", "-C", str(root), "commit", "--quiet", "-m",
                  f"Kickstart {self.a.name}: worker {self.worker}, form {self.form_scope}"])
        # This push already starts a deploy run; it fails until step_deploy sets the secret, which is fine.
        self.run(["git", "-C", str(root), "push", "--quiet", "origin", "HEAD:main"])

    # ---- 2. ReplyPen App access -----------------------------------------------------------------

    def installation(self):
        """The org's ReplyPen App installation, False when absent, None on a credential-less dry run.
        `GET /user/installations` is 403 for a classic PAT, so ask the org (gh login = org owner)."""
        data = self.gh("GET", f"/orgs/{self.a.org}/installations?per_page=100")
        if data is None:
            return None
        return next((i for i in data.get("installations", []) if i.get("app_id") == REPLYPEN_APP_ID), False)

    def step_github_app(self):
        inst = self.installation()
        if inst is None:
            return f"would add the repo to the {REPLYPEN_APP} App"
        if inst is False:
            raise StepFailed(f"no {REPLYPEN_APP} App installation on {self.a.org}: an org owner installs it once "
                             "(https://github.com/apps/replypen), then rerun")
        # The website scope needs Checks + Commit statuses read (preview readiness); an org that installed
        # the App earlier keeps the old set until an owner accepts the update, and every chat edit then
        # fails with "could not authenticate with GitHub".
        lacking = [k for k in ("checks", "statuses") if (inst.get("permissions") or {}).get(k) not in ("read", "write")]
        if lacking:
            raise StepFailed(f"{REPLYPEN_APP} on {self.a.org} lacks {', '.join(lacking)}: read; accept the pending "
                             f"permission update at https://github.com/organizations/{self.a.org}/settings/"
                             f"installations/{inst['id']}/permissions/update")
        repo_id = self.s.get("repo_id")
        if inst.get("repository_selection") == "all":
            return f"{REPLYPEN_APP} {inst['id']}: all repos"
        if repo_id is None:  # dry-run before the repo exists
            self.plan(f"PUT /user/installations/{inst['id']}/repositories/<repo id>")
            return f"{REPLYPEN_APP} {inst['id']}: would add"
        try:
            present = repo_id in self.install_repo_ids(inst["id"])
        except StepFailed as e:
            if e.status != 403:
                raise
            # No token may list or edit app repos here (org forbids classic PATs; gh's OAuth token is
            # refused by /user/installations). An owner adds the repo by hand; the mirror refresh proves it.
            return (f"{REPLYPEN_APP} {inst['id']}: unverifiable, add {self.repo_name} by hand at "
                    f"https://github.com/organizations/{self.a.org}/settings/installations/{inst['id']}")
        if present:
            return f"{REPLYPEN_APP} {inst['id']}: already"
        self.gh("PUT", f"/user/installations/{inst['id']}/repositories/{repo_id}", pat=True)
        if not self.a.dry_run and repo_id not in self.install_repo_ids(inst["id"]):
            raise StepFailed(f"{REPLYPEN_APP} {inst['id']}: repo still not listed after PUT")
        return f"{REPLYPEN_APP} {inst['id']}: {'would add' if self.a.dry_run else 'added'}"

    def install_repo_ids(self, install_id):
        ids, page = set(), 1
        while True:
            data = self.gh("GET", f"/user/installations/{install_id}/repositories?per_page=100&page={page}",
                           pat=True) or {}
            repos = data.get("repositories", [])
            ids |= {r["id"] for r in repos}
            if len(repos) < 100:
                return ids
            page += 1

    # ---- 3. deploy (GitHub Actions) -------------------------------------------------------------

    def step_deploy(self):
        """Arm the workflow's deploy job, then see main deployed: check-run `Website deploy` success on
        the head sha, the live page, and the revision marker naming that sha. The first deploy creates
        the Worker, so there is no separate create step (and no Cloudflare token on this laptop's build)."""
        if not self.deploy_token and not self.a.dry_run:
            raise StepFailed("CF_SITES_DEPLOY_TOKEN is not set (mise-env replypen/replypen.env)")
        repo = ["--repo", self.full_name]
        # The token travels on stdin only: never argv, never a log line.
        self.act(["gh", "secret", "set", DEPLOY_SECRET, *repo], stdin=self.deploy_token,
                 shown=f"gh secret set {DEPLOY_SECRET} --repo {self.full_name} < $CF_SITES_DEPLOY_TOKEN")
        self.act(["gh", "variable", "set", ACCOUNT_VAR, "--body", self.a.account, *repo])
        sha = self.s.get("head_sha")
        if self.a.dry_run and not sha:
            self.plan(f"gh workflow run deploy.yml --ref main, wait for that run, "
                      f"GET {self.live_url}/ and {REVISION_PATH}")
            return "would deploy main"
        run = self.deploy_run(sha)
        if run and run.get("status") != "completed":
            run = self.wait_check(sha, run["id"])  # a push-triggered run; it may still succeed
        if run and run.get("conclusion") == "success":
            # A green check alone is not enough: the Worker may since have been deleted or overwritten.
            try:
                live = self.probe_live(sha, polls=1)
                self.s["deploy_run"] = run.get("html_url", "-")
                return f"'{DEPLOY_CHECK}' on {sha[:8]} already succeeded; {live}"
            except StepFailed as err:
                self.say("·", "deploy", f"live site is not {sha[:8]} ({err}); redeploying")
        # None yet, one that ran before the secret existed, or a stale live site: dispatch a fresh run and
        # follow THAT run, never "any newer check": an older push run can still land a failure meanwhile.
        seen = None if self.a.dry_run else {r["id"] for r in self.dispatch_runs(sha)}
        self.act(["gh", "workflow", "run", Path(WORKFLOW).name, "--ref", "main", *repo])
        if self.a.dry_run:
            return f"would dispatch {Path(WORKFLOW).name} on main and wait for that run on {sha[:8]}"
        run = self.wait_run(self.dispatched_run(sha, seen))
        self.s["deploy_run"] = run.get("html_url", "-")
        if run.get("conclusion") != "success":
            raise StepFailed(f"dispatched {Path(WORKFLOW).name} run concluded {run.get('conclusion')}: "
                             f"{run.get('html_url')}")
        return f"dispatched run on {sha[:8]} success; {self.probe_live(sha)}"

    def deploy_run(self, sha):
        """The newest `Website deploy` check-run on `sha` (ids grow; filter=all keeps reruns)."""
        q = urllib.parse.quote(DEPLOY_CHECK)
        data = self.gh("GET", f"/repos/{self.full_name}/commits/{sha}/check-runs?check_name={q}&filter=all") or {}
        return max(data.get("check_runs") or [], key=lambda r: r["id"], default=None)

    def wait_check(self, sha, check_id):
        """Poll until check-run `check_id` completes."""
        for _ in range(DEPLOY_POLLS):
            data = self.gh("GET", f"/repos/{self.full_name}/check-runs/{check_id}") or {}
            if data.get("status") == "completed":
                return data
            self.sleep(POLL_SECONDS)
        raise StepFailed(f"'{DEPLOY_CHECK}' on {sha[:8]} still running after {DEPLOY_POLLS * POLL_SECONDS}s: "
                         f"https://github.com/{self.full_name}/actions")

    def workflow_runs(self, query=""):
        data = self.gh("GET", f"/repos/{self.full_name}/actions/workflows/{Path(WORKFLOW).name}/runs"
                              f"?per_page=100{query}") or {}
        return data.get("workflow_runs") or []

    def dispatch_runs(self, sha):
        return self.workflow_runs(f"&event=workflow_dispatch&head_sha={sha}")

    def dispatched_run(self, sha, seen):
        """Our dispatch: a workflow_dispatch run of deploy.yml on `sha` that did not exist before it. Matching
        by id (not by created_at) needs no agreement between this laptop's clock and GitHub's."""
        for _ in range(DISPATCH_POLLS):
            fresh = [r for r in self.dispatch_runs(sha) if r["id"] not in seen and r.get("head_sha") == sha]
            if fresh:
                return min(fresh, key=lambda r: r["id"])
            self.sleep(POLL_SECONDS)
        raise StepFailed(f"dispatched {Path(WORKFLOW).name} but no workflow_dispatch run on {sha[:8]} appeared "
                         f"(main moved meanwhile?): https://github.com/{self.full_name}/actions")

    def wait_run(self, run):
        for _ in range(DEPLOY_POLLS):
            if run.get("status") == "completed":
                return run
            self.sleep(POLL_SECONDS)
            run = self.gh("GET", f"/repos/{self.full_name}/actions/runs/{run['id']}") or run
        raise StepFailed(f"dispatched run not completed after {DEPLOY_POLLS * POLL_SECONDS}s: {run.get('html_url')}")

    def probe_live(self, sha, polls=LIVE_POLLS):
        needle = html.escape(self.a.name)
        last = ""
        for attempt in range(polls):
            if attempt:
                self.sleep(POLL_SECONDS)
            status, body = self.http.request("GET", self.live_url + "/")
            text = body.decode(errors="replace")
            if status != 200 or not (needle in text or self.a.name in text):
                last = f"{self.live_url}: HTTP {status}" + ("" if status != 200 else f", {self.a.name!r} not in page")
            else:
                status, raw = self.http.request("GET", self.live_url + REVISION_PATH)
                try:
                    revision = json.loads(raw).get("revision") if status == 200 else None
                except (ValueError, AttributeError):
                    revision = None
                if revision == sha:
                    return f"{self.live_url} 200, shows {self.a.name!r}, {REVISION_PATH} = {sha[:8]}"
                last = f"{self.live_url}{REVISION_PATH}: HTTP {status}, revision {revision!r}, want {sha[:8]}"
        raise StepFailed(last)

    # ---- 4. ReplyPen mirror --------------------------------------------------------------------

    def step_replypen(self):
        scope = self.rp_scope()
        want = {"git_url": f"https://github.com/{self.full_name}.git", "role": "website",
                "live_url": self.live_url, "preview_url_template": self.preview_template,
                "deploy_check": DEPLOY_CHECK, "revision_path": REVISION_PATH}
        # Bot check on by default: ReplyPen refuses form posts without a verified Turnstile token, but only
        # once the site renders the widget (a filled turnstile_sitekey), else its form would break.
        if self.has_turnstile_sitekey():
            want["form_challenge"] = "turnstile"
        try:
            repos = json.loads(self.run(scope + ["project", "repo", "ls", "-o", "json"]) or "[]")
        except (StepFailed, json.JSONDecodeError) as err:
            if not self.a.dry_run:
                raise StepFailed(f"rp project repo ls failed: {err}") from err
            repos = []
        name = self.a.mirror_name
        have = next((r for r in repos if r.get("name") == name), None)
        if have and have.get("git_url") not in (None, "", want["git_url"]):
            raise StepFailed(f"{self.scope_label} already has mirror {name!r} for {have['git_url']} (pass --mirror-name)")
        if have is None:
            cmd, verb = scope + ["project", "repo", "add", f"name={name}"], "added"
            cmd += [f"{k}={v}" for k, v in want.items()]
        else:
            drift = {k: v for k, v in want.items() if have.get(k) != v}
            cmd = scope + ["project", "repo", "set", name] + [f"{k}={v}" for k, v in drift.items()]
            verb = "updated " + ",".join(sorted(drift)) if drift else "ok"
            if not drift:
                cmd = None
        sha = self.s.get("head_sha") or "<head sha>"
        refresh = scope + ["dev", "mirror", "refresh", "--repo", name, "--expect-sha", sha]
        for c in filter(None, (cmd, refresh)):
            self.act(c)
        return f"mirror {name} {verb if not self.a.dry_run else 'would be ' + verb}; refreshed @ {sha[:12]}"

    def has_turnstile_sitekey(self):
        """Does the site render the bot-check widget? Read from main; a failed read fails the step."""
        if self.a.dry_run and not self.s.get("repo_id"):
            return True  # plan as the template ships; the repo does not exist yet
        text = self.file_text("_data/site.yml")
        if text is None:
            return False  # not our template (an adopted Astro site…): leave the mirror's form_challenge as it is
        site = yaml.safe_load(text) or {}
        return bool(str(site.get("turnstile_sitekey") or "").strip())

    # ---- 5. screenshots -------------------------------------------------------------------------

    def step_screenshots(self):
        paths = []
        for label, (w, h) in VIEWPORTS.items():
            path = self.out / f"{self.a.slug}-{label}.png"
            body = {"url": self.live_url + "/", "viewport": {"width": w, "height": h},
                    "screenshotOptions": {"fullPage": True}, "gotoOptions": {"waitUntil": "networkidle0"}}
            if self.a.dry_run:
                self.plan(f"POST /accounts/{self.br_account}/browser-rendering/screenshot → {path}")
                continue
            if not self.br_token:
                raise StepFailed("CF_BROWSER_RENDERING_TOKEN is not set")
            for _ in range(6):  # Browser Rendering free tier rate-limits back-to-back calls (429)
                status, png = self.http.request(
                    "POST", f"{CF_API}/accounts/{self.br_account}/browser-rendering/screenshot",
                    headers={"Authorization": f"Bearer {self.br_token}"}, body=body)
                if status != 429:
                    break
                self.sleep(POLL_SECONDS)
            if status != 200 or not png.startswith(b"\x89PNG"):
                raise StepFailed(f"screenshot {label}: HTTP {status} {png[:200]!r}")
            self.out.mkdir(parents=True, exist_ok=True)
            path.write_bytes(png)
            paths.append(str(path))
        self.s["screenshots"] = paths
        return ", ".join(paths) if paths else "would save mobile + desktop PNGs"

    # ---- teardown (throwaway sites only) --------------------------------------------------------

    def teardown(self):
        """Undo a kickstart: stop deploys, then rp mirror row, Worker, App access + deploy secret. Ownership
        (this machine's record, or --force-orphan) is proven before the first write. Neither our gh login nor the PAT may delete repos, so the repo is renamed out of
        the way (the slug is free again) and archived; the delete command is printed. Rerunnable from any
        partial failure (record + redirect + idempotent deletes)."""
        if self.a.teardown != self.worker:
            print(f"✗ teardown — pass --teardown {self.worker} to confirm", flush=True)
            return 1
        try:
            plan = self.teardown_preflight()
        except StepFailed as err:
            self.say("✗", "preflight", f"{err} — nothing was changed")
            return 1
        self.say("·" if self.a.dry_run else "✓", "preflight", plan["detail"])
        # Gate: a queued deploy run still holds the secret and would recreate the Worker after its delete.
        try:
            detail = self.teardown_actions(plan["repo"])
            self.say("·" if self.a.dry_run else "✓", "deploys", ("would: " if self.a.dry_run else "") + detail)
        except StepFailed as err:
            self.say("✗", "deploys", f"{err} — nothing else was changed; rerun the same command")
            return 1
        steps = (("ReplyPen mirror", lambda: self.teardown_replypen()),
                 ("cloudflare", lambda: self.teardown_cloudflare(plan["worker"])),
                 ("github", lambda: self.teardown_github(plan["repo"], plan["parked"])))
        ok = True
        for label, fn in steps:
            try:
                detail = fn()
                self.say("·" if self.a.dry_run else "✓", label, ("would: " if self.a.dry_run else "") + detail)
            except StepFailed as err:
                self.say("✗", label, f"{err} (rerun the same command to resume)")
                ok = False
        if ok and not self.a.dry_run:
            self.state_path.unlink(missing_ok=True)
        return 0 if ok else 1

    def teardown_preflight(self):
        exists = self.worker_exists()
        owner = self.check_owner(self.a.force_orphan, "--force-orphan",
                                 f"Worker {self.worker} " + ("exists" if exists else "is already deleted"))
        repo, parked = self.find_repo()
        if repo is None:
            raise StepFailed(f"no GitHub repo {self.full_name} (nor a teardown-renamed one): "
                             "wrong org/slug, or already fully torn down")
        where = f"repo {repo['full_name']}" + (" (renamed by an earlier teardown)" if parked else "")
        worker = f"Worker {self.worker} " + ("exists" if exists else "already deleted")
        return {"worker": exists, "repo": repo, "parked": parked, "detail": f"{worker}, {owner}; {where}"}

    def teardown_actions(self, repo):
        """Disable deploy.yml, cancel every queued/in-progress run of it and wait (bounded) until none is
        active. Archived ⇒ Actions are off already."""
        if repo.get("archived"):
            return "repo archived, Actions off"
        base = f"/repos/{repo['full_name']}/actions"
        try:
            self.gh("PUT", f"{base}/workflows/{Path(WORKFLOW).name}/disable")
        except StepFailed as err:
            if err.status != 404:
                raise
            return f"no {WORKFLOW}"
        cancelled = set()
        for attempt in range(STOP_POLLS):
            # Filter server-side per non-terminal status: an unfiltered list pages old active runs out of view.
            active = []
            for status in ACTIVE_RUN_STATUSES:
                data = self.gh("GET", f"{base}/workflows/{Path(WORKFLOW).name}/runs?status={status}&per_page=100") or {}
                active += [r for r in data.get("workflow_runs") or [] if r.get("status") != "completed"]
            if not active:
                verb = "disable" if self.a.dry_run else "disabled"
                return f"{verb} {Path(WORKFLOW).name}, " + (f"cancelled {len(cancelled)} run(s)" if cancelled
                                                            else "no active runs")
            for run in active:
                if run["id"] not in cancelled:
                    try:
                        self.gh("POST", f"{base}/runs/{run['id']}/cancel", ok=(202,))
                    except StepFailed as err:
                        if err.status != 409:  # 409: it completed meanwhile
                            raise
                    cancelled.add(run["id"])
            if self.a.dry_run:
                return f"disable {Path(WORKFLOW).name}, cancel {len(active)} active run(s)"
            self.sleep(POLL_SECONDS)
        raise StepFailed(f"deploy runs still active after {STOP_POLLS * POLL_SECONDS}s: "
                         + ", ".join(r.get("html_url", str(r["id"])) for r in active))

    def cf_delete(self, path):
        """DELETE with retries; 404 = already gone."""
        for attempt in range(3):
            try:
                self.cf("DELETE", path)
                return
            except StepFailed as err:
                if err.status == 404:
                    return
                if attempt == 2 or (err.status and err.status < 500 and err.status != 429):
                    raise
                self.sleep(POLL_SECONDS)

    def teardown_replypen(self):
        scope = self.rp_scope()
        repos = json.loads(self.run(scope + ["project", "repo", "ls", "-o", "json"]) or "[]")
        git_url = f"https://github.com/{self.full_name}.git"
        have = next((r for r in repos if r.get("name") == self.a.mirror_name and r.get("git_url") == git_url), None)
        if not have:
            return f"no mirror {self.a.mirror_name} for {git_url}"
        self.act(scope + ["project", "repo", "rm", self.a.mirror_name])
        return f"removed mirror {self.a.mirror_name}"

    def teardown_cloudflare(self, exists):
        if not exists:
            return f"Worker {self.worker} already gone"
        self.cf_delete(f"/workers/scripts/{self.worker}?force=true")
        return f"deleted Worker {self.worker}"

    def teardown_github(self, repo, parked):
        parts = []
        inst = self.installation()
        if inst and inst.get("repository_selection") != "all" and repo["id"] in self.install_repo_ids(inst["id"]):
            self.gh("DELETE", f"/user/installations/{inst['id']}/repositories/{repo['id']}", pat=True)
            parts.append(f"removed from {REPLYPEN_APP}")
        if not repo.get("archived"):  # an archived repo refuses writes; its runs are off anyway
            try:
                self.gh("DELETE", f"/repos/{repo['full_name']}/actions/secrets/{DEPLOY_SECRET}")
                parts.append(f"deleted secret {DEPLOY_SECRET}")
            except StepFailed as err:
                if err.status != 404:
                    raise
        name = repo["full_name"]
        if not parked:
            name = f"{self.full_name}-teardown-{time.strftime('%Y%m%d%H%M%S')}"
            self.gh("PATCH", f"/repos/{self.full_name}",
                    {"name": name.split("/", 1)[1], "description": "kickstart teardown — safe to delete"})
            self.save_state(parked_repo=name)
        if not repo.get("archived") or not parked:
            self.gh("PATCH", f"/repos/{name}", {"archived": True})
        parts.append(f"parked as {name} (archived); delete: gh auth refresh -s delete_repo && "
                     f"gh repo delete {name} --yes")
        return "; ".join(parts)

    # ---- driver ---------------------------------------------------------------------------------

    STEPS = (("preflight", "step_preflight"), ("repo", "step_repo"), ("github app", "step_github_app"),
             ("deploy", "step_deploy"), ("ReplyPen mirror", "step_replypen"), ("screenshots", "step_screenshots"))

    def main(self):
        ok = True
        try:
            for label, method in self.STEPS:
                try:
                    detail = getattr(self, method)()
                except StepFailed as err:
                    self.say("✗", label, str(err))
                    ok = False
                    break
                self.say("·" if self.a.dry_run else "✓", label, detail)
        finally:
            self.cleanup()
        self.summary(ok)
        return 0 if ok else 1

    def summary(self, ok):
        s = self.s
        chat = (f"Pas op de website de openingsuren aan: maandag tot vrijdag van 9 tot 18 uur, "
                f"en vul het telefoonnummer van {self.a.name} in.")
        rows = [
            ("result", "dry-run" if self.a.dry_run else ("ready" if ok else "stopped; fix and rerun the same command")),
            ("repo", s.get("repo_url", f"https://github.com/{self.full_name}")),
            ("live", self.live_url),
            ("preview", self.preview_template),
            ("deploy run", s.get("deploy_run", "-")),
            ("screenshots", ", ".join(s.get("screenshots", [])) or "-"),
            ("try in chat", f'"{chat}"'),
            ("or from cli", " ".join(["rp"] + self.rp_scope()[1:] + ["project", "chat", "send", "--lane", "setup", f'"{chat}"'])),
        ]
        print("\n── website kickstart " + "─" * 40)
        for key, value in rows:
            print(f"{key:>13}  {value}")


def _quote(arg):
    return arg if re.fullmatch(r"[\w@%+=:,./{}<>-]+", arg) else "'" + arg.replace("'", "'\\''") + "'"


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("org", help="GitHub org that owns the site repo (dentai-org, kampadmin, …)")
    p.add_argument("slug", help="site slug: repo + Worker become site-<slug>")
    p.add_argument("--project", required=True, help="ReplyPen project (dentai, kampadmin, …)")
    scope = p.add_mutually_exclusive_group(required=True)
    scope.add_argument("--tenant", help="ReplyPen tenant slug: the tenant's site (form → /forms/<project>/<tenant>)")
    scope.add_argument("--project-scope", action="store_true",
                       help="the project's own site, no tenant: project mirror, form → /forms/<project>")
    p.add_argument("--name", help="display name for _data/site.yml (default: title-cased slug)")
    p.add_argument("--worker-name", help="Worker name (default: site-<slug>)")
    p.add_argument("--account", default=DEFAULT_ACCOUNT, help="Cloudflare account id (default: Rootcause Sites)")
    p.add_argument("--live-url", help="mirror live_url (default: the workers.dev URL; set the real domain after DNS)")
    p.add_argument("--adopt", action="store_true",
                   help="existing repo: skip creation and placeholder filling; accept a same-named Worker "
                        "this machine has no record of creating")
    p.add_argument("--mirror-name", default=MIRROR_NAME, help="ReplyPen mirror name (default: site)")
    p.add_argument("--teardown", metavar="WORKER", help="undo a throwaway kickstart; value must equal the Worker name")
    p.add_argument("--force-orphan", action="store_true",
                   help="teardown: proceed without this machine's record of creating the site")
    p.add_argument("--state-dir", default="~/.local/state/website-kickstart",
                   help="kickstart records (Worker ownership, personalised repo, teardown resume point)")
    p.add_argument("--dry-run", action="store_true", help="read live state, print writes, change nothing")
    p.add_argument("--out", default="~/Downloads", help="where screenshots go")
    args = p.parse_args(argv)
    for flag in ("slug", "project", "tenant", "worker_name", "mirror_name"):
        value = getattr(args, flag)
        if value is not None and not SLUG.fullmatch(value):
            p.error(f"--{flag.replace('_', '-')} must be lowercase letters, digits and dashes: {value!r}")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*", args.org):
        p.error(f"invalid GitHub org {args.org!r}")
    args.name = args.name or args.slug.replace("-", " ").title()
    return args


if __name__ == "__main__":
    ks = Kickstart(parse_args())
    sys.exit(ks.teardown() if ks.a.teardown else ks.main())
