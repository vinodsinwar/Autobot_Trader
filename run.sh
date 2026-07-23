#!/usr/bin/env bash
# =============================================================================
# Autobot Trader — one-click launcher (Linux / macOS)
#
#   ./run.sh
#
# First run: creates .env with a generated encryption key, installs backend
# deps into backend/.venv, builds the dashboard. Every run: starts the server
# on http://localhost:8000 and opens your browser. Ctrl+C stops it.
# =============================================================================
set -euo pipefail
cd "$(dirname "$0")"

PORT="${AUTOBOT_PORT:-8000}"

say()  { printf '\033[1;36m[autobot]\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m[autobot] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# --- prerequisites -----------------------------------------------------------
PY="$(command -v python3.12 || command -v python3.11 || command -v python3 || true)"
[ -n "$PY" ] || fail "python3 not found — install Python 3.11+ (https://python.org)"
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
  || fail "Python 3.11+ required (found $("$PY" --version))"
command -v npm >/dev/null || fail "npm not found — install Node.js 18+ (https://nodejs.org)"
command -v ffmpeg >/dev/null || say "note: ffmpeg not found — only the YouTube live pipeline needs it (apt/brew install ffmpeg)"

# --- backend venv + deps -----------------------------------------------------
VENV="backend/.venv"
if [ ! -x "$VENV/bin/python" ]; then
  say "creating Python virtualenv…"
  "$PY" -m venv "$VENV"
fi
if [ ! -x "$VENV/bin/uvicorn" ]; then
  say "installing backend dependencies (one-time, a few minutes)…"
  "$VENV/bin/pip" install --quiet --upgrade pip
  (cd backend && .venv/bin/pip install --quiet -e .)
fi

# --- .env bootstrap ----------------------------------------------------------
if [ ! -f .env ]; then
  say "creating .env from .env.example…"
  cp .env.example .env
fi
if ! grep -q '^AUTOBOT_MASTER_KEY=.\+' .env; then
  say "generating encryption master key…"
  KEY="$("$VENV/bin/python" -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
  if grep -q '^AUTOBOT_MASTER_KEY=' .env; then
    # portable in-place edit (BSD/mac sed needs a backup suffix)
    sed -i.bak "s|^AUTOBOT_MASTER_KEY=.*|AUTOBOT_MASTER_KEY=${KEY}|" .env && rm -f .env.bak
  else
    printf '\nAUTOBOT_MASTER_KEY=%s\n' "$KEY" >> .env
  fi
fi

# --- dashboard build ---------------------------------------------------------
if [ ! -f backend/static/index.html ]; then
  say "building the dashboard (one-time)…"
  (cd frontend && npm install --no-audit --no-fund --loglevel=error && npm run build)
fi

# --- launch ------------------------------------------------------------------
# export .env so the server sees it regardless of working directory
set -a; . ./.env; set +a
PASSWORD="$(grep '^AUTOBOT_ADMIN_PASSWORD=' .env | cut -d= -f2- || true)"
say "starting Autobot Trader on http://localhost:${PORT}"
say "login password: ${PASSWORD:-change-me}  (change it in Settings after first login)"

( sleep 2
  if command -v xdg-open >/dev/null; then xdg-open "http://localhost:${PORT}" >/dev/null 2>&1 || true
  elif command -v open >/dev/null; then open "http://localhost:${PORT}" || true
  fi ) &

cd backend
exec .venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port "$PORT"
