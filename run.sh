#!/usr/bin/env bash
# Launch the Skill Analytics dashboard (local, stdlib-only).
# Usage: ./run.sh [port]
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${1:-8787}"
exec python3 "$DIR/server.py" "$PORT"
