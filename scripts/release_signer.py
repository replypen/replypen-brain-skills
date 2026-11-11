#!/usr/bin/env python3
"""Enroll a new kit releaser: proof that a GitHub account holds an SSH signing key, then the pin entry.

    release_signer.py challenge --login bynov [--days 14]      # print a one-line challenge for the candidate
    release_signer.py verify --login bynov --challenge LINE --signature FILE

The candidate registers the key on GitHub as a *signing* key and signs the exact challenge line (plus
newline) with `ssh-keygen -Y sign -n replypen-kit-enroll`. `verify` accepts only when the challenge is
unexpired and names the account's numeric GitHub id, and exactly one of that account's GitHub signing keys
verifies the signature. It prints the ALLOWED_SIGNERS entry to append by hand in a release signed by an
already-pinned key; it never edits or publishes anything (RELEASING.md "Adding a releaser").
Tests point RP_SIGNER_GITHUB_FIXTURE at a JSON file {"id": …, "ssh_signing_keys": ["<alg> <b64>", …]}.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile

NAMESPACE = "replypen-kit-enroll"
LINE = re.compile(r"replypen-brain-skills release-signer v1 github=(?P<login>[A-Za-z0-9-]+) id=(?P<id>\d+) "
                  r"nonce=(?P<nonce>[0-9a-f]{32}) expires=(?P<exp>\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ)")
KEY = re.compile(r"^(ssh-ed25519|sk-ssh-ed25519@openssh\.com|ecdsa-sha2-nistp256|sk-ecdsa-sha2-nistp256@openssh\.com|"
                 r"ssh-rsa) [A-Za-z0-9+/]+={0,2}$")


def github(login: str) -> tuple[int, list[str]]:
    """(numeric id, signing keys) as GitHub reports them for `login`."""
    if fixture := os.environ.get("RP_SIGNER_GITHUB_FIXTURE"):
        data = json.load(open(fixture))
        return int(data["id"]), list(data["ssh_signing_keys"])
    def api(path: str):
        return json.loads(subprocess.run(["gh", "api", path], check=True, capture_output=True, text=True).stdout)
    return int(api(f"users/{login}")["id"]), [k["key"] for k in api(f"users/{login}/ssh_signing_keys")]


def challenge(login: str, days: int) -> str:
    uid, _ = github(login)
    exp = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"replypen-brain-skills release-signer v1 github={login} id={uid} nonce={secrets.token_hex(16)} expires={exp}"


def verify(login: str, line: str, signature: str) -> str:
    m = LINE.fullmatch(line.strip())
    if not m or m["login"] != login:
        raise SystemExit(f"error: not a release-signer challenge for {login}")
    if dt.datetime.strptime(m["exp"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc) < dt.datetime.now(dt.timezone.utc):
        raise SystemExit("error: challenge expired; issue a new one")
    uid, keys = github(login)
    if uid != int(m["id"]):
        raise SystemExit(f"error: GitHub id of {login} is {uid}, challenge names {m['id']}")
    good = []
    for key in {" ".join(k.split()[:2]) for k in keys}:
        if not KEY.match(key):
            continue
        with tempfile.NamedTemporaryFile("w") as allowed:
            allowed.write(f'{login} namespaces="{NAMESPACE}" {key}\n')
            allowed.flush()
            done = subprocess.run(["ssh-keygen", "-Y", "verify", "-f", allowed.name, "-I", login, "-n", NAMESPACE,
                                   "-s", signature], input=line.strip() + "\n", capture_output=True, text=True)
        if done.returncode == 0:
            good.append(key)
    if len(good) != 1:
        raise SystemExit(f"error: {len(good)} of {login}'s {len(keys)} GitHub signing keys verify the signature (need 1)")
    fp = subprocess.run(["ssh-keygen", "-lf", "-"], input=good[0] + "\n", capture_output=True, text=True).stdout.split()[1]
    return f'{fp}\n{login} namespaces="git" {good[0]}'


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("challenge")
    c.add_argument("--login", required=True)
    c.add_argument("--days", type=int, default=14)
    v = sub.add_parser("verify")
    v.add_argument("--login", required=True)
    v.add_argument("--challenge", required=True)
    v.add_argument("--signature", required=True)
    a = p.parse_args()
    print(challenge(a.login, a.days) if a.cmd == "challenge" else verify(a.login, a.challenge, a.signature))
    return 0


if __name__ == "__main__":
    sys.exit(main())
