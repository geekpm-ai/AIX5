#!/usr/bin/env bash
# Render and install the local systemd service after building the application.
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: deploy/install.sh [--dry-run]

Environment:
  AIX5_SERVICE_USER  Existing non-root account (default: SUDO_USER or current user)
  AIX5_APP_DIR       Application directory (default: repository containing this script)
  AIX5_VENV_DIR      Python virtual environment (default: AIX5_APP_DIR/venv)
  AIX5_DATA_DIR      Writable database and event directory (default: /var/lib/aix5-ai)
  AIX5_PORT          HTTP port, 1-65535 (default: 8080)
  AIX5_BASE_URL      Reachable HTTP(S) address (default: http://127.0.0.1:AIX5_PORT)

--dry-run validates prerequisites and prints the rendered unit without modifying files
or starting services. Installation requires root on Linux with systemd. Paths must be
absolute and contain only ASCII letters, digits, '.', '_', '/', or '-'.
USAGE
}

fail() { printf 'AIX5 install: %s\n' "$*" >&2; exit 1; }
dry_run=false
case "${1:-}" in
  --dry-run) dry_run=true ;;
  --help|-h) usage; exit 0 ;;
  '') ;;
  *) usage >&2; exit 2 ;;
esac
[[ $# -le 1 ]] || { usage >&2; exit 2; }

script_dir=$(cd -- "$(dirname -- "$0")" && pwd)
repo_dir=$(cd -- "$script_dir/.." && pwd)
run_user=${AIX5_SERVICE_USER:-${SUDO_USER:-$(id -un)}}
[[ "$run_user" =~ ^[A-Za-z_][A-Za-z0-9_.-]*$ ]] || fail 'AIX5_SERVICE_USER must be a valid existing account name.'
run_uid=$(id -u "$run_user" 2>/dev/null) || fail "Account does not exist: $run_user"
[[ "$run_uid" != 0 ]] || fail 'Set AIX5_SERVICE_USER to an existing non-root account.'
run_group=$(id -gn "$run_user")
[[ "$run_group" =~ ^[A-Za-z_][A-Za-z0-9_.-]*$ ]] || fail 'The service account primary group has an unsupported name.'
app_dir=${AIX5_APP_DIR:-$repo_dir}
venv_dir=${AIX5_VENV_DIR:-$app_dir/venv}
data_dir=${AIX5_DATA_DIR:-/var/lib/aix5-ai}
service_port=${AIX5_PORT:-8080}
base_url=${AIX5_BASE_URL:-http://127.0.0.1:$service_port}
command -v python3 >/dev/null 2>&1 || fail 'python3 is required to validate and render the service.'

# Pass data as arguments, never as evaluated shell text. These restrictions also
# prevent systemd specifier expansion and ambiguous command/environment values.
rendered_unit=$(python3 - "$script_dir/aix5-ai.service" "$run_user" "$run_group" "$app_dir" "$venv_dir" "$data_dir" "$service_port" "$base_url" <<'PY'
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

template, user, group, app, venv, data, port, base = sys.argv[1:]
for name, value in (("AIX5_APP_DIR", app), ("AIX5_VENV_DIR", venv), ("AIX5_DATA_DIR", data)):
    if not re.fullmatch(r"/[A-Za-z0-9_./-]+", value) or ".." in value.split("/"):
        raise SystemExit(f"AIX5 install: {name} must be an absolute path without spaces, '..', or special characters.")
    if value.rstrip("/") == "":
        raise SystemExit(f"AIX5 install: {name} cannot be the filesystem root.")
if not re.fullmatch(r"[0-9]{1,5}", port) or not 1 <= int(port) <= 65535:
    raise SystemExit("AIX5 install: AIX5_PORT must be an integer from 1 to 65535.")
try:
    parts = urlsplit(base)
    valid_url = (
        parts.scheme in ("http", "https")
        and parts.hostname
        and parts.username is None
        and parts.password is None
        and not parts.query
        and not parts.fragment
        and not parts.path.strip("/")
        and not any(char.isspace() or char in (chr(37), chr(34), chr(39), chr(92)) for char in base)
        and re.fullmatch(r"[A-Za-z0-9.:[\]-]+", parts.netloc)
        and (parts.port is None or 1 <= parts.port <= 65535)
    )
except ValueError:
    valid_url = False
if not valid_url:
    raise SystemExit("AIX5 install: AIX5_BASE_URL must be an HTTP(S) origin without credentials, path, query, fragment, whitespace, or special characters.")
values = {
    "RUN_USER": user, "RUN_GROUP": group, "APP_DIR": app.rstrip("/"),
    "VENV_DIR": venv.rstrip("/"), "DATA_DIR": data.rstrip("/"),
    "PORT": str(int(port)), "BASE_URL": base.rstrip("/"),
}
unit = Path(template).read_text()
for key, value in values.items():
    unit = unit.replace(f"@{key}@", value)
if re.search(r"@[A-Z_]+@", unit):
    raise SystemExit("AIX5 install: the service template has unresolved placeholders.")
print(unit, end="")
PY
) || exit 1

[[ -f "$app_dir/aix5_ai/main.py" ]] || fail "Application is missing: $app_dir/aix5_ai/main.py"
[[ -x "$venv_dir/bin/python" ]] || fail "Python environment is missing: $venv_dir/bin/python"
for binary in rdk_capture_mm rdk_video_stream mediamtx; do
  [[ -x "$app_dir/native/$binary" ]] || fail "Build or install native/$binary before installing the service."
done
[[ -r "$app_dir/native/librdk_h264_encoder.so" ]] || fail 'Build native/librdk_h264_encoder.so before installing the service.'
if "$dry_run"; then
  printf '%s\n' "$rendered_unit"
  exit 0
fi

[[ $(id -u) == 0 ]] || fail 'Installation requires root. Run with sudo and set AIX5_SERVICE_USER if needed.'
[[ $(uname -s) == Linux ]] || fail 'Installation requires Linux with systemd. Use --dry-run on other systems.'
command -v systemctl >/dev/null 2>&1 || fail 'systemctl is required.'
[[ ! -L "$data_dir" ]] || fail 'AIX5_DATA_DIR must not be a symbolic link.'
stage=$(mktemp -d)
trap 'rm -rf -- "$stage"' EXIT
printf '%s\n' "$rendered_unit" > "$stage/aix5-ai.service"
install -d -m 0750 -o "$run_user" -g "$run_group" "$data_dir"
install -m 0644 "$stage/aix5-ai.service" /etc/systemd/system/aix5-ai.service
systemctl daemon-reload
systemctl enable --now aix5-ai.service
systemctl --no-pager --full status aix5-ai.service
