# Spec — Brain Test Runs

Historical implementation details moved to code and public docs. Current shipped behavior:

- `rp ask "<question>"` creates a production run.
- `rp ask "<question>" --brain-ref dev/<branch>` creates a test run against a pushed dev branch
  without moving live refs.
- `rp run trace <id> -o json` returns the run/event trace bundle.
- `brain_dump.py` renders the bundle through `replypen-runtime`'s shared run-dump renderer.
- `rp run debug <id>` writes the CLI's own decomposed debug files; keep its sections in parity with
  this kit's run-dump renderer when the run bundle changes.

Auth is OAuth via `rp auth login`. Do not use API keys, private debug scripts, registry DB access, or host
infrastructure from this kit.

See:

- [docs/rp-cli.md](../rp-cli.md)
- [docs/run-trace-model.md](../run-trace-model.md)
- [skills/brain-ask/SKILL.md](../../skills/brain-ask/SKILL.md)
- [skills/rp-debug/SKILL.md](../../skills/rp-debug/SKILL.md)
