#!/usr/bin/env bash
set -euo pipefail

SESSION_NAME="${1:-nids-stack}"
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

if ! command -v tmux >/dev/null 2>&1; then
  echo "tmux is required. Install with: brew install tmux"
  exit 1
fi

if [ ! -x "$ROOT_DIR/.venv/bin/python3" ]; then
  echo "Virtualenv not found. Run: make setup"
  exit 1
fi

echo "Ensuring Kafka and MongoDB are running..."
brew services start kafka >/dev/null 2>&1 || true
brew services start mongodb/brew/mongodb-community >/dev/null 2>&1 || true

if tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
  echo "Session '$SESSION_NAME' already exists. Attach with:"
  echo "  tmux attach -t $SESSION_NAME"
  exit 0
fi

tmux new-session -d -s "$SESSION_NAME" -c "$ROOT_DIR"
tmux rename-window -t "$SESSION_NAME:0" "stack"

# Pane 0: consumer (Spark Structured Streaming)
tmux send-keys -t "$SESSION_NAME:0.0" "make spark-stream-consumer" C-m

# Pane 1: aggregator
tmux split-window -h -t "$SESSION_NAME:0" -c "$ROOT_DIR"
tmux send-keys -t "$SESSION_NAME:0.1" "make campaign-aggregator" C-m

# Pane 2: API
tmux split-window -v -t "$SESSION_NAME:0.1" -c "$ROOT_DIR"
tmux send-keys -t "$SESSION_NAME:0.2" "make api" C-m

# Pane 3: producer helper shell
tmux split-window -v -t "$SESSION_NAME:0.0" -c "$ROOT_DIR"
tmux send-keys -t "$SESSION_NAME:0.3" "echo 'Use: make stream-producer (or STREAM_MAX_ROWS=... make stream-producer)'" C-m

tmux select-layout -t "$SESSION_NAME:0" tiled

echo "Started tmux session: $SESSION_NAME"
echo "Attach with: tmux attach -t $SESSION_NAME"
