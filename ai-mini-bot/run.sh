#!/usr/bin/env bash
# Convenience launcher. Run from the repo root:  ./run.sh
set -e
cd "$(dirname "$0")/brain"

# activate venv if present
if [ -d "../venv" ]; then source ../venv/bin/activate; fi

# load .env if present
if [ -f "../.env" ]; then set -a; source ../.env; set +a; fi

python bot_brain.py
