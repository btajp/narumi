#!/usr/bin/env bash
# scripts/dev.sh — start the narumi MCP server for development, optionally with gaia-library.
#
# Usage:
#   scripts/dev.sh                       # narumi-server over Streamable HTTP on 127.0.0.1:8765
#   scripts/dev.sh --port 9000           # different port (or NARUMI_PORT=9000)
#   scripts/dev.sh --stdio               # narumi-server over stdio (for MCP client configs)
#   GAIA_LIBRARY_CMD="gaia serve" scripts/dev.sh
#                                        # also start gaia-library as a subprocess (any command
#                                        # string; run with `bash -c`). narumi works without it.
#
# Behaviour:
#   * SIGINT / SIGTERM are forwarded to both children.
#   * When either child exits, the other is stopped and the script exits with that child's status.
#   * In --stdio mode the server owns stdin/stdout; gaia-library's stdout is sent to stderr so the
#     MCP protocol stream stays clean.
#
# Environment:
#   NARUMI_PORT       HTTP port (default 8765); the server binds 127.0.0.1
#   NARUMI_HOME       data root (see narumi.config)
#   GAIA_LIBRARY_CMD  optional gaia-library start command
#
# Extra arguments (after the options above, or after `--`) are passed to narumi-server verbatim.
# Requires bash 3.2+ (macOS default) — no `wait -n`, no associative arrays.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

mode="http"
port="${NARUMI_PORT:-8765}"
server_extra=()

usage() {
  # print the header comment block of this file (everything before `set -euo pipefail`)
  sed -n '2,/^set -euo pipefail/p' "${BASH_SOURCE[0]}" | sed '$d' | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
  case "$1" in
    --stdio) mode="stdio" ;;
    --http) mode="http" ;;
    --port)
      [ $# -ge 2 ] || { echo "dev.sh: --port needs a value" >&2; exit 2; }
      port="$2"
      shift
      ;;
    --port=*) port="${1#*=}" ;;
    -h|--help) usage; exit 0 ;;
    --) shift; server_extra+=("$@"); break ;;
    *) server_extra+=("$1") ;;
  esac
  shift
done

command -v uv >/dev/null 2>&1 || { echo "dev.sh: uv not found (https://docs.astral.sh/uv/)" >&2; exit 1; }

log() { echo "dev.sh: $*" >&2; }

pids=()
names=()

# Forward a signal to every live child. Called from traps, so keep it side-effect free otherwise.
forward() {
  local sig="$1" pid
  for pid in ${pids[@]+"${pids[@]}"}; do
    kill -0 "$pid" 2>/dev/null && kill "-$sig" "$pid" 2>/dev/null || true
  done
}

cleanup() {
  trap - INT TERM EXIT
  local pid
  for pid in ${pids[@]+"${pids[@]}"}; do
    if kill -0 "$pid" 2>/dev/null; then
      kill -TERM "$pid" 2>/dev/null || true
    fi
  done
  # give children a moment to finalize (recorder / DB), then reap
  local i=0
  while [ $i -lt 50 ]; do
    local alive=0
    for pid in ${pids[@]+"${pids[@]}"}; do
      kill -0 "$pid" 2>/dev/null && alive=1
    done
    [ "$alive" -eq 0 ] && break
    sleep 0.1
    i=$((i + 1))
  done
  for pid in ${pids[@]+"${pids[@]}"}; do
    kill -0 "$pid" 2>/dev/null && kill -KILL "$pid" 2>/dev/null || true
  done
  wait 2>/dev/null || true
}

trap 'forward INT' INT
trap 'forward TERM' TERM
trap cleanup EXIT

# --- gaia-library (optional) -------------------------------------------------------------
if [ -n "${GAIA_LIBRARY_CMD:-}" ]; then
  log "starting gaia-library: $GAIA_LIBRARY_CMD"
  if [ "$mode" = "stdio" ]; then
    bash -c "$GAIA_LIBRARY_CMD" </dev/null 1>&2 &
  else
    bash -c "$GAIA_LIBRARY_CMD" </dev/null &
  fi
  pids+=("$!")
  names+=("gaia-library")
fi

# --- narumi-server -------------------------------------------------------------------------
server_cmd=(uv run narumi-server)
if [ "$mode" = "stdio" ]; then
  server_cmd+=(--stdio)
else
  server_cmd+=(--http --port "$port")
fi
server_cmd+=(${server_extra[@]+"${server_extra[@]}"})

log "starting narumi-server: ${server_cmd[*]}"
if [ "$mode" = "http" ]; then
  log "MCP endpoint: http://127.0.0.1:${port}/mcp"
fi
# `<&0` keeps the child attached to our stdin even though it is backgrounded (needed for --stdio).
"${server_cmd[@]}" <&0 &
pids+=("$!")
names+=("narumi-server")

# --- supervise: exit when either child dies ------------------------------------------------
while :; do
  i=0
  for pid in "${pids[@]}"; do
    if ! kill -0 "$pid" 2>/dev/null; then
      status=0
      wait "$pid" || status=$?
      log "${names[$i]} exited with status $status"
      exit "$status"
    fi
    i=$((i + 1))
  done
  sleep 1
done
