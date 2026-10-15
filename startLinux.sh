#!/bin/sh
set -eu

scriptDirectory=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$scriptDirectory"

echo "To manage users, run: ./startLinux.sh manage-users"

if [ ! -x ".venv/bin/python" ]; then
    if command -v python3 >/dev/null 2>&1; then
        python3 -m venv .venv
    elif command -v python >/dev/null 2>&1; then
        python -m venv .venv
    else
        echo "Python 3 is required. Install Python and try again." >&2
        exit 1
    fi
fi

if ! .venv/bin/python -m pip --version >/dev/null 2>&1; then
    .venv/bin/python -m ensurepip --upgrade || true
    if ! .venv/bin/python -m pip --version >/dev/null 2>&1; then
        echo "The installed Python lacks virtual-environment or pip support. Repair Python or install its venv component." >&2
        exit 1
    fi
fi

.venv/bin/python -m pip install -r requirements.txt
exec .venv/bin/python app.py "$@"
