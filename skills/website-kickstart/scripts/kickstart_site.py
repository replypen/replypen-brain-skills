#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml"]
# ///
"""Provision a chat-editable public website on Cloudflare Workers Builds in one run.

repo (from replypen/site-template) → GitHub App access → Worker + Workers Builds triggers →
first production build → ReplyPen `role=website` mirror → Browser Rendering screenshots.

Every step reads live state first and only writes what is missing, so a rerun after a failure
continues where it stopped. `--dry-run` never writes: reads run when their credential is present,
writes are printed. urllib + PyYAML; shells out to git, pnpm, rp with a minimal environment.

Ownership: a Worker is only touched when its Workers Builds connection points at <org>/site-<slug>,
or when this machine's kickstart record (--state-dir) says we created it. Everything else needs an
explicit --adopt (provision) or --force-orphan (teardown).
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
DEFAULT_ACCOUNT = "60c5feed78b351b48868d3a691184c4b"  # Cloudflare "Rootcause Sites"
WORKERS_SUBDOMAIN = "rootcause-sites"  # <worker>.rootcause-sites.workers.dev
TEMPLATE = "replypen/site-template"
APP_SLUGS = ("rootcause-app", "cloudflare-workers-and-pages")
MIRROR_NAME = "site"
DEPLOY_CHECK = "Workers Builds"
POLL_SECONDS = 10
BUILD_POLLS = 60  # 10 min
LIVE_POLLS = 12  # 2 min
VIEWPORTS = {"mobile": (390, 844), "desktop": (1280, 800)}
TRIGGER_COMPARE = ("build_command", "deploy_command", "root_directory", "branch_includes",
                   "branch_excludes", "build_caching_enabled", "build_token_uuid")


class StepFailed(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


SLUG = re.compile(r"[a-z0-9][a-z0-9-]*")
# What a child process may see: tool plumbing, never the operator's tokens. Per-command extras are
# added explicitly (wrangler: its Cloudflare token; gh/git: their own config/keyring; rc: RC_*).
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


def run_command(cmd, *, cwd=None, env=None):
    proc = subprocess.run(cmd, cwd=cwd, env=child_env(cmd, env), text=True, capture_output=True, check=False)
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
        self.gh_pat = env.get("GH_SITE_PROVISIONING_PAT", "")
        self.br_token = env.get("CF_BROWSER_RENDERING_TOKEN", "")
        self.br_account = env.get("CF_BROWSER_RENDERING_ACCOUNT_ID", "") or args.account
        self._gh_token = None
        self.repo_name = f"site-{args.slug}"
        self.full_name = f"{args.org}/{self.repo_name}"
        self.worker = args.worker_name or self.repo_name
        self.live_url = args.live_url or f"https://{self.worker}.{WORKERS_SUBDOMAIN}.workers.dev"
        self.preview_template = f"https://{{branch}}-{self.worker}.{WORKERS_SUBDOMAIN}.workers.dev"
        self.out = Path(args.out).expanduser()
        self.s = {}  # facts gathered along the way, printed in the summary
        self._checkout = None
        self.secrets = [t for t in (self.cf_token, self.gh_pat, self.br_token) if len(t) >= 8]
        # This machine's record of what it provisioned: the ownership proof for an unbound Worker and
        # the resume point for a partial teardown (Cloudflare has no list-connections endpoint).
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

    def plan(self, what):
        print(f"    (dry-run) would {what}", flush=True)

    def gh_token(self):
        if self._gh_token is None:
            token = self.gh_pat
            if not token:
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

    def gh(self, method, path, body=None, *, pat=False):
        token = self.gh_pat if pat else self.gh_token()
        return self._api(GH_API, token, "GH_SITE_PROVISIONING_PAT" if pat else "a GitHub token",
                         method, path, body)

    def checkout(self):
        """Clone the repo once per invocation (needed to personalise and for the first deploy)."""
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

    def local_env(self, cwd):
        # A fresh clone's mise.toml is untrusted; trust exactly this checkout for pnpm/node.
        return {"MISE_TRUSTED_CONFIG_PATHS": f"{cwd}:{Path(cwd).resolve()}"}

    def cleanup(self):
        if self._checkout:
            shutil.rmtree(self._checkout.parent, ignore_errors=True)

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
                     "description": f"Website {self.a.name} (Eleventy → Cloudflare Workers Builds)"})
            if self.a.dry_run:
                self.plan(f"clone, fill placeholders (__WORKER_NAME__={self.worker}, "
                          f"__SITE_NAME__={self.a.name}, form → {self.a.project}/{self.a.tenant}), push main")
                return f"would create {self.full_name} from {TEMPLATE}"
            for _ in range(6):
                repo = self.get_repo()
                if repo:
                    break
                self.sleep(5)
            else:
                raise StepFailed(f"{self.full_name} not visible after creation")
        self.s |= {"repo_id": repo["id"], "owner_id": repo["owner"]["id"], "repo_url": repo["html_url"]}
        if created:
            self.save_state(repo_id=repo["id"])
        for _ in range(12):  # a repo generated from a template is empty for a few seconds
            wrangler = self.file_text("wrangler.jsonc")
            if wrangler is not None:
                break
            self.sleep(5)
        else:
            raise StepFailed(f"{self.full_name} has no wrangler.jsonc on main")
        if self.a.adopt:
            m = re.search(r'"name"\s*:\s*"([^"]+)"', wrangler)
            if not m or m.group(1) != self.worker:
                raise StepFailed(f"wrangler.jsonc name is {m.group(1) if m else 'missing'!r}, "
                                 f"expected {self.worker!r} (pass --worker-name)")
            note = "" if '"previews"' in wrangler else ' (add "previews": {} to wrangler.jsonc for branch previews)'
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
                             "pass --adopt to take it over (its triggers will be converged)")
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
        if self.state.get("parked_repo"):
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
        values = {'"__SITE_NAME__"': name, "__SITE_NAME__": name, "__WORKER_NAME__": self.worker,
                  "__PROJECT__": self.a.project, "__TENANT__": self.a.tenant}
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
                  f"Kickstart {self.a.name}: worker {self.worker}, form {self.a.project}/{self.a.tenant}"])
        self.run(["git", "-C", str(root), "push", "--quiet", "origin", "HEAD:main"])

    # ---- 2. GitHub App access -------------------------------------------------------------------

    def installations(self):
        """{app_slug: installation} on the org. `GET /user/installations` is 403 for a classic PAT, so ask
        the org (gh login = org owner); the PAT then manages repo access per installation."""
        data = self.gh("GET", f"/orgs/{self.a.org}/installations?per_page=100")
        if data is None:
            return None
        return {i["app_slug"]: i for i in data.get("installations", []) if i["app_slug"] in APP_SLUGS}

    def step_github_apps(self):
        installs = self.installations()
        if installs is None:
            return "would add the repo to " + " + ".join(APP_SLUGS)
        missing = [slug for slug in APP_SLUGS if slug not in installs]
        if missing:
            raise StepFailed(f"no {', '.join(missing)} installation on {self.a.org}: install it once in the "
                             "browser (Cloudflare: Workers & Pages → any Worker → Settings → Builds → Connect)")
        repo_id = self.s.get("repo_id")
        parts = []
        for slug in APP_SLUGS:
            inst = installs[slug]
            self.s[f"install_{slug}"] = inst["id"]
            if inst.get("repository_selection") == "all":
                parts.append(f"{slug} {inst['id']}: all repos")
                continue
            if repo_id is None:  # dry-run before the repo exists
                self.plan(f"PUT /user/installations/{inst['id']}/repositories/<repo id>")
                parts.append(f"{slug} {inst['id']}: would add")
                continue
            if repo_id in self.install_repo_ids(inst["id"]):
                parts.append(f"{slug} {inst['id']}: already")
                continue
            self.gh("PUT", f"/user/installations/{inst['id']}/repositories/{repo_id}", pat=True)
            if not self.a.dry_run and repo_id not in self.install_repo_ids(inst["id"]):
                raise StepFailed(f"{slug} {inst['id']}: repo still not listed after PUT")
            parts.append(f"{slug} {inst['id']}: {'would add' if self.a.dry_run else 'added'}")
        return "; ".join(parts)

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

    # ---- 3. Worker + Workers Builds -------------------------------------------------------------

    def worker_tag(self):
        scripts = self.cf("GET", "/workers/scripts")
        return next((s["tag"] for s in scripts or [] if s.get("id") == self.worker), None)

    def bindings(self, tag):
        """The repos this Worker builds from, as `org/repo` per Workers Builds trigger connection."""
        triggers = self.cf("GET", f"/builds/workers/{tag}/triggers") or []
        conns = [t.get("repo_connection") or {} for t in triggers]
        return triggers, {f"{c.get('provider_account_name')}/{c.get('repo_name')}" for c in conns if c}

    def check_owner(self, tag, override, flag):
        """Raise unless Worker `tag` belongs to this org/repo: its connection points here, or it is
        unbound and this machine's kickstart record created it, or `override` was given."""
        _, bound = self.bindings(tag)
        foreign = {b for b in bound if b.lower() != self.full_name.lower()}
        if foreign:
            raise StepFailed(f"Worker {self.worker} builds from {', '.join(sorted(foreign))}, not "
                             f"{self.full_name}; refusing to touch it (pick another --worker-name/slug)")
        if bound:
            return f"bound to {self.full_name}"
        if self.state.get("tag") == tag and self.state.get("repo", "").lower() == self.full_name.lower():
            return "unbound, created by this machine's kickstart"
        if override:
            return f"unbound, {flag}"
        raise StepFailed(f"Worker {self.worker} exists without a Git connection and no kickstart record "
                         f"({self.state_path}); pass {flag} if it is really {self.full_name}'s")

    def step_preflight(self):
        """Before any write: a same-named Worker in the shared account must already be ours."""
        tag = self.worker_tag()
        if tag is None:
            return f"Worker {self.worker} is free"
        return f"Worker {self.worker} exists, {self.check_owner(tag, self.a.adopt, '--adopt')}"

    def step_worker(self):
        tag = self.worker_tag()
        detail = "exists"
        if tag is not None:
            detail = self.check_owner(tag, self.a.adopt, "--adopt")
            self.save_state(tag=tag)  # accepted (bound, recorded or adopted): reusable on rerun
        else:
            if self.a.dry_run:
                self.plan(f"bash build.sh && pnpm dlx wrangler@4 deploy (creates Worker {self.worker})")
                return f"would create Worker {self.worker} by a first wrangler deploy"
            root = self.checkout()
            self.run(["bash", "build.sh"], cwd=root, env=self.local_env(root))
            self.run(["pnpm", "dlx", "wrangler@4", "deploy"], cwd=root,  # the only step that gets a CF token
                     env=self.local_env(root) | {"CLOUDFLARE_API_TOKEN": self.cf_token,
                                                 "CLOUDFLARE_ACCOUNT_ID": self.a.account})
            tag = self.worker_tag()
            if tag is None:
                raise StepFailed(f"wrangler deployed but GET /workers/scripts has no {self.worker}")
            self.save_state(tag=tag)
            detail = "created by wrangler deploy"
        self.s["worker_tag"] = tag
        return f"{self.worker} tag {tag} ({detail})"

    def step_builds(self):
        tag = self.s.get("worker_tag")
        try:
            conn = self.cf("PUT", "/builds/repos/connections", {
                "provider_type": "github", "provider_account_id": str(self.s.get("owner_id", "<owner id>")),
                "provider_account_name": self.a.org, "repo_id": str(self.s.get("repo_id", "<repo id>")),
                "repo_name": self.repo_name})
        except StepFailed as err:
            if "8000008" in str(err):  # "This project is disconnected from your Git account"
                raise StepFailed(f"Cloudflare is not linked to GitHub org {self.a.org}: link the GitHub org to the "
                                 "Cloudflare account once: Workers & Pages → Create → Import a repository → "
                                 f"Add GitHub account → {self.a.org}; then rerun (the Worker is reused)",
                                 err.status) from err
            raise
        conn_uuid = (conn or {}).get("repo_connection_uuid", "<repo_connection_uuid>")
        if conn:
            self.save_state(tag=tag, connections=sorted(set(self.state.get("connections", [])) | {conn_uuid}))
        existing = (self.cf("GET", f"/builds/workers/{tag}/triggers") if tag else None) or []
        token = self.build_token(existing)
        triggers, parts = [], []
        for kind, want in self.desired_triggers(tag or "<worker tag>", conn_uuid, token).items():
            have = next((t for t in existing if self.trigger_kind(t) == kind), None)
            if have is None:
                result = self.cf("POST", "/builds/triggers", want)
                parts.append(f"{kind} created")
            else:
                drift = {k: want[k] for k in TRIGGER_COMPARE if have.get(k) != want[k]}
                if (have.get("repo_connection") or {}).get("repo_connection_uuid") not in (None, conn_uuid):
                    drift["repo_connection_uuid"] = conn_uuid
                result = self.cf("PATCH", f"/builds/triggers/{have['trigger_uuid']}", drift) if drift else have
                parts.append(f"{kind} {'updated ' + ','.join(sorted(drift)) if drift else 'ok'}")
            triggers.append(result or want)
            self.s[f"trigger_{kind}"] = (result or {}).get("trigger_uuid", "<dry-run>")
        if not self.a.dry_run:
            self.out.mkdir(parents=True, exist_ok=True)
            path = self.out / f"{self.a.slug}-triggers.json"
            path.write_text(json.dumps(triggers, indent=2, sort_keys=True) + "\n")
            self.s["triggers_json"] = str(path)
        return f"connection {conn_uuid}; build token {token}; " + "; ".join(parts)

    def build_token(self, existing):
        """The account's build token: --build-token, else the one this Worker's triggers use, else the
        only/first one Cloudflare lists (Workers Builds creates one when Builds is first connected)."""
        if self.a.build_token:
            return self.a.build_token
        for t in existing:
            if t.get("build_token_uuid"):
                return t["build_token_uuid"]
        tokens = self.cf("GET", "/builds/tokens")
        if tokens is None:
            return "<build_token_uuid>"
        if not tokens:
            raise StepFailed("no Workers Builds token on the account: connect any Worker to Git once in the "
                             "dashboard (Settings → Builds), or pass --build-token")
        return tokens[0]["build_token_uuid"]

    @staticmethod
    def trigger_kind(trigger):
        return "production" if trigger.get("branch_includes") == ["main"] else "preview"

    def desired_triggers(self, tag, conn_uuid, token):
        common = {"external_script_id": tag, "repo_connection_uuid": conn_uuid, "build_token_uuid": token,
                  "build_command": "bash build.sh", "root_directory": "/", "path_includes": ["*"],
                  "path_excludes": [], "build_caching_enabled": True}
        return {
            "production": common | {"trigger_name": "Deploy default branch", "branch_includes": ["main"],
                                    "branch_excludes": [], "deploy_command": "npx wrangler@4 deploy"},
            # `wrangler preview` = the dashboard's non-production builds: https://<branch>-<worker>.<sub>.workers.dev
            "preview": common | {"trigger_name": "Deploy non-production branches", "branch_includes": ["*"],
                                 "branch_excludes": ["main"], "deploy_command": "npx wrangler@4 preview"},
        }

    # ---- 4. first production build --------------------------------------------------------------

    def step_build(self):
        tag, sha, trigger = self.s.get("worker_tag"), self.s.get("head_sha"), self.s.get("trigger_production")
        if self.a.dry_run and (not tag or trigger in (None, "<dry-run>")):
            self.plan("POST /builds/triggers/<production>/builds {branch: main}, poll until success, "
                      f"check-run 'Workers Builds: {self.worker}', GET {self.live_url}")
            return "would build main"
        build = self.build_for(tag, sha)
        if build and build.get("build_outcome") == "success":
            detail = f"build {build['build_uuid']} for {sha[:8]} already succeeded"
        else:
            if not build or build.get("status") == "stopped":
                started = self.cf("POST", f"/builds/triggers/{trigger}/builds", {"branch": "main", "commit_hash": sha})
                if started is None:
                    return "would trigger a main build"
                build = {"build_uuid": started["build_uuid"]}
            uuid = build["build_uuid"]
            for _ in range(BUILD_POLLS):
                build = next((b for b in self.cf("GET", f"/builds/workers/{tag}/builds?per_page=20") or []
                              if b.get("build_uuid") == uuid), build)
                if build.get("status") == "stopped":
                    break
                self.sleep(POLL_SECONDS)
            else:
                raise StepFailed(f"build {uuid} still {build.get('status')} after {BUILD_POLLS * POLL_SECONDS}s")
            if build.get("build_outcome") != "success":
                logs = self.cf("GET", f"/builds/builds/{uuid}/logs")
                path = self.out / f"{self.a.slug}-build-{uuid}.log.json"
                self.out.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(logs, indent=2) + "\n")
                raise StepFailed(f"build {uuid} outcome {build.get('build_outcome')}; logs: {path}")
            detail = f"build {uuid} success"
        self.s["build_uuid"] = build["build_uuid"]
        return f"{detail}; {self.check_run(sha)}; {self.probe_live()}"

    def build_for(self, tag, sha):
        builds = self.cf("GET", f"/builds/workers/{tag}/builds?per_page=20") or []
        matches = [b for b in builds if (b.get("build_trigger_metadata") or {}).get("commit_hash") == sha
                   and (b.get("build_trigger_metadata") or {}).get("branch") == "main"]
        return max(matches, key=lambda b: b.get("created_on") or "", default=None)

    def check_run(self, sha):
        name = f"Workers Builds: {self.worker}"
        q = urllib.parse.quote(name)
        for _ in range(3):
            runs = (self.gh("GET", f"/repos/{self.full_name}/commits/{sha}/check-runs?check_name={q}") or {})
            run = next(iter(runs.get("check_runs") or []), None)
            if run and run.get("status") == "completed":
                if run.get("conclusion") != "success":
                    raise StepFailed(f"check-run '{name}' concluded {run.get('conclusion')}")
                return f"check-run '{name}' success"
            self.sleep(POLL_SECONDS)
        # Builds started over the API may not post a check-run; the PR loop relies on push builds.
        return f"no check-run '{name}' on {sha[:8]} yet (API builds may not post one; next push will)"

    def probe_live(self):
        needle = html.escape(self.a.name)
        last = ""
        for _ in range(LIVE_POLLS):
            status, body = self.http.request("GET", self.live_url + "/")
            text = body.decode(errors="replace")
            if status == 200 and (needle in text or self.a.name in text):
                return f"{self.live_url} 200, shows {self.a.name!r}"
            last = f"HTTP {status}" + ("" if status != 200 else f", {self.a.name!r} not in page")
            self.sleep(POLL_SECONDS)
        raise StepFailed(f"{self.live_url}: {last}")

    # ---- 5. ReplyPen mirror --------------------------------------------------------------------

    def step_replypen(self):
        scope = [_cli(), "--project", self.a.project, "--tenant", self.a.tenant]
        want = {"git_url": f"https://github.com/{self.full_name}.git", "role": "website",
                "live_url": self.live_url, "preview_url_template": self.preview_template,
                "deploy_check": DEPLOY_CHECK}
        try:
            repos = json.loads(self.run(scope + ["project", "repo", "ls", "-o", "json"]) or "[]")
        except (StepFailed, json.JSONDecodeError) as err:
            if not self.a.dry_run:
                raise StepFailed(f"rp project repo ls failed: {err}") from err
            repos = []
        name = self.a.mirror_name
        have = next((r for r in repos if r.get("name") == name), None)
        if have and have.get("git_url") not in (None, "", want["git_url"]):
            raise StepFailed(f"tenant already has mirror {name!r} for {have['git_url']} (pass --mirror-name)")
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
            if self.a.dry_run:
                self.plan("$ " + " ".join(_quote(x) for x in c))
            else:
                self.run(c)
        return f"mirror {name} {verb if not self.a.dry_run else 'would be ' + verb}; refreshed @ {sha[:12]}"

    # ---- 6. screenshots -------------------------------------------------------------------------

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
        """Undo a kickstart: rp mirror row, Cloudflare triggers + repo connections + Worker, App access.
        Ownership is proven before the first delete. Neither our gh login nor the PAT may delete repos,
        so the repo is renamed out of the way (the slug is free again) and archived; the delete command
        is printed. Rerunnable from any partial failure (record + redirect + idempotent deletes)."""
        if self.a.teardown != self.worker:
            print(f"✗ teardown — pass --teardown {self.worker} to confirm", flush=True)
            return 1
        try:
            plan = self.teardown_preflight()
        except StepFailed as err:
            self.say("✗", "preflight", f"{err} — nothing was changed")
            return 1
        self.say("·" if self.a.dry_run else "✓", "preflight", plan["detail"])
        steps = (("ReplyPen mirror", lambda: self.teardown_replypen()),
                 ("cloudflare", lambda: self.teardown_cloudflare(plan["tag"])),
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
        tag = self.worker_tag()
        owner = self.check_owner(tag, self.a.force_orphan, "--force-orphan") if tag else "already deleted"
        repo, parked = self.find_repo()
        if repo is None:
            raise StepFailed(f"no GitHub repo {self.full_name} (nor a teardown-renamed one): "
                             "wrong org/slug, or already fully torn down")
        where = f"repo {repo['full_name']}" + (" (renamed by an earlier teardown)" if parked else "")
        return {"tag": tag, "repo": repo, "parked": parked, "detail": f"Worker {self.worker} {owner}; {where}"}

    def act(self, cmd):
        if self.a.dry_run:
            self.plan("$ " + " ".join(_quote(x) for x in cmd))
        else:
            self.run(cmd)

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
        scope = [_cli(), "--project", self.a.project, "--tenant", self.a.tenant]
        repos = json.loads(self.run(scope + ["project", "repo", "ls", "-o", "json"]) or "[]")
        git_url = f"https://github.com/{self.full_name}.git"
        have = next((r for r in repos if r.get("name") == self.a.mirror_name and r.get("git_url") == git_url), None)
        if not have:
            return f"no mirror {self.a.mirror_name} for {git_url}"
        self.act(scope + ["project", "repo", "rm", self.a.mirror_name])
        return f"removed mirror {self.a.mirror_name}"

    def teardown_cloudflare(self, tag):
        triggers = self.bindings(tag)[0] if tag else []
        conns = set(self.state.get("connections", []))
        conns |= {(t.get("repo_connection") or {}).get("repo_connection_uuid") for t in triggers} - {None}
        # Record what preflight proved before the first delete: once the triggers are gone, the Worker
        # is unbound and these ids are the only way back to the connections (no list endpoint).
        self.save_state(**({"tag": tag} if tag else {}), connections=sorted(conns))
        for t in triggers:
            self.cf_delete(f"/builds/triggers/{t['trigger_uuid']}")
        for uuid in sorted(conns):
            self.cf_delete(f"/builds/repos/connections/{uuid}")
            self.save_state(connections=sorted(set(self.state.get("connections", [])) - {uuid}))
        if tag:
            self.cf_delete(f"/workers/scripts/{self.worker}?force=true")
        return (f"deleted {len(triggers)} triggers, {len(conns)} repo connection(s), "
                f"Worker {self.worker if tag else '(already gone)'}")

    def teardown_github(self, repo, parked):
        parts = []
        for slug, inst in (self.installations() or {}).items():
            if inst.get("repository_selection") != "all" and repo["id"] in self.install_repo_ids(inst["id"]):
                self.gh("DELETE", f"/user/installations/{inst['id']}/repositories/{repo['id']}", pat=True)
                parts.append(f"removed from {slug}")
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

    STEPS = (("preflight", "step_preflight"), ("repo", "step_repo"), ("github apps", "step_github_apps"), ("worker", "step_worker"),
             ("workers builds", "step_builds"), ("first build", "step_build"),
             ("ReplyPen mirror", "step_replypen"), ("screenshots", "step_screenshots"))

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
            ("worker tag", s.get("worker_tag", "-")),
            ("triggers", f"production {s.get('trigger_production', '-')} · preview {s.get('trigger_preview', '-')}"),
            ("trigger json", s.get("triggers_json", "-")),
            ("screenshots", ", ".join(s.get("screenshots", [])) or "-"),
            ("try in chat", f'"{chat}"'),
            ("or from cli", f'rp --project {self.a.project} --tenant {self.a.tenant} project chat send --lane setup "{chat}"'),
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
    p.add_argument("--tenant", required=True, help="ReplyPen tenant slug")
    p.add_argument("--name", help="display name for _data/site.yml (default: title-cased slug)")
    p.add_argument("--worker-name", help="Worker name (default: site-<slug>)")
    p.add_argument("--account", default=DEFAULT_ACCOUNT, help="Cloudflare account id (default: Rootcause Sites)")
    p.add_argument("--live-url", help="mirror live_url (default: the workers.dev URL; set the real domain after DNS)")
    p.add_argument("--build-token", help="Workers Builds build_token_uuid (default: discovered)")
    p.add_argument("--adopt", action="store_true", help="existing repo: skip creation and placeholder filling")
    p.add_argument("--mirror-name", default=MIRROR_NAME, help="ReplyPen mirror name (default: site)")
    p.add_argument("--teardown", metavar="WORKER", help="undo a throwaway kickstart; value must equal the Worker name")
    p.add_argument("--force-orphan", action="store_true",
                   help="teardown: also delete a Worker with no Git connection and no kickstart record")
    p.add_argument("--state-dir", default="~/.local/state/website-kickstart",
                   help="kickstart records (ownership + teardown resume points)")
    p.add_argument("--dry-run", action="store_true", help="read live state, print writes, change nothing")
    p.add_argument("--out", default="~/Downloads", help="where screenshots + trigger JSON go")
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
