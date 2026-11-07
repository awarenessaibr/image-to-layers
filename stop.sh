#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="$SCRIPT_DIR/.run/server.pid"
SERVER="$SCRIPT_DIR/image-to-layers-server.py"
SERVER_NAME="$(basename "$SERVER")"

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

if [ ! -f "$PID_FILE" ]; then
    if ! pid="$(find_server_process)"; then
        printf 'Image to Layers is not running (no matching server process or PID file).\n'
        exit 0
    fi
    printf 'Found manually started Image to Layers server (PID %s); stopping it.\n' "$pid"
else
    pid="$(<"$PID_FILE")"
fi

if [[ ! "$pid" =~ ^[0-9]+$ ]] || ! kill -0 "$pid" 2>/dev/null; then
    [ ! -f "$PID_FILE" ] || rm -f "$PID_FILE"
    printf 'Removed stale PID file; Image to Layers is not running.\n'
    exit 0
fi
if ! is_server_process "$pid"; then
    printf 'PID %s does not belong to Image to Layers; refusing to stop it.\n' "$pid" >&2
    exit 1
fi

kill -TERM "$pid"
for _ in {1..10}; do
    if ! kill -0 "$pid" 2>/dev/null || ! is_server_process "$pid"; then
        rm -f "$PID_FILE"
        printf 'Image to Layers stopped.\n'
        exit 0
    fi
    sleep 1
done

printf 'Image to Layers (PID %s) did not stop within 10 seconds; PID file was kept.\n' "$pid" >&2
exit 1
