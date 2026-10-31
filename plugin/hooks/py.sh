#!/bin/sh
# Run a hook script with the Python this machine has: python3 on Linux/macOS; on Windows (Git Bash) py or python,
# where "python3" is often only the Microsoft Store stub. No working Python: skip the hook, never break the session.
# A hook script runs inside hook_status.py, which keeps a crash or a failed exit for the Tasks tab (sent from the
# per-turn hook); anything else (plugin/python3 passes Python's own options) runs as given.
case ${1:-} in *.py) set -- "$(dirname "$0")/hook_status.py" run "$@" ;; esac
for p in python3 py python; do
    if command -v "$p" >/dev/null 2>&1 && "$p" -c "import sys; sys.exit(sys.version_info < (3, 8))" >/dev/null 2>&1; then
        exec "$p" "$@"
    fi
done
exit 0
