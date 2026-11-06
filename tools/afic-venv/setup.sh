#!/bin/sh
# Makes (or updates) the one shared Python venv for every Arcade Foundry repo
# and installs requirements.txt into it. Nothing goes into the system's Python.
#   tools/afic-venv/setup.sh
# AFIC_VENV picks the folder (default ~/venvs/afic); PYTHON the Python it is
# made from (default python3, 3.11 or later). Run it again after
# requirements.txt changes. A module that will not install (no wheel for a
# new Python yet, say) is named at the end; the others still go in.
set -e
here=$(cd "$(dirname "$0")" && pwd)
venv=${AFIC_VENV:-$HOME/venvs/afic}
py=${PYTHON:-python3}

"$py" -c 'import sys; sys.exit(sys.version_info < (3, 11))' || {
    echo "afic-venv: $py is $("$py" -V 2>&1); 3.11 or later is needed" >&2; exit 1; }
if [ ! -x "$venv/bin/python" ]; then
    mkdir -p "$(dirname "$venv")"
    "$py" -m venv "$venv"
    echo "afic-venv: made $venv from $("$py" -V 2>&1)"
fi
"$venv/bin/python" -m pip install -q --upgrade pip
if ! "$venv/bin/pip" install -q -r "$here/requirements.txt"; then
    # one module at a time, so one that fails does not keep the rest out
    failed=
    for m in $(sed -e 's/#.*//' "$here/requirements.txt"); do
        "$venv/bin/pip" install -q "$m" || failed="$failed $m"
    done
    [ -z "$failed" ] || echo "afic-venv: not installed:$failed" >&2
fi
echo "afic-venv: $venv ready ($("$venv/bin/python" -V 2>&1))"
