#!/bin/sh
set -eu
script=$0
while [ -L "$script" ]; do
    dir=$(CDPATH= cd -- "$(dirname -- "$script")" && pwd)
    link=$(readlink "$script")
    case "$link" in
        /*) script=$link ;;
        *) script=$dir/$link ;;
    esac
done
dir=$(CDPATH= cd -- "$(dirname -- "$script")" && pwd)
exec /usr/bin/python3 "$dir/server.py" --open
