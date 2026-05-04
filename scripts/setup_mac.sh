#!/usr/bin/env bash
set -euo pipefail

echo "[1/6] Checking Homebrew..."
if ! command -v brew >/dev/null 2>&1; then
  echo "Homebrew is required. Install from https://brew.sh and rerun."
  exit 1
fi

echo "[2/6] Installing system dependencies (Java, Kafka, MongoDB, Python)..."
brew install openjdk@17 kafka mongodb-community python@3.10 || true

echo "[3/6] Creating Python virtual environment..."
if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi

echo "[4/6] Installing Python dependencies..."
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

echo "[5/6] Preparing local env file..."
if [ ! -f ".env" ]; then
  cp .env.example .env
fi

echo "[6/6] Registering Jupyter kernel..."
python -m ipykernel install --user --name nids-local --display-name "Python (nids-local)"

echo ""
echo "Setup complete."
echo "Activate env with: source .venv/bin/activate"
echo "Start notebook with: jupyter notebook"
