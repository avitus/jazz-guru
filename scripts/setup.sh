#!/usr/bin/env bash
# jazz-guru local-dev bootstrap (no Docker).
# Installs Postgres+pgvector, Redis, and FluidSynth via Homebrew on macOS;
# creates the database and the pgvector extension; runs alembic migrations.
set -euo pipefail

DB_NAME="${DB_NAME:-jazz_guru}"

bold()  { printf '\033[1m%s\033[0m\n' "$*"; }
ok()    { printf '\033[32m✓\033[0m %s\n' "$*"; }
warn()  { printf '\033[33m!\033[0m %s\n' "$*"; }
die()   { printf '\033[31m✗\033[0m %s\n' "$*" >&2; exit 1; }

# --- platform check -----------------------------------------------------------
case "$(uname -s)" in
  Darwin) PLATFORM=mac ;;
  Linux)  PLATFORM=linux ;;
  *) die "unsupported platform: $(uname -s)" ;;
esac

# --- Homebrew packages (macOS) ------------------------------------------------
if [[ "$PLATFORM" == "mac" ]]; then
  command -v brew >/dev/null 2>&1 || die "Homebrew not installed. See https://brew.sh"
  bold "Installing system deps via Homebrew"
  brew list --versions redis       >/dev/null 2>&1 || brew install redis
  brew list --versions fluid-synth >/dev/null 2>&1 || brew install fluid-synth
  brew list --versions ffmpeg      >/dev/null 2>&1 || brew install ffmpeg

  # Detect a running/installed Homebrew Postgres. Reuse it if present, else install @16.
  PG_FORMULA=""
  for f in postgresql@17 postgresql@16 postgresql@15 postgresql@14 postgresql; do
    if brew list --versions "$f" >/dev/null 2>&1; then
      PG_FORMULA="$f"
      break
    fi
  done
  if [[ -z "$PG_FORMULA" ]]; then
    bold "Installing postgresql@16"
    brew install postgresql@16
    PG_FORMULA="postgresql@16"
  else
    ok "reusing existing $PG_FORMULA"
  fi

  # Self-healing `brew services start`. The launchctl bootstrap step
  # fails with "Input/output error" (exit 5) when a stale registration
  # for the service is still loaded in gui/<uid>. Force a bootout and
  # retry once before giving up — this is the single most common
  # reason ./scripts/setup.sh fails on a re-run.
  start_service() {
    local svc="$1"
    if brew services start "$svc" >/dev/null 2>&1; then
      return 0
    fi
    warn "brew services start $svc failed; clearing stale launchd registration"
    launchctl bootout "gui/$(id -u)/homebrew.mxcl.$svc" >/dev/null 2>&1 || true
    brew services start "$svc" >/dev/null
  }

  bold "Starting services"
  start_service "$PG_FORMULA"
  start_service redis
  ok "$PG_FORMULA + redis running"

  PG_PREFIX="$(brew --prefix "$PG_FORMULA")"
  PG_BIN="$PG_PREFIX/bin"
  export PATH="$PG_BIN:$PATH"
  PG_CONFIG="$PG_BIN/pg_config"

  # --- pgvector: install/build for the *running* Postgres version -------------
  PG_VERSION="$("$PG_CONFIG" --version | awk '{print $2}' | cut -d. -f1)"
  PG_SHARE="$("$PG_CONFIG" --sharedir)"
  if [[ -f "$PG_SHARE/extension/vector.control" ]]; then
    ok "pgvector already installed for Postgres $PG_VERSION"
  else
    bold "Installing pgvector for Postgres $PG_VERSION"
    # try the Homebrew formula first (works when it matches the active PG version)
    brew list --versions pgvector >/dev/null 2>&1 || brew install pgvector || true
    if [[ ! -f "$PG_SHARE/extension/vector.control" ]]; then
      warn "Homebrew pgvector does not target Postgres $PG_VERSION; building from source"
      tmp="$(mktemp -d)"
      git clone --depth 1 --branch v0.8.0 https://github.com/pgvector/pgvector.git "$tmp/pgvector"
      ( cd "$tmp/pgvector" && PG_CONFIG="$PG_CONFIG" make && PG_CONFIG="$PG_CONFIG" make install )
      ok "pgvector built and installed for Postgres $PG_VERSION"
    fi
  fi
fi

# --- database -----------------------------------------------------------------
bold "Provisioning database '$DB_NAME'"
if psql -lqt | cut -d '|' -f 1 | grep -qw "$DB_NAME"; then
  ok "database $DB_NAME already exists"
else
  createdb "$DB_NAME"
  ok "created database $DB_NAME"
fi

psql "$DB_NAME" -v ON_ERROR_STOP=1 -c "CREATE EXTENSION IF NOT EXISTS vector;" >/dev/null
ok "pgvector extension ensured"

# --- Python venv --------------------------------------------------------------
if [[ ! -d .venv ]]; then
  bold "Creating Python venv at .venv"
  python3 -m venv .venv
fi
.venv/bin/python -m pip install -q --upgrade pip
bold "Installing project (.venv)"
.venv/bin/python -m pip install -q -e ".[dev]"
ok "python deps installed"

# --- .env ---------------------------------------------------------------------
if [[ ! -f .env ]]; then
  cp .env.example .env
  warn "created .env from .env.example — edit it to add your ANTHROPIC_API_KEY"
fi

# --- soundfont hint -----------------------------------------------------------
if [[ -z "${FLUIDSYNTH_SOUNDFONT:-}" ]]; then
  if [[ "$PLATFORM" == "mac" ]]; then
    SF_GUESS="$(brew --prefix fluid-synth 2>/dev/null)/share/fluid-synth/sf2"
    warn "no FLUIDSYNTH_SOUNDFONT set; download a GM .sf2 (e.g. FluidR3_GM) and point .env at it"
    warn "  one option: curl -L -o ~/FluidR3_GM.sf2 https://archive.org/download/fluidr3-gm-gs/FluidR3_GM.sf2"
  fi
fi

# --- alembic ------------------------------------------------------------------
bold "Running alembic migrations"
.venv/bin/alembic upgrade head
ok "schema up to date"

bold "Verifying"
.venv/bin/jazz-guru info | head -20 || true

cat <<EOF

$(bold "Done.")
Next:
  source .venv/bin/activate          # optional: activate the venv
  jazz-guru ping                     # test Anthropic connectivity
  jazz-guru chat "compose a Cmaj7 arpeggio as out/arp.mid"
  jazz-guru-server                   # FastAPI on http://127.0.0.1:8000
  jazz-guru-worker                   # background distillation/eval worker (needs redis)
EOF
