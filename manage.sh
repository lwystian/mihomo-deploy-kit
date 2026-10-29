#!/usr/bin/env bash
set +x
set -euo pipefail
umask 077
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
exec python3 "$script_dir/kit.py" --root "$script_dir" "$@"
