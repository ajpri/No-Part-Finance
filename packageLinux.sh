#!/bin/sh
set -eu

scriptDirectory=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$scriptDirectory"

if [ -x ".venv/bin/python" ]; then
    exec .venv/bin/python packageApplication.py "$@"
elif command -v python3 >/dev/null 2>&1; then
    exec python3 packageApplication.py "$@"
elif command -v python >/dev/null 2>&1; then
    exec python packageApplication.py "$@"
else
    echo "Python 3 is required. Install Python and try again." >&2
    exit 1
fi
