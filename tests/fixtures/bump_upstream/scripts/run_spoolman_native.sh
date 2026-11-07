#!/usr/bin/env bash
# Launch isolated Spoolman 0.27.0 natively for Print Handy service tests.
set -euo pipefail
python_bin="${PYTHON:-python3}"
cache_dir="${SPOOLMAN_CACHE_DIR:-${TMPDIR:-${HOME}/.cache}/print-handy-spoolman-0.27.0}"
archive="$cache_dir/spoolman.zip"
app_dir="$cache_dir"
venv_dir="$cache_dir/venv"
data_dir="${SPOOLMAN_DIR_DATA:-${TMPDIR:-${HOME}/.cache}/print-handy-spoolman-data}"
port="${SPOOLMAN_PORT:-7912}"
mkdir -p "$cache_dir" "$data_dir"
if [[ ! -f "$archive" ]]; then curl -fL --retry 3 https://github.com/Donkie/Spoolman/releases/download/v0.27.0/spoolman.zip -o "$archive"; fi
echo 'bcc961af4c20d7ad7f1740f5609cd3efd94c4556baafaeba5036122dd7e46704  '"$archive" | sha256sum -c -
if [[ ! -f "$app_dir/spoolman/main.py" ]]; then unzip -oq "$archive" -d "$cache_dir"; fi
if [[ ! -x "$venv_dir/bin/python" ]]; then "$python_bin" -m venv "$venv_dir"; "$venv_dir/bin/python" -m pip install uvicorn~=0.34 -r "$app_dir/requirements.txt"; fi
export SPOOLMAN_DIR_DATA="$data_dir" SPOOLMAN_PORT="$port" SPOOLMAN_HOST=127.0.0.1
export PATH="$venv_dir/bin:$PATH"
cd "$app_dir"
exec "$venv_dir/bin/python" -m uvicorn spoolman.main:app --host 127.0.0.1 --port "$port"
