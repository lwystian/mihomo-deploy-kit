#!/usr/bin/env bash
set +x
set -euo pipefail
umask 077
command -v python3 >/dev/null || { echo '缺少 Python 3，请通过当前系统的包管理器安装 Python 3 和 PyYAML。' >&2; exit 1; }
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
exec python3 "$script_dir/kit.py" install "$@"
