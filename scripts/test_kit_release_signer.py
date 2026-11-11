"""release_signer: only an unexpired challenge for the right GitHub id, signed by exactly one of that account's
GitHub signing keys, yields a pin entry."""

import json
import subprocess
import sys
from pathlib import Path

HELPER = Path(__file__).resolve().parent / "release_signer.py"


def key(tmp_path, name):
    k = tmp_path / name
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", name, "-f", str(k)], check=True)
    return k, " ".join(Path(f"{k}.pub").read_text().split()[:2])


def helper(tmp_path, fixture, *args, code=0):
    done = subprocess.run([sys.executable, str(HELPER), *args], capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "RP_SIGNER_GITHUB_FIXTURE": str(fixture)})
    assert done.returncode == code, done.stdout + done.stderr
    return done.stdout + done.stderr


def sign(tmp_path, k, line, namespace="replypen-kit-enroll"):
    msg = tmp_path / f"challenge-{len(list(tmp_path.glob('challenge-*.txt')))}.txt"
    msg.write_text(line + "\n")
    subprocess.run(["ssh-keygen", "-q", "-Y", "sign", "-n", namespace, "-f", str(k), str(msg)], check=True)
    return str(msg) + ".sig"


def test_enrollment_needs_a_github_signing_key_and_a_fresh_challenge(tmp_path):
    good, good_pub = key(tmp_path, "alexey")
    other, other_pub = key(tmp_path, "other")
    fixture = tmp_path / "gh.json"
    fixture.write_text(json.dumps({"id": 18293649, "ssh_signing_keys": [other_pub, good_pub]}))
    line = helper(tmp_path, fixture, "challenge", "--login", "bynov").strip()
    assert "id=18293649" in line

    out = helper(tmp_path, fixture, "verify", "--login", "bynov", "--challenge", line, "--signature", sign(tmp_path, good, line))
    assert out.splitlines()[-1] == f'bynov namespaces="git" {good_pub}' and out.startswith("SHA256:")

    stranger, _ = key(tmp_path, "stranger")  # valid signature, key not on the account
    assert "0 of bynov's 2" in helper(tmp_path, fixture, "verify", "--login", "bynov", "--challenge", line,
                                       "--signature", sign(tmp_path, stranger, line), code=1)
    assert "0 of" in helper(tmp_path, fixture, "verify", "--login", "bynov", "--challenge", line,  # wrong namespace
                            "--signature", sign(tmp_path, good, line, "git"), code=1)
    tampered = line.replace("nonce=", "nonce=0")[:-1]
    assert "error" in helper(tmp_path, fixture, "verify", "--login", "bynov", "--challenge", tampered,
                             "--signature", sign(tmp_path, good, line), code=1)
    expired = helper(tmp_path, fixture, "challenge", "--login", "bynov", "--days", "-1").strip()
    assert "expired" in helper(tmp_path, fixture, "verify", "--login", "bynov", "--challenge", expired,
                               "--signature", sign(tmp_path, good, expired), code=1)
    fixture.write_text(json.dumps({"id": 1, "ssh_signing_keys": [good_pub]}))  # login now names another account
    assert "challenge names 18293649" in helper(tmp_path, fixture, "verify", "--login", "bynov", "--challenge", line,
                                                "--signature", sign(tmp_path, good, line), code=1)
