#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

RUN_DIR="$SCRIPT_DIR/.run"
PID_FILE="$RUN_DIR/server.pid"
LOG_FILE="$RUN_DIR/server.log"
SERVER="$SCRIPT_DIR/image-to-layers-server.py"
SERVER_NAME="$(basename "$SERVER")"
PYTHON="$SCRIPT_DIR/.venv/bin/python"

if [ ! -x "$PYTHON" ]; then
    PYTHON="$(command -v python3 || true)"
fi
if [ -z "$PYTHON" ]; then
    printf 'python3 was not found. Run ./install.sh or install Python 3.\n' >&2
    exit 1
fi

is_server_process() {
    local pid="$1"
    local args process_cwd
    args="$(ps -p "$pid" -o args= 2>/dev/null || true)"
    process_cwd="$(readlink "/proc/$pid/cwd" 2>/dev/null || true)"
    [[ "$args" == *"$SERVER"* || ( "$process_cwd" == "$SCRIPT_DIR" && "$args" == *"$SERVER_NAME"* ) ]]
}

find_server_process() {
    local process_dir pid
    for process_dir in /proc/[0-9]*; do
        pid="${process_dir##*/}"
        if is_server_process "$pid"; then
            printf '%s\n' "$pid"
            return 0
        fi
    done
    return 1
}

if [ -f "$PID_FILE" ]; then
    old_pid="$(<"$PID_FILE")"
    if [[ "$old_pid" =~ ^[0-9]+$ ]] && kill -0 "$old_pid" 2>/dev/null && is_server_process "$old_pid"; then
        printf 'Image to Layers is already running (PID %s).\n' "$old_pid"
        exit 0
    fi
    printf 'Removing stale PID file.\n'
    rm -f "$PID_FILE"
fi

if existing_pid="$(find_server_process)"; then
    printf '%s\n' "$existing_pid" >"$PID_FILE"
    printf 'Image to Layers is already running (PID %s); recorded it for ./stop.sh.\n' "$existing_pid"
    exit 0
fi

requested_port="${PORT:-8009}"
if [[ ! "$requested_port" =~ ^[0-9]+$ ]] || [ "$requested_port" -lt 1 ] || [ "$requested_port" -gt 65535 ]; then
    printf 'PORT must be an integer between 1 and 65535 (got %s).\n' "$requested_port" >&2
    exit 1
fi
requested_port=$((10#$requested_port))
host="${HOST:-0.0.0.0}"
port="$requested_port"
for _ in {1..100}; do
    if "$PYTHON" -c 'import socket, sys; s = socket.socket(); s.bind((sys.argv[2], int(sys.argv[1]))); s.close()' "$port" "$host" 2>/dev/null; then
        break
    fi
    port=$((port + 1))
done
if [ "$port" -gt 65535 ]; then
    printf 'No available port found starting at %s.\n' "$requested_port" >&2
    exit 1
fi

mkdir -p "$RUN_DIR"
if [ "$port" -ne "$requested_port" ]; then
    printf 'Port %s is occupied; starting Image to Layers on port %s instead.\n' "$requested_port" "$port"
fi
PORT="$port" HOST="$host" nohup "$PYTHON" "$SERVER" >"$LOG_FILE" 2>&1 </dev/null &
pid=$!
printf '%s\n' "$pid" >"$PID_FILE"

sleep 1
if ! kill -0 "$pid" 2>/dev/null || ! is_server_process "$pid"; then
    rm -f "$PID_FILE"
    printf 'Image to Layers failed to start. See %s\n' "$LOG_FILE" >&2
    exit 1
fi

printf 'Image to Layers started (PID %s): http://127.0.0.1:%s/image-to-layers-server.html\n' "$pid" "$port"
printf 'Log: %s\n' "$LOG_FILE"
