"""Repo-wide pytest isolation: fixture repos must not inherit the developer's git config.

A global `commit.gpgsign=true` (ssh signing here) makes every fixture commit depend on a live
ssh-agent/key, so tests fail intermittently (agent restarted, key evicted, sandbox without
SSH_AUTH_SOCK). Tests that need their own config may still override GIT_CONFIG_GLOBAL.
"""
import shutil

import pytest


@pytest.fixture(autouse=True)
def _isolated_git_config(tmp_path_factory, monkeypatch):
    config = tmp_path_factory.getbasetemp() / 'gitconfig'
    if not config.exists():
        config.write_text('[user]\n\tname = Test\n\temail = test@example.org\n'
                          '[init]\n\tdefaultBranch = main\n')
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', str(config))
    monkeypatch.setenv('GIT_CONFIG_NOSYSTEM', '1')


@pytest.fixture(autouse=True)
def _cli_resolves_to_rp(monkeypatch):
    """Scripts run `rp` when it is on PATH, else `rc`. Pretend `rp` is installed so in-process command
    assertions do not depend on which name this laptop has; fake-binary tests ship both names."""
    real = shutil.which
    monkeypatch.setattr(shutil, "which", lambda cmd, *a, **k: "/usr/local/bin/rp" if cmd == "rp" else real(cmd, *a, **k))
