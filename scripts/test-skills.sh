#!/usr/bin/env bash
# Offline suite for every shipped skill engine: one pytest process per skill, because each skill puts
# its own scripts/ on sys.path and sibling skills reuse module names (schema, corpus, query, ...).
#   ./scripts/test-skills.sh                    # all skills
#   ./scripts/test-skills.sh brain-publish ...  # only these
# DB-backed tests skip without FLEET_TEST_DSN; the runtime/ suite is separate (see AGENTS.md).
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPS=(--with pytest --with pyyaml --with pydantic --with 'psycopg[binary]' --with httpx
      --with beautifulsoup4 --with lxml --with markdownify --with pillow)
names=("$@"); [ ${#names[@]} -gt 0 ] || names=($(ls "$ROOT/skills"))
failed=()
for name in "${names[@]}"; do
  [ -d "$ROOT/skills/$name" ] || { echo "error: no skill $name" >&2; exit 2; }
  files=($(find "$ROOT/skills/$name" -name 'test_*.py' -not -path '*/__pycache__/*' -not -path '*/fixtures/*' | sort))
  [ ${#files[@]} -gt 0 ] || continue
  echo "== $name"
  uv run --no-project "${DEPS[@]}" pytest -q -p no:cacheprovider "${files[@]}" || failed+=("$name")
done
[ ${#failed[@]} -eq 0 ] || { echo "FAILED skills: ${failed[*]}" >&2; exit 1; }
