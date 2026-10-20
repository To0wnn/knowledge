#!/bin/sh
# Run a hook script with the Python this machine has: python3 on Linux/macOS; on Windows (Git Bash) py or python,
# where "python3" is often only the Microsoft Store stub. No working Python: skip the hook, never break the session.
for p in python3 py python; do
    if command -v "$p" >/dev/null 2>&1 && "$p" -c "import sys; sys.exit(sys.version_info < (3, 8))" >/dev/null 2>&1; then
        exec "$p" "$@"
    fi
done
exit 0
