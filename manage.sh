#!/usr/bin/env bash
set +x
set -euo pipefail
umask 077
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
if [[ $# -eq 0 ]]; then set -- help; fi
exec python3 "$script_dir/kit.py" --root "$script_dir" "$@"
