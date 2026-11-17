# Sourced by every tests/*.sh after TMP is set: fixture git must never read (or write through) the
# developer's global/system config. A global core.hooksPath (git-secret-guard's machine-wide dispatcher)
# made `--git-path hooks/<name>` resolve to that shared dir, and a fixture's `cat >"$hook"` overwrote it,
# aborting every git ref update on the machine (2026-11-17). Same file as the pytest conftest.py.
: "${TMP:?source git-isolation.sh after TMP is set}"
export GIT_CONFIG_GLOBAL="$TMP/.gitconfig-isolated"
export GIT_CONFIG_NOSYSTEM=1
# Agent/hook env that would point fixture git elsewhere or inject `-c` config past the isolation.
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_COMMON_DIR GIT_CONFIG GIT_CONFIG_PARAMETERS GIT_CONFIG_COUNT
printf '%s\n' '[user]' '	name = Test' '	email = test@example.org' '[commit]' '	gpgSign = false' \
  '[tag]' '	gpgSign = false' '[init]' '	defaultBranch = main' >"$GIT_CONFIG_GLOBAL"

# Guard for every path a fixture writes a hook to: it must resolve inside TMP.
fixture_path() {  # path: echoes it, or fails the suite when it points outside the fixture dir
  local dir="${1%/*}" rest="" tmp
  while [ ! -d "$dir" ]; do rest="/${dir##*/}$rest"; dir="${dir%/*}"; done  # nearest existing ancestor
  dir="$(cd "$dir" && pwd -P)$rest" tmp="$(cd "$TMP" && pwd -P)"
  case "$dir/" in
    "$tmp"/*) printf '%s\n' "$1" ;;
    *) echo "error: fixture path escapes TMP: $1 (a global core.hooksPath leaked in?)" >&2; exit 1 ;;
  esac
}
