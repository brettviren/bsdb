#!/usr/bin/env bash
# inject-status.sh — write a SessionInfo JSON drop file for the bsdb loop monitor.
#
# Must be run inside a tmux session/window.  On the next loop iteration, bsdb
# will pick up the file instead of polling tmux list-windows.
#
# Usage:
#   inject-status.sh [CMD ...]
#   echo "some message" | inject-status.sh [CMD ...]
#
# Positional args, if given, override the cmd field.
# Stdin, if not a terminal, is placed in the message field.

set -euo pipefail

command -v jq >/dev/null 2>&1 || { echo "inject-status: jq is required" >&2; exit 1; }

if [ -z "${XDG_RUNTIME_DIR:-}" ]; then
    echo "inject-status: XDG_RUNTIME_DIR is not set" >&2
    exit 1
fi

# ID: <session_id_no_dollar>-<window_id_no_at>  e.g. "$3"/"@1" -> "3-1"
ID=$(tmux lsp -F '#{=-1:session_id}-#{=-1:window_id}' 2>/dev/null | head -1)
if [ -z "$ID" ]; then
    echo "inject-status: not running inside a tmux session" >&2
    exit 1
fi

BSDB_DIR=$XDG_RUNTIME_DIR/bsdb
mkdir -p "$BSDB_DIR"

# cmd: positional args if given, else the pane's current command
if [ $# -gt 0 ]; then
    CMD="$*"
else
    CMD=$(tmux display-message -p '#{pane_current_command}')
fi

# message: stdin when piped, empty when run interactively
if [ -t 0 ]; then
    MESSAGE=""
else
    MESSAGE=$(cat)
fi

jq -cn \
    --arg   session_name            "$(tmux display-message -p '#{session_name}')" \
    --arg   session_id              "$(tmux display-message -p '#{session_id}')" \
    --arg   window_id               "$(tmux display-message -p '#{window_id}')" \
    --arg   pane_id                 "$(tmux display-message -p '#{pane_id}')" \
    --argjson pane_pid              "$(tmux display-message -p '#{pane_pid}')" \
    --argjson session_created       "$(tmux display-message -p '#{session_created}')" \
    --arg   cmd                     "$CMD" \
    --arg   cwd                     "$(tmux display-message -p '#{pane_current_path}')" \
    --argjson session_attached      "$(tmux display-message -p '#{session_attached}')" \
    --argjson session_activity      "$(tmux display-message -p '#{session_activity}')" \
    --argjson window_activity       "$(tmux display-message -p '#{window_activity}')" \
    --arg   session_last_attached   "$(tmux display-message -p '#{session_last_attached}')" \
    --argjson session_many_attached "$(tmux display-message -p '#{session_many_attached}')" \
    --arg   message                 "$MESSAGE" \
    '{session_name:$session_name,session_id:$session_id,window_id:$window_id,
      pane_id:$pane_id,pane_pid:$pane_pid,session_created:$session_created,
      cmd:$cmd,cwd:$cwd,session_attached:$session_attached,
      session_activity:$session_activity,window_activity:$window_activity,
      session_last_attached:$session_last_attached,
      session_many_attached:$session_many_attached,message:$message}' \
    > "$BSDB_DIR/writing-$ID.json"

mv "$BSDB_DIR/writing-$ID.json" "$BSDB_DIR/status-$ID.json"
