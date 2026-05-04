#!/usr/bin/env bash
set -euo pipefail

SESSION_NAME="${1:-nids-stack}"

if ! command -v tmux >/dev/null 2>&1; then
  echo "tmux is not installed."
  exit 1
fi

if tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
  tmux kill-session -t "$SESSION_NAME"
  echo "Stopped tmux session: $SESSION_NAME"
else
  echo "No tmux session named '$SESSION_NAME' is running."
fi
