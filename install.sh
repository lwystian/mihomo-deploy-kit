#!/usr/bin/env bash
set +x
set -euo pipefail
umask 077
command -v python3 >/dev/null || { echo '缺少 Python 3，请先安装 python3 和 python3-yaml。' >&2; exit 1; }
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
exec python3 "$script_dir/kit.py" install "$@"
